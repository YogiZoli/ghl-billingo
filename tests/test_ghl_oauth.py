"""Session 3: agency OAuth app — token exchange, refresh rotation, and
on-demand location-token minting/caching. All HTTP is mocked via
``responses``; nothing here touches a real GHL account."""
import base64

import responses
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import crypto
from app.ghl_oauth import (
    OAuthError,
    OAuthSettings,
    TokenManager,
    build_authorize_url,
    verify_marketplace_webhook,
    verify_webhook_signature,
)
from app.store import Store

BASE = "https://services.leadconnectorhq.com"


def make_settings(**overrides):
    defaults = dict(
        client_id="client-123",
        client_secret="secret-abc",
        redirect_uri="https://connector.example.com/oauth/callback",
        scopes="contacts.write locations/customValues.readonly",
        api_base=BASE,
    )
    defaults.update(overrides)
    return OAuthSettings(**defaults)


def make_manager(monkeypatch, settings=None):
    monkeypatch.setattr("app.config.FERNET_KEY", Fernet.generate_key().decode())
    crypto.reset_cache()
    store = Store(":memory:")
    return TokenManager(settings or make_settings(), store, sleep=lambda _s: None)


# -- authorize URL -----------------------------------------------------
def test_build_authorize_url_contains_params():
    url = build_authorize_url(make_settings())
    assert url.startswith("https://marketplace.gohighlevel.com/oauth/chooselocation?")
    assert "client_id=client-123" in url
    assert "response_type=code" in url


def test_settings_validate_raises_when_missing():
    settings = make_settings(client_id="")
    try:
        build_authorize_url(settings)
        assert False, "expected OAuthError"
    except OAuthError:
        pass


# -- step 1: exchange code -> Company token -----------------------------
@responses.activate
def test_exchange_code_persists_company_token(monkeypatch):
    responses.add(
        responses.POST,
        f"{BASE}/oauth/token",
        json={
            "access_token": "acc-1",
            "refresh_token": "ref-1",
            "expires_in": 86399,
            "companyId": "COMP1",
            "scope": "contacts.write",
        },
        status=200,
    )
    tm = make_manager(monkeypatch)
    data = tm.exchange_code("install-code")
    assert data["companyId"] == "COMP1"

    row = tm.store.get_company_token("COMP1")
    assert crypto.decrypt(row["access_token_enc"]) == "acc-1"
    assert crypto.decrypt(row["refresh_token_enc"]) == "ref-1"


@responses.activate
def test_exchange_code_rejects_incomplete_response(monkeypatch):
    responses.add(
        responses.POST,
        f"{BASE}/oauth/token",
        json={"access_token": "acc-1", "companyId": "COMP1"},  # no refresh_token
        status=200,
    )
    tm = make_manager(monkeypatch)
    try:
        tm.exchange_code("install-code")
        assert False, "expected OAuthError"
    except OAuthError:
        pass


# -- step 4: refresh rotation --------------------------------------------
@responses.activate
def test_get_company_access_token_refreshes_when_expired(monkeypatch):
    tm = make_manager(monkeypatch)
    tm.store.save_company_token(
        "COMP1",
        crypto.encrypt("stale-access"),
        crypto.encrypt("ref-old"),
        expires_at="2000-01-01T00:00:00+00:00",  # long expired
    )
    responses.add(
        responses.POST,
        f"{BASE}/oauth/token",
        json={
            "access_token": "acc-new",
            "refresh_token": "ref-new",
            "expires_in": 86399,
            "companyId": "COMP1",
        },
        status=200,
    )
    token = tm.get_company_access_token("COMP1")
    assert token == "acc-new"

    row = tm.store.get_company_token("COMP1")
    assert crypto.decrypt(row["refresh_token_enc"]) == "ref-new"
    # the request used the OLD refresh token, per rotation semantics
    sent = responses.calls[0].request.body
    assert "ref-old" in sent


def test_get_company_access_token_with_no_stored_token_raises(monkeypatch):
    tm = make_manager(monkeypatch)
    try:
        tm.get_company_access_token("COMP1")
        assert False, "expected OAuthError"
    except OAuthError:
        pass


@responses.activate
def test_get_company_access_token_reuses_valid_token(monkeypatch):
    tm = make_manager(monkeypatch)
    tm.store.save_company_token(
        "COMP1",
        crypto.encrypt("still-good"),
        crypto.encrypt("ref-1"),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    token = tm.get_company_access_token("COMP1")
    assert token == "still-good"
    assert len(responses.calls) == 0  # no refresh call needed


# -- step 3: per-location tokens -----------------------------------------
@responses.activate
def test_mint_location_token_and_cache(monkeypatch):
    tm = make_manager(monkeypatch)
    tm.store.save_company_token(
        "COMP1",
        crypto.encrypt("company-access"),
        crypto.encrypt("ref-1"),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    responses.add(
        responses.POST,
        f"{BASE}/oauth/locationToken",
        json={"access_token": "loc-token-1", "expires_in": 86399},
        status=200,
    )
    token = tm.get_location_token("LOC1", "COMP1")
    assert token == "loc-token-1"
    assert len(responses.calls) == 1

    # second call should hit the cache, not mint again
    token_again = tm.get_location_token("LOC1", "COMP1")
    assert token_again == "loc-token-1"
    assert len(responses.calls) == 1


@responses.activate
def test_token_provider_returns_callable_that_resolves(monkeypatch):
    tm = make_manager(monkeypatch)
    tm.store.save_company_token(
        "COMP1",
        crypto.encrypt("company-access"),
        crypto.encrypt("ref-1"),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    responses.add(
        responses.POST,
        f"{BASE}/oauth/locationToken",
        json={"access_token": "loc-token-9", "expires_in": 86399},
        status=200,
    )
    provider = tm.token_provider("LOC1", "COMP1")
    assert provider() == "loc-token-9"


# -- webhook signature verification ---------------------------------------
def test_verify_webhook_signature_accepts_valid_and_rejects_tampered():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    body = b'{"type":"INSTALL","locationId":"LOC1","companyId":"COMP1"}'
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(signature).decode()

    assert verify_webhook_signature(body, sig_b64, public_pem) is True
    assert verify_webhook_signature(body + b"tampered", sig_b64, public_pem) is False
    assert verify_webhook_signature(body, "not-a-real-signature", public_pem) is False


# -- webhook signature verification (Ed25519 + dual-scheme dispatch) ------
def _ed25519_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


def test_verify_marketplace_webhook_accepts_valid_ed25519_signature(monkeypatch):
    private_key, public_pem = _ed25519_keypair()
    body = b'{"type":"INSTALL","locationId":"LOC1","companyId":"COMP1"}'
    signature = private_key.sign(body)
    sig_b64 = base64.b64encode(signature).decode()

    monkeypatch.setattr("app.ghl_oauth._GHL_ED25519_PUBLIC_KEY_PEM", public_pem)
    assert verify_marketplace_webhook(body, {"x-ghl-signature": sig_b64}) is True


def test_verify_marketplace_webhook_rejects_tampered_ed25519_body(monkeypatch):
    private_key, public_pem = _ed25519_keypair()
    body = b'{"type":"INSTALL","locationId":"LOC1","companyId":"COMP1"}'
    signature = private_key.sign(body)
    sig_b64 = base64.b64encode(signature).decode()

    monkeypatch.setattr("app.ghl_oauth._GHL_ED25519_PUBLIC_KEY_PEM", public_pem)
    assert verify_marketplace_webhook(body + b"tampered", {"x-ghl-signature": sig_b64}) is False


def test_verify_marketplace_webhook_falls_back_to_legacy_rsa(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    body = b'{"type":"UNINSTALL","locationId":"LOC1","companyId":"COMP1"}'
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(signature).decode()

    monkeypatch.setattr("app.ghl_oauth._GHL_LEGACY_RSA_PUBLIC_KEY_PEM", public_pem)
    # no x-ghl-signature present -> must use legacy x-wh-signature path
    assert verify_marketplace_webhook(body, {"x-wh-signature": sig_b64}) is True


def test_verify_marketplace_webhook_prefers_ed25519_over_legacy(monkeypatch):
    ed_private, ed_public_pem = _ed25519_keypair()
    rsa_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_public_pem = rsa_private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    body = b'{"type":"INSTALL","locationId":"LOC1","companyId":"COMP1"}'
    ed_sig = base64.b64encode(ed_private.sign(body)).decode()
    # deliberately bogus legacy signature -- should be ignored since the
    # Ed25519 header takes priority
    bogus_legacy_sig = base64.b64encode(b"not-a-real-signature").decode()

    monkeypatch.setattr("app.ghl_oauth._GHL_ED25519_PUBLIC_KEY_PEM", ed_public_pem)
    monkeypatch.setattr("app.ghl_oauth._GHL_LEGACY_RSA_PUBLIC_KEY_PEM", rsa_public_pem)
    headers = {"x-ghl-signature": ed_sig, "x-wh-signature": bogus_legacy_sig}
    assert verify_marketplace_webhook(body, headers) is True


def test_verify_marketplace_webhook_rejects_when_no_signature_header():
    body = b'{"type":"INSTALL","locationId":"LOC1","companyId":"COMP1"}'
    assert verify_marketplace_webhook(body, {}) is False
