"""Smoke tests for the Railway runtime entrypoint.

We avoid starting the real BackgroundScheduler (that's the FastAPI startup
event); instead we exercise the job functions directly. With no Billingo key
and no GHL token in the env, both jobs must no-op cleanly and hit no network.
"""
from app import runner


def test_health_payload_shape():
    out = runner.health()
    assert out["status"] == "ok"
    assert "scheduler_running" in out


def test_poll_job_noops_without_key(monkeypatch):
    # Ensure a clean env: no GHL token, no Billingo key -> inactive path.
    for var in ("GHL_PIT_TOKEN", "GHL_LOCATION_ID", "BILLINGO_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Should return without raising and without any network call.
    assert runner._run_poll() is None


def test_scheduler_job_noops_without_ghl(monkeypatch):
    for var in ("GHL_PIT_TOKEN", "GHL_LOCATION_ID"):
        monkeypatch.delenv(var, raising=False)
    assert runner._run_scheduler() is None


def test_app_exposes_health_route():
    paths = {r.path for r in runner.app.routes}
    assert "/health" in paths
