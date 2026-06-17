from datetime import date

from app import dates


def test_parse_date_variants():
    assert dates.parse_date("2026-06-10") == date(2026, 6, 10)
    assert dates.parse_date("2026-06-10T08:30:00Z") == date(2026, 6, 10)
    assert dates.parse_date("2026-06-10 08:30:00") == date(2026, 6, 10)
    assert dates.parse_date(None) is None
    assert dates.parse_date("") is None
    assert dates.parse_date("not-a-date") is None


def test_anchor_fallback_to_invoice_date():
    doc = {"fulfillment_date": None, "invoice_date": "2026-06-12"}
    assert dates.anchor_value(doc, "fulfillment_date") == date(2026, 6, 12)


def test_due_date_adds_delay():
    doc = {"fulfillment_date": "2026-06-10"}
    assert dates.due_date(doc, "fulfillment_date", 1) == date(2026, 6, 11)
    assert dates.due_date(doc, "fulfillment_date", 0) == date(2026, 6, 10)
    assert dates.due_date(doc, "fulfillment_date", 3) == date(2026, 6, 13)


def test_due_date_none_when_no_anchor():
    assert dates.due_date({}, "fulfillment_date", 1) is None


def test_cancelled_document_is_not_triggering():
    assert dates.is_triggering_document({"type": "invoice", "cancelled": True}) is False


def test_proforma_is_not_triggering():
    assert dates.is_triggering_document({"type": "proforma"}) is False


def test_invoice_and_receipt_are_triggering():
    assert dates.is_triggering_document({"type": "invoice"}) is True
    assert dates.is_triggering_document({"type": "receipt"}) is True


def test_missing_type_treated_as_invoice():
    assert dates.is_triggering_document({"invoice_number": "X"}) is True


def test_extract_email_from_list_and_string():
    assert dates.extract_email({"partner": {"emails": ["A@Example.hu"]}}) == "a@example.hu"
    assert dates.extract_email({"partner": {"email": "B@Example.hu"}}) == "b@example.hu"
    assert dates.extract_email({"document_partner": {"emails": ["c@x.hu"]}}) == "c@x.hu"
    assert dates.extract_email({"partner": {}}) is None
