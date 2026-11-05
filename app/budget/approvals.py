"""Approval requests and tier-change audit, stored in UC Delta tables.

Tables (created by sql/02_approvals_audit.sql):
  <catalog>.<schema>.approvals   (pending/decided tier-increase requests)
  <catalog>.<schema>.tier_audit  (append-only log of every tier change)
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient

from app.budget.config import Config


@dataclass
class Approval:
    request_id: str
    principal: str
    from_tier: str
    to_tier: str
    status: str            # "pending" | "approved" | "denied"
    approver: str | None
    reason: str | None


@dataclass
class Notification:
    id: str
    principal: str
    kind: str              # budget_hit | request_created | approved | denied
    title: str
    body: str
    request_id: str | None
    created_at: str
    read: bool


class Store:
    def __init__(self, cfg: Config, warehouse_id: str | None = None, client: WorkspaceClient | None = None):
        self._cfg = cfg
        self._wh = warehouse_id or os.environ["WAREHOUSE_ID"]
        self._w = client or WorkspaceClient()

    def _exec(self, statement: str):
        return self._w.statement_execution.execute_statement(
            warehouse_id=self._wh, statement=statement, wait_timeout="50s"
        )

    # --- approvals -------------------------------------------------------
    def create_request(self, principal: str, from_tier: str, to_tier: str, spend: float) -> str:
        rid = str(uuid.uuid4())
        self._exec(f"""
            INSERT INTO {self._cfg.fqn_approvals}
              (request_id, principal, from_tier, to_tier, spend_usd, status, requested_at)
            VALUES ('{rid}', '{_e(principal)}', '{_e(from_tier)}', '{_e(to_tier)}',
                    {float(spend)}, 'pending', current_timestamp())
        """)
        return rid

    def get(self, request_id: str) -> Approval | None:
        r = self._exec(f"""
            SELECT request_id, principal, from_tier, to_tier, status, approver, reason
            FROM {self._cfg.fqn_approvals} WHERE request_id = '{_e(request_id)}'
        """)
        rows = (r.result.data_array if r.result else None) or []
        if not rows:
            return None
        c = rows[0]
        return Approval(*c[:5], approver=c[5], reason=c[6])

    def list_pending(self) -> list[Approval]:
        r = self._exec(f"""
            SELECT request_id, principal, from_tier, to_tier, status, approver, reason
            FROM {self._cfg.fqn_approvals} WHERE status = 'pending' ORDER BY requested_at
        """)
        rows = (r.result.data_array if r.result else None) or []
        return [Approval(*c[:5], approver=c[5], reason=c[6]) for c in rows]

    def list_for(self, principal: str, limit: int = 20) -> list[Approval]:
        """Recent requests raised by a principal (their own history)."""
        r = self._exec(f"""
            SELECT request_id, principal, from_tier, to_tier, status, approver, reason
            FROM {self._cfg.fqn_approvals} WHERE principal = '{_e(principal)}'
            ORDER BY requested_at DESC LIMIT {int(limit)}
        """)
        rows = (r.result.data_array if r.result else None) or []
        return [Approval(*c[:5], approver=c[5], reason=c[6]) for c in rows]

    def has_open_request(self, principal: str) -> bool:
        r = self._exec(f"""
            SELECT count(*) FROM {self._cfg.fqn_approvals}
            WHERE principal = '{_e(principal)}' AND status = 'pending'
        """)
        rows = (r.result.data_array if r.result else None) or []
        return bool(rows and int(rows[0][0]) > 0)

    def decide(self, request_id: str, approver: str, decision: str, reason: str | None) -> None:
        self._exec(f"""
            UPDATE {self._cfg.fqn_approvals}
            SET status = '{_e(decision)}', approver = '{_e(approver)}',
                reason = {("'" + _e(reason) + "'") if reason else 'NULL'},
                decided_at = current_timestamp()
            WHERE request_id = '{_e(request_id)}' AND status = 'pending'
        """)

    # --- entitlement (user_tier) ----------------------------------------
    def get_entitlement(self, principal: str) -> tuple[str, str] | None:
        """Return (tier, state) the principal is entitled to, or None if unset."""
        r = self._exec(f"""
            SELECT tier, state FROM {self._cfg.fqn_user_tier}
            WHERE principal = '{_e(principal)}'
        """)
        rows = (r.result.data_array if r.result else None) or []
        return (rows[0][0], rows[0][1]) if rows else None

    def all_entitlements(self) -> list[tuple[str, str, str]]:
        """Return (principal, tier, state) for every tracked user."""
        r = self._exec(f"SELECT principal, tier, state FROM {self._cfg.fqn_user_tier}")
        rows = (r.result.data_array if r.result else None) or []
        return [(c[0], c[1], c[2]) for c in rows]

    def set_entitlement(self, principal: str, tier: str, state: str) -> None:
        """Upsert the principal's entitled tier + state."""
        self._exec(f"""
            MERGE INTO {self._cfg.fqn_user_tier} t
            USING (SELECT '{_e(principal)}' AS principal) s
            ON t.principal = s.principal
            WHEN MATCHED THEN UPDATE SET tier='{_e(tier)}', state='{_e(state)}', updated_at=current_timestamp()
            WHEN NOT MATCHED THEN INSERT (principal, tier, state, updated_at)
              VALUES ('{_e(principal)}', '{_e(tier)}', '{_e(state)}', current_timestamp())
        """)

    # --- audit -----------------------------------------------------------
    def audit(self, principal: str, old_tier: str | None, new_tier: str, trigger: str,
              verified_spend: float, actor: str) -> None:
        self._exec(f"""
            INSERT INTO {self._cfg.fqn_audit}
              (principal, old_tier, new_tier, trigger, verified_spend_usd, actor, changed_at)
            VALUES ('{_e(principal)}', {("'" + _e(old_tier) + "'") if old_tier else 'NULL'},
                    '{_e(new_tier)}', '{_e(trigger)}', {float(verified_spend)},
                    '{_e(actor)}', current_timestamp())
        """)

    # --- notifications (in-app inbox) -----------------------------------
    def add_notification(self, principal: str, kind: str, title: str, body: str,
                         request_id: str | None = None) -> str:
        nid = str(uuid.uuid4())
        rid_sql = ("'" + _e(request_id) + "'") if request_id else "NULL"
        self._exec(f"""
            INSERT INTO {self._cfg.fqn_notifications}
              (id, principal, kind, title, body, request_id, created_at, read)
            VALUES ('{nid}', '{_e(principal)}', '{_e(kind)}', '{_e(title)}', '{_e(body)}',
                    {rid_sql}, current_timestamp(), false)
        """)
        return nid

    def list_notifications(self, principal: str, limit: int = 30) -> list[Notification]:
        r = self._exec(f"""
            SELECT id, principal, kind, title, body, request_id,
                   date_format(created_at, "yyyy-MM-dd HH:mm") AS created_at, read
            FROM {self._cfg.fqn_notifications} WHERE principal = '{_e(principal)}'
            ORDER BY created_at DESC LIMIT {int(limit)}
        """)
        rows = (r.result.data_array if r.result else None) or []
        return [Notification(c[0], c[1], c[2], c[3], c[4], c[5], c[6],
                             str(c[7]).lower() == "true") for c in rows]

    def mark_notifications_read(self, principal: str) -> None:
        self._exec(f"""
            UPDATE {self._cfg.fqn_notifications} SET read = true
            WHERE principal = '{_e(principal)}' AND read = false
        """)


def _e(s: str) -> str:
    return s.replace("'", "''")
