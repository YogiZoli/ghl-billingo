from datetime import date

from app.scheduler import run_due_reviews
from app.store import Store
from tests.test_poller import make_cfg


class FakeGHL:
    """Records calls so the scheduler's branching can be asserted."""

    def __init__(self, contact=None):
        self._contact = contact
        self.created = []
        self.tagged = []

    def find_contact(self, email=None, phone=None):
        return self._contact

    def create_contact(self, email=None, phone=None, name=None):
        c = {"id": "created-1", "email": email, "phone": phone, "name": name, "tags": []}
        self.created.append(c)
        return c

    def apply_review_tag(self, contact, tag="customer", retag_if_present=True):
        existing = {t.lower() for t in (contact.get("tags") or [])}
        if tag.lower() in existing and not retag_if_present:
            outcome = "already-present"
        elif tag.lower() in existing:
            outcome = "retagged"
        else:
            outcome = "added"
        self.tagged.append((contact["id"], outcome))
        return outcome


def _seed(store, cfg, email="a@x.hu", phone=None, name=None):
    store.enqueue_review(cfg.ghl_location_id, 5001, date(2026, 6, 1), email, phone, name)


def test_existing_contact_gets_tagged():
    cfg = make_cfg()
    store = Store(":memory:")
    _seed(store, cfg)
    ghl = FakeGHL(contact={"id": "c1", "tags": ["lead"]})
    res = run_due_reviews(cfg, store, ghl, dry_run=False)
    assert res.applied == 1 and res.created == 0
    assert ghl.tagged == [("c1", "added")]
    assert store.pending_count(cfg.ghl_location_id) == 0


def test_missing_contact_is_created_then_tagged():
    cfg = make_cfg(create_contact_if_missing=True)
    store = Store(":memory:")
    _seed(store, cfg, email="new@x.hu", name="New Customer")
    ghl = FakeGHL(contact=None)
    res = run_due_reviews(cfg, store, ghl, dry_run=False)
    assert res.created == 1 and res.applied == 1
    assert ghl.created[0]["email"] == "new@x.hu"
    assert ghl.tagged == [("created-1", "added")]


def test_missing_contact_skipped_when_create_disabled():
    cfg = make_cfg(create_contact_if_missing=False)
    store = Store(":memory:")
    _seed(store, cfg, email="ghost@x.hu")
    ghl = FakeGHL(contact=None)
    res = run_due_reviews(cfg, store, ghl, dry_run=False)
    assert res.not_found == 1 and res.applied == 0 and res.created == 0
    assert ghl.created == []


def test_repeat_customer_is_retagged():
    cfg = make_cfg(retag_if_present=True)
    store = Store(":memory:")
    _seed(store, cfg)
    ghl = FakeGHL(contact={"id": "c2", "tags": ["customer"]})
    res = run_due_reviews(cfg, store, ghl, dry_run=False)
    assert res.applied == 1
    assert ghl.tagged == [("c2", "retagged")]


def test_record_with_no_contactable_key_is_skipped():
    cfg = make_cfg(create_contact_if_missing=True)
    store = Store(":memory:")
    _seed(store, cfg, email=None, phone=None)
    ghl = FakeGHL(contact=None)
    res = run_due_reviews(cfg, store, ghl, dry_run=False)
    assert res.not_found == 1 and res.created == 0
