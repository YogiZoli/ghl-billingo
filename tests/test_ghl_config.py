from app.ghl_config import apply_ghl_overrides
from tests.test_poller import make_cfg


def test_overrides_api_key_and_timing():
    cfg = make_cfg(billingo_api_key="", delay_days=1, poll_interval_min=30)
    out = apply_ghl_overrides(
        cfg,
        {
            "billingo_api_key": "live-key",
            "billingo_delay_days": "3",
            "billingo_poll_min": "15",
        },
    )
    assert out.billingo_api_key == "live-key"
    assert out.delay_days == 3
    assert out.poll_interval_min == 15


def test_blank_values_keep_defaults():
    cfg = make_cfg(billingo_api_key="env-key", delay_days=1, poll_interval_min=30)
    out = apply_ghl_overrides(cfg, {"billingo_api_key": "", "billingo_delay_days": ""})
    assert out.billingo_api_key == "env-key"
    assert out.delay_days == 1


def test_invalid_poll_min_is_ignored():
    cfg = make_cfg(poll_interval_min=30)
    out = apply_ghl_overrides(cfg, {"billingo_poll_min": "7"})  # not in {10,15,30,60}
    assert out.poll_interval_min == 30


def test_no_values_returns_same_config():
    cfg = make_cfg()
    assert apply_ghl_overrides(cfg, {}) is cfg
