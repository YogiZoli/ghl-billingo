"""GHL Marketplace OAuth 2.0 — agency app, zero-touch sub-account access.

Session 3 adds a second auth path alongside the existing PIT token: a
private, agency-level GHL Marketplace OAuth app. Once installed on the
agency (one-time, by Zoltan/Mate), every current and future sub-account can
be written to with NO further human action — no PIT creation, no Custom
Value editing for tokens.

How the pieces fit together
----------------------------
1. One-time authorization: an agency admin visits the GHL "choose location /
   install" screen and approves the app. GHL redirects to our
   ``redirect_uri`` with a ``code``. We exchange it for a **Company**-level
   access + refresh token pair (``exchange_code``). This is the only manual
   step, ever.
2. GHL fires an INSTALL webhook per sub-account (now and for every new one
   created later, if the app distribution is "install on all sub-accounts").
   The webhook handler (see ``app/runner.py``) records the location in the
   ``tenants`` table — that's what makes it pollable.
3. To act on a sub-account, we mint a short-lived (~24h) **Location** token
   via ``POST /oauth/locationToken``, authenticated with the agency's
   Company access token. We cache it in ``oauth_location_tokens`` and only
   re-mint when it's expired.
4. The Company access token itself expires (~24h). Its refresh token
   ROTATES on every use — GHL invalidates the old one and returns a new one
   that we must persist immediately, or the next refresh fails permanently.
   ``get_company_access_token`` refreshes proactively (with a safety buffer)
   so step 3 never has to deal with an expired Company token.

All tokens are encrypted at rest via ``app.crypto`` (Fernet / FERNET_KEY).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from . import crypto
from .store import Store

log = logging.getLogger("connector.ghl_oauth")

# Refresh/re-mint this long before actual expiry so a slow request never
# straddles the boundary and gets a 401 mid-call.
REFRESH_SAFETY_BUFFER = timedelta(seconds=120)

# Where an agency admin goes to install the app (one-time, manual).
AUTHORIZE_BASE_URL = "https://marketplace.gohighlevel.com/oauth/chooselocation"

DEFAULT_API_BASE = "https://services.leadconnectorhq.com"


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str  # space-separated, e.g. "contacts.readonly contacts.write locations/customValues.readonly"
    api_base: str = DEFAULT_API_BASE

    def validate(self) -> None:
        missing = [
            name
            for name, val in (
                ("client_id", self.client_id),
                ("client_secret", self.client_secret),
                ("redirect_uri", self.redirect_uri),
            )
            if not val
        ]
        if missing:
            raise OAuthError(
                f"OAuth not configured — missing env var(s) for: {', '.join(missing)}"
            )


def load_oauth_settings() -> OAuthSettings:
    return OAuthSettings(
        client_id=os.getenv("GHL_OAUTH_CLIENT_ID", ""),
        client_secret=os.getenv("GHL_OAUTH_CLIENT_SECRET", ""),
        redirect_uri=os.getenv("GHL_OAUTH_REDIRECT_URI", ""),
        scopes=os.getenv(
            "GHL_OAUTH_SCOPES",
            "contacts.readonly contacts.write locations/customValues.readonly",
        ),
        api_base=os.getenv("GHL_BASE_URL", DEFAULT_API_BASE),
    )


def build_authorize_url(settings: OAuthSettings) -> str:
    """The one-time, human-clicked install link (agency admin only)."""
    settings.validate()
    params = {
        "response_type": "code",
        "redirect_uri": settings.redirect_uri,
        "client_id": settings.client_id,
        "scope": settings.scopes,
    }
    return f"{AUTHORIZE_BASE_URL}?{urlencode(params)}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(seconds: int) -> str:
    return (_utcnow() + timedelta(seconds=int(seconds))).isoformat()


def _is_expired(expires_at_iso: str) -> bool:
    try:
        exp = datetime.fromisoformat(expires_at_iso)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return _utcnow() >= (exp - REFRESH_SAFETY_BUFFER)


class TokenManager:
    """Owns the agency Company token and mints/caches Location tokens."""

    def __init__(
        self,
        settings: OAuthSettings,
        store: Store,
        session: requests.Session | None = None,
        timeout: int = 30,
        sleep=time.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self.session = session or requests.Session()
        self.timeout = timeout
        self._sleep = sleep

    # -- HTTP -----------------------------------------------------------
    def _post_token(self, body: dict) -> dict:
        url = f"{self.settings.api_base}/oauth/token"
        backoff = 1.0
        for attempt in range(1, 4):
            resp = self.session.post(url, data=body, timeout=self.timeout)
            if resp.status_code == 429 and attempt < 3:
                retry_after = resp.headers.get("Retry-After")
                self._sleep(float(retry_after) if retry_after else backoff)
                backoff = min(backoff * 2, 30)
                continue
            if resp.status_code >= 400:
                raise OAuthError(
                    f"POST /oauth/token -> {resp.status_code}: {resp.text[:300]}"
                )
            return resp.json()
        raise OAuthError("POST /oauth/token failed after retries")

    # -- step 1: one-time authorization ----------------------------------
    def exchange_code(self, code: str) -> dict:
        """Trade the install ``code`` for the first Company token pair."""
        self.settings.validate()
        data = self._post_token(
            {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.redirect_uri,
                "user_type": "Company",
            }
        )
        self._persist_company_token(data)
        return data

    # -- step 4: keep the Company token alive forever ---------------------
    def _refresh_company_token(self, row) -> dict:
        refresh_token = crypto.decrypt(row["refresh_token_enc"])
        data = self._post_token(
            {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "user_type": "Company",
            }
        )
        self._persist_company_token(data, fallback_company_id=row["company_id"])
        return data

    def _persist_company_token(self, data: dict, fallback_company_id: str | None = None) -> None:
        company_id = data.get("companyId") or fallback_company_id
        if not company_id:
            raise OAuthError(f"token response missing companyId: {data!r}")
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        if not access_token or not refresh_token:
            raise OAuthError(f"token response missing access/refresh token: {data!r}")
        self.store.save_company_token(
            company_id=company_id,
            access_token_enc=crypto.encrypt(access_token),
            refresh_token_enc=crypto.encrypt(refresh_token),
            expires_at=_expires_at(data.get("expires_in", 86399)),
            scope=data.get("scope"),
        )
        log.info("agency Company token stored/rotated for company_id=%s", company_id)

    def get_company_access_token(self, company_id: str | None = None) -> str:
        """Return a valid Company access token, refreshing (and rotating
        the refresh token) when it's at or near expiry."""
        row = self.store.get_company_token(company_id)
        if row is None:
            raise OAuthError(
                "no agency token on file — run the one-time OAuth install "
                "(see `python -m app.cli oauth-authorize-url`) first"
            )
        if _is_expired(row["expires_at"]):
            self._refresh_company_token(row)
            row = self.store.get_company_token(row["company_id"])
        return crypto.decrypt(row["access_token_enc"])

    # -- step 3: per-location tokens, minted on demand ---------------------
    def mint_location_token(self, location_id: str, company_id: str) -> dict:
        company_token = self.get_company_access_token(company_id)
        url = f"{self.settings.api_base}/oauth/locationToken"
        resp = self.session.post(
            url,
            headers={
                "Authorization": f"Bearer {company_token}",
                "Version": "2021-07-28",
                "Accept": "application/json",
            },
            data={"companyId": company_id, "locationId": location_id},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OAuthError(
                f"POST /oauth/locationToken -> {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            raise OAuthError(f"locationToken response missing access_token: {data!r}")
        self.store.save_location_token(
            location_id=location_id,
            company_id=company_id,
            access_token_enc=crypto.encrypt(access_token),
            expires_at=_expires_at(data.get("expires_in", 86399)),
        )
        log.info("minted location token for location_id=%s", location_id)
        return data

    def get_location_token(self, location_id: str, company_id: str) -> str:
        """Cache-or-mint: the workhorse the multi-tenant poll loop calls."""
        row = self.store.get_location_token(location_id)
        if row is not None and not _is_expired(row["expires_at"]):
            return crypto.decrypt(row["access_token_enc"])
        self.mint_location_token(location_id, company_id)
        row = self.store.get_location_token(location_id)
        return crypto.decrypt(row["access_token_enc"])

    def token_provider(self, location_id: str, company_id: str):
        """A zero-arg callable suitable for ``GHLClient(token_provider=...)``."""
        return lambda: self.get_location_token(location_id, company_id)


# GHL's own published webhook-signing public keys (Webhook Integration
# Guide, "Security: Verifying Webhook Authenticity"). These are PUBLIC keys
# -- identical for every Marketplace app, not a per-app secret -- so they
# are hardcoded here rather than pulled from an env var. The legacy RSA
# scheme (X-WH-Signature) is deprecated by GHL on 2026-07-01; after that
# date only the Ed25519 scheme (X-GHL-Signature) is signed, so we verify
# Ed25519 first and fall back to RSA only while both are live.
_GHL_ED25519_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=\n"
    "-----END PUBLIC KEY-----\n"
)

_GHL_LEGACY_RSA_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAokvo/r9tVgcfZ5DysOSC\n"
    "Frm602qYV0MaAiNnX9O8KxMbiyRKWeL9JpCpVpt4XHIcBOK4u3cLSqJGOLaPuXw6\n"
    "dO0t6Q/ZVdAV5Phz+ZtzPL16iCGeK9po6D6JHBpbi989mmzMryUnQJezlYJ3DVfB\n"
    "csedpinheNnyYeFXolrJvcsjDtfAeRx5ByHQmTnSdFUzuAnC9/GepgLT9SM4nCpv\n"
    "uxmZMxrJt5Rw+VUaQ9B8JSvbMPpez4peKaJPZHBbU3OdeCVx5klVXXZQGNHOs8gF\n"
    "3kvoV5rTnXV0IknLBXlcKKAQLZcY/Q9rG6Ifi9c+5vqlvHPCUJFT5XUGG5RKgOKU\n"
    "J062fRtN+rLYZUV+BjafxQauvC8wSWeYja63VSUruvmNj8xkx2zE/Juc+yjLjTXp\n"
    "IocmaiFeAO6fUtNjDeFVkhf5LNb59vECyrHD2SQIrhgXpO4Q3dVNA5rw576PwTzN\n"
    "h/AMfHKIjE4xQA1SZuYJmNnmVZLIZBlQAF9Ntd03rfadZ+yDiOXCCs9FkHibELhC\n"
    "HULgCsnuDJHcrGNd5/Ddm5hxGQ0ASitgHeMZ0kcIOwKDOzOU53lDza6/Y09T7sYJ\n"
    "PQe7z0cvj7aE4B+Ax1ZoZGPzpJlZtGXCsu9aTEGEnKzmsFqwcSsnw3JB31IGKAyk\n"
    "T1hhTiaCeIY/OwwwNUY2yvcCAwEAAQ==\n"
    "-----END PUBLIC KEY-----\n"
)


def _verify_ed25519(raw_body: bytes, signature_b64: str, public_key_pem: str) -> bool:
    import base64

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        public_key = load_pem_public_key(public_key_pem.encode())
        signature = base64.b64decode(signature_b64)
        public_key.verify(signature, raw_body)
        return True
    except (InvalidSignature, ValueError, TypeError) as exc:
        log.warning("webhook Ed25519 (X-GHL-Signature) verification failed: %s", exc)
        return False


def verify_webhook_signature(raw_body: bytes, signature_b64: str, public_key_pem: str) -> bool:
    """Verify a marketplace INSTALL/UNINSTALL webhook signed RSA-SHA256.

    This is GHL's legacy scheme (X-WH-Signature), deprecated 2026-07-01.
    Kept for backward compatibility during the transition window; prefer
    ``verify_marketplace_webhook`` for new code.
    """
    import base64

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        public_key = load_pem_public_key(public_key_pem.encode())
        signature = base64.b64decode(signature_b64)
        public_key.verify(signature, raw_body, padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError) as exc:
        log.warning("webhook signature verification failed: %s", exc)
        return False


def verify_marketplace_webhook(raw_body: bytes, headers) -> bool:
    """Verify a marketplace INSTALL/UNINSTALL webhook using whichever of
    GHL's two signing schemes is present, preferring the current one.

    ``headers`` only needs to support ``.get(name, default)`` (e.g. a
    starlette/fastapi ``Headers`` object or a plain dict). Returns
    ``False`` if neither signature header is present -- callers that want
    to allow unsigned requests (e.g. local dev) should check that case
    explicitly before calling this.
    """
    ghl_sig = headers.get("x-ghl-signature", "")
    if ghl_sig:
        return _verify_ed25519(raw_body, ghl_sig, _GHL_ED25519_PUBLIC_KEY_PEM)

    legacy_sig = headers.get("x-wh-signature", "")
    if legacy_sig:
        return verify_webhook_signature(raw_body, legacy_sig, _GHL_LEGACY_RSA_PUBLIC_KEY_PEM)

    return False
