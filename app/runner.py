"""Railway runtime entrypoint.

A tiny FastAPI app (so Railway sees an open port + /health) that ALSO runs the
connector on a schedule via APScheduler:

  * POLL job      — every ``poll_interval_min`` minutes: detect new Billingo
    invoices and queue review records, for every active tenant.
  * SCHEDULER job — once daily (09:00 in the configured tz): apply the review
    tag to every due record via GHL, for every active tenant.

Two tenant sources, both supported at once (Session 3 adds the second):

  1. Legacy single-tenant PIT — ``GHL_PIT_TOKEN`` + ``GHL_LOCATION_ID`` in env.
     Kept working un-touched; this is how Voxflow/Mate's subaccount runs today.
  2. OAuth multi-tenant — the ``tenants`` table, populated by the INSTALL /
     UNINSTALL marketplace webhook (``POST /webhooks/ghl``). Each active row
     gets a short-lived Location token minted on demand from the agency's
     Company token (see ``app.ghl_oauth.TokenManager``). Zero human touch
     after the one-time agency authorization (``GET /oauth/callback``).

A location appearing in BOTH sources (shouldn't normally happen) is only
processed once, via the OAuth path, since that's where the registry lives.

A single tenant's failure (bad Billingo key, GHL hiccup, OAuth not yet
configured) never blocks the others — failures are caught and logged per
tenant.

Local:    uvicorn app.runner:app --port 8000
Railway:  web: uvicorn app.runner:app --host 0.0.0.0 --port $PORT
NOTE: run with a SINGLE worker so only one scheduler instance exists.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import load_tenant_config
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("connector.runner")

_scheduler: BackgroundScheduler | None = None
_token_manager = None  # set in lifespan; app.ghl_oauth.TokenManager | None


def _db_path() -> str:
    return os.getenv("DB_PATH", "data/connector.db")


def _get_token_manager():
    """Build (once) the OAuth TokenManager if OAuth env vars are present.

    Returns None when OAuth isn't configured yet — the legacy PIT tenant
    still works in that case, OAuth tenants just can't be processed.
    """
    global _token_manager
    if _token_manager is not None:
        return _token_manager
    from .ghl_oauth import OAuthError, load_oauth_settings, TokenManager

    try:
        settings = load_oauth_settings()
        settings.validate()
    except OAuthError as exc:
        log.info("OAuth not configured (%s) — only the legacy PIT tenant runs", exc)
        return None
    store = Store(_db_path())
    _token_manager = TokenManager(settings, store)
    return _token_manager


def _apply_overrides(cfg, ghl):
    """Overlay a tenant's GHL Custom Values onto cfg, env defaults on failure."""
    from .ghl_config import apply_ghl_overrides

    try:
        return apply_ghl_overrides(cfg, ghl.get_location_custom_values())
    except Exception as exc:  # pragma: no cover - network path
        log.warning(
            "location %s: custom values unreadable (%s); using env defaults",
            cfg.ghl_location_id, exc,
        )
        return cfg


def _iter_tenants(store: Store):
    """Yield (cfg, ghl_client) for every tenant that should be polled/scheduled.

    OAuth tenants (the ``tenants`` table) take priority; the legacy env PIT
    tenant is included too, unless its location_id is already covered by the
    OAuth registry.
    """
    from .ghl_client import GHLClient

    base = load_tenant_config()
    seen_locations: set[str] = set()

    tm = _get_token_manager()
    if tm is not None:
        for row in store.list_active_tenants():
            location_id = row["location_id"]
            company_id = row["company_id"]
            seen_locations.add(location_id)
            try:
                ghl = GHLClient(
                    None,
                    location_id,
                    base_url=base.ghl_base_url,
                    api_version=base.ghl_api_version,
                    token_provider=tm.token_provider(location_id, company_id),
                )
                cfg = replace(base, ghl_location_id=location_id, ghl_pit_token="")
                cfg = _apply_overrides(cfg, ghl)
                yield cfg, ghl
            except Exception as exc:  # pragma: no cover - defensive, per-tenant
                log.error("location %s: could not prepare OAuth client (%s)", location_id, exc)

    if base.ghl_pit_token and base.ghl_location_id and base.ghl_location_id not in seen_locations:
        try:
            ghl = GHLClient(
                base.ghl_pit_token,
                base.ghl_location_id,
                base_url=base.ghl_base_url,
                api_version=base.ghl_api_version,
            )
            cfg = _apply_overrides(base, ghl)
            yield cfg, ghl
        except Exception as exc:  # pragma: no cover - defensive
            log.error("legacy PIT tenant %s: could not prepare client (%s)", base.ghl_location_id, exc)


def _run_poll() -> None:
    from .billingo_client import BillingoClient
    from .poller import poll_once

    store = Store(_db_path())
    ran = 0
    for cfg, _ghl in _iter_tenants(store):
        ran += 1
        if not cfg.billingo_api_key:
            log.info("poll: location %s has no Billingo key -> inactive, skipping", cfg.ghl_location_id)
            continue
        try:
            client = BillingoClient(cfg.billingo_api_key, cfg.billingo_base_url)
            log.info("poll[%s]: %s", cfg.ghl_location_id, poll_once(cfg, client, store))
        except Exception as exc:
            log.error("poll[%s]: failed (%s)", cfg.ghl_location_id, exc)
    if ran == 0:
        log.info("poll: no tenants configured (no legacy PIT env, no active OAuth tenants)")


def _run_scheduler() -> None:
    from .scheduler import run_due_reviews

    store = Store(_db_path())
    ran = 0
    for cfg, ghl in _iter_tenants(store):
        ran += 1
        try:
            log.info("scheduler[%s]: %s", cfg.ghl_location_id, run_due_reviews(cfg, store, ghl, dry_run=False))
        except Exception as exc:
            log.error("scheduler[%s]: failed (%s)", cfg.ghl_location_id, exc)
    if ran == 0:
        log.info("scheduler: no tenants configured")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _scheduler
    cfg = load_tenant_config()
    sched = BackgroundScheduler(timezone=cfg.timezone)
    sched.add_job(
        _run_poll, "interval", minutes=cfg.poll_interval_min,
        id="poll", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _run_scheduler, CronTrigger(hour=9, minute=0),
        id="daily-scheduler", max_instances=1, coalesce=True,
    )
    sched.start()
    _scheduler = sched
    log.info(
        "scheduler started: poll every %d min, daily tag pass 09:00 %s",
        cfg.poll_interval_min, cfg.timezone,
    )
    try:
        yield
    finally:
        sched.shutdown(wait=False)


app = FastAPI(title="Billingo->GHL Review Connector", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    running = _scheduler is not None and _scheduler.running
    return {"status": "ok", "scheduler_running": running}


# -- OAuth: one-time agency authorization --------------------------------
@app.get("/oauth/callback")
def oauth_callback(code: str | None = None, error: str | None = None):
    """GHL redirects here after the agency admin approves the app install.

    This is the ONLY manual step in the whole zero-touch flow, and it
    happens once per agency, ever. Exchanges ``code`` for the Company-level
    access+refresh token pair and persists it (encrypted) for every future
    sub-account to borrow from via /oauth/locationToken.
    """
    if error:
        return HTMLResponse(f"<h1>OAuth error</h1><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Missing code</h1>", status_code=400)

    from .ghl_oauth import OAuthError, load_oauth_settings, TokenManager

    try:
        settings = load_oauth_settings()
        store = Store(_db_path())
        tm = TokenManager(settings, store)
        data = tm.exchange_code(code)
    except OAuthError as exc:
        log.error("oauth callback: exchange failed (%s)", exc)
        return HTMLResponse(f"<h1>OAuth exchange failed</h1><p>{exc}</p>", status_code=502)

    company_id = data.get("companyId", "unknown")
    log.info("agency OAuth install complete for company_id=%s", company_id)
    return HTMLResponse(
        "<h1>Connected</h1>"
        f"<p>Agency token stored for company {company_id}. "
        "New sub-accounts will activate automatically as the app is "
        "installed on them — no further action needed.</p>"
    )


# -- OAuth: per-sub-account install / uninstall webhook -------------------
@app.post("/webhooks/ghl")
async def ghl_webhook(request: Request):
    """Marketplace INSTALL / UNINSTALL events — this is what makes a new
    sub-account "show up" with zero human action beyond the one-time agency
    authorization above.

    Expected payload (per GHL docs): {type, appId, locationId, companyId,
    installType, timestamp, webhookId}. If ``GHL_WEBHOOK_PUBLIC_KEY`` is set
    we verify the ``x-wh-signature`` header before trusting the body.
    """
    raw_body = await request.body()
    public_key_pem = os.getenv("GHL_WEBHOOK_PUBLIC_KEY", "")
    if public_key_pem:
        from .ghl_oauth import verify_webhook_signature

        signature = request.headers.get("x-wh-signature", "")
        if not verify_webhook_signature(raw_body, signature, public_key_pem):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
    else:
        log.warning("GHL_WEBHOOK_PUBLIC_KEY not set — accepting webhook UNVERIFIED")

    payload = await request.json()
    event_type = (payload.get("type") or "").upper()
    location_id = payload.get("locationId")
    company_id = payload.get("companyId")
    install_type = payload.get("installType")

    if not location_id or not company_id:
        return JSONResponse({"error": "missing locationId/companyId"}, status_code=400)

    store = Store(_db_path())
    if event_type == "INSTALL":
        store.upsert_tenant(location_id, company_id, install_type=install_type, active=True)
        log.info("tenant activated via INSTALL webhook: location=%s company=%s", location_id, company_id)
    elif event_type == "UNINSTALL":
        store.deactivate_tenant(location_id)
        log.info("tenant deactivated via UNINSTALL webhook: location=%s", location_id)
    else:
        log.info("ignoring webhook event type=%s", event_type)
        return JSONResponse({"status": "ignored"})

    return JSONResponse({"status": "ok"})
