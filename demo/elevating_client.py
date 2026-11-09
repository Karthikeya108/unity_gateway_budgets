#!/usr/bin/env python3
"""Budget-aware client - reports a 403 block to the governance app.

Calls the shared model endpoint through Unity AI Gateway. When the budget blocks the
call (HTTP 403 `Budget "…" reached its limit`), it reports the event to the app
(`POST /api/events/budget-hit`) - which notifies the user (in-app + Slack) with their
usage and a link to request elevation - then falls back to the base model so work
continues. Elevation itself is done by the user in the app, then approved by a steward
(no auto-elevation here).

Env: DATABRICKS_HOST, APP_URL, DATABRICKS_TOKEN, PRINCIPAL
     COMMON_SERVICE, BASE_SERVICE (optional: model service FQNs; defaults below)
Usage: python demo/elevating_client.py ["prompt"]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

WS = os.environ["DATABRICKS_HOST"].rstrip("/")
APP = os.environ["APP_URL"].rstrip("/")
TOKEN = os.environ["DATABRICKS_TOKEN"]
PRINCIPAL = os.environ["PRINCIPAL"]

COMMON_SVC = os.environ.get("COMMON_SERVICE", "internal_ai_services.shared.common_tier_model_service")
BASE_SVC = os.environ.get("BASE_SERVICE", "internal_ai_services.shared.base_model_service")


def _post(url, payload, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def call_model(service, prompt):
    return _post(f"{WS}/ai-gateway/mlflow/v1/chat/completions",
                 {"model": service, "messages": [{"role": "user", "content": prompt}], "max_tokens": 64})


def _content(resp):
    try:
        return resp["choices"][0]["message"]["content"].strip()
    except Exception:
        return str(resp)[:200]


def run(prompt):
    code, resp = call_model(COMMON_SVC, prompt)
    if code == 200:
        print(f"✓ OK - {_content(resp)[:80]}")
        return
    if code == 403 and "budget" in json.dumps(resp).lower():
        print("✗ BLOCKED - budget ceiling reached. Reporting to the app…")
        _, ev = _post(f"{APP}/api/events/budget-hit", {"principal": PRINCIPAL})
        if isinstance(ev, dict) and ev.get("usage"):
            u = ev["usage"]
            print(f"  notified {PRINCIPAL}: MTD ~${u.get('total_usd',0):.2f} at the "
                  f"${ev.get('ceiling_usd',0):.0f} ceiling across {u.get('calls',0)} calls.")
        print(f"  → Open {APP}/ to request more budget (an approver will review it).")
        print("  ↩ falling back to the base model so work continues…")
        bcode, bresp = call_model(BASE_SVC, prompt)
        print(f"  base → {'OK - ' + _content(bresp)[:60] if bcode == 200 else bresp}")
        return
    print(f"! {code}: {json.dumps(resp)[:180]}")


if __name__ == "__main__":
    ask = sys.argv[1] if len(sys.argv) > 1 else "In one sentence, what is Unity Catalog?"
    print(f"principal={PRINCIPAL}\n")
    run(ask)
