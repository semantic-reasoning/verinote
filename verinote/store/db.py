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
        self._ensure_schema_migrations()

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
        # kind is set at first registration (e.g. upload marks 'binary') and
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

    def get_source(self, source_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()

    def get_source_by_path(self, path: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sources WHERE path = ?", (path,)
        ).fetchone()

    def sources_with_counts(self) -> list[sqlite3.Row]:
        """Sources plus how many facts cite each — for the Sources listing."""
        return list(
            self._conn.execute(
                "SELECT s.id, s.path, s.kind, s.added_at, "
                "GROUP_CONCAT(a.path, '\n') AS artifact_paths, "
                "COUNT(DISTINCT f.id) AS fact_count "
                "FROM sources s "
                "LEFT JOIN source_artifacts a ON a.source_id = s.id "
                "LEFT JOIN facts f ON f.source_id = s.id "
                "GROUP BY s.id ORDER BY s.path"
            )
        )

    def add_source_artifact(
        self,
        *,
        source_id: int,
        kind: str,
        path: str,
        content_type: str = "text/plain",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO source_artifacts(source_id, kind, path, content_type) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(source_id, kind) DO UPDATE SET "
                "path=excluded.path, content_type=excluded.content_type "
                "RETURNING id",
                (source_id, kind, path, content_type),
            )
            return int(cur.fetchone()[0])

    def source_artifacts(self, source_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM source_artifacts WHERE source_id = ? ORDER BY id",
                (source_id,),
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

    def source_extraction_jobs(self) -> list[sqlite3.Row]:
        """Latest extraction jobs, newest first, for the Sources listing."""
        return list(
            self._conn.execute(
                "SELECT j.*, s.path AS source_path, a.path AS artifact_path "
                "FROM extraction_jobs j JOIN sources s ON s.id = j.source_id "
                "LEFT JOIN source_artifacts a ON a.id = j.artifact_id "
                "ORDER BY j.id DESC"
            )
        )

    def delete_source(self, source_id: int) -> sqlite3.Row | None:
        """Delete a source and every fact extracted from it.

        Returns the deleted source row, or None if it did not exist. Facts are
        deleted with their DuckDB term rows so source removal does not leave
        engine-side term data behind.
        """
        with self._lock:
            source = self._conn.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None:
                return None
            fact_ids = [
                int(row["id"])
                for row in self._conn.execute(
                    "SELECT id FROM facts WHERE source_id = ? ORDER BY id",
                    (source_id,),
                )
            ]
            deleted_terms: list[int] = []
            self._conn.execute("BEGIN")
            try:
                for fact_id in fact_ids:
                    self.fact_terms.delete_fact_terms(fact_id)
                    deleted_terms.append(fact_id)
                self._conn.execute("DELETE FROM facts WHERE source_id = ?", (source_id,))
                self._conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
                self._conn.execute("COMMIT")
                return source
            except Exception:
                self._rollback_quietly()
                for fact_id in deleted_terms:
                    self._restore_fact_terms_from_row_quietly(fact_id)
                raise

    # --- extraction jobs/chunks -----------------------------------------
    def create_extraction_job(
        self,
        *,
        source_id: int,
        artifact_id: int | None = None,
        provider: str | None,
        model: str | None,
        total_chunks: int,
        message: str = "",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO extraction_jobs("
                "source_id, artifact_id, provider, model, total_chunks, message"
                ") VALUES(?,?,?,?,?,?) RETURNING id",
                (source_id, artifact_id, provider, model, total_chunks, message),
            )
            return int(cur.fetchone()[0])

    def add_source_chunks(
        self, *, job_id: int, source_id: int, chunks: Iterable[str]
    ) -> list[int]:
        with self._lock:
            ids: list[int] = []
            for index, text in enumerate(chunks):
                cur = self._conn.execute(
                    "INSERT INTO source_chunks(source_id, job_id, chunk_index, text) "
                    "VALUES(?,?,?,?) RETURNING id",
                    (source_id, job_id, index, text),
                )
                ids.append(int(cur.fetchone()[0]))
            return ids

    def get_extraction_job(self, job_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM extraction_jobs WHERE id = ?", (job_id,)
        ).fetchone()

    def get_source_chunk(self, chunk_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM source_chunks WHERE id = ?", (chunk_id,)
        ).fetchone()

    def source_chunks(self, job_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM source_chunks WHERE job_id = ? ORDER BY chunk_index",
                (job_id,),
            )
        )

    def reset_running_chunks(self, job_id: int) -> int:
        """Return stale running chunks for a job to pending before resume."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE source_chunks SET status = 'pending', error = '', "
                "updated_at = datetime('now') "
                "WHERE job_id = ? AND status = 'running'",
                (job_id,),
            )
            self._refresh_extraction_job(job_id)
            return int(cur.rowcount)

    def next_pending_chunk(self, job_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM source_chunks "
            "WHERE job_id = ? AND status = 'pending' "
            "ORDER BY chunk_index LIMIT 1",
            (job_id,),
        ).fetchone()

    def mark_extraction_job_running(self, job_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE extraction_jobs SET status = 'running', "
                "message = 'Analyzing chunks...', updated_at = datetime('now') "
                "WHERE id = ? AND status != 'canceled'",
                (job_id,),
            )

    def mark_chunk_running(self, chunk_id: int) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE source_chunks SET status = 'running', attempts = attempts + 1, "
                "error = '', updated_at = datetime('now') "
                "WHERE id = ? AND status = 'pending'",
                (chunk_id,),
            )
            if cur.rowcount != 1:
                return None
            return self.get_source_chunk(chunk_id)

    def mark_chunk_done(self, chunk_id: int, *, candidates: int = 0) -> None:
        with self._lock:
            chunk = self.get_source_chunk(chunk_id)
            if chunk is None:
                return
            self._conn.execute(
                "UPDATE source_chunks SET status = 'done', error = '', "
                "updated_at = datetime('now') WHERE id = ?",
                (chunk_id,),
            )
            self._conn.execute(
                "UPDATE extraction_jobs SET candidate_count = candidate_count + ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (candidates, chunk["job_id"]),
            )
            self._refresh_extraction_job(int(chunk["job_id"]))

    def mark_chunk_failed(self, chunk_id: int, error: str) -> None:
        with self._lock:
            chunk = self.get_source_chunk(chunk_id)
            if chunk is None:
                return
            self._conn.execute(
                "UPDATE source_chunks SET status = 'failed', error = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (error, chunk_id),
            )
            self._refresh_extraction_job(int(chunk["job_id"]))

    def retry_failed_chunks(self, job_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE source_chunks SET status = 'pending', error = '', "
                "updated_at = datetime('now') "
                "WHERE job_id = ? AND status = 'failed'",
                (job_id,),
            )
            self._refresh_extraction_job(job_id)
            return int(cur.rowcount)

    def finish_extraction_job(self, job_id: int) -> None:
        with self._lock:
            self._refresh_extraction_job(job_id, final=True)

    def fail_extraction_job(self, job_id: int, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE extraction_jobs SET status = 'failed', message = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (message, job_id),
            )

    def fact_exists_for_source(
        self, *, source_id: int, subject: object, relation: object, obj: object
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM facts WHERE source_id = ? AND subject = ? "
            "AND relation = ? AND object = ? LIMIT 1",
            (
                source_id,
                _display_fact_value(subject),
                _display_fact_value(relation),
                _display_fact_value(obj),
            ),
        ).fetchone()
        return row is not None

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
        job_id: int | None = None,
        note: str = "",
    ) -> int:
        with self._lock:
            fact_id: int | None = None
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO facts("
                    "subject, relation, object, status, confidence, source_id, "
                    "run_id, job_id, note"
                    ") VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
                    (
                        _display_fact_value(subject),
                        _display_fact_value(relation),
                        _display_fact_value(obj),
                        status,
                        confidence,
                        source_id,
                        run_id,
                        job_id,
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

    def delete_question(self, question_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))

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

    def _restore_fact_terms_from_row_quietly(self, fact_id: int) -> None:
        try:
            row = self._conn.execute(
                "SELECT subject, relation, object FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if row is not None:
                self.fact_terms.put_fact_terms(
                    fact_id, row["subject"], row["relation"], row["object"]
                )
        except Exception:
            pass

    def _ensure_schema_migrations(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_artifacts (
                id           INTEGER PRIMARY KEY,
                source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                kind         TEXT NOT NULL CHECK (kind IN ('original_text','extracted_text')),
                path         TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL DEFAULT 'text/plain',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(source_id, kind)
            );
            """
        )
        job_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(extraction_jobs)")
        }
        if "artifact_id" not in job_columns:
            self._conn.execute(
                "ALTER TABLE extraction_jobs ADD COLUMN artifact_id INTEGER "
                "REFERENCES source_artifacts(id) ON DELETE SET NULL"
            )
        fact_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(facts)")
        }
        if "job_id" not in fact_columns:
            self._conn.execute(
                "ALTER TABLE facts ADD COLUMN job_id INTEGER "
                "REFERENCES extraction_jobs(id) ON DELETE SET NULL"
            )

    def _refresh_extraction_job(self, job_id: int, *, final: bool = False) -> None:
        counts = self._conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done, "
            "COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed, "
            "COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0) AS running, "
            "COUNT(*) AS total "
            "FROM source_chunks WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if counts is None:
            return
        done = int(counts["done"])
        failed = int(counts["failed"])
        running = int(counts["running"])
        total = int(counts["total"])
        if final and failed:
            status = "failed"
        elif final or (total and done == total):
            status = "done"
        elif running:
            status = "running"
        elif failed:
            status = "failed"
        else:
            status = "pending"

        if status == "done":
            message = f"Analysis complete: {done}/{total} chunk(s)"
        elif status == "failed":
            error = self._conn.execute(
                "SELECT error FROM source_chunks WHERE job_id = ? "
                "AND status = 'failed' AND error != '' ORDER BY chunk_index LIMIT 1",
                (job_id,),
            ).fetchone()
            detail = f": {error['error']}" if error is not None else ""
            message = (
                f"Analysis failed: {failed} chunk(s) failed, "
                f"{done}/{total} complete{detail}"
            )
        elif status == "running":
            message = f"Analyzing: {done}/{total} chunk(s) complete"
        else:
            message = f"Pending: {done}/{total} chunk(s) complete"

        self._conn.execute(
            "UPDATE extraction_jobs SET status = ?, completed_chunks = ?, "
            "failed_chunks = ?, message = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (status, done, failed, message, job_id),
        )


def _display_fact_value(value: object) -> str:
    from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Var, render_term

    if isinstance(value, (Atom, Compound, NumberLit, StringLit, Var)):
        return render_term(value)
    return str(value)
