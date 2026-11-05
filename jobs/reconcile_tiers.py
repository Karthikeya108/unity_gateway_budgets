"""Scheduled reconciliation job - the self-healing backbone.

Enforcement is group membership (each tier group grants its endpoint pool). This
job converges live membership toward the correct state, independent of the alert
channel. For every user with an entitlement in user_tier:

  - Over their entitled tier's threshold + entitled tier is "auto"     -> escalate
        (remove from current group, add to next, notify).
  - Over threshold + entitled tier is "approval" and no pending request -> drop to
        base and open an approval request.
  - Under their entitled tier's threshold but NOT in that tier's group  -> restore
        (e.g. after a monthly budget reset): add them back to their entitled group,
        state active.

Run on a schedule (e.g. every 15 min) as a Databricks Job. Requires WAREHOUSE_ID
and TIERS_CONFIG env/params and the same identity/scope as the app.
"""
from __future__ import annotations

import logging
import os
import sys

# Make the repo root importable and resolve the config regardless of cwd (job-safe).
# `__file__` is not defined when a serverless spark_python_task exec's this file, so
# fall back to the --config path (<root>/config/tiers.yaml) or argv[0], then cwd.
def _resolve_repo_root() -> str:
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    for i, a in enumerate(sys.argv):
        if a == "--config" and i + 1 < len(sys.argv):
            return os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[i + 1])))
        if a.startswith("--config="):
            return os.path.dirname(os.path.dirname(os.path.abspath(a.split("=", 1)[1])))
    if sys.argv and sys.argv[0]:
        return os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))
    return os.getcwd()


_REPO_ROOT = _resolve_repo_root()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.environ.setdefault("TIERS_CONFIG", os.path.join(_REPO_ROOT, "config", "tiers.yaml"))

from app.budget.approvals import Store
from app.budget.config import get_config
from app.budget.escalation import Escalator
from app.budget.groups import GroupManager
from app.budget.notify import Notifier, Slack
from app.budget.verifier import Verifier
from app.core.systables import SystemTables

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reconcile")


def run() -> dict:
    """Backstop for the user-initiated flow.

    For members of the override groups (the only users we can enumerate), it (a) notifies
    anyone over their current ceiling who has no open request - a safety net if the
    real-time 403-catch client didn't fire - and (b) clears over_pending state once spend
    falls back under the ceiling (e.g. a new billing month). Default-tier users have no
    group to enumerate; the 403-catch client covers them. Never elevates on its own -
    elevation stays user-initiated + steward-approved.
    """
    cfg = get_config()
    st = SystemTables()
    verifier = Verifier(cfg, st)
    groups = GroupManager([t.group for t in cfg.tiers])
    store = Store(cfg)
    esc = Escalator(cfg, st, groups, store, Notifier(store, Slack()))

    summary = {"checked": 0, "notified": 0, "cleared": 0, "noop": 0}

    for tier in cfg.tiers:
        if not tier.group:                       # default tier has no override group to list
            continue
        for principal in groups.members(tier.group):
            summary["checked"] += 1
            over = verifier.check(principal, esc.current_tier(principal)).over_threshold
            if over and not store.has_open_request(principal):
                esc.record_budget_hit(principal)   # notify (in-app + Slack); no elevation
                summary["notified"] += 1
            elif not over:
                ent = store.get_entitlement(principal)
                if ent and ent[1] == "over_pending":
                    store.set_entitlement(principal, ent[0], "active")
                    summary["cleared"] += 1
                else:
                    summary["noop"] += 1
            else:
                summary["noop"] += 1

    log.info("reconcile summary: %s", summary)
    return summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Reconcile tier membership to entitlement.")
    ap.add_argument("--warehouse-id", help="SQL warehouse id (else uses WAREHOUSE_ID env)")
    ap.add_argument("--config", help="path to tiers.yaml (else uses TIERS_CONFIG env)")
    ap.add_argument("--account-id", help="Databricks account id (for account SCIM group management)")
    ap.add_argument("--account-secret-scope", help="secret scope holding account SP creds")
    ap.add_argument("--account-host", help="account console host")
    args, _ = ap.parse_known_args()
    if args.warehouse_id:
        os.environ["WAREHOUSE_ID"] = args.warehouse_id
    if args.config:
        os.environ["TIERS_CONFIG"] = args.config
    if args.account_id:
        os.environ["DATABRICKS_ACCOUNT_ID"] = args.account_id
    if args.account_secret_scope:
        os.environ["ACCOUNT_SP_SECRET_SCOPE"] = args.account_secret_scope
    if args.account_host:
        os.environ["ACCOUNT_HOST"] = args.account_host
    run()
