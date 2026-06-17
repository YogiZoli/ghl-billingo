"""Railway runtime entrypoint.

A tiny FastAPI app (so Railway sees an open port + /health) that ALSO runs the
connector on a schedule via APScheduler:

  * POLL job      — every ``poll_interval_min`` minutes: detect new Billingo
    invoices and queue review records.
  * SCHEDULER job — once daily (09:00 in the configured tz): apply the review
    tag to every due record via GHL.

Single-tenant for the MVP (config from env + GHL Custom Values). A blank
Billingo key simply means the subaccount is inactive and the poll no-ops.

Local:    uvicorn app.runner:app --port 8000
Railway:  web: uvicorn app.runner:app --host 0.0.0.0 --port $PORT
NOTE: run with a SINGLE worker so only one scheduler instance exists.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from .config import load_tenant_config
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("connector.runner")

_scheduler: BackgroundScheduler | None = None


def _db_path() -> str:
    return os.getenv("DB_PATH", "data/connector.db")


def _config_with_overrides():
    """Load env config and overlay GHL Custom Values when reachable."""
    from .ghl_client import GHLClient
    from .ghl_config import apply_ghl_overrides

    cfg = load_tenant_config()
    if not (cfg.ghl_pit_token and cfg.ghl_location_id):
        return cfg, None
    ghl = GHLClient(
        cfg.ghl_pit_token,
        cfg.ghl_location_id,
        base_url=cfg.ghl_base_url,
        api_version=cfg.ghl_api_version,
    )
    try:
        cfg = apply_ghl_overrides(cfg, ghl.get_location_custom_values())
    except Exception as exc:  # pragma: no cover - network path
        log.warning("GHL custom values unreadable (%s); using env defaults", exc)
    return cfg, ghl


def _run_poll() -> None:
    from .billingo_client import BillingoClient
    from .poller import poll_once

    cfg, _ = _config_with_overrides()
    if not cfg.billingo_api_key:
        log.info("poll: no Billingo key -> subaccount inactive, skipping")
        return
    store = Store(_db_path())
    client = BillingoClient(cfg.billingo_api_key, cfg.billingo_base_url)
    log.info("poll result: %s", poll_once(cfg, client, store))


def _run_scheduler() -> None:
    from .scheduler import run_due_reviews

    cfg, ghl = _config_with_overrides()
    if ghl is None:
        log.info("scheduler: no GHL token/location -> skipping")
        return
    store = Store(_db_path())
    log.info("scheduler result: %s", run_due_reviews(cfg, store, ghl, dry_run=False))


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
