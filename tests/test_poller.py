import glob
import json
import os
from datetime import date

from app.config import TenantConfig
from app.poller import poll_once
from app.scheduler import run_due_reviews
from app.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "documents_*.json")


class FakeClient:
    def __init__(self, fixtures_glob):
        docs = []
        for path in sorted(glob.glob(fixtures_glob)):
            with open(path, encoding="utf-8") as fh:
                docs.extend(json.load(fh).get("data", []))
        docs.sort(key=lambda d: int(d["id"]), reverse=True)
        self._docs = docs

    def iter_documents(self, per_page=25, max_pages=100, extra_params=None):
        yield from self._docs


def make_cfg(**over):
    base = dict(
        ghl_base_url="https://services.leadconnectorhq.com",
        ghl_api_version="2021-07-28",
        ghl_pit_token="pit-test",
        ghl_location_id="LOC1",
        billingo_base_url="https://api.billingo.hu/v3",
        billingo_api_key="k",
        anchor_date="fulfillment_date",
        delay_days=1,
        poll_interval_min=30,
        review_entry_tag="customer",
        retag_if_present=True,
        timezone="Europe/Budapest",
    )
    base.update(over)
    return TenantConfig(**base)


def test_poll_queues_eligible_only():
    cfg = make_cfg()
    store = Store(":memory:")
    res = poll_once(cfg, FakeClient(FIXTURES), store)
    # 1005 invoice, 1002 invoice (date fallback), 1001 receipt, 1000 invoice = 4
    assert res.queued == 4
    # 1004 proforma + 1003 cancelled = 2 ineligible
    assert res.skipped_ineligible == 2
    assert res.new_cursor == 1005


def test_poll_is_idempotent():
    cfg = make_cfg()
    store = Store(":memory:")
    poll_once(cfg, FakeClient(FIXTURES), store)
    # second run: cursor at 1005, nothing newer -> no work
    res2 = poll_once(cfg, FakeClient(FIXTURES), store)
    assert res2.queued == 0
    assert res2.seen == 0


def test_missing_fulfillment_uses_invoice_date():
    cfg = make_cfg()
    store = Store(":memory:")
    poll_once(cfg, FakeClient(FIXTURES), store)
    rows = store.due_reviews(cfg.ghl_location_id, date(2026, 12, 31))
    by_invoice = {r["invoice_id"]: r for r in rows}
    # invoice 1002 had no fulfillment_date; invoice_date 2026-06-12 + 1 day
    assert by_invoice[1002]["due_date"] == "2026-06-13"


def test_dry_run_scheduler_does_not_consume_queue():
    cfg = make_cfg()
    store = Store(":memory:")
    poll_once(cfg, FakeClient(FIXTURES), store)
    before = store.pending_count(cfg.ghl_location_id)
    res = run_due_reviews(cfg, store, ghl=None, dry_run=True)
    assert res.dry_run_preview == before
    # dry-run leaves everything pending
    assert store.pending_count(cfg.ghl_location_id) == before
