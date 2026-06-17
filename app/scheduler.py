"""Daily pass: apply the review tag to every due record.

For each pending review whose due date has arrived, find the matching GHL
contact (email first, phone fallback) and apply the review-entry tag. In
``dry_run`` mode no GHL calls are made — the intended action is logged and the
record is left pending — which lets the whole pipeline be demonstrated with no
live account.

NOTE: live GHL wiring + the full set of edge cases (contact-not-found create,
match ambiguity) get their dedicated hardening pass in Session 2. The control
flow lives here so that pass is fill-in-the-gaps, not new structure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import TenantConfig
from .dates import today_in
from .ghl_client import GHLClient, GHLError
from .store import Store

log = logging.getLogger("connector.scheduler")


@dataclass
class RunResult:
    due: int = 0
    applied: int = 0
    not_found: int = 0
    errors: int = 0
    dry_run_preview: int = 0


def run_due_reviews(
    cfg: TenantConfig,
    store: Store,
    ghl: GHLClient | None = None,
    dry_run: bool = False,
) -> RunResult:
    result = RunResult()
    today = today_in(cfg.timezone)
    rows = store.due_reviews(cfg.ghl_location_id, today)
    result.due = len(rows)

    for row in rows:
        email, phone, invoice_id = row["email"], row["phone"], row["invoice_id"]

        if dry_run:
            result.dry_run_preview += 1
            log.info(
                "[dry-run] would tag contact for invoice %s "
                "(email=%s phone=%s) with '%s'",
                invoice_id,
                email,
                phone,
                cfg.review_entry_tag,
            )
            continue

        if ghl is None:
            raise ValueError("ghl client required when dry_run is False")

        try:
            contact = ghl.find_contact(email=email, phone=phone)
            if contact is None:
                result.not_found += 1
                store.mark_review(row["id"], "skipped", "contact not found")
                log.warning("no GHL contact for invoice %s (%s)", invoice_id, email)
                continue
            outcome = ghl.apply_review_tag(
                contact,
                tag=cfg.review_entry_tag,
                retag_if_present=cfg.retag_if_present,
            )
            store.mark_review(row["id"], "applied", outcome)
            result.applied += 1
            log.info("invoice %s: tag %s (%s)", invoice_id, outcome, email)
        except GHLError as exc:
            result.errors += 1
            store.mark_review(row["id"], "error", str(exc))
            log.error("invoice %s: GHL error %s", invoice_id, exc)

    log.info(
        "scheduler done: due=%d applied=%d not_found=%d errors=%d preview=%d",
        result.due,
        result.applied,
        result.not_found,
        result.errors,
        result.dry_run_preview,
    )
    return result
