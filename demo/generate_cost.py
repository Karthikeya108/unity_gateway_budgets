"""Fire prompts at the external OpenAI model service to generate a target USD spend.

Invokes the Responses API through the gateway:
  POST /ai-gateway/openai/v1/responses
  header Databricks-Model-Provider-Service: <model service FQN>

Cost is estimated live from the token usage each response returns (input/output),
since external_model_spend records nothing for this service. Stops when the running
estimate reaches TARGET_USD (or the call cap is hit).

Env: DATABRICKS_HOST, DATABRICKS_CONFIG_PROFILE, SERVICE, MODEL, TARGET_USD
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
SERVICE = os.environ.get("SERVICE", "external_ai_services.default.open_ai_model")
MODEL = os.environ.get("MODEL", "gpt-5.4-mini")
PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "DEFAULT")

TARGET_USD = float(os.environ.get("TARGET_USD", "1.00"))
MAX_CALLS = int(os.environ.get("MAX_CALLS", "400"))
WORKERS = int(os.environ.get("WORKERS", "8"))
MAX_OUT = int(os.environ.get("MAX_OUT", "4000"))

# mini-class pricing estimate (USD per 1M tokens)
PRICE_IN = float(os.environ.get("PRICE_IN", "0.25"))
PRICE_OUT = float(os.environ.get("PRICE_OUT", "2.00"))

PROMPTS = [
    "Write a thorough 800-word technical explanation of the Databricks Lakehouse architecture.",
    "Explain Unity Catalog governance in exhaustive detail with examples, about 800 words.",
    "Write a long, detailed comparison of data lakes, warehouses, and lakehouses (~800 words).",
    "Describe Delta Lake internals (transaction log, checkpoints, Z-ordering) in ~800 words.",
    "Write a comprehensive guide to Apache Spark performance tuning, around 800 words.",
    "Explain MLOps on Databricks end to end in great detail, about 800 words.",
    "Write a detailed essay on data engineering best practices for streaming, ~800 words.",
    "Describe the medallion architecture with concrete examples in depth, ~800 words.",
]


def token():
    out = subprocess.check_output(["databricks", "auth", "token", "-p", PROFILE])
    return json.loads(out)["access_token"]


TOKEN = token()

_lock = threading.Lock()
state = {"usd": 0.0, "in": 0, "out": 0, "calls": 0, "errors": 0, "stop": False}


def call(prompt):
    payload = {
        "model": MODEL,
        "max_output_tokens": MAX_OUT,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
    }
    req = urllib.request.Request(
        f"{HOST}/ai-gateway/openai/v1/responses",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Databricks-Model-Provider-Service": SERVICE,
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def worker(i):
    if state["stop"]:
        return
    prompt = PROMPTS[i % len(PROMPTS)] + f" (variation {i})"
    try:
        resp = call(prompt)
        u = resp.get("usage") or {}
        it, ot = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
        cost = it / 1e6 * PRICE_IN + ot / 1e6 * PRICE_OUT
        with _lock:
            state["in"] += it
            state["out"] += ot
            state["usd"] += cost
            state["calls"] += 1
            if state["usd"] >= TARGET_USD:
                state["stop"] = True
            print(f"[{state['calls']:3d}] +{ot:>4} out tok  est_total=${state['usd']:.4f}", flush=True)
    except urllib.error.HTTPError as e:
        with _lock:
            state["errors"] += 1
        body = e.read().decode()[:200]
        print(f"  HTTP {e.code}: {body}", flush=True)
        if e.code == 403 and "budget" in body.lower():
            state["stop"] = True
    except Exception as e:
        with _lock:
            state["errors"] += 1
        print(f"  ERR: {e}", flush=True)


def main():
    print(f"target=${TARGET_USD} model={MODEL} via {SERVICE}")
    print(f"pricing est: ${PRICE_IN}/1M in, ${PRICE_OUT}/1M out  (max_out={MAX_OUT}, workers={WORKERS})\n")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = []
        for i in range(MAX_CALLS):
            if state["stop"]:
                break
            futures.append(ex.submit(worker, i))
            # keep the queue shallow so we can stop promptly near target
            if len(futures) >= WORKERS * 2:
                for f in as_completed(futures):
                    pass
                futures = []
                if state["stop"]:
                    break
        for f in as_completed(futures):
            pass
    dt = time.time() - t0
    print(f"\ndone: {state['calls']} calls, {state['errors']} errors, "
          f"{state['in']} in tok, {state['out']} out tok, "
          f"est ${state['usd']:.4f} in {dt:.0f}s")


if __name__ == "__main__":
    main()
