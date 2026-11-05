"""Verified caller identity for the approval console.

Approver identity must not be spoofable - it drives who is allowed to approve a
tier escalation and is written to the audit record. On Databricks Apps the
signed-in user arrives via X-Forwarded-* headers already verified by the Apps
front door; that is the trusted source.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request


@dataclass
class CallerIdentity:
    principal: str                 # user email or service-principal id
    method: str                    # "forwarded" | "oauth"
    raw_token: str | None = None   # forwarded user token, for downstream UC checks


async def caller_dependency(
    request: Request,
    x_forwarded_email: str | None = Header(default=None),
    x_forwarded_access_token: str | None = Header(default=None),
) -> CallerIdentity:
    """FastAPI dependency: resolve the verified signed-in user.

    Databricks Apps injects X-Forwarded-Email / -Access-Token after validating
    the user at the front door. If absent (e.g. local dev) and a dev principal
    is configured on app.state, fall back to it; otherwise 401.
    """
    if x_forwarded_email:
        return CallerIdentity(
            principal=x_forwarded_email,
            method="forwarded",
            raw_token=x_forwarded_access_token,
        )
    dev_principal = getattr(request.app.state, "dev_principal", None)
    if dev_principal:
        return CallerIdentity(principal=dev_principal, method="oauth")
    raise HTTPException(401, "no verified caller identity (expected Databricks Apps X-Forwarded-Email)")
