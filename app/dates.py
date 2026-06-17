"""Date/anchor helpers and document-eligibility rules.

All date math runs in the configured timezone (Europe/Budapest by default).
Billingo returns dates as ``YYYY-MM-DD`` strings; we treat them as calendar
dates, not timestamps.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

# Billingo document types we will trigger a review for. Anything else
# (proforma, draft, waybill, cancellation, ...) is ignored.
TRIGGERING_TYPES = {"invoice", "advance", "receipt", "advance_receipt"}


def today_in(tz: str) -> date:
    """Current calendar date in the given timezone."""
    return datetime.now(ZoneInfo(tz)).date()


def parse_date(value) -> date | None:
    """Parse a Billingo date field into a date, tolerantly.

    Accepts 'YYYY-MM-DD', full ISO datetimes, or an existing date/datetime.
    Returns None when the value is missing or unparseable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    # Take just the date part if a time component is present.
    text = text.replace("Z", "").split("T")[0].split(" ")[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def anchor_value(document: dict, anchor: str) -> date | None:
    """Resolve the anchor date for a document, with fallback.

    Primary field is the configured anchor; if it's missing we fall back to
    invoice_date (per spec). Returns None if neither is present.
    """
    primary = parse_date(document.get(anchor))
    if primary is not None:
        return primary
    return parse_date(document.get("invoice_date"))


def due_date(document: dict, anchor: str, delay_days: int) -> date | None:
    """The date on which the review tag should be applied."""
    base = anchor_value(document, anchor)
    if base is None:
        return None
    from datetime import timedelta

    return base + timedelta(days=delay_days)


def is_triggering_document(document: dict) -> bool:
    """True if this document should ever produce a review request.

    Skips cancelled documents and storno/cancellation document types so a
    correction never fires a review.
    """
    if document.get("cancelled") is True:
        return False
    doc_type = (document.get("type") or "").strip().lower()
    # Empty type is treated as a normal invoice (some payloads omit it).
    if doc_type and doc_type not in TRIGGERING_TYPES:
        return False
    return True


def extract_email(document: dict) -> str | None:
    """Pull the partner email from a Billingo document, defensively.

    Real payloads have nested partner objects; field shape can be
    partner.emails[] (list) or partner.email (string). We also check
    document_partner as a fallback. LIVE-VERIFY this against a real key.
    """
    for key in ("partner", "document_partner"):
        node = document.get(key) or {}
        emails = node.get("emails")
        if isinstance(emails, list) and emails:
            return str(emails[0]).strip().lower()
        single = node.get("email")
        if single:
            return str(single).strip().lower()
    return None


def extract_phone(document: dict) -> str | None:
    """Pull the partner phone, defensively (fallback match key)."""
    for key in ("partner", "document_partner"):
        node = document.get(key) or {}
        phone = node.get("phone")
        if phone:
            return str(phone).strip()
    return None
