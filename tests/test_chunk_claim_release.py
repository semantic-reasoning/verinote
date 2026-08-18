# SPDX-License-Identifier: MPL-2.0
"""A claimed source chunk is released however the job pass ends (#475).

THE INVARIANT THESE TESTS PIN:

    Whether `process_extraction_job` returns or escapes by exception, no chunk
    THAT CALL CLAIMED is left `running`. Every claim it took has reached a resting
    place by the time the call is over.

Stated over the *chunks*, deliberately, and not as "once the job reaches a
terminal status...". `process_extraction_job` does not write the job row when a
non-`LLMError` escapes; its CALLERS do — the worker thread's `except Exception`
in `_start_source_extraction` (`web/app.py`), and since #488 the matching clause
in `cmd_sync` (`cli.py`). Phrasing the invariant over the job would therefore
make it a statement about callers rather than about this function, so the tests
below that need a job in its post-failure resting state call
`_worker_marks_job_failed` and say so. It would also be false where no caller
writes at all: `BaseException` escapes both clauses, and a call that loses the
ownership CAS writes nothing by design (both cases are pinned below).

Scoped to THIS CALL'S CLAIMS, equally deliberately, because a broader reading is
false and `extract.py` says so in two places:

* A CALL THAT NEVER TOOK THE JOB promises nothing about that job's chunks. Lose
  the ownership CAS and `process_extraction_job` raises `ExtractionJobBusyError`
  having touched nothing — the comment on that raise (`extract.py`) is explicit
  that the owner "may have a chunk in flight, so we must NOT reset its chunks"
  (#240). A `running` chunk therefore survives such a call, and must. This needs
  no concurrency to see: one call against a KB carrying a zombie `running` job
  does it, and `test_a_busy_job_is_left_entirely_to_its_owner` is that call.
* `BaseException` IS NOT COVERED, by the same design. `extract.py` states that
  Ctrl-C and `SystemExit` are "deliberately not caught", because the job does not
  reach a terminal status on that path either and the startup resume
  (`_resume_source_extraction_jobs` -> `rollback_extraction_job`) is what reclaims
  the chunk. `test_a_keyboard_interrupt_leaves_the_claim_to_the_resume_path` pins
  both halves: the claim survives the interrupt, and the resume path clears it.

THREE RESTING PLACES, not two. A claim ends as `done`, or as `failed` carrying its
cause, or — when the release is for a condition the chunk did not cause, i.e. the
fact-term sidecar being held by another process — back at `pending` with its
attempt refunded and the job rolled back around it. That third one has its own
module, `test_chunk_claim_sidecar_lock.py`, because what matters there is what the
release COSTS rather than that it happened.

`canceled` is out of scope, and not as a resting place this module declines to
cover — A CHUNK CANNOT BE `canceled` AT ALL. `source_chunks`'s CHECK constraint is
`('pending','running','done','failed')` (`schema.sql`), and writing the value to a
chunk raises `IntegrityError`. It belongs to `extraction_jobs`, whose CHECK does
list it, and even there no production code writes it. The two sites that NAME the
value do not behave alike, so they are worth keeping apart:

* `rollback_extraction_job` returns early, ahead of every write, and does not
  touch such a job at all;
* `mark_extraction_job_running` excludes the value from its UPDATE
  (`... AND status != 'canceled'`), so the status survives — but its
  `extraction_job_started` event row is written anyway, because that write is not
  conditioned on the UPDATE having matched. A `canceled` job therefore collects an
  event saying it started, with `before == after == canceled`. Out of scope here
  and unreachable today; it is #526, whose point is that `test_store.py` holds
  `rollback_extraction_job` to the standard this one misses.

Two further sites refuse such a job WITHOUT naming it — the two claim CASes,
`claim_pending_extraction_job` (`status = 'pending'`) and
`claim_extraction_job_for_retry` (`status IN ('pending','failed')`); both return
False. The only writers are tests setting the status by hand.

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
    ExtractionJobBusyError,
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
    """Stand-in for a caller's `except Exception`. NOT the code under test.

    `process_extraction_job` leaves the job row alone when a non-`LLMError`
    escapes; a caller's broad clause is what writes `failed` — the worker thread's
    in `_start_source_extraction` (`web/app.py`), or `cmd_sync`'s (`cli.py`,
    #488). A test that wants the job in its post-failure resting state has to
    perform that write itself, so it is spelled out here rather than hidden behind
    a fixture — nothing in `verinote/pipeline` will do it, which is why the tests
    below call `process_extraction_job` directly and then this.

    #524's branch carries a copy of this stand-in marked "DELETE THIS WHEN #488
    LANDS" — correct for that copy, whose caller is a `verinote sync` that will
    write the row itself once this PR lands. THIS copy stays either way: the tests
    below call `process_extraction_job` directly, and it never writes that row.
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

    The `fail_extraction_job` call below stands in for a caller's broad clause —
    the web worker's, or `cmd_sync`'s since #488 — performed here by the harness
    because `process_extraction_job` does not do it. It is a stand-in, not
    production code under test.
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
    # Both halves of the cause, so that writing a constant ("failed") or dropping
    # either half still leaves a chunk whose recorded reason is worth reading.
    error = s.source_chunks(job_id)[0]["error"]
    assert "RuntimeError" in error
    assert "completion write failed" in error
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
    along with it. Release the chunk any other way while leaving the job to be
    written `failed` — rewind the chunk to `pending`, or mark it `done` — and the
    job's `failed_chunks` drops to zero, planning falls through to "rebuild
    fresh", and chunk 0's completed work is paid for twice.

    The qualifier is load-bearing, because one release DOES rewind the chunk to
    `pending`: a locked fact-term sidecar (`test_chunk_claim_sidecar_lock.py`).
    That path is safe from this trap precisely because it does not stop at the
    chunk — it rolls the JOB back to `pending` too, and a `pending` job with
    unfinished chunks is a resume, which needs no failed chunk to be planned.
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

    TWO INDEPENDENT OUTCOMES CATCH THIS, and both assertions below are real.
    Delete the `except PolicyMissingError: raise` clause and the broad handler
    releases the chunk as `failed`; then

    * `fact_events` gains a `chunk_failed` row, and
    * the chunk is still `failed` at the end. `rollback_extraction_job` rewinds
      ONLY the in-flight `running` chunk (`store/db.py`: "`failed` chunks keep
      their error. Only the in-flight `running` chunk is returned to the queue" —
      its UPDATE is scoped `WHERE job_id = ? AND status = 'running'`), so a chunk
      the release already failed is not swept back to `pending`.

    The event assertion is written first because it names the thing that must not
    happen — a release fired — instead of inferring it from a state, not because
    the status assertion is too weak. Both are load-bearing; keep both.

    An `assert` on a monkeypatched `_release_claimed_chunk` would catch it too,
    but that watches for a named private call rather than for an outcome, and a
    later refactor that renames or inlines the helper would silently defang it.
    The spy is kept only as a redundant, non-load-bearing check.
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

    event_types = [
        row["event_type"]
        for row in s._conn.execute(
            "SELECT event_type FROM fact_events WHERE job_id = ? ORDER BY id", (job_id,)
        )
    ]
    assert "chunk_failed" not in event_types
    assert "extraction_job_rolled_back" in event_types

    assert _statuses(s, job_id) == ["done", "pending", "pending"]
    assert _running(s, job_id) == []
    job = s.get_extraction_job(job_id)
    assert job["status"] == "pending"
    assert "policy reset --force" in job["message"]
    summaries = [row["summary"] for row in s._conn.execute("SELECT summary FROM runs")]
    assert any("halted because this KB's policy file went missing" in x for x in summaries)
    assert released == []  # redundant with the event assertion above, not load-bearing
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
    job = s.get_extraction_job(job_id)
    assert job["status"] == "failed"
    # The job message carries the cause on THIS path and only this one. Running to
    # completion with a failed chunk is what reaches `finish_extraction_job(final=
    # True)` -> `_refresh_extraction_job`'s `status == 'failed'` branch, whose
    # `AND error != ''` lookup copies the first failed chunk's error into the job
    # message. A non-`LLMError` escape never gets here (nothing calls
    # `finish_extraction_job`), which is why the sibling tests assert on the chunk
    # row and the rendered page instead of on this string.
    assert "provider down" in job["message"]
    assert "1 chunk(s) failed" in job["message"]
    s.close()


# --- The two escapes the invariant cannot cover --------------------------------


def test_a_busy_job_is_left_entirely_to_its_owner(tmp_path):
    """A call that loses the ownership CAS promises nothing about that job's chunks.

    This is the exclusion the module docstring names first, and it is why the
    invariant is scoped to the claims a call TOOK. The setup is a KB in the state
    a live owner (or a crashed one) leaves behind: job `running`, chunk 0 claimed.
    A second pass must back off having written nothing — resetting that chunk is
    precisely the #240 bug, the same chunk sent to the LLM twice.

    No second thread is needed to reach it: the state is a property of the rows,
    not of the timing, so one call against those rows reproduces it exactly.

    The `client.calls` assertion is the one that says "nothing happened" rather
    than "nothing changed": a pass that claimed a chunk and then rewound it would
    leave the same statuses behind but would already have spent an LLM call.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    assert s.claim_pending_extraction_job(job_id) is True  # the owner takes it
    chunk_id = int(s.next_pending_chunk(job_id)["id"])
    assert s.mark_chunk_running(chunk_id) is not None  # ...and puts one in flight
    before = [(c["status"], c["attempts"]) for c in s.source_chunks(job_id)]
    client = _ChunkClient()

    with pytest.raises(ExtractionJobBusyError):
        process_extraction_job(s, client, job_id=job_id)

    assert _statuses(s, job_id) == ["running", "pending", "pending"]
    assert _running(s, job_id) == [chunk_id]  # the owner's claim, untouched
    assert [(c["status"], c["attempts"]) for c in s.source_chunks(job_id)] == before
    assert client.calls == []
    s.close()


def test_a_keyboard_interrupt_leaves_the_claim_to_the_resume_path(tmp_path):
    """Ctrl-C is not caught, on purpose — and something else is responsible for it.

    The second exclusion. `extract.py` says `BaseException` is "deliberately not
    caught" because the job does not reach a terminal status on that path either;
    the claim outlives the process and the startup resume is what reclaims it.
    Prose alone would leave that as an intention, so both halves are pinned here:
    the interrupt really does strand the claim, AND the named recovery really does
    clear it.

    The second half calls `rollback_extraction_job` directly — the same call
    `_resume_source_extraction_jobs` (`web/app.py`) makes on a job left `running`.
    It is a stand-in for that loop, not the code under test.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)

    with pytest.raises(KeyboardInterrupt):
        process_extraction_job(
            s, _ChunkClient(fail_on=2, exc=KeyboardInterrupt()), job_id=job_id
        )

    # chunk 0 finished, chunk 1's claim is still held: nothing released it
    assert _statuses(s, job_id) == ["done", "running", "pending"]

    s.rollback_extraction_job(job_id, "rewound at startup after an interrupted run")

    assert _statuses(s, job_id) == ["done", "pending", "pending"]
    assert _running(s, job_id) == []
    s.close()


# --- The other caller: the CLI ------------------------------------------------


def test_cli_sync_fails_the_job_and_never_leaves_the_chunk_claimed(
    tmp_path, monkeypatch
):
    """`verinote sync` writes the job row too now, and the chunk still holds.

    DOMAIN NOTE — READ BEFORE REUSING THIS SHAPE. The invariant this module pins
    is still stated over CHUNKS, and this test is still why: the release happens
    inside `process_extraction_job`, below every caller, so it holds no matter
    what the caller does with the job row. The job row is a caller's business,
    and the two callers now agree about it — `_start_source_extraction`
    (`web/app.py`) and `cmd_sync` each end with a broad clause that writes
    `failed`. That agreement is a fact about two call sites, not something the
    invariant may be re-phrased over: `BaseException` and a lost ownership CAS
    still leave a job in no terminal status at all (see the two tests above).

    (a) WAS A CHARACTERISATION AND IS NOW A GUARANTEE. It used to read `running`
    and carried #488's instruction to turn it red; #488 is that fix, and this is
    the update it asked for. The stranded row it described — no terminal status,
    so every later `sync` skipped the source as busy — is what the new clause
    ends. (b), the chunk, was a guarantee all along (#475) and is unchanged.

    THE MESSAGE IS PART OF THE ASSERTION. `str()` of a bare `ValueError()` is the
    empty string, so the handler type-qualifies the cause for the same reason
    `_release_claimed_chunk` does one layer down: a job whose `message` says only
    "analysis failed" names nothing a reader can act on.

    THIS ALSO CHANGES BEHAVIOUR, DELIBERATELY, and #475 already changed it once:
    before #475 the chunk stayed `running` with no failed chunk on the job, so
    `plan_source_extraction` never saw a retry budget and `verinote sync
    --recover` re-ran the same source every time. Now the chunk is `failed`, the
    attempt counts up, and after `MAX_CHUNK_ATTEMPTS` the job is surfaced as
    exhausted and skipped. What #488 adds on top is that the job reaches a
    terminal status at all, so the NEXT ordinary `sync` retries this source
    instead of skipping it — `tests/test_job_resume.py` measures that end to end.
    """
    from verinote import cli

    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    src = tmp_path / "note.txt"
    src.write_text(
        "alpha beta gamma delta epsilon\n\nzeta eta theta iota kappa",
        encoding="utf-8",
    )
    assert cli.main(["ingest", str(src)]) == 0
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: _ChunkClient(fail_on=1, exc=ValueError("boom")),
    )

    with pytest.raises(ValueError):
        cli.main(["sync"])

    store = Store(tmp_path / "kb.sqlite")
    jobs = list(store._conn.execute("SELECT id, status, message FROM extraction_jobs"))
    assert len(jobs) == 1
    job_id = int(jobs[0]["id"])
    # (a) GUARANTEE (#488): `cmd_sync`'s broad clause writes the job row before
    # letting the exception out, so the source is not stranded behind a `running`
    # job no later sync will touch.
    assert jobs[0]["status"] == "failed"
    assert "ValueError" in jobs[0]["message"]  # the type, since str() would be ""
    assert "boom" in jobs[0]["message"]
    # (b) GUARANTEE (#475): the chunk is released regardless of the caller
    chunks = store.source_chunks(job_id)
    assert len(chunks) > 1, "the note must split, or 'first chunk' means nothing"
    assert chunks[0]["status"] == "failed"
    assert chunks[0]["attempts"] == 1
    assert [c for c in chunks if c["status"] == "running"] == []
    store.close()


def test_a_source_that_crashes_does_not_bury_a_sibling_job_that_finished(
    tmp_path, monkeypatch
):
    """The handler writes the job it was handling, and no other.

    Two sources, one job each; the first finishes, the second crashes. Extraction
    is per-source and so is its resting state: a handler that reached for "the
    job" without owning it, or a rollback of the batch, would take the finished
    source down with the crashed one.

    IT DOES NOT DISTINGUISH WHERE THE HANDLER IS ATTACHED, and it should not be
    read as doing so. Measured: move the broad clause out to wrap the whole
    `for source in registered:` loop and this test still passes — the loop-bound
    `job_id` at crash time is the crashing source's either way, and the status
    re-read declines the finished one. What does catch that move is
    `test_a_halt_never_provokes_a_write_even_if_the_job_is_left_running`, for an
    unrelated reason: a loop-level BROAD clause sits outside the re-raise clauses
    above, so it swallows their `raise` and writes to a halted KB.

    AND ONLY THAT MOVE. Carry the re-raise clauses out to loop level along with
    the broad one and nothing in the suite distinguishes the two placements
    (measured: 135 passed). "The suite covers where the handler is attached" is
    therefore false however it is phrased; what it covers is one specific way of
    getting the attachment wrong.

    THE SIBLING'S CANDIDATE IS ASSERTED TOO, not just its job status. A `done` job
    whose facts were rolled back is not a survivor.
    """
    from verinote import cli

    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "200")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    first = tmp_path / "a.txt"
    first.write_text("alpha beta gamma", encoding="utf-8")
    second = tmp_path / "b.txt"
    second.write_text("bravo delta epsilon", encoding="utf-8")
    assert cli.main(["ingest", str(first)]) == 0
    assert cli.main(["ingest", str(second)]) == 0

    class _CrashesOnTheSecondSource:
        name = "stub"

        def __init__(self):
            self.seen: list[str] = []

        def extract_facts(self, *, source_text: str, schema_hint: str = ""):
            self.seen.append(source_text.split()[0])
            if source_text.startswith("bravo"):
                raise ValueError("boom")
            return [ExtractedFact(source_text.split()[0], "seen_in", "source", 0.9)]

    client = _CrashesOnTheSecondSource()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    with pytest.raises(ValueError):
        cli.main(["sync"])

    assert client.seen == ["alpha", "bravo"], "a.txt must be the one that finished"
    store = Store(tmp_path / "kb.sqlite")
    rows = list(
        store._conn.execute(
            "SELECT s.path AS path, j.status AS status FROM extraction_jobs j "
            "JOIN sources s ON s.id = j.source_id ORDER BY j.id"
        )
    )
    assert [(r["path"], r["status"]) for r in rows] == [
        ("sources/a.txt", "done"),
        ("sources/b.txt", "failed"),
    ]
    assert [f["subject"] for f in store.facts()] == ["alpha"]
    store.close()


def test_a_prompt_that_cannot_be_read_leaves_the_untouched_job_pending(
    tmp_path, monkeypatch
):
    """A failure BEFORE the job-owning call must not write the job row.

    `cmd_sync` creates the job row — this source has none to continue — and then
    resolves the extraction schema hint, which reads `policy/prompts/` off disk.
    `extraction_schema_hint` wraps `PromptError` and nothing else, so an override
    that is not valid UTF-8 escapes as the `UnicodeDecodeError` `Path.read_text`
    raised — a real path, and one that reaches this line before
    `process_extraction_job` is entered. The job is left exactly as planning found
    it — `pending` here, `failed` on a retry pass (both measured): no chunk was
    claimed, nothing was attempted, and fixing the file must let the next sync
    pick it up untouched.

    TWO THINGS KEEP THAT TRUE and this test does not distinguish them, deliberately
    — it asserts the outcome both are there for. The hint is resolved OUTSIDE the
    `try`, so the handler never sees this exception; and the handler re-reads the
    job's status before writing, so it would decline a job that is not `running`,
    which this one is not. Belt
    and braces on a row that must not be buried before it has done anything.
    """
    from verinote import cli
    from verinote.prompts.library import prompt_override_path

    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    src = tmp_path / "note.txt"
    src.write_text("alpha beta gamma delta epsilon", encoding="utf-8")
    assert cli.main(["ingest", str(src)]) == 0
    override = prompt_override_path(tmp_path, "extraction-limit-hint")
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_bytes(b"at most {max_facts} facts \xff\xfe and a bad byte")
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: _ChunkClient(),  # must never be reached
    )

    with pytest.raises(UnicodeDecodeError):
        cli.main(["sync"])

    store = Store(tmp_path / "kb.sqlite")
    jobs = list(store._conn.execute("SELECT id, status FROM extraction_jobs"))
    assert len(jobs) == 1  # the row was created before the read failed
    assert jobs[0]["status"] == "pending"
    chunks = store.source_chunks(int(jobs[0]["id"]))
    assert [c["status"] for c in chunks] == ["pending"] * len(chunks)
    assert [c["attempts"] for c in chunks] == [0] * len(chunks)
    store.close()


def test_a_job_that_already_finished_is_not_buried_by_a_later_failure(
    tmp_path, monkeypatch
):
    """The status re-read, and the regression it exists to prevent.

    `mark_chunk_done` writes the job to `done` once the last chunk lands, and
    `finish_extraction_job` runs AFTER that. So an error there — a sqlite/WAL-class
    failure, no LLM involved — escapes `process_extraction_job` with the job
    already `done`, every chunk complete, and the candidates committed.

    A broad clause that wrote unconditionally would record that as
    `failed: analysis failed`. It is the same hazard `_release_claimed_chunk`
    names one layer down (`mark_chunk_done` can raise after the chunk is `done`),
    and the same answer: re-read the status and write only a job this call still
    owns. Without the re-read the fix is a pure regression on this path — before
    #488 nothing wrote the job row here at all, so it stayed `done`.

    `web/app.py` HAS NO EQUIVALENT, AND BURIES THE JOB ON THIS PATH. Its two local
    guards wrap the stale-citation sweep and auto-accept only; `process_extraction_job`
    itself sits bare inside the worker's try, and the worker's broad clause calls
    `fail_extraction_job` with no status re-read at all. Driven through a real
    worker, this same scenario leaves `failed: analysis failed: ...` over a job
    that is `done` with its chunk complete. Not this change's doing and not its
    scope — it is #525 — but nothing here should be read as saying the web path is
    already safe.
    """
    from verinote import cli

    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    src = tmp_path / "note.txt"
    src.write_text(
        "alpha beta gamma delta epsilon\n\nzeta eta theta iota kappa",
        encoding="utf-8",
    )
    assert cli.main(["ingest", str(src)]) == 0
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _ChunkClient())

    def _boom(self, job_id):
        raise ValueError("the job row is already done by the time we get here")

    monkeypatch.setattr(Store, "finish_extraction_job", _boom)

    with pytest.raises(ValueError):
        cli.main(["sync"])

    store = Store(tmp_path / "kb.sqlite")
    jobs = list(store._conn.execute("SELECT id, status, message FROM extraction_jobs"))
    assert len(jobs) == 1
    job_id = int(jobs[0]["id"])
    # THE ASSERTION THIS TEST EXISTS FOR: the finished run was not overwritten.
    assert jobs[0]["status"] == "done"
    assert "analysis failed" not in jobs[0]["message"]
    assert _statuses(store, job_id) == ["done", "done"]
    # both chunks' candidates are committed — a `failed` here would be a lie
    assert [f["subject"] for f in store.facts()] == [
        "alpha beta gamma delta epsilon",
        "zeta eta theta iota kappa",
    ]
    store.close()


def test_a_halt_never_provokes_a_write_even_if_the_job_is_left_running(
    tmp_path, monkeypatch
):
    """The re-raise clauses, on the only shape that can distinguish them.

    #194's rule is that a halted KB is not written to. `cmd_sync`'s
    `except PolicyMissingError` is what keeps that rule at this call site — and
    on the halt path production actually takes, it cannot be caught doing it:
    `process_extraction_job` rewinds the job to `pending` before raising, so the
    broad clause's status re-read declines to write and the outcome is the same
    with the clause deleted. Measured, and the reason this test does not use a
    real halt: with a real one, deleting the clause changes nothing.

    THAT REWIND IS A PROPERTY OF `extract.py`, NOT OF THIS FILE'S SUBJECT. The
    rule here has to hold without it, so the stub below removes it: the job is
    left `running` — the state a halt raised anywhere that does not rewind would
    produce — and the assertion is that `cmd_sync` still writes nothing. Delete
    the clause and the broad one finds a `running` job and buries the halted KB's
    job as "analysis failed", which is the #194 violation with an
    `extraction_job_failed` row to prove it.

    THIS IS ORTHOGONAL TO THE STATUS RE-READ, and both are needed.
    `test_a_job_that_already_finished_is_not_buried_by_a_later_failure` covers the
    re-read: do not bury a job that finished. This one covers the clauses: a halt
    must not provoke a write at all, whatever state the job is in. Neither
    substitutes for the other — with the re-read in place and the clauses gone,
    only this test goes red.

    The same argument covers `except DuckDBFactTermStoreLockedError`, which sits
    beside it for the same reason and is likewise invisible on today's rewinding
    path. `PolicyMissingError` is the one exercised here because #194 states the
    rule in the strongest terms.
    """
    from verinote import cli
    import verinote.pipeline as pipeline

    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    src = tmp_path / "note.txt"
    src.write_text(
        "alpha beta gamma delta epsilon\n\nzeta eta theta iota kappa",
        encoding="utf-8",
    )
    assert cli.main(["ingest", str(src)]) == 0
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _ChunkClient())

    def _halts_without_rewinding(store, client, *, job_id, **kwargs):
        # Leaves the job in the state the real call leaves it in — `running`,
        # message "Analyzing chunks...", one `extraction_job_started` event — and
        # then halts WITHOUT the rollback. It is NOT the real claim: production
        # takes the job through `claim_pending_extraction_job`, an atomic CAS on
        # `status = 'pending'` that also reclaims stray chunks. None of that
        # affects what is asserted below, but the line is a stand-in, not a
        # reproduction. `cmd_sync` imports `process_extraction_job` from
        # `verinote.pipeline` inside the function body, so that is the attribute
        # to replace.
        store.mark_extraction_job_running(job_id)
        raise PolicyMissingError("the KB policy file is missing")

    monkeypatch.setattr(pipeline, "process_extraction_job", _halts_without_rewinding)

    assert cli.main(["sync"]) == 2  # the halt diagnosis, unchanged by any of this

    store = Store(tmp_path / "kb.sqlite")
    jobs = list(store._conn.execute("SELECT id, status, message FROM extraction_jobs"))
    assert len(jobs) == 1
    job_id = int(jobs[0]["id"])
    # THE ASSERTIONS THIS TEST EXISTS FOR: nothing was written to the halted KB.
    assert jobs[0]["status"] == "running"
    assert "analysis failed" not in jobs[0]["message"]
    events = [
        row["event_type"]
        for row in store._conn.execute(
            "SELECT event_type FROM fact_events WHERE job_id = ? ORDER BY id", (job_id,)
        )
    ]
    assert "extraction_job_failed" not in events
    store.close()
