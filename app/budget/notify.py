"""Notifications: in-app inbox (always) + Slack (optional).

Every user- or approver-facing event is written to the in-app notifications table
(shown in the app) and, when a Slack bot token + channel are configured, also posted
to Slack. Slack degrades to logging when unconfigured, so the app works with no
external creds. The bot token comes from SLACK_BOT_TOKEN, or the `slack_bot_token`
key in the account-SP secret scope (ACCOUNT_SP_SECRET_SCOPE); channels from
SLACK_CHANNEL_USER / SLACK_CHANNEL_STEWARD.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request

log = logging.getLogger("budget.notify")


class Slack:
    def __init__(self, token: str | None = None):
        self._token = token or os.environ.get("SLACK_BOT_TOKEN") or _token_from_scope()

    def post(self, channel: str | None, text: str) -> None:
        if not self._token or not channel:
            log.info("slack (skipped, no token/channel): %s", text)
            return
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text}).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                if not body.get("ok"):
                    log.warning("slack post failed: %s", body.get("error"))
        except Exception as exc:  # notification failure must never break the flow
            log.warning("slack post error: %s", exc)


def _token_from_scope() -> str | None:
    scope = os.environ.get("ACCOUNT_SP_SECRET_SCOPE")
    if not scope:
        return None
    try:
        from databricks.sdk import WorkspaceClient
        resp = WorkspaceClient().secrets.get_secret(scope, "slack_bot_token")
        return base64.b64decode(resp.value).decode()
    except Exception:
        return None


def _fmt_usage(usage: dict) -> str:
    """One-line-per-model usage summary for a notification body."""
    lines = [f"- {m['model']}: {m['calls']} calls, "
             f"{m['in_tok']:,} in / {m['out_tok']:,} out tokens, ~${m['usd']:,.2f}"
             for m in usage.get("models", [])]
    return "\n".join(lines) or "- (no usage recorded yet)"


class Notifier:
    """Writes an in-app notification and mirrors it to Slack."""

    def __init__(self, store, slack: Slack | None = None):
        self._store = store
        self._slack = slack or Slack()
        self._ch_user = os.environ.get("SLACK_CHANNEL_USER") or os.environ.get("SLACK_CHANNEL_MANAGER") or ""
        self._ch_steward = os.environ.get("SLACK_CHANNEL_STEWARD") or ""

    # --- user-facing ----------------------------------------------------
    def budget_hit(self, principal: str, tier: str, ceiling: float, usage: dict, app_url: str) -> None:
        title = f"You've reached your ${ceiling:,.0f} AI budget"
        body = (f"Your month-to-date AI spend is ~${usage.get('total_usd', 0):,.2f} across "
                f"{usage.get('calls', 0)} calls "
                f"({usage.get('total_in', 0):,} in / {usage.get('total_out', 0):,} out tokens).\n"
                f"{_fmt_usage(usage)}\n\n"
                f"Request more budget: {app_url}/")
        self._store.add_notification(principal, "budget_hit", title, body)
        self._slack.post(self._ch_user,
                         f":warning: *{principal}* hit their *${ceiling:,.0f}* budget ({tier}). "
                         f"MTD ~${usage.get('total_usd', 0):,.2f}. Request elevation: {app_url}/")

    def request_submitted(self, principal: str, from_tier: str, to_tier: str, request_id: str) -> None:
        self._store.add_notification(
            principal, "request_created", "Budget elevation requested",
            f"Your request to move {from_tier} → {to_tier} was submitted and is awaiting approval.",
            request_id)

    def decision(self, principal: str, from_tier: str, to_tier: str, decision: str,
                 approver: str, reason: str | None, request_id: str) -> None:
        if decision == "approved":
            title = f"Budget elevation approved → {to_tier}"
            body = (f"{approver} approved your move {from_tier} → {to_tier}. "
                    f"Your higher AI budget is now active (allow a minute to propagate).")
            emoji = ":white_check_mark:"
        else:
            title = "Budget elevation declined"
            body = (f"{approver} declined your move {from_tier} → {to_tier}."
                    + (f" Reason: {reason}" if reason else ""))
            emoji = ":x:"
        self._store.add_notification(principal, decision, title, body, request_id)
        self._slack.post(self._ch_user, f"{emoji} *{principal}*: {title}")

    # --- approver-facing ------------------------------------------------
    def notify_approver(self, principal: str, from_tier: str, to_tier: str,
                        usage: dict, request_id: str, app_url: str) -> None:
        self._slack.post(
            self._ch_steward,
            f":lock: *Budget elevation request* - `{principal}` requests *{from_tier} → {to_tier}* "
            f"(MTD ~${usage.get('total_usd', 0):,.2f}). Review: {app_url}/")
