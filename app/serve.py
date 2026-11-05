"""Tiered budget governance app (budget-override model).

Serves a role-aware single-page UI plus JSON APIs:

  GET  /                        -> the app UI (role decided client-side from /api/me)
  GET  /health
  GET  /api/me                  -> signed-in user: tier, ceiling, next tier, steward?
  GET  /api/usage               -> per-model usage + $ vs ceiling (this user)
  GET  /api/notifications       -> this user's in-app inbox
  POST /api/notifications/read  -> mark this user's inbox read
  GET  /api/requests            -> this user's elevation requests (history)
  POST /api/request-elevation   -> raise an elevation request (current tier -> next)
  GET  /api/approvals           -> pending requests (stewards only)
  POST /api/approvals/{id}      -> {decision, reason} approve/deny (stewards only)
  POST /api/events/budget-hit   -> a 403 budget block occurred; notify the user

Identity is the verified Databricks Apps `X-Forwarded-Email`. Approver = member of the
configured stewards group. Elevation is user-initiated and steward-approved.
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.budget.approvals import Store
from app.budget.config import get_config
from app.budget.escalation import Escalator
from app.budget.groups import GroupManager
from app.budget.notify import Notifier, Slack
from app.core.auth import CallerIdentity, caller_dependency
from app.core.systables import SystemTables

app = FastAPI(title="UAG Tiered Budget Governance")

_UI_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")


@app.on_event("startup")
def _startup() -> None:
    cfg = get_config()
    store = Store(cfg)
    st = SystemTables()
    groups = GroupManager([t.group for t in cfg.tiers])
    notifier = Notifier(store, Slack())
    app.state.cfg = cfg
    app.state.store = store
    app.state.groups = groups
    app.state.escalator = Escalator(cfg, st, groups, store, notifier,
                                    app_url=os.environ.get("APP_URL", os.environ.get("CONSOLE_URL", "")))
    app.state.dev_principal = os.environ.get("DEV_PRINCIPAL")
    try:
        with open(_UI_PATH, encoding="utf-8") as fh:
            app.state.ui_html = fh.read()
    except FileNotFoundError:
        app.state.ui_html = "<h1>UI not found</h1>"


class Decision(BaseModel):
    decision: str
    reason: str | None = None


class BudgetHit(BaseModel):
    tier: str | None = None
    principal: str | None = None    # fallback only; header is preferred


def _is_steward(principal: str) -> bool:
    cfg = app.state.cfg
    if not cfg.stewards_group:
        return False
    try:
        return app.state.groups.is_member(cfg.stewards_group, principal)
    except Exception:
        return False


# --- pages ---------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    cfg = app.state.cfg
    return {"status": "ok", "tiers": [t.name for t in cfg.tiers], "spend_source": cfg.spend.source}


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return app.state.ui_html


# --- user APIs -----------------------------------------------------------
@app.get("/api/me")
def api_me(caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    cfg, esc = app.state.cfg, app.state.escalator
    cur = esc.current_tier(caller.principal)
    nxt = cfg.next_tier(cur.name)
    ent = app.state.store.get_entitlement(caller.principal)
    return JSONResponse({
        "email": caller.principal,
        "is_steward": _is_steward(caller.principal),
        "current_tier": cur.name,
        "ceiling_usd": cur.threshold_usd,
        "next_tier": nxt.name if nxt else None,
        "next_ceiling_usd": nxt.threshold_usd if nxt else None,
        "can_request": nxt is not None and cur.escalation != "none",
        "state": ent[1] if ent else "active",
        "has_open_request": app.state.store.has_open_request(caller.principal),
    })


@app.get("/api/usage")
def api_usage(caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    esc = app.state.escalator
    cur = esc.current_tier(caller.principal)
    usage = esc.usage_for(caller.principal)
    usage["ceiling_usd"] = cur.threshold_usd
    usage["over"] = usage.get("total_usd", 0.0) >= cur.threshold_usd
    return JSONResponse(usage)


@app.get("/api/notifications")
def api_notifications(caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    items = app.state.store.list_notifications(caller.principal)
    return JSONResponse([n.__dict__ for n in items])


@app.post("/api/notifications/read")
def api_notifications_read(caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    app.state.store.mark_notifications_read(caller.principal)
    return JSONResponse({"ok": True})


@app.get("/api/requests")
def api_requests(caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    items = app.state.store.list_for(caller.principal)
    return JSONResponse([a.__dict__ for a in items])


@app.post("/api/request-elevation")
def api_request_elevation(caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    res = app.state.escalator.request_elevation(caller.principal)
    return JSONResponse(res.__dict__)


# --- approver APIs -------------------------------------------------------
@app.get("/api/approvals")
def api_approvals(caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    if not _is_steward(caller.principal):
        raise HTTPException(403, "not an approver")
    out = []
    for a in app.state.store.list_pending():
        usage = app.state.escalator.usage_for(a.principal)
        d = a.__dict__ | {"spend_usd": usage.get("total_usd", 0.0), "models": usage.get("models", [])}
        out.append(d)
    return JSONResponse(out)


@app.post("/api/approvals/{request_id}")
def api_decide(request_id: str, body: Decision,
               caller: CallerIdentity = Depends(caller_dependency)) -> JSONResponse:
    if not _is_steward(caller.principal):
        raise HTTPException(403, "not an approver")
    res = app.state.escalator.apply_decision(request_id, caller.principal, body.decision, body.reason)
    return JSONResponse(res.__dict__)


# --- event sink (403-catch client) --------------------------------------
@app.post("/api/events/budget-hit")
def api_budget_hit(body: BudgetHit,
                   x_forwarded_email: str | None = Header(default=None)) -> JSONResponse:
    """A 403 budget block occurred; record it and notify the user (no elevation).

    Prefer the verified `X-Forwarded-Email` header; fall back to the payload principal
    for programmatic callers whose token does not carry the header.
    """
    principal = x_forwarded_email or body.principal
    if not principal:
        raise HTTPException(400, "principal required")
    res = app.state.escalator.record_budget_hit(principal, body.tier)
    return JSONResponse(res)
