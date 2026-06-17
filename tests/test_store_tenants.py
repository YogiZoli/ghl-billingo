"""Persistence for Session 3: agency Company token, location token cache,
and the tenant registry driven by the INSTALL/UNINSTALL webhook."""
from app.store import Store


def make_store():
    return Store(":memory:")


def test_tenant_lifecycle_install_then_uninstall():
    store = make_store()
    assert store.list_active_tenants() == []

    store.upsert_tenant("LOC1", "COMP1", install_type="Location")
    active = store.list_active_tenants()
    assert len(active) == 1
    assert active[0]["location_id"] == "LOC1"
    assert active[0]["company_id"] == "COMP1"
    assert active[0]["active"] == 1

    store.deactivate_tenant("LOC1")
    assert store.list_active_tenants() == []
    # row still exists, just inactive — re-install should reactivate it.
    row = store.get_tenant("LOC1")
    assert row["active"] == 0

    store.upsert_tenant("LOC1", "COMP1")
    assert len(store.list_active_tenants()) == 1


def test_multiple_tenants_are_independent():
    store = make_store()
    store.upsert_tenant("LOC1", "COMP1")
    store.upsert_tenant("LOC2", "COMP1")
    store.deactivate_tenant("LOC1")
    active_ids = {r["location_id"] for r in store.list_active_tenants()}
    assert active_ids == {"LOC2"}


def test_company_token_upsert_overwrites():
    store = make_store()
    store.save_company_token("COMP1", "enc-access-1", "enc-refresh-1", "2099-01-01T00:00:00+00:00")
    row = store.get_company_token("COMP1")
    assert row["access_token_enc"] == "enc-access-1"

    # Rotation: refresh_token must be replaced, not appended.
    store.save_company_token("COMP1", "enc-access-2", "enc-refresh-2", "2099-01-02T00:00:00+00:00")
    row = store.get_company_token("COMP1")
    assert row["access_token_enc"] == "enc-access-2"
    assert row["refresh_token_enc"] == "enc-refresh-2"


def test_get_company_token_without_id_returns_most_recent():
    store = make_store()
    store.save_company_token("COMP1", "a1", "r1", "2099-01-01T00:00:00+00:00")
    row = store.get_company_token()
    assert row["company_id"] == "COMP1"


def test_location_token_cache_roundtrip():
    store = make_store()
    assert store.get_location_token("LOC1") is None
    store.save_location_token("LOC1", "COMP1", "enc-loc-access", "2099-01-01T00:00:00+00:00")
    row = store.get_location_token("LOC1")
    assert row["access_token_enc"] == "enc-loc-access"
    assert row["company_id"] == "COMP1"
