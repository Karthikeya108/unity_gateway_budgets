"""Tier escalation state machine (budget-override model, user-initiated).

One shared endpoint governed by ONE budget with per-group overrides (highest applies).
Every elevation is APPROVAL-GATED and USER-INITIATED:

  1. The user hits their ceiling (native 403 block). The client reports it →
     `record_budget_hit`: compute the user's usage, notify them (in-app + Slack) with
     a link to the app. No elevation happens here.
  2. The user opens the app and clicks "Request elevation" → `request_elevation`:
     create an approval request (current tier → next), notify the approver.
  3. A steward approves/denies in the app → `apply_decision`: on approve, ADD the user
     to the next override group (raising their ceiling); notify the user either way.

A user's current tier is derived from their override-group membership (the source of
truth for their ceiling). A base endpoint keeps them working while a request is pending.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.budget.approvals import Store
from app.budget.config import Config, Tier
from app.budget.groups import GroupManager
from app.budget.notify import Notifier

log = logging.getLogger("budget.escalation")


@dataclass
class EscalationResult:
    principal: str
    action: str            # "approval_requested" | "escalated" | "at_top" | "denied" | "noop"
    from_tier: str
    to_tier: str | None
    spend_usd: float
    request_id: str | None = None
    detail: str = ""


class Escalator:
    def __init__(self, cfg: Config, systables, groups: GroupManager,
                 store: Store, notifier: Notifier, app_url: str | None = None):
        self._cfg = cfg
        self._st = systables
        self._g = groups
        self._store = store
        self._n = notifier
        self._app_url = (app_url or os.environ.get("APP_URL") or os.environ.get("CONSOLE_URL", "")).rstrip("/")

    # --- tier / usage helpers -------------------------------------------
    def current_tier(self, principal: str) -> Tier:
        """Derive the user's current tier from override-group membership (highest wins)."""
        for tier in reversed(self._cfg.tiers):          # highest first
            if tier.group and self._g.is_member(tier.group, principal):
                return tier
        return self._cfg.tiers[0]                        # default (no override group)

    def usage_for(self, principal: str) -> dict:
        """Per-model usage + $ for the principal on the shared endpoint(s)."""
        services = sorted({t.service for t in self._cfg.tiers})
        return self._st.usage_breakdown(principal, services, self._cfg.spend.window,
                                        self._cfg.spend.price_map)

    # --- step 1: a budget block was hit ---------------------------------
    def record_budget_hit(self, principal: str, tier_name: str | None = None) -> dict:
        """Record that the user hit their ceiling and notify them (no elevation)."""
        cur = self._cfg.tier_by_name(tier_name) if tier_name else None
        if cur is None:
            cur = self.current_tier(principal)
        usage = self.usage_for(principal)
        self._store.set_entitlement(principal, cur.name, "over_pending")
        self._store.audit(principal, cur.name, cur.name, "block", cur.threshold_usd, actor="system")
        self._n.budget_hit(principal, cur.name, cur.threshold_usd, usage, self._app_url)
        return {"tier": cur.name, "ceiling_usd": cur.threshold_usd, "usage": usage}

    # --- step 2: user asks for more budget ------------------------------
    def request_elevation(self, principal: str) -> EscalationResult:
        cur = self.current_tier(principal)
        nxt = self._cfg.next_tier(cur.name)
        if nxt is None or cur.escalation == "none":
            return EscalationResult(principal, "at_top", cur.name, None, cur.threshold_usd,
                                    detail="already at the top tier")
        if self._store.has_open_request(principal):
            return EscalationResult(principal, "noop", cur.name, nxt.name, cur.threshold_usd,
                                    detail="a request is already pending")
        usage = self.usage_for(principal)
        rid = self._store.create_request(principal, cur.name, nxt.name, usage.get("total_usd", 0.0))
        self._store.set_entitlement(principal, cur.name, "over_pending")
        self._store.audit(principal, cur.name, cur.name, "request", usage.get("total_usd", 0.0), actor=principal)
        self._n.request_submitted(principal, cur.name, nxt.name, rid)
        self._n.notify_approver(principal, cur.name, nxt.name, usage, rid, self._app_url)
        return EscalationResult(principal, "approval_requested", cur.name, nxt.name,
                                usage.get("total_usd", 0.0), request_id=rid,
                                detail="submitted; awaiting steward approval")

    # --- step 3: steward decides ----------------------------------------
    def apply_decision(self, request_id: str, approver: str, decision: str,
                       reason: str | None = None) -> EscalationResult:
        appr = self._store.get(request_id)
        if appr is None or appr.status != "pending":
            raise ValueError("request not found or already decided")
        self._store.decide(request_id, approver, decision, reason)

        if decision != "approved":
            self._n.decision(appr.principal, appr.from_tier, appr.to_tier, "denied",
                             approver, reason, request_id)
            return EscalationResult(appr.principal, "denied", appr.from_tier, appr.to_tier, 0.0,
                                    request_id=request_id, detail="declined; ceiling unchanged")

        to = self._cfg.tier_by_name(appr.to_tier)
        frm = self._cfg.tier_by_name(appr.from_tier)
        spend = frm.threshold_usd if frm else 0.0
        self._g.add(appr.principal, to.group)            # raise ceiling via override group
        self._store.set_entitlement(appr.principal, appr.to_tier, "active")
        self._store.audit(appr.principal, appr.from_tier, appr.to_tier, "approval", spend, actor=approver)
        self._n.decision(appr.principal, appr.from_tier, appr.to_tier, "approved",
                         approver, reason, request_id)
        return EscalationResult(appr.principal, "escalated", appr.from_tier, appr.to_tier, spend,
                                request_id=request_id)
