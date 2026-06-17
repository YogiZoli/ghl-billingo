import responses

from app.ghl_client import GHLClient

BASE = "https://services.leadconnectorhq.com"


def make_client():
    return GHLClient("pit-test", "LOC1", base_url=BASE)


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
