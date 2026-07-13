# SPDX-License-Identifier: MPL-2.0
"""Thin SQLite data-access layer — the system-of-record for verinote.

Deliberately small and synchronous: the workload is a handful of small
transactional writes (the review toggle is a single-row UPDATE) plus reads for
rendering. SQLite/WAL is the right fit for writes; DuckDB reads confirmed rows
for deterministic inference and attaches this same file read-only for analytics.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
import sqlite3
import threading
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

# Status tiers (kept in code so the web/pipeline layers share one definition).
REVIEW_STATUSES = frozenset({"candidate", "needs_review"})
# kb_meta key under which a KB declares that it owns a logic policy file.
POLICY_MARKER_KEY = "policy.logic"
# kb_meta key declaring that fact logical terms are managed by facts.duckdb.
FACT_TERMS_MARKER_KEY = "fact_terms.duckdb"
ENGINE_STATUSES = frozenset({"confirmed", "accepted"})
MAX_EVIDENCE_SNIPPET_CHARS = 1000
REVIEW_PAGE_SIZES = (25, 50, 100)
DEFAULT_REVIEW_PAGE_SIZE = 50
DEFAULT_REVIEW_SORT = "newest"
QUESTION_STATUSES = frozenset(
    {
        "pending",
        "translated",
        "review_required",
        "translation_failed",
        "no_answer",
        "ambiguous",
    }
)
REVIEW_SORT_SQL = {
    "newest": "f.id DESC",
    "oldest": "f.id ASC",
    "updated": "f.updated_at DESC, f.id DESC",
    "confidence": "f.confidence DESC, f.id DESC",
    "source": "s.path IS NULL ASC, lower(s.path) ASC, f.id DESC",
}


def _status_filter(statuses: Iterable[str]) -> tuple[str, tuple[str, ...]]:
    """Deterministic (placeholders, params) for a `status IN (...)` filter.

    Status values are bound as parameters, never spliced into SQL text, and
    `sorted()` fixes the parameter order a `frozenset` would otherwise leave
    unspecified. Callers must read the status constant at call time so the
    constant stays the single definition of the tier.

    An empty tier is rejected loudly. SQLite answers `status IN ()` with zero
    rows instead of an error, which would turn an empty constant into exactly
    the silent wrong answer this filter exists to prevent: coverage would call
    every source a gap, and `accept_review_facts_for_source` would promote
    nothing while reporting success. A crash is cheaper.
    """
    ordered = tuple(sorted(frozenset(statuses)))
    if not ordered:
        raise ValueError("status filter must not be empty")
    return ",".join("?" * len(ordered)), ordered


def _load_schema() -> str:
    return resources.files("verinote.store").joinpath("schema.sql").read_text(encoding="utf-8")


@dataclass(frozen=True)
class ReviewQueuePage:
    rows: list[sqlite3.Row]
    total: int
    page: int
    page_size: int
    sort: str

    @property
    def page_count(self) -> int:
        if self.total == 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size

    @property
    def start(self) -> int:
        if self.total == 0:
            return 0
        return (self.page - 1) * self.page_size + 1

    @property
    def end(self) -> int:
        return min(self.page * self.page_size, self.total)

    @classmethod
    def from_rows(
        cls,
        rows: list[sqlite3.Row],
        *,
        total: int,
        page: object,
        page_size: object,
        sort: object,
    ) -> "ReviewQueuePage":
        page_size = _review_page_size(page_size)
        page = _positive_int(page, 1)
        sort = _review_sort(sort)
        page_count = max(1, (total + page_size - 1) // page_size)
        return cls(
            rows=rows,
            total=total,
            page=min(page, page_count),
            page_size=page_size,
            sort=sort,
        )


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _review_page_size(value: object) -> int:
    parsed = _positive_int(value, DEFAULT_REVIEW_PAGE_SIZE)
    return parsed if parsed in REVIEW_PAGE_SIZES else DEFAULT_REVIEW_PAGE_SIZE


def _review_sort(value: object) -> str:
    sort = str(value)
    return sort if sort in REVIEW_SORT_SQL else DEFAULT_REVIEW_SORT


def _missing_fact_terms_error(fact_ids: Iterable[int]) -> Exception:
    from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError

    ids = ", ".join(str(fact_id) for fact_id in fact_ids)
    return DuckDBFactTermStoreError(
        "missing DuckDB fact terms for fact id(s): "
        f"{ids}. Restore facts.duckdb from backup or re-enter the affected "
        "facts. Refusing to rebuild them from SQLite display text because "
        "that would reinterpret structural terms as strings."
    )


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

    # --- kb metadata -----------------------------------------------------
    # `kb_meta` has explicit clients — the policy and fact-term markers below —
    # and no generic
    # read/write/delete API is exposed for it. That is deliberate. A public
    # `delete_meta("policy.logic")` (or a plain `set_meta`) is `clear_policy_marker`
    # under another name: it downgrades a *lost* policy back to a KB that "never had
    # one", which silently restores the shipped-default fallback this table exists
    # to prevent. Keep the accessors private and the intent stays enforceable.
    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM kb_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._set_meta_unlocked(key, value)

    def _set_meta_unlocked(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kb_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
            (key, value),
        )

    def policy_marker(self) -> dict[str, object] | None:
        """The KB's declaration that it has a logic policy file, or None.

        Presence of the row *is* the declaration; a payload we cannot parse is
        still a declaration (returning None there would silently downgrade a
        lost policy to a benign one).
        """
        raw = self._get_meta(POLICY_MARKER_KEY)
        if raw is None:
            return None
        try:
            marker = json.loads(raw)
        except json.JSONDecodeError:
            return {"sha256": "", "recorded_at": "", "origin": "unknown"}
        if not isinstance(marker, dict):
            return {"sha256": "", "recorded_at": "", "origin": "unknown"}
        return marker

    def record_policy_marker(self, sha256: str, *, origin: str) -> dict[str, object]:
        """Record that this KB has a policy file. Hash is evidence, not a verdict."""
        marker: dict[str, object] = {
            "sha256": sha256,
            "recorded_at": _utc_now(),
            "origin": origin,
        }
        self._set_meta(POLICY_MARKER_KEY, json.dumps(marker, ensure_ascii=False))
        return marker

    def fact_terms_marker(self) -> dict[str, object] | None:
        """The KB's declaration that DuckDB owns canonical fact terms, or None."""
        raw = self._get_meta(FACT_TERMS_MARKER_KEY)
        if raw is None:
            return None
        try:
            marker = json.loads(raw)
        except json.JSONDecodeError:
            return {"version": 0, "recorded_at": "", "origin": "unknown"}
        if not isinstance(marker, dict):
            return {"version": 0, "recorded_at": "", "origin": "unknown"}
        return marker

    def _has_fact_terms_marker(self) -> bool:
        return self._get_meta(FACT_TERMS_MARKER_KEY) is not None

    def _record_fact_terms_marker(self, *, origin: str) -> dict[str, object]:
        with self._lock:
            return self._record_fact_terms_marker_unlocked(origin=origin)

    def _record_fact_terms_marker_unlocked(self, *, origin: str) -> dict[str, object]:
        marker: dict[str, object] = {
            "version": 1,
            "recorded_at": _utc_now(),
            "origin": origin,
        }
        self._set_meta_unlocked(
            FACT_TERMS_MARKER_KEY, json.dumps(marker, ensure_ascii=False)
        )
        return marker

    # Deliberately no `clear_policy_marker()` — and, per the note above, no public
    # `delete_meta` that would be one. Dropping the marker downgrades a lost policy
    # back to a benign default, which is the bug this table exists to prevent.
    # Recovery is restoring the file or running `verinote policy reset --force`,
    # both of which re-record the marker.

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
        """Sources plus analysis and fact summaries for the Sources listing.

        `engine_count` is derived from `ENGINE_STATUSES` (read at call time), so
        the Sources page and the coverage report can never disagree about what
        the engine actually reads.
        """
        placeholders, params = _status_filter(ENGINE_STATUSES)
        return list(
            self._conn.execute(
                "SELECT s.id, s.path, s.kind, s.added_at, "
                "(SELECT COUNT(*) FROM facts f WHERE f.source_id = s.id) "
                "AS fact_count, "
                "(SELECT COUNT(*) FROM facts f WHERE f.source_id = s.id "
                "AND f.status = 'candidate') AS candidate_count, "
                "(SELECT COUNT(*) FROM facts f WHERE f.source_id = s.id "
                "AND f.status = 'needs_review') AS needs_review_count, "
                "(SELECT COUNT(*) FROM facts f WHERE f.source_id = s.id "
                f"AND f.status IN ({placeholders})) AS engine_count, "
                "j.id AS job_id, j.status AS analysis_status, "
                "j.total_chunks, j.completed_chunks, j.failed_chunks, "
                "j.candidate_count AS analysis_candidate_count, "
                "j.message AS analysis_message, j.provider, j.model, "
                "j.updated_at AS last_analyzed_at "
                "FROM sources s "
                "LEFT JOIN extraction_jobs j ON j.id = ("
                "  SELECT MAX(j2.id) FROM extraction_jobs j2 "
                "  WHERE j2.source_id = s.id"
                ") "
                "ORDER BY s.path",
                params,
            )
        )

    def add_source_artifact(
        self,
        *,
        source_id: int,
        kind: str,
        path: str,
        content_type: str = "text/plain",
        checksum: str = "",
    ) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_artifacts("
                "source_id, kind, path, content_type, checksum"
                ") VALUES(?,?,?,?,?) "
                "ON CONFLICT(source_id, kind, checksum) DO NOTHING",
                (source_id, kind, path, content_type, checksum),
            )
            row = self._conn.execute(
                "SELECT id FROM source_artifacts "
                "WHERE source_id = ? AND kind = ? AND checksum = ?",
                (source_id, kind, checksum),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("source artifact insert failed")
            return int(row["id"])

    def get_source_artifact(self, artifact_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM source_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()

    def get_extraction_job_detail(self, job_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT j.*, s.path AS source_path, a.path AS artifact_path "
            "FROM extraction_jobs j "
            "JOIN sources s ON s.id = j.source_id "
            "LEFT JOIN source_artifacts a ON a.id = j.artifact_id "
            "WHERE j.id = ?",
            (job_id,),
        ).fetchone()

    def source_artifacts(self, source_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM source_artifacts WHERE source_id = ? ORDER BY id",
                (source_id,),
            )
        )

    def latest_source_text_artifact(self, source_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM source_artifacts "
            "WHERE source_id = ? AND kind IN ('original_text','extracted_text') "
            "ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()

    def source_text_inputs(self) -> list[sqlite3.Row]:
        """Original source path plus the text artifact path to feed extraction."""
        return list(
            self._conn.execute(
                "SELECT s.id AS source_id, s.path AS source_path, "
                "a.id AS artifact_id, a.path AS artifact_path "
                "FROM sources s JOIN source_artifacts a ON a.source_id = s.id "
                "WHERE a.kind IN ('original_text','extracted_text') "
                "AND a.id = ("
                "  SELECT MAX(a2.id) FROM source_artifacts a2 "
                "  WHERE a2.source_id = s.id "
                "  AND a2.kind IN ('original_text','extracted_text')"
                ") "
                "ORDER BY s.path, a.id"
            )
        )

    def source_fact_counts(self) -> list[sqlite3.Row]:
        """Per-source total vs engine-input fact counts.

        The engine tier is derived from `ENGINE_STATUSES` (read at call time) so
        the coverage report counts exactly the facts the engine consumes.
        """
        placeholders, params = _status_filter(ENGINE_STATUSES)
        return list(
            self._conn.execute(
                "SELECT s.id, s.path, s.kind, "
                "COUNT(f.id) AS total, "
                f"COALESCE(SUM(CASE WHEN f.status IN ({placeholders}) "
                "THEN 1 ELSE 0 END), 0) AS engine "
                "FROM sources s LEFT JOIN facts f ON f.source_id = s.id "
                "GROUP BY s.id ORDER BY s.path",
                params,
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

    def clear_source_analysis(self, source_id: int) -> int:
        """Remove extracted facts and extraction jobs while keeping source files.

        Returns the number of facts removed. DuckDB term rows are deleted in the
        same logical operation so re-analysis starts from a clean source state.
        """
        with self._lock:
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
                self._conn.execute(
                    "DELETE FROM extraction_jobs WHERE source_id = ?", (source_id,)
                )
                self._conn.execute("COMMIT")
                return len(fact_ids)
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
            if artifact_id is not None:
                artifact = self.get_source_artifact(artifact_id)
                if artifact is None or int(artifact["source_id"]) != source_id:
                    raise sqlite3.IntegrityError(
                        "extraction job artifact must belong to the source"
                    )
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
            job = self.get_extraction_job(job_id)
            self._conn.execute(
                "UPDATE extraction_jobs SET status = 'running', "
                "message = 'Analyzing chunks...', updated_at = datetime('now') "
                "WHERE id = ? AND status != 'canceled'",
                (job_id,),
            )
            after = self.get_extraction_job(job_id)
            if job is not None and after is not None:
                self._add_fact_event(
                    fact_id=None,
                    event_type="extraction_job_started",
                    actor="system",
                    source_id=int(after["source_id"]),
                    job_id=job_id,
                    before=_job_event_payload(job),
                    after=_job_event_payload(after),
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
            failed = self.get_source_chunk(chunk_id)
            self._add_fact_event(
                fact_id=None,
                event_type="chunk_failed",
                actor="system",
                source_id=int(chunk["source_id"]),
                job_id=int(chunk["job_id"]),
                chunk_id=chunk_id,
                before=_chunk_event_payload(chunk),
                after=_chunk_event_payload(failed) if failed is not None else None,
            )
            self._refresh_extraction_job(int(chunk["job_id"]))

    def retry_failed_chunks(self, job_id: int) -> int:
        with self._lock:
            failed_chunks = list(
                self._conn.execute(
                    "SELECT * FROM source_chunks WHERE job_id = ? AND status = 'failed'",
                    (job_id,),
                )
            )
            cur = self._conn.execute(
                "UPDATE source_chunks SET status = 'pending', error = '', "
                "updated_at = datetime('now') "
                "WHERE job_id = ? AND status = 'failed'",
                (job_id,),
            )
            for chunk in failed_chunks:
                pending = self.get_source_chunk(int(chunk["id"]))
                self._add_fact_event(
                    fact_id=None,
                    event_type="chunk_retried",
                    actor="system",
                    source_id=int(chunk["source_id"]),
                    job_id=job_id,
                    chunk_id=int(chunk["id"]),
                    before=_chunk_event_payload(chunk),
                    after=_chunk_event_payload(pending) if pending is not None else None,
                )
            self._refresh_extraction_job(job_id)
            return int(cur.rowcount)

    def finish_extraction_job(self, job_id: int) -> None:
        with self._lock:
            before = self.get_extraction_job(job_id)
            self._refresh_extraction_job(job_id, final=True)
            after = self.get_extraction_job(job_id)
            if after is not None:
                self._add_fact_event(
                    fact_id=None,
                    event_type="extraction_job_completed",
                    actor="system",
                    source_id=int(after["source_id"]),
                    job_id=job_id,
                    before=_job_event_payload(before) if before is not None else None,
                    after=_job_event_payload(after),
                )

    def fail_extraction_job(self, job_id: int, message: str) -> None:
        with self._lock:
            before = self.get_extraction_job(job_id)
            self._conn.execute(
                "UPDATE extraction_jobs SET status = 'failed', message = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (message, job_id),
            )
            after = self.get_extraction_job(job_id)
            if after is not None:
                self._add_fact_event(
                    fact_id=None,
                    event_type="extraction_job_failed",
                    actor="system",
                    source_id=int(after["source_id"]),
                    job_id=job_id,
                    before=_job_event_payload(before) if before is not None else None,
                    after=_job_event_payload(after),
                )

    def rollback_extraction_job(self, job_id: int, message: str) -> None:
        """Rewind an interrupted job to `pending` so it can be resumed later.

        Used when this KB's logic policy file vanishes mid-job (#194). The job must
        not be left `running` with a `running` chunk: nothing resets that state, so
        the source becomes permanently stuck — it can neither finish nor be
        re-synced. `failed` is no better, since the chunks that never ran are not
        failures. `pending` is the only state from which "restore the policy file,
        re-run the analysis" actually works.

        Chunks already `done` stay `done` (their candidate facts are real, and
        re-extracting them would just re-do work), and `failed` chunks keep their
        error. Only the in-flight `running` chunk is returned to the queue, which is
        safe: it was rolled back *before* any of its facts were written.

        Counters are untouched on purpose — `completed_chunks`/`failed_chunks`/
        `candidate_count` count `done`/`failed` chunks, and this method changes
        neither, so they stay true.

        A `canceled` job is left alone ENTIRELY — job row, chunks and history. A
        human took it out of the queue, and reviving its in-flight chunk would put
        it back in (`next_pending_chunk` would hand out a chunk of a canceled job),
        while recording an `extraction_job_rolled_back` event for a job that was not
        rolled back would write a fresh falsehood into the very history this change
        exists to keep honest. Hence the early return rather than a `WHERE` clause
        on the job UPDATE alone.
        """
        with self._lock:
            before = self.get_extraction_job(job_id)
            if before is None:
                return
            if before["status"] == "canceled":
                return
            self._conn.execute(
                "UPDATE source_chunks SET status = 'pending', error = '', "
                "updated_at = datetime('now') "
                "WHERE job_id = ? AND status = 'running'",
                (job_id,),
            )
            self._conn.execute(
                "UPDATE extraction_jobs SET status = 'pending', message = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (message, job_id),
            )
            after = self.get_extraction_job(job_id)
            if after is not None:
                self._add_fact_event(
                    fact_id=None,
                    event_type="extraction_job_rolled_back",
                    actor="system",
                    source_id=int(after["source_id"]),
                    job_id=job_id,
                    before=_job_event_payload(before),
                    after=_job_event_payload(after),
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

    # --- fact evidence ---------------------------------------------------
    def add_fact_evidence(
        self,
        *,
        fact_id: int,
        source_id: int,
        artifact_id: int | None = None,
        job_id: int | None = None,
        chunk_id: int | None = None,
        evidence_kind: str = "chunk",
        start_offset: int | None = None,
        end_offset: int | None = None,
        locator: str = "",
        snippet: str = "",
    ) -> int:
        """Persist a bounded source-backed evidence anchor for a fact."""
        snippet = snippet[:MAX_EVIDENCE_SNIPPET_CHARS]
        with self._lock:
            self._validate_fact_evidence_refs(
                fact_id=fact_id,
                source_id=source_id,
                artifact_id=artifact_id,
                job_id=job_id,
                chunk_id=chunk_id,
            )
            cur = self._conn.execute(
                "INSERT INTO fact_evidence("
                "fact_id, source_id, artifact_id, job_id, chunk_id, "
                "evidence_kind, start_offset, end_offset, locator, snippet"
                ") VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (
                    fact_id,
                    source_id,
                    artifact_id,
                    job_id,
                    chunk_id,
                    evidence_kind,
                    start_offset,
                    end_offset,
                    locator,
                    snippet,
                ),
            )
            return int(cur.fetchone()[0])

    def fact_evidence(self, fact_id: int) -> list[sqlite3.Row]:
        """Evidence anchors for one fact, oldest first."""
        return list(
            self._conn.execute(
                "SELECT e.*, s.path AS source_path, a.path AS artifact_path, "
                "c.chunk_index, c.status AS chunk_status "
                "FROM fact_evidence e "
                "JOIN sources s ON s.id = e.source_id "
                "LEFT JOIN source_artifacts a ON a.id = e.artifact_id "
                "LEFT JOIN source_chunks c ON c.id = e.chunk_id "
                "WHERE e.fact_id = ? ORDER BY e.id",
                (fact_id,),
            )
        )

    def source_evidence_snippets(self, source_id: int, *, limit: int = 2) -> list[str]:
        """Distinct evidence snippets for one source, oldest first."""
        rows = self._conn.execute(
            "SELECT e.snippet FROM fact_evidence e "
            "WHERE e.source_id = ? AND e.snippet != '' "
            "GROUP BY e.snippet "
            "ORDER BY MIN(e.id) "
            "LIMIT ?",
            (source_id, limit),
        )
        return [str(row["snippet"]) for row in rows]

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
                self._record_fact_terms_marker_unlocked(origin="write")
                self._add_fact_event(
                    fact_id=fact_id,
                    event_type="candidate_created",
                    actor="system",
                    source_id=source_id,
                    job_id=job_id,
                    after={
                        "status": status,
                        "run_id": run_id,
                        "has_note": bool(note),
                    },
                )
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
            if self._has_fact_terms_marker():
                raise _missing_fact_terms_error(missing)
            self.backfill_fact_terms()
            terms = self.fact_terms.get_many_fact_terms(ids)
            missing = [fact_id for fact_id in ids if fact_id not in terms]
        if missing:
            raise _missing_fact_terms_error(missing)
        if not self._has_fact_terms_marker():
            self._record_fact_terms_marker(origin="existing_sidecar")

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
        """Backfill legacy SQLite-only fact rows into DuckDB as StringLit terms."""
        with self._lock:
            rows = list(
                self._conn.execute(
                    "SELECT id, subject, relation, object FROM facts ORDER BY id"
                )
            )
            existing = self.fact_terms.get_many_fact_terms(row["id"] for row in rows)
            missing = [int(row["id"]) for row in rows if int(row["id"]) not in existing]
            if missing and self._has_fact_terms_marker():
                raise _missing_fact_terms_error(missing)
            written = 0
            for row in rows:
                fact_id = int(row["id"])
                if fact_id in existing:
                    continue
                self.fact_terms.put_fact_terms(
                    fact_id, row["subject"], row["relation"], row["object"]
                )
                written += 1
            if rows:
                origin = "legacy_backfill" if written else "existing_sidecar"
                self._record_fact_terms_marker_unlocked(origin=origin)
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
            placeholders, params = _status_filter(statuses)
            sql += f" WHERE f.status IN ({placeholders})"
        sql += " ORDER BY f.id"
        return list(self._conn.execute(sql, params))

    def review_queue(self) -> list[sqlite3.Row]:
        return self.facts(statuses=REVIEW_STATUSES)

    def review_queue_ids(self, *, sort: object = DEFAULT_REVIEW_SORT) -> list[int]:
        sort = _review_sort(sort)
        placeholders, statuses = _status_filter(REVIEW_STATUSES)
        rows = self._conn.execute(
            "SELECT f.id FROM facts f "
            "LEFT JOIN sources s ON s.id = f.source_id "
            f"WHERE f.status IN ({placeholders}) "
            f"ORDER BY {REVIEW_SORT_SQL[sort]}",
            statuses,
        )
        return [int(row["id"]) for row in rows]

    def facts_by_ids(self, fact_ids: Iterable[int]) -> list[sqlite3.Row]:
        ids = [int(fact_id) for fact_id in fact_ids]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = list(
            self._conn.execute(
                "SELECT f.*, s.path AS source_path FROM facts f "
                "LEFT JOIN sources s ON s.id = f.source_id "
                f"WHERE f.id IN ({placeholders})",
                tuple(ids),
            )
        )
        by_id = {int(row["id"]): row for row in rows}
        return [by_id[fact_id] for fact_id in ids if fact_id in by_id]

    def review_queue_page(
        self,
        *,
        page: object = 1,
        page_size: object = DEFAULT_REVIEW_PAGE_SIZE,
        sort: object = DEFAULT_REVIEW_SORT,
    ) -> ReviewQueuePage:
        page_size = _review_page_size(page_size)
        page = _positive_int(page, 1)
        sort = _review_sort(sort)
        placeholders, statuses = _status_filter(REVIEW_STATUSES)
        where = f"f.status IN ({placeholders})"
        total = int(
            self._conn.execute(
                f"SELECT COUNT(*) AS c FROM facts f WHERE {where}",
                statuses,
            ).fetchone()["c"]
        )
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        offset = (page - 1) * page_size
        rows = list(
            self._conn.execute(
                "SELECT f.*, s.path AS source_path FROM facts f "
                "LEFT JOIN sources s ON s.id = f.source_id "
                f"WHERE {where} "
                f"ORDER BY {REVIEW_SORT_SQL[sort]} "
                "LIMIT ? OFFSET ?",
                (*statuses, page_size, offset),
            )
        )
        return ReviewQueuePage.from_rows(
            rows=rows,
            total=total,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) c FROM facts GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def set_status(
        self,
        fact_id: int,
        status: str,
        *,
        action: str = "set_status",
        actor: str = "human",
        rule_name: str = "",
    ) -> sqlite3.Row | None:
        with self._lock:
            before = self.get_fact(fact_id)
            if before is None:
                return None
            self._conn.execute(
                "UPDATE facts SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, fact_id),
            )
            after = self.get_fact(fact_id)
            self._log(fact_id, action, before, after, actor=actor, rule_name=rule_name)
            return after

    def accept_review_facts_for_source(self, source_id: int) -> list[sqlite3.Row]:
        """Promote all review-queue facts for one source to confirmed."""
        placeholders, statuses = _status_filter(REVIEW_STATUSES)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                facts = list(
                    self._conn.execute(
                        "SELECT * FROM facts WHERE source_id = ? "
                        f"AND status IN ({placeholders}) ORDER BY id",
                        (source_id, *statuses),
                    )
                )
                accepted: list[sqlite3.Row] = []
                for before in facts:
                    fact_id = int(before["id"])
                    self._conn.execute(
                        "UPDATE facts SET status = 'confirmed', updated_at = datetime('now') "
                        "WHERE id = ?",
                        (fact_id,),
                    )
                    after = self.get_fact(fact_id)
                    self._log(fact_id, "accepted", before, after)
                    if after is not None:
                        accepted.append(after)
                self._conn.execute("COMMIT")
                return accepted
            except Exception:
                self._rollback_quietly()
                raise

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
                self._record_fact_terms_marker_unlocked(origin="write")
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

    def set_question_query(
        self, question_id: int, query_dl: str | None, status: str, reason: str = ""
    ) -> None:
        if status not in QUESTION_STATUSES:
            raise ValueError(f"unknown question status: {status}")
        with self._lock:
            self._conn.execute(
                "UPDATE questions SET query_dl = ?, status = ?, reason = ? WHERE id = ?",
                (query_dl, status, reason, question_id),
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

    def fact_events(self, fact_id: int) -> list[sqlite3.Row]:
        """Lifecycle events (oldest first) for one fact."""
        return list(
            self._conn.execute(
                "SELECT id, fact_id, event_type, actor, source_id, job_id, chunk_id, "
                "rule_name, before_json, after_json, at "
                "FROM fact_events WHERE fact_id = ? ORDER BY id",
                (fact_id,),
            )
        )

    def count_facts_with_events(self, event_types: Iterable[str]) -> int:
        event_types = tuple(event_types)
        if not event_types:
            return 0
        placeholders = ",".join("?" * len(event_types))
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT fact_id) AS c FROM fact_events "
            f"WHERE fact_id IS NOT NULL AND event_type IN ({placeholders})",
            event_types,
        ).fetchone()
        return int(row["c"])

    def add_fact_event(
        self,
        *,
        fact_id: int | None,
        event_type: str,
        actor: str = "system",
        source_id: int | None = None,
        job_id: int | None = None,
        chunk_id: int | None = None,
        rule_name: str = "",
        before: dict[str, object] | sqlite3.Row | None = None,
        after: dict[str, object] | sqlite3.Row | None = None,
    ) -> int:
        with self._lock:
            return self._add_fact_event(
                fact_id=fact_id,
                event_type=event_type,
                actor=actor,
                source_id=source_id,
                job_id=job_id,
                chunk_id=chunk_id,
                rule_name=rule_name,
                before=before,
                after=after,
            )

    def _log(
        self,
        fact_id: int,
        action: str,
        before: sqlite3.Row | None,
        after: sqlite3.Row | None,
        *,
        actor: str = "human",
        rule_name: str = "",
    ) -> None:
        source_id = int(after["source_id"]) if after and after["source_id"] is not None else None
        job_id = int(after["job_id"]) if after and after["job_id"] is not None else None
        self._conn.execute(
            "INSERT INTO review_log(fact_id, action, before_json, after_json) VALUES(?,?,?,?)",
            (
                fact_id,
                action,
                json.dumps(dict(before), ensure_ascii=False) if before else None,
                json.dumps(dict(after), ensure_ascii=False) if after else None,
            ),
        )
        self._add_fact_event(
            fact_id=fact_id,
            event_type=action,
            actor=actor,
            source_id=source_id,
            job_id=job_id,
            rule_name=rule_name,
            before=before,
            after=after,
        )

    def _add_fact_event(
        self,
        *,
        fact_id: int | None,
        event_type: str,
        actor: str = "system",
        source_id: int | None = None,
        job_id: int | None = None,
        chunk_id: int | None = None,
        rule_name: str = "",
        before: dict[str, object] | sqlite3.Row | None = None,
        after: dict[str, object] | sqlite3.Row | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO fact_events("
            "fact_id, event_type, actor, source_id, job_id, chunk_id, "
            "rule_name, before_json, after_json"
            ") VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (
                fact_id,
                event_type,
                actor,
                source_id,
                job_id,
                chunk_id,
                rule_name,
                _json_payload(before),
                _json_payload(after),
            ),
        )
        return int(cur.fetchone()[0])

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

    def _validate_fact_evidence_refs(
        self,
        *,
        fact_id: int,
        source_id: int,
        artifact_id: int | None,
        job_id: int | None,
        chunk_id: int | None,
    ) -> None:
        fact = self.get_fact(fact_id)
        if fact is None:
            raise sqlite3.IntegrityError("fact evidence fact does not exist")
        if fact["source_id"] is not None and int(fact["source_id"]) != source_id:
            raise sqlite3.IntegrityError("fact evidence source must match the fact")
        if self.get_source(source_id) is None:
            raise sqlite3.IntegrityError("fact evidence source does not exist")
        if artifact_id is not None:
            artifact = self.get_source_artifact(artifact_id)
            if artifact is None or int(artifact["source_id"]) != source_id:
                raise sqlite3.IntegrityError(
                    "fact evidence artifact must belong to the source"
                )
        if job_id is not None:
            job = self.get_extraction_job(job_id)
            if job is None or int(job["source_id"]) != source_id:
                raise sqlite3.IntegrityError("fact evidence job must belong to the source")
        if chunk_id is not None:
            chunk = self.get_source_chunk(chunk_id)
            if chunk is None or int(chunk["source_id"]) != source_id:
                raise sqlite3.IntegrityError(
                    "fact evidence chunk must belong to the source"
                )
            if job_id is not None and int(chunk["job_id"]) != job_id:
                raise sqlite3.IntegrityError(
                    "fact evidence chunk must belong to the job"
                )

    def _ensure_schema_migrations(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_artifacts (
                id           INTEGER PRIMARY KEY,
                source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                kind         TEXT NOT NULL CHECK (kind IN ('original_text','extracted_text')),
                path         TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL DEFAULT 'text/plain',
                checksum     TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(source_id, kind, checksum)
            );
            """
        )
        artifact_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(source_artifacts)")
        }
        if "checksum" not in artifact_columns:
            self._conn.execute(
                "ALTER TABLE source_artifacts ADD COLUMN checksum TEXT NOT NULL DEFAULT ''"
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
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fact_evidence (
                id            INTEGER PRIMARY KEY,
                fact_id       INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                artifact_id   INTEGER REFERENCES source_artifacts(id) ON DELETE SET NULL,
                job_id        INTEGER REFERENCES extraction_jobs(id) ON DELETE SET NULL,
                chunk_id      INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
                evidence_kind TEXT NOT NULL DEFAULT 'chunk',
                start_offset  INTEGER,
                end_offset    INTEGER,
                locator       TEXT NOT NULL DEFAULT '',
                snippet       TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK (length(evidence_kind) > 0),
                CHECK (start_offset IS NULL OR start_offset >= 0),
                CHECK (end_offset IS NULL OR end_offset >= 0),
                CHECK (
                    start_offset IS NULL
                    OR end_offset IS NULL
                    OR end_offset >= start_offset
                )
            );
            CREATE INDEX IF NOT EXISTS idx_fact_evidence_fact
                ON fact_evidence(fact_id);
            CREATE INDEX IF NOT EXISTS idx_fact_evidence_source
                ON fact_evidence(source_id);
            CREATE INDEX IF NOT EXISTS idx_fact_evidence_chunk
                ON fact_evidence(chunk_id);
            """
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fact_events (
                id          INTEGER PRIMARY KEY,
                fact_id     INTEGER REFERENCES facts(id) ON DELETE SET NULL,
                event_type  TEXT NOT NULL,
                actor       TEXT NOT NULL DEFAULT 'system',
                source_id   INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                job_id      INTEGER REFERENCES extraction_jobs(id) ON DELETE SET NULL,
                chunk_id    INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
                rule_name   TEXT NOT NULL DEFAULT '',
                before_json TEXT,
                after_json  TEXT,
                at          TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK (length(event_type) > 0),
                CHECK (actor IN ('system','human','rule'))
            );
            CREATE INDEX IF NOT EXISTS idx_fact_events_fact
                ON fact_events(fact_id, id);
            CREATE INDEX IF NOT EXISTS idx_fact_events_job
                ON fact_events(job_id);
            """
        )
        self._ensure_question_schema()

    def _ensure_question_schema(self) -> None:
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(questions)")
        }
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'questions'"
        ).fetchone()
        create_sql = row["sql"] if row is not None and row["sql"] is not None else ""
        needs_rebuild = "reason" not in columns or any(
            status not in create_sql for status in QUESTION_STATUSES
        )
        if not needs_rebuild:
            return

        select_reason = "reason" if "reason" in columns else "''"
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DROP TABLE IF EXISTS questions_new")
            self._conn.execute(
                """
                CREATE TABLE questions_new (
                    id         INTEGER PRIMARY KEY,
                    text       TEXT NOT NULL,
                    query_dl   TEXT,
                    status     TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (
                                     status IN (
                                         'pending',
                                         'translated',
                                         'review_required',
                                         'translation_failed',
                                         'no_answer',
                                         'ambiguous'
                                     )
                                 ),
                    reason     TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            self._conn.execute(
                "INSERT INTO questions_new(id, text, query_dl, status, reason, created_at) "
                f"SELECT id, text, query_dl, status, {select_reason}, created_at "
                "FROM questions"
            )
            self._conn.execute("DROP TABLE questions")
            self._conn.execute("ALTER TABLE questions_new RENAME TO questions")
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback_quietly()
            raise

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


def _utc_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _display_fact_value(value: object) -> str:
    from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Var, render_term

    if isinstance(value, (Atom, Compound, NumberLit, StringLit, Var)):
        return render_term(value)
    return str(value)


def _json_payload(value: dict[str, object] | sqlite3.Row | None) -> str | None:
    if value is None:
        return None
    return json.dumps(dict(value), ensure_ascii=False)


def _job_event_payload(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "status": row["status"],
        "source_id": row["source_id"],
        "artifact_id": row["artifact_id"],
        "total_chunks": row["total_chunks"],
        "completed_chunks": row["completed_chunks"],
        "failed_chunks": row["failed_chunks"],
        "candidate_count": row["candidate_count"],
        "message": row["message"],
    }


def _chunk_event_payload(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "status": row["status"],
        "source_id": row["source_id"],
        "job_id": row["job_id"],
        "chunk_index": row["chunk_index"],
        "attempts": row["attempts"],
        "error": row["error"],
    }
