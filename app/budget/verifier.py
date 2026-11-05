"""Authoritative spend verification.

A budget alert is only a trigger. Before any tier change, the verifier reads the
system tables to confirm the principal's paid-pool spend against their current
tier threshold. This prevents a spoofed or stale alert from granting a higher
budget.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.budget.config import Config, Tier
from app.core.systables import SystemTables


@dataclass
class SpendCheck:
    principal: str
    current_tier: Tier
    spend_usd: float
    threshold_usd: float
    over_threshold: bool


class Verifier:
    def __init__(self, cfg: Config, systables: SystemTables):
        self._cfg = cfg
        self._st = systables

    def spend_usd(self, principal: str, tier: Tier) -> float:
        """Spend on THIS tier's endpoint only - matches the native per-tier budget block.

        Each tier's budget is scoped to its own endpoint, so spend is measured per-tier
        (not cumulative across tiers): the same basis the budget uses to block.
        """
        services = [tier.service]
        if self._cfg.spend.source == "usage_tokens":
            return self._st.token_spend_usd(
                principal, services, self._cfg.spend.window, self._cfg.spend.price_map
            )
        return self._st.usd_spend(principal, services, self._cfg.spend.window)

    def check(self, principal: str, current_tier: Tier) -> SpendCheck:
        spent = self.spend_usd(principal, current_tier)
        return SpendCheck(
            principal=principal,
            current_tier=current_tier,
            spend_usd=spent,
            threshold_usd=current_tier.threshold_usd,
            over_threshold=spent >= current_tier.threshold_usd,
        )
