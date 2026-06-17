"""Per-subaccount settings pulled from GHL Custom Values (LOCKED model).

Mate maintains a few values in the GHL UI under the subaccount settings; the
connector reads them at run time and overlays them on the env defaults:

    billingo_api_key    -> the client's Billingo key (required to poll)
    billingo_delay_days -> days after the anchor date before tagging (default 1)
    billingo_poll_min   -> poll cadence in minutes, 10/15/30/60 (default 30)

Anything missing or blank simply keeps the env/default value, so an empty
``billingo_api_key`` means "subaccount inactive" rather than an error.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from .config import _VALID_POLL, TenantConfig

log = logging.getLogger("connector.ghl_config")

KEY_API = "billingo_api_key"
KEY_DELAY = "billingo_delay_days"
KEY_POLL = "billingo_poll_min"


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def apply_ghl_overrides(
    cfg: TenantConfig, custom_values: dict[str, str]
) -> TenantConfig:
    """Return a config with GHL Custom Value overrides applied over ``cfg``."""
    changes: dict = {}

    api_key = (custom_values.get(KEY_API) or "").strip()
    if api_key:
        changes["billingo_api_key"] = api_key

    if custom_values.get(KEY_DELAY) not in (None, ""):
        delay = _as_int(custom_values.get(KEY_DELAY), cfg.delay_days)
        changes["delay_days"] = delay if delay >= 0 else cfg.delay_days

    if custom_values.get(KEY_POLL) not in (None, ""):
        poll = _as_int(custom_values.get(KEY_POLL), cfg.poll_interval_min)
        if poll in _VALID_POLL:
            changes["poll_interval_min"] = poll
        else:
            log.warning(
                "ignoring %s=%s (must be one of %s)", KEY_POLL, poll, sorted(_VALID_POLL)
            )

    if not changes:
        return cfg
    merged = replace(cfg, **changes)
    merged.validate()
    return merged
