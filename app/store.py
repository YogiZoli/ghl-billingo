"""SQLite persistence: the poll cursor and scheduled review records.

Two responsibilities:
  * remember the highest Billingo document id we've already processed
    (the last-seen cursor) so each invoice is handled exactly once;
  * queue a review record (contact match key + due date) that the daily
    scheduler drains when the due date arrives.

SQLite is fine for the MVP; the schema maps cleanly to Postgres for Phase 2.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    location_id     TEXT PRIMARY KEY,
    last_invoice_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id   TEXT NOT NULL,
    invoice_id    INTEGER NOT NULL,
    email         TEXT,
    phone         TEXT,
    name          TEXT,
    due_date      TEXT NOT NULL,          -- ISO YYYY-MM-DD
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|skipped|error
    detail        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at    TEXT,
    UNIQUE(location_id, invoice_id)
);

-- OAuth / multi-tenant (Session 3: Agency OAuth app, zero-touch onboarding).

-- One row per agency Company-level token. We only ever install the app once
-- per agency, but keyed by company_id so a re-install doesn't collide.
CREATE TABLE IF NOT EXISTS oauth_company_token (
    company_id          TEXT PRIMARY KEY,
    access_token_enc    TEXT NOT NULL,
    refresh_token_enc   TEXT NOT NULL,
    expires_at          TEXT NOT NULL,   -- ISO 8601 UTC
    scope               TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Short-lived (~24h) location-scoped tokens, minted on demand from the
-- agency token via POST /oauth/locationToken. No refresh_token here — when
-- expired we just mint a new one.
CREATE TABLE IF NOT EXISTS oauth_location_tokens (
    location_id      TEXT PRIMARY KEY,
    company_id       TEXT NOT NULL,
    access_token_enc TEXT NOT NULL,
    expires_at       TEXT NOT NULL,      -- ISO 8601 UTC
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tenant registry, driven by the INSTALL / UNINSTALL marketplace webhook.
-- A location only gets polled/scheduled once it shows up here as active.
CREATE TABLE IF NOT EXISTS tenants (
    location_id   TEXT PRIMARY KEY,
    company_id    TEXT NOT NULL,
    install_type  TEXT,                  -- e.g. "Location" | "Company"
    active        INTEGER NOT NULL DEFAULT 1,
    installed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Store:
    def __init__(self, db_path: str = "data/connector.db") -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread False so the scheduler thread can reuse it.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Forward-compatible column adds for DBs created by older versions."""
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(review_queue)").fetchall()
        }
        if "name" not in cols:
            self._conn.execute("ALTER TABLE review_queue ADD COLUMN name TEXT")

    @contextmanager
    def _tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- cursor --------------------------------------------------------
    def get_cursor(self, location_id: str) -> int:
        row = self._conn.execute(
            "SELECT last_invoice_id FROM cursor WHERE location_id = ?",
            (location_id,),
        ).fetchone()
        return int(row["last_invoice_id"]) if row else 0

    def set_cursor(self, location_id: str, invoice_id: int) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO cursor (location_id, last_invoice_id)
                VALUES (?, ?)
                ON CONFLICT(location_id) DO UPDATE SET last_invoice_id = excluded.last_invoice_id
                WHERE excluded.last_invoice_id > cursor.last_invoice_id
                """,
                (location_id, invoice_id),
            )

    # -- review queue --------------------------------------------------
    def enqueue_review(
        self,
        location_id: str,
        invoice_id: int,
        due: date,
        email: str | None,
        phone: str | None,
        name: str | None = None,
    ) -> bool:
        """Queue a review record. Returns False if the invoice was already
        queued (idempotent duplicate guard)."""
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO review_queue
                    (location_id, invoice_id, email, phone, name, due_date)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (location_id, invoice_id, email, phone, name, due.isoformat()),
            )
            return cur.rowcount > 0

    def due_reviews(self, location_id: str, on_or_before: date) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM review_queue
            WHERE location_id = ? AND status = 'pending' AND due_date <= ?
            ORDER BY due_date, id
            """,
            (location_id, on_or_before.isoformat()),
        ).fetchall()

    def mark_review(self, review_id: int, status: str, detail: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE review_queue
                SET status = ?, detail = ?, applied_at = datetime('now')
                WHERE id = ?
                """,
                (status, detail, review_id),
            )

    def pending_count(self, location_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM review_queue WHERE location_id = ? AND status = 'pending'",
            (location_id,),
        ).fetchone()
        return int(row["n"])

    # -- OAuth: agency Company token ------------------------------------
    def save_company_token(
        self,
        company_id: str,
        access_token_enc: str,
        refresh_token_enc: str,
        expires_at: str,
        scope: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO oauth_company_token
                    (company_id, access_token_enc, refresh_token_enc, expires_at, scope, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(company_id) DO UPDATE SET
                    access_token_enc = excluded.access_token_enc,
                    refresh_token_enc = excluded.refresh_token_enc,
                    expires_at = excluded.expires_at,
                    scope = excluded.scope,
                    updated_at = datetime('now')
                """,
                (company_id, access_token_enc, refresh_token_enc, expires_at, scope),
            )

    def get_company_token(self, company_id: str | None = None) -> sqlite3.Row | None:
        """Return the stored agency token row.

        If ``company_id`` is omitted, return the single row we have (there is
        normally exactly one agency per install) — used by the scheduler
        loop, which doesn't always know the company_id up front.
        """
        if company_id:
            return self._conn.execute(
                "SELECT * FROM oauth_company_token WHERE company_id = ?",
                (company_id,),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM oauth_company_token ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()

    # -- OAuth: ephemeral location tokens -------------------------------
    def save_location_token(
        self, location_id: str, company_id: str, access_token_enc: str, expires_at: str
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO oauth_location_tokens
                    (location_id, company_id, access_token_enc, expires_at, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(location_id) DO UPDATE SET
                    company_id = excluded.company_id,
                    access_token_enc = excluded.access_token_enc,
                    expires_at = excluded.expires_at,
                    updated_at = datetime('now')
                """,
                (location_id, company_id, access_token_enc, expires_at),
            )

    def get_location_token(self, location_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM oauth_location_tokens WHERE location_id = ?",
            (location_id,),
        ).fetchone()

    # -- tenant registry (INSTALL / UNINSTALL webhook) -------------------
    def upsert_tenant(
        self,
        location_id: str,
        company_id: str,
        install_type: str | None = None,
        active: bool = True,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO tenants (location_id, company_id, install_type, active, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(location_id) DO UPDATE SET
                    company_id = excluded.company_id,
                    install_type = excluded.install_type,
                    active = excluded.active,
                    updated_at = datetime('now')
                """,
                (location_id, company_id, install_type, 1 if active else 0),
            )

    def deactivate_tenant(self, location_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE tenants SET active = 0, updated_at = datetime('now') WHERE location_id = ?",
                (location_id,),
            )

    def get_tenant(self, location_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tenants WHERE location_id = ?", (location_id,)
        ).fetchone()

    def list_active_tenants(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM tenants WHERE active = 1 ORDER BY location_id"
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
