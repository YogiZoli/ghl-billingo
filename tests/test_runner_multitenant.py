"""Session 3: the multi-tenant fan-out in app.runner._iter_tenants.

Covers the three cases that matter for zero-touch onboarding:
  * legacy single PIT tenant still works when OAuth isn't configured;
  * an OAuth tenant (from the ``tenants`` table) gets a working GHLClient
    whose token is minted on demand via the agency Company token;
  * a location present in both sources is only processed once (OAuth wins).
"""
import responses
from cryptography.fernet import Fernet

from app import crypto, runner
from app.ghl_oauth import OAuthSettings, TokenManager
from app.store import Store

BASE = "https://services.leadconnectorhq.com"


def _clear_oauth_env(monkeypatch):
    for var in ("GHL_OAUTH_CLIENT_ID", "GHL_OAUTH_CLIENT_SECRET", "GHL_OAUTH_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)


def _clear_pit_env(monkeypatch):
    for var in ("GHL_PIT_TOKEN", "GHL_LOCATION_ID"):
        monkeypatch.delenv(var, raising=False)


@responses.activate
def test_legacy_pit_tenant_used_when_oauth_not_configured(monkeypatch):
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setattr(runner, "_token_manager", None)
    _clear_oauth_env(monkeypatch)
    monkeypatch.setenv("GHL_PIT_TOKEN", "pit-env-token")
    monkeypatch.setenv("GHL_LOCATION_ID", "ENV_LOC")

    responses.add(
        responses.GET,
        f"{BASE}/locations/ENV_LOC/customValues",
        json={"customValues": []},
        status=200,
    )

    store = Store(":memory:")
    results = list(runner._iter_tenants(store))
    assert len(results) == 1
    cfg, _ghl = results[0]
    assert cfg.ghl_location_id == "ENV_LOC"


def test_no_tenants_when_nothing_configured(monkeypatch):
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setattr(runner, "_token_manager", None)
    _clear_oauth_env(monkeypatch)
    _clear_pit_env(monkeypatch)

    store = Store(":memory:")
    assert list(runner._iter_tenants(store)) == []


@responses.activate
def test_oauth_tenant_gets_working_client(monkeypatch):
    monkeypatch.setenv("DB_PATH", ":memory:")
    _clear_pit_env(monkeypatch)
    monkeypatch.setattr("app.config.FERNET_KEY", Fernet.generate_key().decode())
    crypto.reset_cache()

    store = Store(":memory:")
    store.upsert_tenant("LOC1", "COMP1", install_type="Location")
    store.save_company_token(
        "COMP1",
        crypto.encrypt("company-access"),
        crypto.encrypt("company-refresh"),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    settings = OAuthSettings(
        client_id="cid", client_secret="csecret",
        redirect_uri="https://x.example.com/oauth/callback",
        scopes="contacts.write", api_base=BASE,
    )
    tm = TokenManager(settings, store, sleep=lambda _s: None)
    monkeypatch.setattr(runner, "_token_manager", tm)

    responses.add(
        responses.POST, f"{BASE}/oauth/locationToken",
        json={"access_token": "loc-token", "expires_in": 86399}, status=200,
    )
    responses.add(
        responses.GET, f"{BASE}/locations/LOC1/customValues",
        json={"customValues": [{"name": "billingo_api_key", "value": "bk-123"}]},
        status=200,
    )

    results = list(runner._iter_tenants(store))
    assert len(results) == 1
    cfg, _ghl = results[0]
    assert cfg.ghl_location_id == "LOC1"
    assert cfg.billingo_api_key == "bk-123"


@responses.activate
def test_oauth_registry_wins_over_legacy_pit_for_same_location(monkeypatch):
    monkeypatch.setenv("DB_PATH", ":memory:")
    _clear_oauth_env(monkeypatch)
    monkeypatch.setenv("GHL_PIT_TOKEN", "pit-env-token")
    monkeypatch.setenv("GHL_LOCATION_ID", "SHARED_LOC")
    monkeypatch.setattr("app.config.FERNET_KEY", Fernet.generate_key().decode())
    crypto.reset_cache()

    store = Store(":memory:")
    store.upsert_tenant("SHARED_LOC", "COMP1")
    store.save_company_token(
        "COMP1", crypto.encrypt("acc"), crypto.encrypt("ref"),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    settings = OAuthSettings(
        client_id="cid", client_secret="csecret",
        redirect_uri="https://x.example.com/oauth/callback",
        scopes="contacts.write", api_base=BASE,
    )
    tm = TokenManager(settings, store, sleep=lambda _s: None)
    monkeypatch.setattr(runner, "_token_manager", tm)

    responses.add(
        responses.POST, f"{BASE}/oauth/locationToken",
        json={"access_token": "loc-token", "expires_in": 86399}, status=200,
    )
    responses.add(
        responses.GET, f"{BASE}/locations/SHARED_LOC/customValues",
        json={"customValues": []}, status=200,
    )

    results = list(runner._iter_tenants(store))
    # exactly one tenant, not two, for the shared location_id
    assert len(results) == 1
    assert results[0][0].ghl_location_id == "SHARED_LOC"
    # only the OAuth-flavoured request pair should have fired (no PIT call)
    assert len(responses.calls) == 2
