"""GoHighLevel client (LeadConnector v2).

Auth is a Private Integration Token (``pit-...``) — no OAuth flow. The token
for the Voxflow Reputation subaccount is already live; the connector writes
with it directly.

The connector's only "lever" is the ``customer`` tag: adding it starts the
published "02. Review Request" workflow. For a repeat customer whose contact
already carries the tag, we remove then re-add it so the workflow fires again.

Endpoints are confirmed against the v2 docs at build time; if GHL changes
them, update the paths here only.
"""
from __future__ import annotations

import time

import requests

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 4


class GHLError(RuntimeError):
    pass


class GHLClient:
    def __init__(
        self,
        pit_token: str,
        location_id: str,
        base_url: str = "https://services.leadconnectorhq.com",
        api_version: str = "2021-07-28",
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.location_id = location_id
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {pit_token}",
                "Version": api_version,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:  # transient network error
                last_exc = exc
                if attempt == self.max_retries:
                    raise GHLError(f"{method} {path} failed: {exc}") from exc
                self._sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            # Rate limited or server hiccup -> back off and retry.
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise GHLError(
                        f"{method} {path} -> {resp.status_code} after "
                        f"{self.max_retries} retries: {resp.text[:200]}"
                    )
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                self._sleep(wait)
                backoff = min(backoff * 2, 30)
                continue

            if resp.status_code >= 400:
                raise GHLError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:300]}"
                )
            if not resp.content:
                return {}
            return resp.json()

        # Unreachable, but keeps type-checkers happy.
        raise GHLError(f"{method} {path} failed: {last_exc}")

    # -- contacts ------------------------------------------------------
    def _search(self, query: str) -> dict | None:
        body = {"locationId": self.location_id, "query": query, "pageLimit": 1}
        data = self._request("POST", "/contacts/search", json=body)
        contacts = data.get("contacts") or []
        return contacts[0] if contacts else None

    def find_contact(
        self, email: str | None = None, phone: str | None = None
    ) -> dict | None:
        """Find a single contact, email first then phone fallback.

        Each key is searched separately (POST /contacts/search) so a partner
        whose email isn't in GHL can still be matched on phone. Returns the
        first match or None.
        """
        for query in (email, phone):
            if not query:
                continue
            match = self._search(query)
            if match:
                return match
        return None

    def create_contact(
        self,
        email: str | None = None,
        phone: str | None = None,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Create a contact in this location. Returns the created contact.

        Used as the not-found fallback so an invoiced customer who isn't yet
        in GHL still receives the review request. We create WITHOUT the review
        tag and let the caller apply it, keeping the trigger deterministic.
        """
        if not email and not phone:
            raise GHLError("cannot create a contact without email or phone")
        body: dict = {"locationId": self.location_id}
        if email:
            body["email"] = email
        if phone:
            body["phone"] = phone
        if name:
            body["name"] = name
        if tags:
            body["tags"] = tags
        data = self._request("POST", "/contacts/", json=body)
        # GHL wraps the created record under "contact".
        return data.get("contact") or data

    def find_or_create_contact(
        self,
        email: str | None = None,
        phone: str | None = None,
        name: str | None = None,
        create: bool = True,
    ) -> dict | None:
        """Find by email/phone; create when missing if ``create`` is set."""
        contact = self.find_contact(email=email, phone=phone)
        if contact is not None:
            return contact
        if not create:
            return None
        return self.create_contact(email=email, phone=phone, name=name)

    def get_location_custom_values(self) -> dict[str, str]:
        """Return this location's custom values as a {name: value} dict.

        Used to read per-subaccount settings (billingo_api_key,
        billingo_delay_days, billingo_poll_min) that Mate maintains in the GHL
        UI. Names are lower-cased for stable lookup.
        """
        data = self._request(
            "GET", f"/locations/{self.location_id}/customValues"
        )
        out: dict[str, str] = {}
        for cv in data.get("customValues") or []:
            name = (cv.get("name") or "").strip().lower()
            if name:
                out[name] = cv.get("value")
        return out

    def add_tags(self, contact_id: str, tags: list[str]) -> dict:
        return self._request(
            "POST", f"/contacts/{contact_id}/tags", json={"tags": tags}
        )

    def remove_tags(self, contact_id: str, tags: list[str]) -> dict:
        # DELETE with a body is supported by the GHL tags endpoint.
        return self._request(
            "DELETE", f"/contacts/{contact_id}/tags", json={"tags": tags}
        )

    # -- the connector lever ------------------------------------------
    def apply_review_tag(
        self, contact: dict, tag: str = "customer", retag_if_present: bool = True
    ) -> str:
        """Apply the review-entry tag, handling the repeat-customer case.

        Returns one of: "added", "retagged", "already-present".
        """
        contact_id = contact.get("id")
        if not contact_id:
            raise GHLError("contact has no id")
        existing = {t.lower() for t in (contact.get("tags") or [])}
        if tag.lower() in existing:
            if not retag_if_present:
                return "already-present"
            self.remove_tags(contact_id, [tag])
            self.add_tags(contact_id, [tag])
            return "retagged"
        self.add_tags(contact_id, [tag])
        return "added"
