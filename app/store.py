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
    due_date      TEXT NOT NULL,          -- ISO YYYY-MM-DD
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|skipped|error
    detail        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at    TEXT,
    UNIQUE(location_id, invoice_id)
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
        self._conn.commit()

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
    ) -> bool:
        """Queue a review record. Returns False if the invoice was already
        queued (idempotent duplicate guard)."""
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO review_queue
                    (location_id, invoice_id, email, phone, due_date)
                VALUES (?, ?, ?, ?, ?)
                """,
                (location_id, invoice_id, email, phone, due.isoformat()),
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

    def close(self) -> None:
        self._conn.close()
