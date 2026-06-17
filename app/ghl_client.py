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

import requests

DEFAULT_TIMEOUT = 30


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.location_id = location_id
        self.timeout = timeout
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
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            raise GHLError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return {}
        return resp.json()

    # -- contacts ------------------------------------------------------
    def find_contact(self, email: str | None = None, phone: str | None = None) -> dict | None:
        """Find a single contact by email (preferred) or phone.

        Uses POST /contacts/search. Returns the first match or None.
        """
        if not email and not phone:
            return None
        query = email or phone
        body = {"locationId": self.location_id, "query": query, "pageLimit": 1}
        data = self._request("POST", "/contacts/search", json=body)
        contacts = data.get("contacts") or []
        return contacts[0] if contacts else None

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
