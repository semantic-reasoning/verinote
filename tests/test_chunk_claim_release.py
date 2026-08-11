# SPDX-License-Identifier: MPL-2.0
"""A claimed source chunk is released however the job pass ends (#475).

THE INVARIANT THESE TESTS PIN:

    Whether `process_extraction_job` returns or escapes by exception, immediately
    after the call none of that job's chunks are `running`.

Stated over the *chunks*, deliberately, and not as "once the job reaches a
terminal status...". `process_extraction_job` does not write the job row when a
non-`LLMError` escapes — the web worker's `except Exception` does
(`web/app.py:1753-1755`), and the CLI does not do it at all. Phrasing the
invariant over the job would make it a statement about a caller, so the tests
below that need a job in its post-failure resting state call
`_worker_marks_job_failed` and say so.

`canceled` is out of scope: it exists in the `schema.sql` CHECK constraint but no
production code sets it.

The failure this guards is real and observed: a chunk claimed (`status='running'`,
one attempt burned, `error=''`) and then abandoned when an exception that was not
an `LLMError` walked out of the loop. Nothing resets such a chunk, so the source
could neither finish nor be re-synced.

The exceptions injected here are plain builtins raised from the LLM stub. That is
on purpose — the guard must not depend on any particular adapter normalising its
errors into `LLMError` (#474), or it goes vacuous the moment that adapter is
fixed.
"""

import pytest

from verinote.llm.base import ExtractedFact, LLMError
import verinote.pipeline.extract as extract_mod
from verinote.pipeline.extract import (
    ExtractionJobPlan,
    MAX_CHUNK_ATTEMPTS,
    create_chunked_extraction_job,
    plan_source_extraction,
    process_extraction_job,
)
from verinote.pipeline.policy_state import PolicyMissingError
from verinote.store import Store

SOURCE_TEXT = "alpha\n\nbeta\n\ngamma"
CHUNK_CHARS = 8
CHUNK_OVERLAP_CHARS = 0
PROVIDER = "fake"
MODEL = "m"


class _ChunkClient:
    """One fact per chunk, with an exception injected at a chosen call.

    `fail_on` counts `extract_facts` calls from 1. The chunk texts carry no role
    cue, so `_extract_chunk_facts` makes exactly one call per chunk and the count
    is the chunk ordinal.
    """

    name = "stub"

    def __init__(self, *, fail_on: int | None = None, exc=None):
        self.calls: list[str] = []
        self._fail_on = fail_on
        self._exc = exc

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls.append(source_text)
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise self._exc
        return [ExtractedFact(source_text.strip(), "seen_in", "source", 0.9)]


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _three_chunk_job(store: Store) -> tuple[int, int]:
    """A real job over three chunks — `alpha`, `beta`, `gamma`.

    Built through `create_chunked_extraction_job` with explicit chunk settings so
    that `_plan` can hand `plan_source_extraction` the very same artifact,
    provider, model and chunk config and clear its staleness gates.
    """
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
    assert [c["text"] for c in store.source_chunks(job_id)] == ["alpha", "beta", "gamma"]
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


def _worker_marks_job_failed(store: Store, job_id: int, message: str) -> None:
    """Stand-in for the web worker. NOT the code under test.

    `process_extraction_job` leaves the job row alone when a non-`LLMError`
    escapes; `web/app.py:1753-1755` is what writes `failed`. A test that wants the
    job in its post-failure resting state has to perform that write itself, so it
    is spelled out here rather than hidden behind a fixture — nothing in
    `verinote/pipeline` will do it.
    """
    store.fail_extraction_job(job_id, message)


def _statuses(store: Store, job_id: int) -> list[str]:
    return [c["status"] for c in store.source_chunks(job_id)]


def _running(store: Store, job_id: int) -> list[int]:
    return [int(c["id"]) for c in store.source_chunks(job_id) if c["status"] == "running"]


# --- T1: the invariant, with the failure originating in `_extract_chunk` --------


@pytest.mark.parametrize("exc_type", [ValueError, RuntimeError, KeyError])
def test_no_chunk_stays_claimed_when_a_chunk_raises_a_non_llm_error(tmp_path, exc_type):
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    client = _ChunkClient(fail_on=2, exc=exc_type("boom"))

    # The exception must still reach the caller: releasing the claim is not the
    # same as swallowing an unmodelled failure. Without this the loop could
    # `continue` past anything at all and the job would report success.
    with pytest.raises(exc_type):
        process_extraction_job(s, client, job_id=job_id)

    assert _running(s, job_id) == []
    assert _statuses(s, job_id) == ["done", "failed", "pending"]
    # chunk 0's work survives — the release must not touch a completed chunk
    assert [f["subject"] for f in s.facts()] == ["alpha"]
    s.close()


def test_terminal_job_has_no_running_chunk_after_a_non_llm_error(tmp_path):
    """T1's invariant restated the way an operator sees it, once the job is `failed`.

    The `fail_extraction_job` call below is the web worker's write, performed here
    by the harness because `process_extraction_job` does not do it. It is a
    stand-in, not production code under test.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)

    with pytest.raises(ValueError):
        process_extraction_job(s, _ChunkClient(fail_on=2, exc=ValueError("boom")), job_id=job_id)
    _worker_marks_job_failed(s, job_id, "analysis failed: boom")

    job = s.get_extraction_job(job_id)
    assert job["status"] == "failed"
    assert _running(s, job_id) == []
    # the symptom in #475 was `failed=0` on a `failed` job — the claim never landed
    assert job["failed_chunks"] == 1
    s.close()


# --- T2: the invariant, with the failure originating in `mark_chunk_done` -------


def test_no_chunk_stays_claimed_when_mark_chunk_done_raises(tmp_path, monkeypatch):
    """`mark_chunk_done` is inside the claim, so it must be inside the `try`.

    A `try` that wraps only the LLM call leaves this write unguarded, and a chunk
    whose completion write failed is exactly as stranded as one whose LLM call
    did.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)

    def _boom(chunk_id, *, candidates=0):
        raise RuntimeError("completion write failed")

    monkeypatch.setattr(s, "mark_chunk_done", _boom)

    with pytest.raises(RuntimeError):
        process_extraction_job(s, _ChunkClient(), job_id=job_id)

    assert _running(s, job_id) == []
    assert _statuses(s, job_id) == ["failed", "pending", "pending"]
    s.close()


def test_a_chunk_already_written_done_is_not_flipped_to_failed(tmp_path, monkeypatch):
    """The release asks whether the claim is still held — it does not re-decide.

    `mark_chunk_done` writes in several steps and can raise *after* the chunk row
    already says `done` (the job's `candidate_count` update, or
    `_refresh_extraction_job`). Overwriting that with `failed` would destroy real
    work. Counters are deliberately not asserted here: the stand-in aborts
    part-way through `mark_chunk_done`, so they are mid-write by construction.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)

    def _done_then_boom(chunk_id, *, candidates=0):
        s._conn.execute(
            "UPDATE source_chunks SET status = 'done' WHERE id = ?", (chunk_id,)
        )
        raise RuntimeError("counter update failed")

    monkeypatch.setattr(s, "mark_chunk_done", _done_then_boom)

    with pytest.raises(RuntimeError):
        process_extraction_job(s, _ChunkClient(), job_id=job_id)

    assert _running(s, job_id) == []
    assert _statuses(s, job_id) == ["done", "pending", "pending"]
    s.close()


# --- T3: the released chunk says why ------------------------------------------


def test_a_released_chunk_records_a_cause_even_for_an_empty_message(tmp_path):
    """`str(ValueError())` is `''`, and an empty `error` renders as a bare "failed".

    `sources.html` falls back to the literal word "failed" when `error` is empty,
    so a release that copied the message verbatim would reproduce the very
    `error=''` chunk #475 reports. The exception type is therefore part of the
    recorded cause.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)

    with pytest.raises(ValueError):
        process_extraction_job(s, _ChunkClient(fail_on=2, exc=ValueError()), job_id=job_id)

    error = s.source_chunks(job_id)[1]["error"]
    assert error != ""
    assert "ValueError" in error
    s.close()


def test_the_recorded_cause_reaches_the_sources_page(tmp_path):
    """The other half of T3: the cause has to be visible, not merely stored.

    Rendered through `create_app` + `TestClient` so the assertion runs against the
    real `_source_inspector_rows` -> `sources.html` path rather than a hand-built
    context dict.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from verinote.config import Config
    from verinote.web import create_app

    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider=PROVIDER,
        model=MODEL,
        api_key=None,
        base_url=None,
    )
    app = create_app(cfg)
    store = app.state.store
    _source_id, job_id = _three_chunk_job(store)
    with pytest.raises(ValueError):
        process_extraction_job(
            store, _ChunkClient(fail_on=2, exc=ValueError()), job_id=job_id
        )
    _worker_marks_job_failed(store, job_id, "analysis failed: ")  # worker stand-in

    html = TestClient(app).get("/sources").text

    assert "chunk 1</span> ValueError" in html


# --- T4/T5: what the released chunk costs, and what it preserves ---------------


def test_a_released_chunk_burns_exactly_one_attempt(tmp_path):
    """The release must not spend the retry budget faster than a modelled failure.

    Three passes take a chunk from one attempt to exhausted, which is the same
    budget `MAX_CHUNK_ATTEMPTS` gives an `LLMError`. Each pass ends with the
    worker stand-in writing `failed`, because the retry claim only accepts a
    `pending`/`failed` job.
    """
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)

    with pytest.raises(ValueError):
        process_extraction_job(s, _ChunkClient(fail_on=1, exc=ValueError("boom")), job_id=job_id)
    _worker_marks_job_failed(s, job_id, "analysis failed: boom")

    assert s.source_chunks(job_id)[0]["attempts"] == 1
    assert _plan(s, source_id) == ExtractionJobPlan(retry_job_id=job_id)

    for expected_attempts, expected_plan in (
        (2, ExtractionJobPlan(retry_job_id=job_id)),
        (3, ExtractionJobPlan(exhausted_job_id=job_id)),
    ):
        with pytest.raises(ValueError):
            process_extraction_job(
                s,
                _ChunkClient(fail_on=1, exc=ValueError("boom")),
                job_id=job_id,
                retry=True,
                retry_max_attempts=MAX_CHUNK_ATTEMPTS,
            )
        _worker_marks_job_failed(s, job_id, "analysis failed: boom")
        assert s.source_chunks(job_id)[0]["attempts"] == expected_attempts
        assert _running(s, job_id) == []
        assert _plan(s, source_id) == expected_plan

    s.close()


def test_a_released_job_is_retried_rather_than_rebuilt_from_scratch(tmp_path):
    """Releasing as `failed` is what keeps the finished chunk's work.

    Compared as a whole `ExtractionJobPlan` on purpose: `retry_job_id` alone would
    also be satisfied if `resume_job_id`/`exhausted_job_id`/`busy_job_id` came
    along with it. Release the chunk any other way — rewind it to `pending`, or
    mark it `done` — and the job's `failed_chunks` drops to zero, planning falls
    through to "rebuild fresh", and chunk 0's completed work is paid for twice.
    """
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)

    with pytest.raises(RuntimeError):
        process_extraction_job(
            s, _ChunkClient(fail_on=2, exc=RuntimeError("boom")), job_id=job_id
        )
    _worker_marks_job_failed(s, job_id, "analysis failed: boom")

    assert _plan(s, source_id) == ExtractionJobPlan(retry_job_id=job_id)

    # Corroborating, not load-bearing: the retry pass re-reads chunks 1 and 2 only.
    retry_client = _ChunkClient()
    process_extraction_job(
        s,
        retry_client,
        job_id=job_id,
        retry=True,
        retry_max_attempts=MAX_CHUNK_ATTEMPTS,
    )
    assert retry_client.calls == ["beta", "gamma"]
    assert _statuses(s, job_id) == ["done", "done", "done"]
    s.close()


# --- T6/T7: the two paths that already worked, unchanged -----------------------


def test_a_halted_kb_still_rewinds_the_whole_job_without_failing_the_chunk(
    tmp_path, monkeypatch
):
    """`PolicyMissingError` is not this chunk's failure and must skip the release.

    The outer handler rewinds the entire job (`_halt_extraction_job` ->
    `rollback_extraction_job`, which returns the in-flight chunk to the queue).
    Releasing it as `failed` here would both double-write and label a healthy
    chunk as broken.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    released: list[int] = []
    real_release = extract_mod._release_claimed_chunk

    def _spy(store, chunk_id, exc):
        released.append(chunk_id)
        return real_release(store, chunk_id, exc)

    monkeypatch.setattr(extract_mod, "_release_claimed_chunk", _spy)

    with pytest.raises(PolicyMissingError):
        process_extraction_job(
            s,
            _ChunkClient(fail_on=2, exc=PolicyMissingError("policy file is missing")),
            job_id=job_id,
        )

    assert released == []
    assert _statuses(s, job_id) == ["done", "pending", "pending"]
    assert _running(s, job_id) == []
    job = s.get_extraction_job(job_id)
    assert job["status"] == "pending"
    assert "policy reset --force" in job["message"]
    summaries = [row["summary"] for row in s._conn.execute("SELECT summary FROM runs")]
    assert any("halted because this KB's policy file went missing" in x for x in summaries)
    s.close()


def test_an_llm_error_still_fails_only_its_own_chunk_and_the_pass_continues(tmp_path):
    """The modelled failure keeps its old behaviour: `failed`, verbatim message, next chunk."""
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)

    result = process_extraction_job(
        s, _ChunkClient(fail_on=2, exc=LLMError("provider down")), job_id=job_id
    )

    assert _statuses(s, job_id) == ["done", "failed", "done"]
    assert _running(s, job_id) == []
    assert s.source_chunks(job_id)[1]["error"] == "provider down"
    assert (result.completed_chunks, result.failed_chunks) == (2, 1)
    assert s.get_extraction_job(job_id)["status"] == "failed"
    s.close()
