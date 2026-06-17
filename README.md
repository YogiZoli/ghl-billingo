# Billingo → GHL Review Connector (MVP)

Private internal tool for **Voxflow** (Mindful Momentum Ltd). When a Hungarian
SMB issues an invoice in their own Billingo account, the matching GoHighLevel
contact is automatically dropped into the existing Google-review automation a
configurable number of days after the fulfillment date.

> Status: **Session 1 — engine scaffold.** Billingo client + poller + scheduler
> control-flow + full test suite + a network-free `dry-run`. Live GHL wiring
> hardening and the live Billingo end-to-end test land in Session 2/3.

## How it works

```
Billingo (client's account)
   │  GET /documents  (poll, X-API-KEY) — no webhooks, no credit cost
   ▼
[CONNECTOR]
   • POLL: detect new invoices, compute due = anchor_date + delay_days, queue them
   • SCHEDULER (daily): due ≤ today → add "customer" tag to the GHL contact
   ▼
GHL subaccount (Private Integration Token)
   • "customer" tag added → "02. Review Request" workflow fires → review goes out
```

The delay lives in the **connector**, not GHL, because the review workflow is
tag-triggered with no date wait. Repeat customers are handled by removing then
re-adding the tag so the workflow re-fires.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # fill in GHL_PIT_TOKEN etc. (Billingo key optional for now)

# See the whole pipeline run on bundled fixtures — NO account needed:
python -m app.cli dry-run

# Generate a Fernet key for at-rest API-key encryption:
python -m app.cli gen-key

# Once a Billingo key exists:
python -m app.cli poll            # detect + queue
python -m app.cli run-scheduler   # apply due review tags via GHL
```

## Tests

```bash
python -m pytest -q
```

The suite covers the Billingo client (auth header, 429 backoff/retry,
pagination), the date/eligibility rules (anchor fallback, storno/cancelled
skip, email extraction), the poller (queueing, idempotent cursor), and the GHL
tag logic (add / retag / no-op). No network or credentials required.

## Configuration

All behaviour is set via `.env` — see `.env.example`. Key knobs:
`ANCHOR_DATE`, `DELAY_DAYS`, `POLL_INTERVAL_MIN`, `REVIEW_ENTRY_TAG`,
`RETAG_IF_PRESENT`.

## Layout

```
app/
  config.py          tenant config (env now; per-subaccount rows in Phase 2)
  billingo_client.py X-API-KEY client, pagination, 429 backoff
  ghl_client.py      pit- token client, contact search, tag add/remove/retag
  dates.py           anchor/delay math, storno skip, email/phone extraction
  store.py           SQLite: poll cursor + review queue (idempotent)
  poller.py          detect new invoices → queue review records
  scheduler.py       daily: apply due review tags (dry-run aware)
  cli.py             gen-key | poll | run-scheduler | dry-run
tests/               pytest suite + Billingo document fixtures
```

## Known live-verify items (Session 3, when a Billingo key exists)

- Exact partner-email nesting in real `GET /documents` payloads
  (`partner.emails[]` vs `partner.email`) — extraction already handles both.
- Real Billingo document `type` values for storno/cancellation.
- Confirm GHL v2 tag endpoints (`POST`/`DELETE /contacts/{id}/tags`) and
  `POST /contacts/search` shapes against current docs.
