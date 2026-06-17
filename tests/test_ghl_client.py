import responses

from app.ghl_client import GHLClient

BASE = "https://services.leadconnectorhq.com"


def make_client():
    # sleep is a no-op so retry tests don't actually wait.
    return GHLClient("pit-test", "LOC1", base_url=BASE, sleep=lambda _s: None)


@responses.activate
def test_apply_tag_when_absent_adds_once():
    responses.add(responses.POST, f"{BASE}/contacts/abc/tags", json={"tags": ["customer"]}, status=200)
    client = make_client()
    outcome = client.apply_review_tag({"id": "abc", "tags": ["lead"]})
    assert outcome == "added"
    assert len(responses.calls) == 1


@responses.activate
def test_apply_tag_when_present_retags():
    responses.add(responses.DELETE, f"{BASE}/contacts/abc/tags", json={}, status=200)
    responses.add(responses.POST, f"{BASE}/contacts/abc/tags", json={}, status=200)
    client = make_client()
    outcome = client.apply_review_tag({"id": "abc", "tags": ["customer"]}, retag_if_present=True)
    assert outcome == "retagged"
    assert len(responses.calls) == 2  # remove then add


@responses.activate
def test_apply_tag_present_no_retag_is_noop():
    client = make_client()
    outcome = client.apply_review_tag({"id": "abc", "tags": ["customer"]}, retag_if_present=False)
    assert outcome == "already-present"
    assert len(responses.calls) == 0


@responses.activate
def test_find_contact_uses_search():
    responses.add(
        responses.POST,
        f"{BASE}/contacts/search",
        json={"contacts": [{"id": "c1", "email": "a@x.hu"}]},
        status=200,
    )
    client = make_client()
    contact = client.find_contact(email="a@x.hu")
    assert contact["id"] == "c1"


@responses.activate
def test_find_contact_falls_back_to_phone():
    # First search (by email) returns nothing; second (by phone) hits.
    responses.add(responses.POST, f"{BASE}/contacts/search", json={"contacts": []}, status=200)
    responses.add(
        responses.POST,
        f"{BASE}/contacts/search",
        json={"contacts": [{"id": "c9", "phone": "+36301234567"}]},
        status=200,
    )
    client = make_client()
    contact = client.find_contact(email="missing@x.hu", phone="+36301234567")
    assert contact["id"] == "c9"
    assert len(responses.calls) == 2  # email miss, then phone hit


@responses.activate
def test_create_contact_posts_and_unwraps():
    responses.add(
        responses.POST,
        f"{BASE}/contacts/",
        json={"contact": {"id": "new1", "email": "z@x.hu"}},
        status=201,
    )
    client = make_client()
    contact = client.create_contact(email="z@x.hu", phone="+3630", name="Zed")
    assert contact["id"] == "new1"
    body = responses.calls[0].request.body
    assert b"z@x.hu" in body and b"LOC1" in body


def test_create_contact_requires_a_key():
    import pytest

    from app.ghl_client import GHLError

    client = make_client()
    with pytest.raises(GHLError):
        client.create_contact()


@responses.activate
def test_find_or_create_creates_when_missing():
    responses.add(responses.POST, f"{BASE}/contacts/search", json={"contacts": []}, status=200)
    responses.add(
        responses.POST,
        f"{BASE}/contacts/",
        json={"contact": {"id": "made", "email": "q@x.hu"}},
        status=201,
    )
    client = make_client()
    contact = client.find_or_create_contact(email="q@x.hu", create=True)
    assert contact["id"] == "made"


@responses.activate
def test_find_or_create_returns_none_when_create_disabled():
    responses.add(responses.POST, f"{BASE}/contacts/search", json={"contacts": []}, status=200)
    client = make_client()
    assert client.find_or_create_contact(email="q@x.hu", create=False) is None


@responses.activate
def test_request_retries_on_429_then_succeeds():
    responses.add(responses.POST, f"{BASE}/contacts/abc/tags", json={}, status=429)
    responses.add(responses.POST, f"{BASE}/contacts/abc/tags", json={"ok": True}, status=200)
    client = make_client()
    outcome = client.apply_review_tag({"id": "abc", "tags": []})
    assert outcome == "added"
    assert len(responses.calls) == 2  # one 429, one success


@responses.activate
def test_get_location_custom_values_maps_name_to_value():
    responses.add(
        responses.GET,
        f"{BASE}/locations/LOC1/customValues",
        json={
            "customValues": [
                {"name": "Billingo_API_Key", "value": "abc123"},
                {"name": "billingo_delay_days", "value": "2"},
            ]
        },
        status=200,
    )
    client = make_client()
    values = client.get_location_custom_values()
    assert values["billingo_api_key"] == "abc123"
    assert values["billingo_delay_days"] == "2"
