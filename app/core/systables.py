"""Read client for Unity AI Gateway system tables.

Authoritative per-user spend comes from:
  - system.ai_gateway.external_model_spend  (USD, per identity, per tag; preferred)
  - system.ai_gateway.usage                 (token counts; fallback via a price map)

Queries run through the Databricks SQL Statement Execution API against a
SQL warehouse (WAREHOUSE_ID env var). A single small aggregate query per call.
"""
from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient


class SystemTables:
    def __init__(self, warehouse_id: str | None = None, client: WorkspaceClient | None = None):
        self._wh = warehouse_id or os.environ["WAREHOUSE_ID"]
        self._w = client or WorkspaceClient()

    def _scalar(self, statement: str) -> float:
        resp = self._w.statement_execution.execute_statement(
            warehouse_id=self._wh, statement=statement, wait_timeout="50s"
        )
        data = (resp.result.data_array if resp.result else None) or []
        if not data or data[0][0] is None:
            return 0.0
        return float(data[0][0])

    def _window_predicate(self, col: str, window: str) -> str:
        if window == "rolling_30d":
            return f"{col} >= current_timestamp() - INTERVAL 30 DAYS"
        # month_to_date (default)
        return f"{col} >= date_trunc('month', current_timestamp())"

    def usd_spend(self, principal: str, services: list[str], window: str) -> float:
        """Sum USD spend for a principal on the tier model services, from external_model_spend.

        WARNING: `external_model_spend` covers EXTERNAL (BYOK) model providers ONLY. It does
        NOT contain Databricks-hosted pay-per-token (PPT) foundation-model spend - for PPT
        tiers this returns 0. Use `token_spend_usd` (usage tokens x price map) for PPT models;
        this method is only valid when the tier endpoints route to an external provider.

        Spend is scoped by `service_name` membership in the tier services (the paid pools);
        anything outside that set (e.g. the always-open base pool) is excluded.
        """
        pred = self._window_predicate("usage_start_time", window)
        stmt = f"""
            SELECT COALESCE(SUM(usage_quantity), 0) AS usd
            FROM system.ai_gateway.external_model_spend
            WHERE identity_metadata.run_by = '{_esc(principal)}'
              AND {pred}
              AND service_name IN ({_in_list(services)})
        """
        return self._scalar(stmt)

    def token_spend_usd(self, principal: str, services: list[str],
                        window: str, price_map: dict) -> float:
        """Estimate USD from token counts in the usage table × a price map (near-real-time).

        Preferred for a responsive demo: usage is near-real-time, while external_model_spend
        USD is hourly-aggregated. price_map: {"default": {"input": x, "output": y}, "<glob>": {...}}.
        Costs are computed per destination_model then summed. Scoped to the tier services.
        """
        pred = self._window_predicate("event_time", window)
        stmt = f"""
            SELECT destination_model,
                   COALESCE(SUM(input_tokens), 0)  AS in_tok,
                   COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM system.ai_gateway.usage
            WHERE requester = '{_esc(principal)}'
              AND {pred}
              AND service_name IN ({_in_list(services)})
            GROUP BY destination_model
        """
        resp = self._w.statement_execution.execute_statement(
            warehouse_id=self._wh, statement=stmt, wait_timeout="50s"
        )
        rows = (resp.result.data_array if resp.result else None) or []
        total = 0.0
        for model, in_tok, out_tok in rows:
            rate = _price_for(model or "", price_map)
            total += (float(in_tok or 0) / 1e6) * rate["input"]
            total += (float(out_tok or 0) / 1e6) * rate["output"]
        return total

    def usage_breakdown(self, principal: str, services: list[str],
                        window: str, price_map: dict) -> dict:
        """Per-model usage + estimated $ for a principal on the tier services.

        Returns {"total_usd", "total_in", "total_out", "calls", "models":[{model,
        calls, in_tok, out_tok, usd}]}. Drives the user's usage view and the
        budget-hit notification.
        """
        pred = self._window_predicate("event_time", window)
        stmt = f"""
            SELECT destination_model,
                   COUNT(*)                        AS calls,
                   COALESCE(SUM(input_tokens), 0)  AS in_tok,
                   COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM system.ai_gateway.usage
            WHERE requester = '{_esc(principal)}'
              AND {pred}
              AND service_name IN ({_in_list(services)})
            GROUP BY destination_model
            ORDER BY out_tok DESC
        """
        resp = self._w.statement_execution.execute_statement(
            warehouse_id=self._wh, statement=stmt, wait_timeout="50s"
        )
        rows = (resp.result.data_array if resp.result else None) or []
        models, tot_usd, tot_in, tot_out, tot_calls = [], 0.0, 0, 0, 0
        for model, calls, in_tok, out_tok in rows:
            rate = _price_for(model or "", price_map)
            in_i, out_i, calls_i = int(in_tok or 0), int(out_tok or 0), int(calls or 0)
            usd = (in_i / 1e6) * rate["input"] + (out_i / 1e6) * rate["output"]
            models.append({"model": model or "(unknown)", "calls": calls_i,
                           "in_tok": in_i, "out_tok": out_i, "usd": round(usd, 4)})
            tot_usd += usd; tot_in += in_i; tot_out += out_i; tot_calls += calls_i
        return {"total_usd": round(tot_usd, 4), "total_in": tot_in, "total_out": tot_out,
                "calls": tot_calls, "models": models}


def _price_for(model: str, price_map: dict) -> dict:
    import fnmatch
    for pattern, rate in price_map.items():
        if pattern != "default" and fnmatch.fnmatch(model, pattern):
            return rate
    return price_map.get("default", {"input": 0.0, "output": 0.0})


def _esc(s: str) -> str:
    """Escape single quotes for SQL literals (principals/tags are identifiers, not free text)."""
    return s.replace("'", "''")


def _in_list(values: list[str]) -> str:
    """Render a list of strings as a SQL IN(...) body: 'a','b'. Empty -> NULL (matches nothing)."""
    if not values:
        return "NULL"
    return ", ".join(f"'{_esc(v)}'" for v in values)
