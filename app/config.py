"""Configuration loading.

For the MVP we read a single tenant's settings from environment variables
(.env). Phase 2 swaps this for a per-subaccount config row in the DB; the
``TenantConfig`` dataclass is already the unit we pass around so that change
stays local.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # optional in prod, handy in dev
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


_VALID_ANCHORS = {"fulfillment_date", "invoice_date", "paid_date"}
_VALID_POLL = {10, 15, 30, 60}


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class TenantConfig:
    """Everything the connector needs to serve one GHL subaccount."""

    # GHL
    ghl_base_url: str
    ghl_api_version: str
    ghl_pit_token: str
    ghl_location_id: str
    # Billingo
    billingo_base_url: str
    billingo_api_key: str
    # behaviour
    anchor_date: str
    delay_days: int
    poll_interval_min: int
    review_entry_tag: str
    retag_if_present: bool
    timezone: str

    def validate(self) -> None:
        if self.anchor_date not in _VALID_ANCHORS:
            raise ValueError(
                f"ANCHOR_DATE must be one of {_VALID_ANCHORS}, got {self.anchor_date!r}"
            )
        if self.poll_interval_min not in _VALID_POLL:
            raise ValueError(
                f"POLL_INTERVAL_MIN must be one of {_VALID_POLL}, got {self.poll_interval_min}"
            )
        if self.delay_days < 0:
            raise ValueError("DELAY_DAYS must be >= 0")


def load_tenant_config() -> TenantConfig:
    """Build a TenantConfig from environment variables."""
    cfg = TenantConfig(
        ghl_base_url=os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com"),
        ghl_api_version=os.getenv("GHL_API_VERSION", "2021-07-28"),
        ghl_pit_token=os.getenv("GHL_PIT_TOKEN", ""),
        ghl_location_id=os.getenv("GHL_LOCATION_ID", ""),
        billingo_base_url=os.getenv("BILLINGO_BASE_URL", "https://api.billingo.hu/v3"),
        billingo_api_key=os.getenv("BILLINGO_API_KEY", ""),
        anchor_date=os.getenv("ANCHOR_DATE", "fulfillment_date"),
        delay_days=int(os.getenv("DELAY_DAYS", "1")),
        poll_interval_min=int(os.getenv("POLL_INTERVAL_MIN", "30")),
        review_entry_tag=os.getenv("REVIEW_ENTRY_TAG", "customer"),
        retag_if_present=_as_bool(os.getenv("RETAG_IF_PRESENT"), True),
        timezone=os.getenv("TIMEZONE", "Europe/Budapest"),
    )
    cfg.validate()
    return cfg


DB_PATH = os.getenv("DB_PATH", "data/connector.db")
FERNET_KEY = os.getenv("FERNET_KEY", "")
