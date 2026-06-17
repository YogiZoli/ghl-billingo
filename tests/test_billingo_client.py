import responses

from app.billingo_client import BillingoClient, BillingoError

BASE = "https://api.billingo.hu/v3"


@responses.activate
def test_list_documents_sends_api_key_header():
    responses.add(
        responses.GET,
        f"{BASE}/documents",
        json={"data": [{"id": 1}], "total": 1, "per_page": 25, "current_page": 1},
        status=200,
    )
    client = BillingoClient("secret-key", BASE)
    out = client.list_documents()
    assert out["data"][0]["id"] == 1
    assert responses.calls[0].request.headers["X-API-KEY"] == "secret-key"


@responses.activate
def test_429_then_success_retries():
    responses.add(responses.GET, f"{BASE}/documents", status=429, headers={"Retry-After": "0"})
    responses.add(
        responses.GET,
        f"{BASE}/documents",
        json={"data": [{"id": 7}], "total": 1, "per_page": 25, "current_page": 1},
        status=200,
    )
    calls = []
    client = BillingoClient("k", BASE, sleep=lambda s: calls.append(s))
    out = client.list_documents()
    assert out["data"][0]["id"] == 7
    assert len(responses.calls) == 2  # retried once
    assert calls  # slept at least once


@responses.activate
def test_4xx_raises():
    responses.add(responses.GET, f"{BASE}/documents", status=401, body="unauthorized")
    client = BillingoClient("bad", BASE)
    try:
        client.list_documents()
        assert False, "expected BillingoError"
    except BillingoError as exc:
        assert "401" in str(exc)


@responses.activate
def test_iter_documents_paginates_and_stops():
    responses.add(
        responses.GET,
        f"{BASE}/documents",
        json={"data": [{"id": 3}, {"id": 2}], "total": 3, "per_page": 2, "current_page": 1},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/documents",
        json={"data": [{"id": 1}], "total": 3, "per_page": 2, "current_page": 2},
        status=200,
    )
    client = BillingoClient("k", BASE)
    ids = [d["id"] for d in client.iter_documents(per_page=2)]
    assert ids == [3, 2, 1]
