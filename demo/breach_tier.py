"""Fire opus calls at the tiered endpoint until the budget's native 403 block triggers.

This breaches the caller's current effective ceiling on the shared budget. The block
is real-time and self-limiting: once the ceiling is crossed, subsequent calls 403 and
add no cost, so the run stops itself at the threshold.

Env: DATABRICKS_HOST, DATABRICKS_CONFIG_PROFILE, SERVICE
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
SERVICE = os.environ.get("SERVICE", "internal_ai_services.shared.common_tier_model_service")
PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
MAX_CALLS = 80
MAX_TOKENS = 2048

# price map (config/tiers.yaml) for a live estimate
PRICE = {"input": 8.20, "output": 41.00}

PROMPTS = [
    "Write a thorough 1000-word technical explanation of the Databricks Lakehouse architecture.",
    "Explain Unity Catalog governance end to end in exhaustive detail (~1000 words).",
    "Describe Delta Lake internals in depth: transaction log, checkpoints, Z-order, liquid clustering (~1000 words).",
    "Write a comprehensive guide to Spark performance tuning with examples (~1000 words).",
    "Explain MLOps on Databricks in great detail from experiment to production (~1000 words).",
]

TOKEN = json.loads(subprocess.check_output(["databricks", "auth", "token", "-p", PROFILE]))["access_token"]


def call(prompt):
    payload = {"model": SERVICE, "max_tokens": MAX_TOKENS,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        f"{HOST}/ai-gateway/mlflow/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    est = 0.0
    for i in range(MAX_CALLS):
        code, resp = call(PROMPTS[i % len(PROMPTS)] + f" (variation {i})")
        if code == 200:
            u = resp.get("usage", {})
            it, ot = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
            est += it / 1e6 * PRICE["input"] + ot / 1e6 * PRICE["output"]
            print(f"[{i+1:2d}] 200  +{ot:>4} out tok  est_added=${est:.4f}", flush=True)
        elif code == 403 and "budget" in str(resp).lower():
            print(f"[{i+1:2d}] 403  BUDGET BLOCK TRIGGERED", flush=True)
            try:
                msg = json.loads(resp).get("message", resp)
            except Exception:
                msg = resp
            print(f"     -> {str(msg)[:300]}", flush=True)
            print(f"\nBREACHED. est added this run ~${est:.4f} before the block.", flush=True)
            return
        else:
            print(f"[{i+1:2d}] {code}  {str(resp)[:200]}", flush=True)
    print(f"\nreached MAX_CALLS={MAX_CALLS} without a block. est added ~${est:.4f}", flush=True)


if __name__ == "__main__":
    main()
