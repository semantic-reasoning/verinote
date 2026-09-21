# SPDX-License-Identifier: MPL-2.0
"""A rewind that cannot write still dispatches the ORIGINAL exception (#658).

THE CONTRACT THESE TESTS PIN. The three host-condition handlers in
`process_extraction_job` (`PolicyMissingError`, `DuckDBFactTermStoreLockedError`,
`PromptUnavailableError`) each make several autocommit DB statements and then
re-raise the ORIGINAL exception, because the callers dispatch on its TYPE: the
web worker's log-only clause above its `except Exception`, or the CLI's clean rc
(`cmd_sync` rc=2 for the halt, `main` rc=1 for the two back-offs). When one of
those statements fails, the store error used to become the OUTERMOST exception
with the original demoted to `__context__`, so the callers' generic clauses
fired instead: the web worker wrote `failed` over a committed `pending` rewind —
or into a halted KB, for the halt — and `cmd_sync` wrote `failed` over a halted
KB's job and left a raw traceback. A condition of the host filed as the job's
own failure (#269), a write to the very KB the halt exists to protect (#194).

The fix (decided in #658, D1–D5): a statement inside a rewind that raises is
logged in full — that log line is the inner error's ONLY record, because the
re-raised original escapes with a clean `__context__` (D2, `exc_info=True` is
load-bearing) — and the ORIGINAL is what escapes, so the caller still dispatches
on its type (D1). Whatever a rewind statement raises is logged and demoted; a
host-condition handler never re-dispatches a new host condition. The job rests
`running` (nothing committed — recover with `verinote sync --recover` or a UI
boot, #242) or `pending` (the resume loop continues it, #524); the run summary
is ABSENT, never a synthesized number (#482, D4); and the web guard's refusal
set stays as-is, because the live pre-claim `pending`-write behavior the guard
documents would be silently dropped by widening it (D5 — pinned here by keeping
the committed `pending` unwritten AND by the two live-behavior tests this module
must not break).

FAULT INJECTION. The armed-once class patch the codebase already uses
(`_ReleaseWriteFails`, `tests/test_job_resume.py`): the first call to one
`Store` method raises `sqlite3.OperationalError`, then it heals. Each injected
seam is reached exactly once on these paths, so an armed-once patch cannot fire
anywhere else in the pass.

The D5 regression guards themselves (`test_worker_still_fails_the_job_on_an_ordinary_error`,
`test_worker_records_a_retry_pre_claim_failure_over_a_failed_job`) live in
`tests/test_web.py` and must stay green: they pin the live `failed`-write
behavior on a `pending` job that D5's decision preserves.
"""

import logging
import sqlite3
import threading
import time

import pytest

pytest.importorskip("fastapi")

import verinote.cli as cli  # noqa: E402
import verinote.web.app as webapp  # noqa: E402
from verinote.config import Config  # noqa: E402
from verinote.engine import DEFAULT_POLICY  # noqa: E402
from verinote.llm.base import ExtractedFact  # noqa: E402
from verinote.pipeline.extract import (  # noqa: E402
    ExtractionJobPlan,
    create_chunked_extraction_job,
    plan_source_extraction,
    process_extraction_job,
)
from verinote.pipeline.policy_state import (  # noqa: E402
    POLICY_RELPATH,
    PolicyMissingError,
    policy_sha256,
)
from verinote.prompts import PromptUnavailableError  # noqa: E402
from verinote.store import Store  # noqa: E402
from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreLockedError  # noqa: E402
from verinote.web import create_app  # noqa: E402

SOURCE_TEXT = "alpha CTO\n\nbeta CEO\n\ngamma CFO"
CHUNK_CHARS = 9
CHUNK_OVERLAP_CHARS = 0
PROVIDER = "fake"
MODEL = "m"


# --- shared fixtures ---------------------------------------------------------


class _FailsOnCall:
    """A client whose Nth `extract_facts` call raises a named error.

    The named error is what the three host-condition clauses are built around,
    so it is raised verbatim (the real paths — a lost policy gate, the sidecar
    write, the hint resolution — all surface it to the same clauses).
    `tests/test_chunk_claim_release.py` drives the same clauses this way.
    """

    name = "stub"

    def __init__(self, *, fail_on: int = 1, exc: BaseException):
        self._fail_on = fail_on
        self._exc = exc
        self.calls: list[str] = []
        self.raised = threading.Event()

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls.append(source_text)
        if len(self.calls) == self._fail_on:
            self.raised.set()
            raise self._exc
        return [ExtractedFact(source_text.strip(), "seen_in", "source", 0.9)]


class _PlainClient:
    """One fact per chunk, never fails — for paths where the client is never called.

    The broken-override path resolves the focused-role hint before any provider
    call, so the client here only has to exist (the sibling module asserts
    `client.calls == 0` on exactly that route).
    """

    name = "stub"

    def __init__(self):
        self.calls: list[str] = []

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls.append(source_text)
        return [ExtractedFact(source_text.strip(), "seen_in", "source", 0.9)]


class _FirstCallFails:
    """Armed-once: the first call to one `Store` method raises, then it heals.

    The same pattern as `_ReleaseWriteFails` (`tests/test_job_resume.py`). Each
    injected seam below is reached exactly once on its path, so the armed-once
    patch cannot fire anywhere else in the pass — the failure lands where the
    test says it lands.
    """

    def __init__(self, monkeypatch, method: str, message: str = "database is locked"):
        self.message = message
        self.armed = True
        real = getattr(Store, method)
        armed = self

        def failing(store, *args, **kwargs):
            if armed.armed:
                raise sqlite3.OperationalError(armed.message)
            return real(store, *args, **kwargs)

        monkeypatch.setattr(Store, method, failing)


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _three_chunk_job(store: Store) -> tuple[int, int]:
    source_id = store.add_source("sources/a.txt")
    job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        artifact_id=None,
        source_text=SOURCE_TEXT,
        provider=PROVIDER,
        model=MODEL,
        chunk_chars=CHUNK_CHARS,
        chunk_overlap_chars=CHUNK_OVERLAP_CHARS,
    )
    assert [c["text"] for c in store.source_chunks(job_id)] == ["alpha CTO", "beta CEO", "gamma CFO"]
    return source_id, job_id


def _plan(store: Store, source_id: int) -> ExtractionJobPlan:
    return plan_source_extraction(
        store,
        source_id=source_id,
        artifact_id=None,
        source_text=SOURCE_TEXT,
        provider=PROVIDER,
        model=MODEL,
        chunk_chars=CHUNK_CHARS,
        chunk_overlap_chars=CHUNK_OVERLAP_CHARS,
    )


def _event_types(store: Store, job_id: int) -> list[str]:
    return [
        row["event_type"]
        for row in store._conn.execute(
            "SELECT event_type FROM fact_events WHERE job_id = ? ORDER BY id", (job_id,)
        )
    ]


def _broken_override(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"focused role \xff\xfe and a bad byte")


def _job_kb(tmp_path) -> tuple[Config, int]:
    """A web KB with one pending job and a recorded, present policy."""
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    policy = tmp_path / POLICY_RELPATH
    with Store(cfg.db_path) as store:
        store.init_schema()
        sid = store.add_source("sources/a.txt")
        job_id = store.create_extraction_job(
            source_id=sid, provider="anthropic", model="m", total_chunks=1
        )
        store.add_source_chunks(job_id=job_id, source_id=sid, chunks=["some text"])
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(DEFAULT_POLICY, encoding="utf-8")
        store.record_policy_marker(policy_sha256(DEFAULT_POLICY), origin="scaffold")
    return cfg, job_id


def _job_row(cfg, job_id: int) -> dict:
    with Store(cfg.db_path) as store:
        store.init_schema()
        return dict(store.get_extraction_job(job_id))


def _job_event_types(cfg, job_id: int) -> list[str]:
    with Store(cfg.db_path) as store:
        store.init_schema()
        return _event_types(store, job_id)


def _cli_kb(tmp_path, monkeypatch) -> None:
    """A CLI KB with one ingested source, the shape the sibling CLI tests use."""
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", PROVIDER)
    monkeypatch.setenv("VERINOTE_MODEL", MODEL)
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    assert cli.main(["init"]) == 0
    source = tmp_path / "note.txt"
    source.write_text("alpha CTO\n\nbeta CEO\n\ngamma CFO", encoding="utf-8")
    assert cli.main(["ingest", str(source)]) == 0


def _cli_job_rows(tmp_path) -> list[dict]:
    s = Store(tmp_path / "kb.sqlite")
    rows = [dict(r) for r in s.source_extraction_jobs()]
    s.close()
    return rows


def _cli_job_events(tmp_path, job_id: int) -> list[str]:
    s = Store(tmp_path / "kb.sqlite")
    rows = _event_types(s, job_id)
    s.close()
    return rows


# --- D1: the caller dispatches on the ORIGINAL type ---------------------------


def test_d1_web_named_clause_fires_when_the_count_read_fails(tmp_path, monkeypatch, caplog):
    """D1, web half: the DuckDB clause fires — not the generic write.

    `run_candidate_count` is the handler's first statement; forcing it to fail
    is the issue's own D1 criterion. The worker's `except DuckDBFactTermStoreLockedError`
    clause is log-only, so NOTHING is written: no `failed` over the job, no
    `extraction_job_failed` event. (Before the fix, the store error escaped as
    the outermost exception and the generic clause wrote `failed`.) The state
    assertions are the dispatch evidence; the log lines corroborate.
    """
    cfg, job_id = _job_kb(tmp_path)
    client = _FailsOnCall(
        fail_on=1,
        exc=DuckDBFactTermStoreLockedError(
            "the fact-term store is locked by another process; wait and retry"
        ),
    )
    monkeypatch.setattr(webapp, "get_client", lambda cfg: client)
    _FirstCallFails(monkeypatch, "run_candidate_count")

    with caplog.at_level(logging.WARNING):
        create_app(cfg)
        assert client.raised.wait(timeout=2.0)
        time.sleep(0.3)  # let a (wrong) `failed` write land, if the dispatch regressed

    job = _job_row(cfg, job_id)
    assert job["status"] == "running", "the generic clause wrote over the job row"
    assert "analysis failed" not in job["message"]
    assert "extraction_job_failed" not in _job_event_types(cfg, job_id)
    # corroboration: the worker's NAMED clause fired, and the rewind failure
    # was logged in full (D2's log is the inner error's only record).
    text = caplog.text
    assert "paused (fact-term store locked" in text
    assert "rewind failed for extraction job" in text
    assert "OperationalError" in text


def test_d1_cli_clean_rc_when_the_count_read_fails(tmp_path, monkeypatch, capsys):
    """D1, CLI half: `cmd_sync`'s halt clause fires — rc=2, clean diagnosis.

    Before the fix the store error escaped outermost, `cmd_sync`'s broad
    clause wrote `failed` into the halted KB's job, and the user got a raw
    traceback. Now the halt's own diagnosis reaches stderr and the job row is
    untouched.
    """
    _cli_kb(tmp_path, monkeypatch)
    client = _FailsOnCall(fail_on=1, exc=PolicyMissingError("the policy file is missing"))
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)
    _FirstCallFails(monkeypatch, "run_candidate_count")

    rc = cli.main(["sync"])

    assert rc == 2, "the halt did not dispatch as a halt — the generic path took it"
    err = capsys.readouterr().err
    assert "policy" in err
    assert "Traceback" not in err
    jobs = _cli_job_rows(tmp_path)
    assert len(jobs) == 1
    assert jobs[0]["status"] != "failed", "a halted KB's job was written `failed`"
    assert "extraction_job_failed" not in _cli_job_events(tmp_path, int(jobs[0]["id"]))


def test_d1_prompt_error_escapes_as_the_prompt_error(tmp_path, monkeypatch):
    """D1, third type at the pipeline level: the original escapes, not the store error.

    The real broken-override path (`test_focused_role_prompt_unavailable.py`'s
    route) with the handler's first statement forced to fail: the escaping type
    must be `PromptUnavailableError`, never `sqlite3.OperationalError`.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    _broken_override(tmp_path / "policy" / "prompts" / "focused-role-extraction.md")
    _FirstCallFails(monkeypatch, "run_candidate_count")

    with pytest.raises(PromptUnavailableError):
        process_extraction_job(s, _PlainClient(), job_id=job_id)
    s.close()


def test_d1_cli_clean_rc_when_the_prompt_read_fails_mid_handler(tmp_path, monkeypatch, capsys):
    """D1, third type at the CALLER level (Critic's F8): `main`'s floor takes it.

    The broken override is the real production route for `PromptUnavailableError`;
    with the handler's first statement failing, `cli.main(["sync"])` must still
    land on `main`'s clean rc=1 diagnosis — not a sqlite traceback, and with the
    job row never written `failed`.
    """
    _cli_kb(tmp_path, monkeypatch)
    _broken_override(tmp_path / "policy" / "prompts" / "focused-role-extraction.md")
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _PlainClient())
    _FirstCallFails(monkeypatch, "run_candidate_count")

    rc = cli.main(["sync"])

    assert rc == 1, "the back-off did not dispatch as a back-off"
    err = capsys.readouterr().err
    assert "prompt" in err.lower()
    assert "Traceback" not in err
    jobs = _cli_job_rows(tmp_path)
    assert len(jobs) == 1
    assert jobs[0]["status"] != "failed"
    assert "extraction_job_failed" not in _cli_job_events(tmp_path, int(jobs[0]["id"]))


# --- D2: the duty on rewind failure is the full log — and only that ----------


def test_d2_rewind_failure_is_logged_in_full_and_the_original_escapes(tmp_path, monkeypatch, caplog):
    """D2: forcing `rollback_extraction_job` itself to fail.

    The escaping type is the ORIGINAL (D1), the job rests `running` (nothing
    committed — the #242 recovery paths apply), NO `extraction_job_rolled_back`
    event was written (log-only, not a durable record), and the log carries the
    inner error's full traceback — its only record, since the re-raised original
    escapes with a clean `__context__`.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    client = _FailsOnCall(
        fail_on=1,
        exc=DuckDBFactTermStoreLockedError("locked by a peer"),
    )
    _FirstCallFails(monkeypatch, "rollback_extraction_job")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(DuckDBFactTermStoreLockedError):
            process_extraction_job(s, client, job_id=job_id)

    assert s.get_extraction_job(job_id)["status"] == "running"
    assert "extraction_job_rolled_back" not in _event_types(s, job_id)
    records = [r for r in caplog.records if "rewind failed for extraction job" in r.getMessage()]
    assert records, "the rewind failure left no log record at all"
    record = records[0]
    assert record.levelno == logging.ERROR
    assert str(job_id) in record.getMessage()
    assert record.exc_info is not None, "exc_info is load-bearing: the log is the only record"
    assert record.exc_info[0] is sqlite3.OperationalError
    s.close()


# --- D3: a partial rewind rests recoverable -----------------------------------


def test_d3_mid_rollback_failure_rests_pending_and_the_plan_resumes(tmp_path, monkeypatch):
    """D3: the mid-rollback partial state is documented and recoverable.

    Forcing `_refresh_job_candidate_count` — inside `rollback_extraction_job`,
    after the chunk and job UPDATEs, before the event — leaves exactly the
    partial state the issue names: the job is `pending`, the rollback event was
    never written. The discriminating assertion is that the resume loop still
    recovers it: `plan_source_extraction` answers `resume_job_id`.
    """
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)
    client = _FailsOnCall(
        fail_on=1,
        exc=DuckDBFactTermStoreLockedError("locked by a peer"),
    )
    _FirstCallFails(monkeypatch, "_refresh_job_candidate_count")

    with pytest.raises(DuckDBFactTermStoreLockedError):
        process_extraction_job(s, client, job_id=job_id)

    assert s.get_extraction_job(job_id)["status"] == "pending"
    assert "extraction_job_rolled_back" not in _event_types(s, job_id)
    assert _plan(s, source_id) == ExtractionJobPlan(resume_job_id=job_id)
    s.close()


# --- D4: the summary is absent, never a synthesized number --------------------


def test_d4_summary_failure_leaves_the_summary_absent(tmp_path, monkeypatch):
    """D4: forcing `set_run_summary` to fail, the rollback fully committed.

    The original escapes, the job is `pending`, the rollback event exists, and
    the run's summary is ABSENT — no fallback count anywhere. The job message
    carries only the real `{completed}/{total}` the rollback read from the job
    row (no candidate figure at all), and nothing like a synthesized
    "0 candidate(s)" sentence exists.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    client = _FailsOnCall(fail_on=1, exc=PolicyMissingError("the policy file is missing"))
    _FirstCallFails(monkeypatch, "set_run_summary")

    with pytest.raises(PolicyMissingError):
        process_extraction_job(s, client, job_id=job_id)

    job = s.get_extraction_job(job_id)
    assert job["status"] == "pending"
    assert "policy reset --force" in job["message"]
    assert "candidate" not in job["message"], "the job message carries no candidate figure"
    assert "extraction_job_rolled_back" in _event_types(s, job_id)
    summaries = [row["summary"] for row in s._conn.execute("SELECT summary FROM runs")]
    assert all(summary in (None, "") for summary in summaries), (
        "a synthesized summary appeared where the write failed"
    )
    s.close()


# --- D5: a committed `pending` rewind is never written over -------------------


def test_d5_web_keeps_the_committed_pending_rewind_unwritten(tmp_path, monkeypatch):
    """D5, web half: the committed rewind survives the pass.

    The rollback fully committed (`pending` + event), then `set_run_summary`
    failed. Before the fix, the store error escaped outermost and the worker's
    generic clause wrote `failed` over that committed rewind. Now the original
    dispatches to the log-only clause, and the `pending` the rewind committed is
    exactly what the job row still says. The refusal set itself is unchanged —
    `tests/test_web.py`'s `test_worker_still_fails_the_job_on_an_ordinary_error`
    and `test_worker_records_a_retry_pre_claim_failure_over_a_failed_job` stay
    green and pin the live `pending`-write behavior this decision preserves.
    """
    cfg, job_id = _job_kb(tmp_path)
    client = _FailsOnCall(
        fail_on=1,
        exc=DuckDBFactTermStoreLockedError("the fact-term store is locked by another process"),
    )
    monkeypatch.setattr(webapp, "get_client", lambda cfg: client)
    _FirstCallFails(monkeypatch, "set_run_summary")

    create_app(cfg)
    assert client.raised.wait(timeout=2.0)
    time.sleep(0.3)  # let a (wrong) overwrite land, if the dispatch regressed

    job = _job_row(cfg, job_id)
    assert job["status"] == "pending", "the committed rewind was written over"
    assert "analysis failed" not in job["message"]
    assert "extraction_job_failed" not in _job_event_types(cfg, job_id)
