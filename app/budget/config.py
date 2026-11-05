"""Configuration loader for the tiered budget kit.

All behaviour is driven by config/tiers.yaml plus environment variables for
secrets and connection details. Nothing tier-specific is hardcoded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

import yaml


@dataclass(frozen=True)
class Tier:
    name: str
    group: str
    service: str             # fully-qualified model service (usage.service_name) scoping this tier's spend
    budget_tag: str
    threshold_usd: float
    escalation: str          # "auto" | "approval" | "none"
    notify: str              # key into notify_targets


@dataclass(frozen=True)
class Spend:
    source: str              # "external_model_spend" | "usage_tokens"
    window: str              # "month_to_date" | "rolling_30d"
    price_map: dict


@dataclass(frozen=True)
class Config:
    catalog: str
    schema: str
    spend: Spend
    budget_tag_key: str
    tiers: list[Tier]
    notify_targets: dict
    stewards_group: str | None = None      # account group whose members may approve escalations
    reconcile_interval_minutes: int = 15

    @property
    def fqn_approvals(self) -> str:
        return f"{self.catalog}.{self.schema}.approvals"

    @property
    def fqn_audit(self) -> str:
        return f"{self.catalog}.{self.schema}.tier_audit"

    @property
    def fqn_user_tier(self) -> str:
        return f"{self.catalog}.{self.schema}.user_tier"

    @property
    def fqn_notifications(self) -> str:
        return f"{self.catalog}.{self.schema}.notifications"

    @property
    def tier_services(self) -> list[str]:
        """Model service names (usage.service_name) that scope paid spend across all tiers."""
        return [t.service for t in self.tiers]

    def tier_by_name(self, name: str) -> Tier | None:
        return next((t for t in self.tiers if t.name == name), None)

    def tier_by_group(self, group: str) -> Tier | None:
        return next((t for t in self.tiers if t.group == group), None)

    def next_tier(self, name: str) -> Tier | None:
        for i, t in enumerate(self.tiers):
            if t.name == name and i + 1 < len(self.tiers):
                return self.tiers[i + 1]
        return None

    def channel_for(self, notify_key: str) -> str | None:
        """Resolve a notify target to a concrete Slack channel via its env var."""
        target = self.notify_targets.get(notify_key) or {}
        env = target.get("channel_env")
        return os.environ.get(env) if env else None


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("TIERS_CONFIG", "config/tiers.yaml")
    with open(path) as fh:
        raw = yaml.safe_load(fh)

    tiers = [Tier(**t) for t in raw["tiers"]]
    return Config(
        catalog=raw["catalog"],
        schema=raw["schema"],
        spend=Spend(
            source=raw["spend"]["source"],
            window=raw["spend"].get("window", "month_to_date"),
            price_map=raw["spend"].get("price_map", {}),
        ),
        budget_tag_key=raw["budget_tag_key"],
        tiers=tiers,
        notify_targets=raw.get("notify_targets", {}),
        stewards_group=raw.get("stewards_group"),
        reconcile_interval_minutes=raw.get("reconcile_interval_minutes", 15),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
