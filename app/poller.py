"""Poll Billingo for new invoices and queue review records.

Run on the configured cadence (default 30 min). For each document newer than
the stored cursor that is eligible (not cancelled / not a storno type), we
compute the due date (anchor + delay_days) and enqueue it. The cursor advances
to the highest invoice id seen, so every invoice is processed exactly once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .billingo_client import BillingoClient
from .config import TenantConfig
from .dates import (
    due_date,
    extract_email,
    extract_name,
    extract_phone,
    is_triggering_document,
)
from .store import Store

log = logging.getLogger("connector.poller")


@dataclass
class PollResult:
    seen: int = 0
    queued: int = 0
    skipped_ineligible: int = 0
    skipped_no_date: int = 0
    skipped_duplicate: int = 0
    new_cursor: int = 0


def poll_once(
    cfg: TenantConfig,
    client: BillingoClient,
    store: Store,
    per_page: int = 25,
) -> PollResult:
    result = PollResult()
    cursor = store.get_cursor(cfg.ghl_location_id)
    result.new_cursor = cursor
    highest = cursor

    for doc in client.iter_documents(per_page=per_page):
        doc_id = int(doc.get("id") or 0)
        # Documents come newest-first; stop once we reach known territory.
        if doc_id <= cursor:
            break
        result.seen += 1
        highest = max(highest, doc_id)

        if not is_triggering_document(doc):
            result.skipped_ineligible += 1
            continue

        due = due_date(doc, cfg.anchor_date, cfg.delay_days)
        if due is None:
            result.skipped_no_date += 1
            log.warning("invoice %s has no usable anchor date; skipping", doc_id)
            continue

        email = extract_email(doc)
        phone = extract_phone(doc)
        name = extract_name(doc)
        queued = store.enqueue_review(
            cfg.ghl_location_id, doc_id, due, email, phone, name
        )
        if queued:
            result.queued += 1
        else:
            result.skipped_duplicate += 1

    if highest > cursor:
        store.set_cursor(cfg.ghl_location_id, highest)
        result.new_cursor = highest

    log.info(
        "poll done: seen=%d queued=%d ineligible=%d no_date=%d dup=%d cursor=%d",
        result.seen,
        result.queued,
        result.skipped_ineligible,
        result.skipped_no_date,
        result.skipped_duplicate,
        result.new_cursor,
    )
    return result
