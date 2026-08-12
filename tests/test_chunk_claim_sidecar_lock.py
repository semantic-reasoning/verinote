# SPDX-License-Identifier: MPL-2.0
"""A claim released because the sidecar was locked costs the chunk nothing (#475).

THE DISTINCTION THESE TESTS PIN. `test_chunk_claim_release.py` establishes that a
claimed chunk is never left `running`. It does not say what the release should
cost, and for one class of failure the answer is "nothing": another process
holding the DuckDB fact-term sidecar is a condition of the host, not of the
chunk's content, and the error text itself instructs the reader to wait and retry.
Charging it as a chunk failure spends a retry attempt per occurrence, and
`MAX_CHUNK_ATTEMPTS` of them make `plan_source_extraction` give up on the source
for good — over a condition that clears by itself.

So this release has its own shape, and all of it is load-bearing:

* the CHUNK goes back to `pending` with its attempt refunded — the row exactly as
  `next_pending_chunk` handed it out;
* the JOB is rolled back to `pending` too, without which a `failed` job with zero
  failed chunks reads to planning as "rebuild fresh" and every finished chunk is
  re-sent to the LLM;
* the exception still propagates, so no caller mistakes this for a completed pass.

THREE OUTCOMES MUST STAY APART, and the tests below separate them on purpose:
`failed` (no dedicated clause at all), `pending` with the attempt kept (a clause
that rewinds but does not refund), and `pending` with the attempt refunded (what
is wanted). Only the middle one is subtle, and it is exactly the arrangement that
still gives up on a source: three locks and one real failure reach
`attempts = 4 >= MAX_CHUNK_ATTEMPTS`.

WHY A REAL PEER PROCESS, and not only the injected stub. DuckDB's single-writer
lock only bites across an OS process boundary — two connections inside one process
share its instance cache and never conflict — so an in-process stub can only
assert that the pipeline handles the exception it was handed. The peer test is
what keeps the stub honest about the exception being reachable at all: it holds
the real sidecar with a real second process and lets the real write path find it.
"""

import select
import subprocess
import sys
import time

import pytest

import verinote.cli as cli
from verinote.llm.base import ExtractedFact
from verinote.pipeline.extract import (
    ExtractionJobPlan,
    MAX_CHUNK_ATTEMPTS,
    create_chunked_extraction_job,
    plan_source_extraction,
    process_extraction_job,
)
from verinote.store import Store
from verinote.store import duckdb_fact_terms
from verinote.store.duckdb_fact_terms import (
    FACT_TERMS_FILENAME,
    DuckDBFactTermStoreLockedError,
)

SOURCE_TEXT = "alpha\n\nbeta\n\ngamma"
CHUNK_CHARS = 8
CHUNK_OVERLAP_CHARS = 0
PROVIDER = "fake"
MODEL = "m"

# Same shape as `test_duckdb_fact_terms_concurrency.py`: reading a child's stdout
# through `select` is POSIX-only, and so is the file-locking behaviour being
# exercised. The injected-stub tests carry no such dependency and run everywhere.
_posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="cross-process file-lock contention needs POSIX select/lock semantics",
)

# A raw DuckDB connection held open by another process — a `verinote ui` serving
# the same KB, or a second `verinote sync`. Post-#169 the store holds no
# connection between operations, so only a genuine outside process can hold this
# lock while our pipeline runs.
_HOLDER = """
import sys, time, duckdb
path, max_hold = sys.argv[1], float(sys.argv[2])
con = duckdb.connect(path)
con.execute(
    "CREATE TABLE IF NOT EXISTS fact_terms ("
    "fact_id BIGINT PRIMARY KEY, subject VARCHAR NOT NULL, "
    "rel VARCHAR NOT NULL, object VARCHAR NOT NULL, term_token VARCHAR)"
)
sys.stdout.write("READY\\n")
sys.stdout.flush()
time.sleep(max_hold)
con.close()
"""


def _spawn(tmp_path, source, *args):
    script = tmp_path / "sidecar_holder.py"
    script.write_text(source)
    return subprocess.Popen(
        [sys.executable, str(script), *[str(a) for a in args]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(tmp_path),
    )


def _readline_until(proc, token, timeout):
    end = time.monotonic() + timeout
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            return None
        line = proc.stdout.readline()
        if line == "":
            return None
        if token in line:
            return line


def _terminate(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


class _ChunkClient:
    """One fact per chunk; optionally raises a chosen exception at call `fail_on`.

    Call `n` is chunk `n`: the chunk texts carry no role cue, so
    `_extract_chunk_facts` makes exactly one call per chunk.
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


class _HoldsTheSidecarFromCall:
    """Starts a real peer process holding the sidecar, mid-job.

    The hold has to begin AFTER a chunk has completed, or there is no finished
    work to prove the release preserves. Timing it from inside the LLM stub is the
    only place with that control: by the time call `hold_from` returns, the peer
    owns the file and the write that follows is the one that finds it locked.
    """

    name = "stub"

    def __init__(self, tmp_path, sidecar, *, hold_from: int):
        self.calls: list[str] = []
        self.holder = None
        self._tmp_path = tmp_path
        self._sidecar = sidecar
        self._hold_from = hold_from

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls.append(source_text)
        if len(self.calls) == self._hold_from and self.holder is None:
            assert self._sidecar.is_file(), "no sidecar yet — nothing for a peer to hold"
            self.holder = _spawn(self._tmp_path, _HOLDER, self._sidecar, 60)
            assert _readline_until(self.holder, "READY", 20) is not None, (
                "the peer process never took the sidecar lock"
            )
        return [ExtractedFact(source_text.strip(), "seen_in", "source", 0.9)]


class _LocksOnPut:
    """The real sidecar store, except that put number `lock_on` reports it locked.

    A wrapper rather than a hand-written double: every other call goes to the real
    `DuckDBFactTermStore`, so the surrounding writes behave as they really do and
    only the one failure is injected.
    """

    def __init__(self, inner, *, lock_on: int):
        self._inner = inner
        self._lock_on = lock_on
        self.puts = 0

    def put_fact_terms(self, *args, **kwargs):
        self.puts += 1
        if self.puts == self._lock_on:
            raise DuckDBFactTermStoreLockedError(
                "the DuckDB fact-term store is locked by another process."
            )
        return self._inner.put_fact_terms(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _lock_the_sidecar_on_put(store: Store, *, lock_on: int) -> _LocksOnPut:
    """Route this store's sidecar writes through `_LocksOnPut`.

    `Store.fact_terms` builds the real store on first use and caches it in
    `_fact_terms`; replacing that attribute is what the property itself checks, so
    the wrapper is used from here on without disturbing anything else.
    """
    wrapper = _LocksOnPut(store.fact_terms, lock_on=lock_on)
    store._fact_terms = wrapper
    return wrapper


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


def _chunk_states(store: Store, job_id: int) -> list[tuple[str, int]]:
    return [(c["status"], int(c["attempts"])) for c in store.source_chunks(job_id)]


def _worker_marks_job_failed(store: Store, job_id: int, message: str) -> None:
    """Stand-in for a caller's `except Exception`. NOT code under test.

    `process_extraction_job` does not write the job row when a non-`LLMError`
    escapes; its callers do — `_start_source_extraction` (`web/app.py`), and
    `cmd_sync` (`cli.py`) since #488. A test that needs a job in its post-failure
    resting state performs that write itself.
    """
    store.fail_extraction_job(job_id, message)


# --- the release, and everything it must leave alone --------------------------


def test_a_locked_sidecar_requeues_the_chunk_and_rewinds_the_job(tmp_path):
    """All four outcomes of the release, in the one place they have to agree.

    The `resume_job_id` assertion is the one that catches a half-fix. Rewind the
    chunk without rewinding the job and every state below still looks reasonable
    in isolation — until a caller's broad clause writes `failed` over a job with
    no failed chunk, planning calls that an edge state, and the finished chunk is
    extracted a second time.
    """
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)
    _lock_the_sidecar_on_put(s, lock_on=2)  # chunk 0 lands; chunk 1 finds it locked

    with pytest.raises(DuckDBFactTermStoreLockedError):
        process_extraction_job(s, _ChunkClient(), job_id=job_id)

    # chunk 0 keeps its work and its spent attempt; chunk 1 is back exactly as it
    # was handed out — `pending`, no error, nothing charged
    assert _chunk_states(s, job_id) == [("done", 1), ("pending", 0), ("pending", 0)]
    assert s.source_chunks(job_id)[1]["error"] == ""
    assert s.get_extraction_job(job_id)["status"] == "pending"
    assert _plan(s, source_id) == ExtractionJobPlan(resume_job_id=job_id)
    assert [f["subject"] for f in s.facts()] == ["alpha"]
    s.close()


def test_a_locked_sidecar_records_no_chunk_event(tmp_path):
    """The requeue is not a chunk transition a reader has to be told about.

    `mark_chunk_failed` and the retry claim each leave a row someone may later
    query and need explained; this leaves none. What IS recorded is the job
    rollback, so the pause is not silent.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    _lock_the_sidecar_on_put(s, lock_on=1)

    with pytest.raises(DuckDBFactTermStoreLockedError):
        process_extraction_job(s, _ChunkClient(), job_id=job_id)

    event_types = [
        row["event_type"]
        for row in s._conn.execute(
            "SELECT event_type FROM fact_events WHERE job_id = ? ORDER BY id", (job_id,)
        )
    ]
    assert "chunk_failed" not in event_types
    assert "chunk_retried" not in event_types
    assert "extraction_job_rolled_back" in event_types
    job = s.get_extraction_job(job_id)
    assert "another process is holding this KB's fact-term store" in job["message"]
    s.close()


def test_a_locked_sidecar_leaves_the_whole_retry_budget_for_a_real_failure(tmp_path):
    """The refund, isolated: locks must not bring a real failure closer to giving up.

    Three locked passes first — the number of times the error message invites the
    operator to retry before `MAX_CHUNK_ATTEMPTS` would be gone — and then the
    chunk starts failing for real. It must still get its full three attempts.

    THIS IS THE TEST THAT SEPARATES the refund from a bare rewind. Requeue without
    `attempts - 1` and the three locks leave the chunk at three attempts, so the
    FIRST genuine failure reaches `attempts = 4 >= MAX_CHUNK_ATTEMPTS` and the
    source is abandoned on its first real error.
    """
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)

    for _ in range(3):
        _lock_the_sidecar_on_put(s, lock_on=1)
        with pytest.raises(DuckDBFactTermStoreLockedError):
            process_extraction_job(s, _ChunkClient(), job_id=job_id)
        assert _chunk_states(s, job_id)[0] == ("pending", 0)
        assert _plan(s, source_id) == ExtractionJobPlan(resume_job_id=job_id)

    # the peer let go; the chunk now fails on its own content, and the budget is
    # spent one attempt per pass exactly as if the locks had never happened
    s._fact_terms = None
    for expected_attempts, expected_plan in (
        (1, ExtractionJobPlan(retry_job_id=job_id)),
        (2, ExtractionJobPlan(retry_job_id=job_id)),
        (3, ExtractionJobPlan(exhausted_job_id=job_id)),
    ):
        with pytest.raises(ValueError):
            process_extraction_job(
                s,
                _ChunkClient(fail_on=1, exc=ValueError("boom")),
                job_id=job_id,
                retry=expected_attempts > 1,
                retry_max_attempts=MAX_CHUNK_ATTEMPTS if expected_attempts > 1 else None,
            )
        _worker_marks_job_failed(s, job_id, "analysis failed: boom")
        assert _chunk_states(s, job_id)[0] == ("failed", expected_attempts)
        assert _plan(s, source_id) == expected_plan

    s.close()


def test_a_reclaimed_chunk_is_not_requeued_by_the_call_that_lost_it(tmp_path):
    """The CAS on `attempts`: a release applies to its own claim or to nothing.

    `status = 'running'` alone cannot tell "my claim" from "somebody's claim" —
    the row has no owner column. Here the startup recovery path rewinds the job
    and the chunk is claimed afresh (#242), so the first claimant's release must
    find nothing to release rather than rewinding and refunding the new owner's.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    chunk_id = int(s.next_pending_chunk(job_id)["id"])
    stale_attempts = int(s.mark_chunk_running(chunk_id)["attempts"])

    s.rollback_extraction_job(job_id, "rewound by the resume loop")
    reissued = s.mark_chunk_running(chunk_id)
    assert int(reissued["attempts"]) == stale_attempts + 1

    assert s.requeue_chunk_claim(chunk_id, attempts=stale_attempts) is False

    row = s.get_source_chunk(chunk_id)
    assert (row["status"], int(row["attempts"])) == ("running", stale_attempts + 1)
    s.close()


# --- the same thing, against a real second process ----------------------------


@_posix_only
def test_a_real_peer_process_holding_the_sidecar_requeues_the_chunk(tmp_path, monkeypatch):
    """The injected stub, replaced by an actual process holding the actual file.

    Nothing here is simulated: the peer opens the sidecar with DuckDB, the
    pipeline's own write path (`reconcile_fact` -> `put_fact_terms` ->
    `_open_with_retry`) hits the conflict, and the error class is whatever that
    path really raises. This is what keeps the stub tests above from going vacuous
    if the sidecar ever stops raising `DuckDBFactTermStoreLockedError` from a
    chunk write — the stub would keep passing; this would not.

    The retry budget is set to zero so the pass fails at the lock instead of
    waiting the peer out, which is the same condition a peer that holds the file
    for longer than the budget produces.
    """
    pytest.importorskip("duckdb")
    monkeypatch.setattr(duckdb_fact_terms, "_LOCK_TIMEOUT_SECONDS", 0.0)
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)
    client = _HoldsTheSidecarFromCall(
        tmp_path, tmp_path / FACT_TERMS_FILENAME, hold_from=2
    )

    try:
        with pytest.raises(DuckDBFactTermStoreLockedError):
            process_extraction_job(s, client, job_id=job_id)

        assert client.holder is not None, "the peer never started; nothing was locked"
        assert _chunk_states(s, job_id) == [("done", 1), ("pending", 0), ("pending", 0)]
        assert s.get_extraction_job(job_id)["status"] == "pending"
        assert _plan(s, source_id) == ExtractionJobPlan(resume_job_id=job_id)
        assert [f["subject"] for f in s.facts()] == ["alpha"]
    finally:
        if client.holder is not None:
            _terminate(client.holder)
        s.close()


# --- the same rewind, seen through `verinote sync` -----------------------------


class _SidecarLockedForEveryStore:
    """Stands in for the `DuckDBFactTermStore` CLASS, not for one instance.

    `_lock_the_sidecar_on_put` reaches into a `Store` the test is holding.
    `cmd_sync` builds its own and never hands it out, so the injection has to
    happen where the sidecar is CONSTRUCTED: `Store.fact_terms` imports
    `DuckDBFactTermStore` at call time, which is what makes replacing the module
    attribute enough. Everything except `for_root` is delegated to the real class.
    """

    def __init__(self, inner_cls, *, lock_on: int):
        self._inner_cls = inner_cls
        self._lock_on = lock_on
        self.wrappers: list[_LocksOnPut] = []

    def for_root(self, *args, **kwargs):
        wrapper = _LocksOnPut(
            self._inner_cls.for_root(*args, **kwargs), lock_on=self._lock_on
        )
        self.wrappers.append(wrapper)
        return wrapper

    def __getattr__(self, name):
        return getattr(self._inner_cls, name)


def test_cli_sync_leaves_a_sidecar_locked_job_pending(tmp_path, monkeypatch, capsys):
    """`verinote sync` must not write anything over the rewind either.

    The tests above hold `process_extraction_job` to its half of the bargain: the
    chunk goes back to `pending` with its attempt refunded and the JOB is rolled
    back around it, so the next pass resumes. That bargain is only kept if the
    CALLER also writes nothing — and `cmd_sync` has a job-level failure handler,
    so "writes nothing here" is now a property of the CLI, not an absence of code.
    `web/app.py` states the same requirement from its side, and for the same
    reason: `failed` over a job with zero failed chunks is the edge state
    `plan_source_extraction` rebuilds from scratch.

    `status == "pending"` IS THE ONLY DISCRIMINATING ASSERTION. `rc == 1` is what
    `main`'s central `except DuckDBFactTermStoreLockedError` returns whether or not
    `cmd_sync` wrote the job row first, so it distinguishes nothing on its own; it
    is asserted to keep #246's contract (a clean message, not a traceback) in view.

    The injected stub is enough here. What is under test is the CLI's reaction to
    the exception, and
    `test_a_real_peer_process_holding_the_sidecar_requeues_the_chunk` above is what
    keeps "this exception really reaches a chunk write" honest for the whole
    module.
    """
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", PROVIDER)
    monkeypatch.setenv("VERINOTE_MODEL", MODEL)
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    assert cli.main(["init"]) == 0
    source = tmp_path / "note.txt"
    source.write_text(
        "alpha beta gamma delta epsilon\n\nzeta eta theta iota kappa",
        encoding="utf-8",
    )
    assert cli.main(["ingest", str(source)]) == 0
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _ChunkClient())
    # chunk 0 lands; the sidecar is locked under chunk 1's write
    monkeypatch.setattr(
        duckdb_fact_terms,
        "DuckDBFactTermStore",
        _SidecarLockedForEveryStore(duckdb_fact_terms.DuckDBFactTermStore, lock_on=2),
    )

    rc = cli.main(["sync"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "fact-term store" in err
    assert "Traceback" not in err

    s = Store(tmp_path / "kb.sqlite")
    jobs = list(s.source_extraction_jobs())
    assert len(jobs) == 1
    job_id = int(jobs[0]["id"])
    # THE ASSERTION THIS TEST EXISTS FOR
    assert jobs[0]["status"] == "pending"
    # and the chunk kept its refund: `pending` with nothing charged
    assert _chunk_states(s, job_id) == [("done", 1), ("pending", 0)]
    assert s.source_chunks(job_id)[1]["error"] == ""
    s.close()
