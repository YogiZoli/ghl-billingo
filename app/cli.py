"""Command-line entrypoints.

    python -m app.cli gen-key          # print a fresh Fernet key for .env
    python -m app.cli poll             # one live Billingo poll -> queue
    python -m app.cli run-scheduler    # apply due review tags via GHL
    python -m app.cli dry-run          # full pipeline on local fixtures, no network

``dry-run`` needs no Billingo or GHL credentials: it feeds the bundled fixture
invoices through the real poller and scheduler so you can watch detection ->
queue -> (would-)tag end to end before any account exists.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from dataclasses import replace

from .config import load_tenant_config
from .poller import poll_once
from .scheduler import run_due_reviews
from .store import Store


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


class FixtureBillingoClient:
    """Stand-in for BillingoClient that yields documents from fixture files.

    Matches the ``iter_documents`` contract the poller depends on.
    """

    def __init__(self, fixtures_glob: str) -> None:
        self._docs: list[dict] = []
        for path in sorted(glob.glob(fixtures_glob)):
            with open(path, "r", encoding="utf-8") as fh:
                envelope = json.load(fh)
            self._docs.extend(envelope.get("data", []))
        # newest first, mirroring Billingo's default ordering
        self._docs.sort(key=lambda d: int(d.get("id") or 0), reverse=True)

    def iter_documents(self, per_page: int = 25, max_pages: int = 100, extra_params=None):
        yield from self._docs


def cmd_gen_key(_: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
    return 0


def _build_ghl(cfg):
    from .ghl_client import GHLClient

    return GHLClient(
        cfg.ghl_pit_token,
        cfg.ghl_location_id,
        base_url=cfg.ghl_base_url,
        api_version=cfg.ghl_api_version,
    )


def _with_ghl_overrides(cfg, ghl):
    """Overlay the subaccount's GHL Custom Values onto cfg when reachable.

    Falls back to the env config if the location has no token or the read
    fails, so a misconfigured GHL never blocks a run silently-wrong.
    """
    from .ghl_config import apply_ghl_overrides

    if not (cfg.ghl_pit_token and cfg.ghl_location_id):
        return cfg
    try:
        values = ghl.get_location_custom_values()
    except Exception as exc:  # pragma: no cover - network path
        logging.getLogger("connector.cli").warning(
            "could not read GHL custom values (%s); using env defaults", exc
        )
        return cfg
    return apply_ghl_overrides(cfg, values)


def cmd_poll(args: argparse.Namespace) -> int:
    from .billingo_client import BillingoClient

    cfg = load_tenant_config()
    # The Billingo key + timing live as GHL Custom Values (LOCKED model).
    cfg = _with_ghl_overrides(cfg, _build_ghl(cfg))
    if not cfg.billingo_api_key:
        print(
            "No Billingo key — set billingo_api_key as a GHL Custom Value "
            "(or BILLINGO_API_KEY in .env). Subaccount treated as inactive.",
            file=sys.stderr,
        )
        return 2
    store = Store(os.getenv("DB_PATH", "data/connector.db"))
    client = BillingoClient(cfg.billingo_api_key, cfg.billingo_base_url)
    res = poll_once(cfg, client, store)
    print(res)
    return 0


def cmd_run_scheduler(args: argparse.Namespace) -> int:
    cfg = load_tenant_config()
    ghl = _build_ghl(cfg)
    cfg = _with_ghl_overrides(cfg, ghl)
    store = Store(os.getenv("DB_PATH", "data/connector.db"))
    res = run_due_reviews(cfg, store, ghl, dry_run=False)
    print(res)
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Run the whole pipeline against fixtures with no network calls."""
    # Build a config that doesn't need real secrets.
    base = load_tenant_config()
    cfg = replace(
        base,
        ghl_location_id=base.ghl_location_id or "DRYRUN_LOCATION",
        billingo_api_key="dry-run",
    )
    store = Store(":memory:")
    client = FixtureBillingoClient(args.fixtures)

    print("=== POLL (fixtures) ===")
    poll_res = poll_once(cfg, client, store)
    print(poll_res)
    print(f"pending in queue: {store.pending_count(cfg.ghl_location_id)}")

    print("\n=== SCHEDULER (dry-run, no GHL calls) ===")
    sched_res = run_due_reviews(cfg, store, ghl=None, dry_run=True)
    print(sched_res)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="connector")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("gen-key", help="print a fresh Fernet key").set_defaults(func=cmd_gen_key)
    sub.add_parser("poll", help="one live Billingo poll").set_defaults(func=cmd_poll)
    sub.add_parser("run-scheduler", help="apply due review tags").set_defaults(
        func=cmd_run_scheduler
    )
    dr = sub.add_parser("dry-run", help="full pipeline on fixtures, no network")
    dr.add_argument(
        "--fixtures",
        default=os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "documents_*.json"),
        help="glob of Billingo document fixture files",
    )
    dr.set_defaults(func=cmd_dry_run)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
