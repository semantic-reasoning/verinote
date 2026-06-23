# SPDX-License-Identifier: Apache-2.0
"""Thin SQLite data-access layer — the system-of-record for verinote.

Deliberately small and synchronous: the workload is a handful of small
transactional writes (the review toggle is a single-row UPDATE) plus reads for
rendering. SQLite/WAL is the right fit; DuckDB is reserved for analytics and
attaches this same file read-only (see engine/analytics, future work).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

# Status tiers (kept in code so the web/pipeline layers share one definition).
REVIEW_STATUSES = frozenset({"candidate", "needs_review"})
ENGINE_STATUSES = frozenset({"confirmed", "accepted"})


def _load_schema() -> str:
    return resources.files("verinote.store").joinpath("schema.sql").read_text(encoding="utf-8")


class Store:
    """A connection to one KB's SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI serves sync endpoints from a thread
        # pool. WAL allows concurrent readers; we serialise writes with _lock.
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._lock = threading.Lock()

    # --- lifecycle -------------------------------------------------------
    def init_schema(self) -> None:
        self._conn.executescript(_load_schema())

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- sources ---------------------------------------------------------
    def add_source(self, path: str, kind: str = "text") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sources(path, kind) VALUES(?, ?) "
                "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind RETURNING id",
                (path, kind),
            )
            return int(cur.fetchone()[0])

    def sources(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM sources ORDER BY path"))

    # --- runs ------------------------------------------------------------
    def add_run(self, *, provider: str | None, model: str | None, summary: str = "") -> int:
        """Open an extraction run; facts produced by it cite the returned id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs(provider, model, summary) VALUES(?,?,?) RETURNING id",
                (provider, model, summary),
            )
            return int(cur.fetchone()[0])

    def set_run_summary(self, run_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET summary = ? WHERE id = ?", (summary, run_id))

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    # --- facts -----------------------------------------------------------
    def add_fact(
        self,
        subject: str,
        relation: str,
        obj: str,
        *,
        status: str = "candidate",
        confidence: float = 0.0,
        source_id: int | None = None,
        run_id: int | None = None,
        note: str = "",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts(subject, relation, object, status, confidence, source_id, run_id, note) "
                "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                (subject, relation, obj, status, confidence, source_id, run_id, note),
            )
            return int(cur.fetchone()[0])

    def get_fact(self, fact_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT f.*, s.path AS source_path FROM facts f "
            "LEFT JOIN sources s ON s.id = f.source_id WHERE f.id = ?",
            (fact_id,),
        ).fetchone()

    def facts(self, *, statuses: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT f.*, s.path AS source_path FROM facts f "
            "LEFT JOIN sources s ON s.id = f.source_id"
        )
        params: tuple[Any, ...] = ()
        if statuses is not None:
            statuses = list(statuses)
            placeholders = ",".join("?" * len(statuses))
            sql += f" WHERE f.status IN ({placeholders})"
            params = tuple(statuses)
        sql += " ORDER BY f.id"
        return list(self._conn.execute(sql, params))

    def review_queue(self) -> list[sqlite3.Row]:
        return self.facts(statuses=REVIEW_STATUSES)

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) c FROM facts GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def set_status(self, fact_id: int, status: str, *, action: str = "set_status") -> sqlite3.Row | None:
        with self._lock:
            before = self.get_fact(fact_id)
            if before is None:
                return None
            self._conn.execute(
                "UPDATE facts SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, fact_id),
            )
            after = self.get_fact(fact_id)
            self._log(fact_id, action, before, after)
            return after

    def toggle_review(self, fact_id: int) -> sqlite3.Row | None:
        """needs_review/candidate -> confirmed, and confirmed -> needs_review."""
        row = self.get_fact(fact_id)
        if row is None:
            return None
        new_status = "needs_review" if row["status"] in ENGINE_STATUSES else "confirmed"
        return self.set_status(fact_id, new_status, action="toggled")

    # --- audit -----------------------------------------------------------
    def _log(self, fact_id: int, action: str, before: sqlite3.Row | None, after: sqlite3.Row | None) -> None:
        self._conn.execute(
            "INSERT INTO review_log(fact_id, action, before_json, after_json) VALUES(?,?,?,?)",
            (
                fact_id,
                action,
                json.dumps(dict(before), ensure_ascii=False) if before else None,
                json.dumps(dict(after), ensure_ascii=False) if after else None,
            ),
        )
