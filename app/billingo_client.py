"""Billingo API v3 client.

Auth: ``X-API-KEY`` header (no OAuth). New invoices are discovered by polling
``GET /documents`` — Billingo has no webhooks. Polling does not consume credit,
but it is rate-limited (HTTP 429), so we back off and retry.

The client is deliberately thin and injectable: pass a custom ``session`` (e.g.
``responses``-mocked or a fixture transport) in tests so none of this needs a
live account.
"""
from __future__ import annotations

import time
from typing import Iterator

import requests

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 5


class BillingoError(RuntimeError):
    pass


class BillingoClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.billingo.hu/v3",
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "X-API-KEY": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # -- low level -----------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # network hiccup
                last_exc = exc
                self._sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            if resp.status_code == 429:
                # Respect Retry-After when present, else exponential backoff.
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                self._sleep(wait)
                backoff = min(backoff * 2, 30)
                continue

            if resp.status_code >= 500:
                self._sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            if resp.status_code >= 400:
                raise BillingoError(
                    f"GET {path} -> {resp.status_code}: {resp.text[:300]}"
                )
            return resp.json()

        raise BillingoError(
            f"GET {path} failed after {self.max_retries} retries"
            + (f" (last error: {last_exc})" if last_exc else " (rate limited)")
        )

    # -- documents -----------------------------------------------------
    def list_documents(
        self,
        page: int = 1,
        per_page: int = 25,
        extra_params: dict | None = None,
    ) -> dict:
        """One page of documents. Returns the raw Billingo envelope:
        ``{"data": [...], "total": int, "per_page": int, "current_page": int, ...}``.
        """
        params = {"page": page, "per_page": per_page}
        if extra_params:
            params.update(extra_params)
        return self._get("/documents", params=params)

    def iter_documents(
        self,
        per_page: int = 25,
        max_pages: int = 100,
        extra_params: dict | None = None,
    ) -> Iterator[dict]:
        """Yield documents newest-first across pages.

        Billingo returns documents in reverse-chronological order by default,
        so the poller can stop early once it sees an already-known id.
        """
        page = 1
        while page <= max_pages:
            envelope = self.list_documents(
                page=page, per_page=per_page, extra_params=extra_params
            )
            data = envelope.get("data") or []
            if not data:
                return
            for doc in data:
                yield doc
            # Stop when we've consumed the last page.
            total = envelope.get("total")
            if total is not None and page * per_page >= total:
                return
            if len(data) < per_page:
                return
            page += 1
