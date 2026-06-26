# SPDX-License-Identifier: MPL-2.0
"""Thin SQLite data-access layer — the system-of-record for verinote.

Deliberately small and synchronous: the workload is a handful of small
transactional writes (the review toggle is a single-row UPDATE) plus reads for
rendering. SQLite/WAL is the right fit for writes; DuckDB reads confirmed rows
for deterministic inference and attaches this same file read-only for analytics.
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
        self._inference_cache: Any = None
        self._fact_terms: Any = None

    # --- lifecycle -------------------------------------------------------
    def init_schema(self) -> None:
        self._conn.executescript(_load_schema())

    def close(self) -> None:
        if self._inference_cache is not None:
            self._inference_cache.close()
            self._inference_cache = None
        if self._fact_terms is not None:
            self._fact_terms.close()
            self._fact_terms = None
        self._conn.close()

    @property
    def inference_cache(self):
        """Per-store reusable DuckDB inference cache."""
        if self._inference_cache is None:
            from verinote.engine import DuckDBInferenceCache

            self._inference_cache = DuckDBInferenceCache()
        return self._inference_cache

    @property
    def fact_terms(self):
        """Per-KB DuckDB sidecar for structural fact terms."""
        if self._fact_terms is None:
            from verinote.store.duckdb_fact_terms import DuckDBFactTermStore

            self._fact_terms = DuckDBFactTermStore.for_root(self.db_path.parent)
        return self._fact_terms

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- sources ---------------------------------------------------------
    def add_source(self, path: str, kind: str = "text") -> int:
        # kind is set at first registration (e.g. ingest marks 'conversion') and
        # preserved on re-registration — the no-op SET keeps the existing kind
        # while still letting RETURNING hand back the id on conflict, so a later
        # extraction pass over the same path doesn't downgrade it to 'text'.
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sources(path, kind) VALUES(?, ?) "
                "ON CONFLICT(path) DO UPDATE SET path=excluded.path RETURNING id",
                (path, kind),
            )
            return int(cur.fetchone()[0])

    def sources(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM sources ORDER BY path"))

    def sources_with_counts(self) -> list[sqlite3.Row]:
        """Sources plus how many facts cite each — for the Sources listing."""
        return list(
            self._conn.execute(
                "SELECT s.id, s.path, s.kind, s.added_at, COUNT(f.id) AS fact_count "
                "FROM sources s LEFT JOIN facts f ON f.source_id = s.id "
                "GROUP BY s.id ORDER BY s.path"
            )
        )

    def source_fact_counts(self) -> list[sqlite3.Row]:
        """Per-source total vs engine-input (confirmed/accepted) fact counts."""
        return list(
            self._conn.execute(
                "SELECT s.id, s.path, s.kind, "
                "COUNT(f.id) AS total, "
                "COALESCE(SUM(CASE WHEN f.status IN ('confirmed','accepted') "
                "THEN 1 ELSE 0 END), 0) AS engine "
                "FROM sources s LEFT JOIN facts f ON f.source_id = s.id "
                "GROUP BY s.id ORDER BY s.path"
            )
        )

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
        subject: object,
        relation: object,
        obj: object,
        *,
        status: str = "candidate",
        confidence: float = 0.0,
        source_id: int | None = None,
        run_id: int | None = None,
        note: str = "",
    ) -> int:
        with self._lock:
            fact_id: int | None = None
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO facts(subject, relation, object, status, confidence, source_id, run_id, note) "
                    "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                    (
                        _display_fact_value(subject),
                        _display_fact_value(relation),
                        _display_fact_value(obj),
                        status,
                        confidence,
                        source_id,
                        run_id,
                        note,
                    ),
                )
                fact_id = int(cur.fetchone()[0])
                self.fact_terms.put_fact_terms(fact_id, subject, relation, obj)
                self._conn.execute("COMMIT")
                return fact_id
            except Exception:
                self._rollback_quietly()
                if fact_id is not None:
                    self._delete_fact_terms_quietly(fact_id)
                raise

    def get_fact_terms(self, fact_id: int):
        """Return structural DuckDB terms for a fact metadata row."""
        return self.fact_terms.get_fact_terms(fact_id)

    def engine_fact_terms(self) -> list[dict[str, object]]:
        """Return confirmed/accepted facts using DuckDB terms as logical values."""
        rows = self.facts(statuses=ENGINE_STATUSES)
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []

        terms = self.fact_terms.get_many_fact_terms(ids)
        missing = [fact_id for fact_id in ids if fact_id not in terms]
        if missing:
            self.backfill_fact_terms()
            terms = self.fact_terms.get_many_fact_terms(ids)
            missing = [fact_id for fact_id in ids if fact_id not in terms]
        if missing:
            from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError

            raise DuckDBFactTermStoreError(
                "missing DuckDB fact terms for engine fact id(s): "
                + ", ".join(str(fact_id) for fact_id in missing)
            )

        return [
            {
                "id": fact_id,
                "subject": terms[fact_id][0],
                "relation": terms[fact_id][1],
                "object": terms[fact_id][2],
            }
            for fact_id in ids
        ]

    def backfill_fact_terms(self) -> int:
        """Backfill missing DuckDB term rows from SQLite text mirrors as StringLit."""
        with self._lock:
            rows = list(
                self._conn.execute(
                    "SELECT id, subject, relation, object FROM facts ORDER BY id"
                )
            )
            existing = self.fact_terms.get_many_fact_terms(row["id"] for row in rows)
            written = 0
            for row in rows:
                fact_id = int(row["id"])
                if fact_id in existing:
                    continue
                self.fact_terms.put_fact_terms(
                    fact_id, row["subject"], row["relation"], row["object"]
                )
                written += 1
            return written

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

    def amend_fact(
        self,
        fact_id: int,
        *,
        subject: object,
        relation: object,
        obj: object,
        note: str = "",
    ) -> sqlite3.Row | None:
        """Correct a fact's (subject, relation, object, note); audit as `amended`."""
        with self._lock:
            before = self.get_fact(fact_id)
            if before is None:
                return None
            previous_terms = self.fact_terms.get_fact_terms(fact_id)
            terms_written = False
            self._conn.execute("BEGIN")
            try:
                self.fact_terms.put_fact_terms(fact_id, subject, relation, obj)
                terms_written = True
                self._conn.execute(
                    "UPDATE facts SET subject = ?, relation = ?, object = ?, note = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (
                        _display_fact_value(subject),
                        _display_fact_value(relation),
                        _display_fact_value(obj),
                        note,
                        fact_id,
                    ),
                )
                after = self.get_fact(fact_id)
                self._log(fact_id, "amended", before, after)
                self._conn.execute("COMMIT")
                return after
            except Exception:
                self._rollback_quietly()
                if terms_written:
                    self._restore_fact_terms_quietly(fact_id, previous_terms)
                raise

    # --- questions -------------------------------------------------------
    def add_question(self, text: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO questions(text) VALUES(?) RETURNING id", (text,)
            )
            return int(cur.fetchone()[0])

    def questions(self, *, pending_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM questions"
        if pending_only:
            sql += " WHERE status = 'pending'"
        return list(self._conn.execute(sql + " ORDER BY id"))

    def set_question_query(self, question_id: int, query_dl: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE questions SET query_dl = ?, status = ? WHERE id = ?",
                (query_dl, status, question_id),
            )

    # --- audit -----------------------------------------------------------
    def fact_log(self, fact_id: int) -> list[sqlite3.Row]:
        """Audit trail (oldest first) for one fact — drives the provenance view."""
        return list(
            self._conn.execute(
                "SELECT id, action, at FROM review_log WHERE fact_id = ? ORDER BY id",
                (fact_id,),
            )
        )

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

    def _rollback_quietly(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _delete_fact_terms_quietly(self, fact_id: int) -> None:
        try:
            self.fact_terms.delete_fact_terms(fact_id)
        except Exception:
            pass

    def _restore_fact_terms_quietly(
        self, fact_id: int, terms: tuple[object, object, object] | None
    ) -> None:
        try:
            if terms is None:
                self.fact_terms.delete_fact_terms(fact_id)
            else:
                self.fact_terms.put_fact_terms(fact_id, *terms)
        except Exception:
            pass


def _display_fact_value(value: object) -> str:
    from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Var, render_term

    if isinstance(value, (Atom, Compound, NumberLit, StringLit, Var)):
        return render_term(value)
    return str(value)
