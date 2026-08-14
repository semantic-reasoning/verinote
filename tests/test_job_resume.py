# SPDX-License-Identifier: MPL-2.0
"""A rolled-back job is resumed, and a superseded one is left for dead (#240, #242).

Two failures met at the same line of `cmd_sync`, which used to call
`create_chunked_extraction_job` unconditionally:

* #240 — a job halted mid-flight rolls back to `pending` with its finished chunks
  intact, and every resume mechanism already works. Nothing asked for it, so the
  next `sync` built a fresh job and paid the LLM again for chunks that were
  already done.
* #242 — the abandoned `pending` row is never cleaned up, so the UI launcher
  revives it, the Sources page polls for it forever, and the re-analyse button
  409s on it.

WHAT THESE TESTS MEASURE IS THE CHUNK TEXT THE CLIENT SAW, not which branch ran.
"Resume was taken" is cheap to satisfy and says nothing: a resume that re-sends
chunk zero costs exactly what the bug cost. So the recording client below keeps
every `source_text` it was handed, and the load-bearing assertion is that a
finished chunk's text is absent from the second run.
"""

import pytest

import verinote.cli as cli
from verinote.engine import DEFAULT_POLICY
from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline import (
    MAX_CHUNK_ATTEMPTS,
    create_chunked_extraction_job,
    plan_source_extraction,
)
from verinote.pipeline.policy_state import POLICY_RELPATH
from verinote.store import Store

MARKERS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
SECOND_MARKERS = ("golf", "hotel", "india", "juliett", "kilo", "lima")


def _body(markers=MARKERS) -> str:
    """Six paragraphs, each its own chunk under the 60-char chunk size below."""
    return "\n\n".join(f"{marker} " + ("x " * 20) for marker in markers)


def _env(monkeypatch, tmp_path, *, model: str = "m") -> None:
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")
    monkeypatch.setenv("VERINOTE_MODEL", model)
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "60")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")


class _RecordingClient:
    """Records the text of every chunk it is asked to extract.

    One fact per chunk, keyed on the chunk's leading marker, so a re-sent chunk
    is visible in `markers` even though the store would dedupe its fact away.
    """

    name = "fake"

    def __init__(
        self,
        *,
        delete_policy_on_call: int | None = None,
        policy_path=None,
        fail_on_call: int | None = None,
        crash_on_call: int | None = None,
    ):
        self.seen: list[str] = []
        self._delete_on = delete_policy_on_call
        self._policy_path = policy_path
        self._fail_on = fail_on_call
        self._crash_on = crash_on_call

    @property
    def markers(self) -> list[str]:
        return [text.split()[0] for text in self.seen]

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.seen.append(source_text)
        if self._crash_on is not None and len(self.seen) == self._crash_on:
            # NOT an `LLMError`: unmodelled, so it leaves the whole pass rather
            # than failing one chunk and continuing. A plain builtin on purpose —
            # the point is a type the pipeline has no clause for.
            raise ValueError("boom")
        if self._fail_on is not None and len(self.seen) == self._fail_on:
            raise LLMError("provider down")  # transient: the chunk goes `failed`
        if self._delete_on is not None and len(self.seen) == self._delete_on:
            self._policy_path.unlink()
        return [ExtractedFact(source_text.split()[0], "seen_in", "source", 0.9)]


class _RefusingClient:
    """Fails the test loudly if extraction is attempted at all."""

    name = "fake"

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        raise AssertionError(f"extraction must not run; got chunk {source_text!r}")


class _ThreadRecorder:
    """Stands in for `threading` inside `verinote.web.app`.

    Replacing the module reference in the app's namespace (not the real
    `threading` module) makes "did the launcher start a worker?" a synchronous,
    exact question — no sleeps, no joins, no flake.
    """

    def __init__(self):
        self.started: list[str] = []

    def Thread(self, *, target, name, daemon):  # noqa: N802 - mimics threading.Thread
        recorder = self

        class _Handle:
            def start(self) -> None:
                recorder.started.append(name)

        return _Handle()


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _ingest(tmp_path, monkeypatch, *, body: str = "", init: bool = True) -> None:
    """Scaffold a KB and register `doc.txt` as a source with a text artifact."""
    if init:
        _env(monkeypatch, tmp_path)
        assert cli.main(["init"]) == 0
    source = tmp_path / "doc.txt"
    source.write_text(body or _body(), encoding="utf-8")
    assert cli.main(["ingest", str(source)]) == 0


def _jobs(tmp_path) -> list:
    store = _store(tmp_path)
    try:
        return list(store.source_extraction_jobs())
    finally:
        store.close()


def _run_count(tmp_path) -> int:
    store = _store(tmp_path)
    try:
        return int(store._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])
    finally:
        store.close()


def _halted_job(tmp_path, monkeypatch) -> int:
    """Drive a real mid-job halt: chunk `alpha` lands, then the policy vanishes.

    Uses the production halt path rather than hand-writing a `pending` row, so the
    fixture cannot drift away from the state `_halt_extraction_job` really leaves.
    """
    _ingest(tmp_path, monkeypatch)
    policy = tmp_path / POLICY_RELPATH
    client = _RecordingClient(delete_policy_on_call=2, policy_path=policy)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    assert cli.main(["sync"]) == 2  # halted, rolled back to pending

    jobs = _jobs(tmp_path)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "pending"
    assert int(jobs[0]["total_chunks"]) == len(MARKERS)
    assert int(jobs[0]["completed_chunks"]) == 1
    policy.write_text(DEFAULT_POLICY, encoding="utf-8")  # recovery
    return int(jobs[0]["id"])


# --- A: the fix itself — resume, and do not redo the finished chunk ----------


def test_sync_resumes_rolled_back_job_without_redoing_done_chunks(
    tmp_path, monkeypatch, capsys
):
    job_id = _halted_job(tmp_path, monkeypatch)
    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    assert [int(job["id"]) for job in jobs] == [job_id]  # resumed, not replaced
    assert jobs[0]["status"] == "done"
    # THE ASSERTION THIS FILE EXISTS FOR: the finished chunk never reached the LLM
    # again. A resume that re-sends it has fixed nothing.
    assert "alpha" not in client.markers
    assert client.markers == list(MARKERS[1:])

    store = _store(tmp_path)
    alpha = [f for f in store.facts() if f["subject"] == "alpha"]
    assert len(alpha) == 1  # the halted run's fact survived, and was not duplicated
    assert [f["subject"] for f in store.facts()] == list(MARKERS)
    store.close()

    out = capsys.readouterr().out
    # Run scope and job scope are stated separately: this run wrote 5 of the 6.
    assert "5 candidate(s) this run" in out
    assert f"resumed job #{job_id}: 6 candidate(s) in total" in out
    assert "sync complete: 5 candidate(s)" in out


def test_sync_reports_a_resumed_run_that_fails_everything_as_failed_not_incomplete(
    tmp_path, monkeypatch, capsys
):
    """A resumed job's OWN history must not soften this run's own total failure.

    `_halted_job` leaves a job with one chunk done and zero failed (a clean
    halt, not a chunk failure) — eligible for resume. If THIS run then fails
    every remaining chunk, the job's cumulative `completed_chunks` is still 1
    (from before), but this run completed none and produced nothing: the
    "sync failed" / "sync incomplete" choice must be judged on what THIS run
    did, not smoothed over by a resumed job's earlier, unrelated success.
    """
    job_id = _halted_job(tmp_path, monkeypatch)

    class _AlwaysFailingClient:
        name = "fake"

        def extract_facts(self, *, source_text: str, schema_hint: str = ""):
            raise LLMError("provider down")

    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _AlwaysFailingClient())

    assert cli.main(["sync"]) == 1

    jobs = _jobs(tmp_path)
    assert [int(job["id"]) for job in jobs] == [job_id]  # still resumed, not replaced
    assert int(jobs[0]["completed_chunks"]) == 1  # unchanged: this run finished none

    out, err = capsys.readouterr()
    # This run's own contribution is honestly zero, not the job's stale total.
    assert "0 candidate(s) this run" in out
    assert f"resumed job #{job_id}: 1 candidate(s) in total" in out
    # The judged-on-this-run-alone outcome: every chunk THIS run touched failed.
    assert "sync failed" in err
    assert "sync incomplete" not in err


# --- the chunk a resume can never reach -------------------------------------


def test_sync_retries_a_chunk_that_failed_before_the_halt(tmp_path, monkeypatch):
    """A `failed` chunk must not be stranded by resuming (regression, review).

    `claim_pending_extraction_job`'s reclaim rewinds only `running`;
    `next_pending_chunk` claims only `pending`. A chunk left `failed` by a
    transient provider error is NEITHER, so a resumed job walks straight past it
    and calls the source done.
    Its text never reaches the LLM again and the facts it would have produced are
    lost silently — `sync` exits 0 and the job reads `done`.

    Before this branch, re-syncing rebuilt the job from scratch and the failed
    chunk went out again. Resuming removed that recovery path, so the resume
    predicate has to decline any job carrying a failed chunk and let the old
    full-re-extraction behaviour stand.
    """
    _ingest(tmp_path, monkeypatch)
    policy = tmp_path / POLICY_RELPATH
    # `alpha` lands, `bravo` hits a transient provider error, `charlie` halts.
    first = _RecordingClient(
        fail_on_call=2, delete_policy_on_call=3, policy_path=policy
    )
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: first)
    assert cli.main(["sync"]) == 2
    stalled = _jobs(tmp_path)[0]
    assert stalled["status"] == "pending"
    assert int(stalled["completed_chunks"]) == 1
    assert int(stalled["failed_chunks"]) == 1  # `bravo`, and nothing will retry it
    policy.write_text(DEFAULT_POLICY, encoding="utf-8")

    second = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: second)
    assert cli.main(["sync"]) == 0

    # THE ASSERTION: the failed chunk was tried again. Which route delivers it —
    # a fresh job or a retry inside the old one — is not this test's business.
    assert "bravo" in second.markers
    store = _store(tmp_path)
    assert {f["subject"] for f in store.facts()} == set(MARKERS)
    store.close()


# --- B, C: the reverse — a job that no longer describes the work is replaced --


def test_sync_starts_a_new_job_when_the_source_body_changed(tmp_path, monkeypatch):
    old_job_id = _halted_job(tmp_path, monkeypatch)
    _ingest(tmp_path, monkeypatch, body=_body(SECOND_MARKERS), init=False)
    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    assert cli.main(["sync"]) == 0

    job_ids = [int(job["id"]) for job in _jobs(tmp_path)]
    assert len(job_ids) == 2
    assert old_job_id != max(job_ids)  # the stale job is no longer the newest
    assert client.markers == list(SECOND_MARKERS)  # every new chunk was extracted


def test_sync_starts_a_new_job_when_the_chunk_size_changed(tmp_path, monkeypatch):
    """The chunk-text comparison is the only guard that catches this.

    Same body, same artifact, same provider and model — every other condition
    passes. What moved is the chunk boundaries, so the job's finished chunk no
    longer covers the text the pending ones assume, and resuming would extract
    the source under two different chunkings at once. Without this case the
    comparison could be deleted outright and the suite would stay green.
    """
    old_job_id = _halted_job(tmp_path, monkeypatch)
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "200")
    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    job_ids = [int(job["id"]) for job in jobs]
    assert len(job_ids) == 2
    assert old_job_id != max(job_ids)
    newest = next(job for job in jobs if int(job["id"]) == max(job_ids))
    assert int(newest["total_chunks"]) == 2  # re-chunked under the new size
    # `alpha` was already done under the old chunking; the new job must still send
    # it, because the chunk it now belongs to is not the chunk that finished.
    assert client.markers == ["alpha", "echo"]


def test_sync_starts_a_new_job_when_the_artifact_changed_but_the_text_did_not(
    tmp_path, monkeypatch
):
    """Identical text, different artifact row — the artifact check earns its keep.

    Artifacts are content-addressed per `(source_id, kind, checksum)`, so the same
    body can legitimately exist twice under two kinds — a source re-ingested
    through a converter that reproduces its text exactly. The chunks then match
    and every other condition passes, but the job still points at the OLD
    artifact, and a resumed job stamps that artifact onto the evidence of facts
    extracted from the new one. The chunk comparison cannot see this; without
    this case the artifact condition could be deleted and nothing would notice.
    """
    _halted_job(tmp_path, monkeypatch)
    store = _store(tmp_path)
    source_id = int(store.sources()[0]["id"])
    old = store.latest_source_text_artifact(source_id)
    reconverted = tmp_path / "artifacts" / "sources" / "doc-reconverted.txt"
    reconverted.write_text(
        (tmp_path / str(old["path"])).read_text(encoding="utf-8"), encoding="utf-8"
    )
    new_artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path=str(reconverted.relative_to(tmp_path)),  # identical bytes, new row
        checksum=str(old["checksum"]) + "-reconverted",
    )
    store.close()
    assert new_artifact_id != int(old["id"])
    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    assert len(jobs) == 2
    newest = next(job for job in jobs if int(job["id"]) == max(int(j["id"]) for j in jobs))
    assert int(newest["artifact_id"]) == new_artifact_id
    assert client.markers == list(MARKERS)  # nothing carried over from the old job


def test_sync_re_extracts_a_source_whose_job_already_finished(tmp_path, monkeypatch):
    """`pending` is the only resumable status — a `done` job is a decided outcome.

    Sole guard for the status condition. One job, everything else matching, so
    nothing else can reject it: drop the status check and `sync` resumes a job
    with no pending chunks, finds nothing to do, and quietly becomes a no-op for
    a source the user just asked to re-sync.
    """
    _ingest(tmp_path, monkeypatch)
    first = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: first)
    assert cli.main(["sync"]) == 0
    finished = _jobs(tmp_path)[0]
    assert finished["status"] == "done"
    assert int(finished["failed_chunks"]) == 0

    second = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: second)
    assert cli.main(["sync"]) == 0

    assert second.markers == list(MARKERS)  # re-syncing still re-extracts
    job_ids = [int(job["id"]) for job in _jobs(tmp_path)]
    assert len(job_ids) == 2
    assert max(job_ids) != int(finished["id"])


def test_sync_ignores_an_older_resumable_job_under_a_newer_one(tmp_path, monkeypatch):
    """Sole guard for "newest job only" on the planning side.

    The source carries an abandoned, perfectly resumable `pending` job *and* a
    newer finished one. Only the newest job describes the source's current
    analysis; reach past it to the older row and `sync` resumes work that a later
    job already superseded, skipping the chunk that job had finished.
    """
    _ingest(tmp_path, monkeypatch)
    policy = tmp_path / POLICY_RELPATH
    stalled = _RecordingClient(delete_policy_on_call=2, policy_path=policy)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: stalled)
    assert cli.main(["sync"]) == 2  # job #1: `alpha` done, rolled back to pending
    policy.write_text(DEFAULT_POLICY, encoding="utf-8")
    store = _store(tmp_path)
    source_id = int(store.sources()[0]["id"])
    newer_job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        artifact_id=int(store.latest_source_text_artifact(source_id)["id"]),
        source_text=_body(),
        provider="anthropic",
        model="m",
        chunk_chars=60,
        chunk_overlap_chars=0,
    )
    store.finish_extraction_job(newer_job_id)
    store.close()

    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)
    assert cli.main(["sync"]) == 0

    # The newest job is `done`, so this is a fresh extraction — not a resume of
    # the stale job, which would have skipped `alpha`.
    assert client.markers == list(MARKERS)
    job_ids = [int(job["id"]) for job in _jobs(tmp_path)]
    assert len(job_ids) == 3
    assert max(job_ids) > newer_job_id


def test_sync_starts_a_new_job_when_the_provider_changed(tmp_path, monkeypatch):
    """Sole guard for the PROVIDER half of the provider/model condition.

    The model name is unchanged, so a check that compares only the model waves
    this through. Model names are not globally unique — the same string is served
    by more than one provider — and a job resumed across that switch keeps a
    `provider` column describing work the other provider did.
    """
    old_job_id = _halted_job(tmp_path, monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")  # same model name, new provider
    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    job_ids = [int(job["id"]) for job in jobs]
    assert len(job_ids) == 2
    assert old_job_id != max(job_ids)
    assert client.markers == list(MARKERS)  # nothing carried over
    newest = next(job for job in jobs if int(job["id"]) == max(job_ids))
    assert newest["provider"] == "openai" and newest["model"] == "m"


def test_sync_starts_a_new_job_when_the_model_changed(tmp_path, monkeypatch):
    old_job_id = _halted_job(tmp_path, monkeypatch)
    monkeypatch.setenv("VERINOTE_MODEL", "different-model")
    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    job_ids = [int(job["id"]) for job in jobs]
    assert len(job_ids) == 2
    assert old_job_id != max(job_ids)
    assert client.markers == list(MARKERS)  # nothing was carried over
    newest = next(job for job in jobs if int(job["id"]) == max(job_ids))
    assert newest["model"] == "different-model"


# --- E: a job someone else is running is neither resumed nor replaced --------


def test_sync_leaves_a_running_job_alone(tmp_path, monkeypatch, capsys):
    _ingest(tmp_path, monkeypatch)
    store = _store(tmp_path)
    source_id = int(store.sources()[0]["id"])
    artifact = store.latest_source_text_artifact(source_id)
    job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        artifact_id=int(artifact["id"]),
        source_text=_body(),
        provider="anthropic",
        model="m",
        chunk_chars=60,
        chunk_overlap_chars=0,
    )
    store.mark_extraction_job_running(job_id)
    store.close()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _RefusingClient())

    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    assert [int(job["id"]) for job in jobs] == [job_id]  # no replacement job
    store = _store(tmp_path)
    chunks = store.source_chunks(job_id)
    # Not one chunk was claimed: resuming would have `claim_pending_extraction_job`
    # pull the other process's in-flight chunk back and send it to the LLM twice.
    assert {chunk["status"] for chunk in chunks} == {"pending"}
    assert {int(chunk["attempts"]) for chunk in chunks} == {0}
    store.close()
    err = capsys.readouterr().err
    assert f"extraction job #{job_id} is already running" in err


def test_sync_skips_a_job_claimed_between_plan_and_process(tmp_path, monkeypatch, capsys):
    """Plan sees `pending`; another worker wins the claim first; sync skips cleanly (#240).

    The plan-time `busy_job_id` branch only catches a job already `running` when the
    plan is drawn. A job still `pending` at plan time can be claimed by a competing
    worker in the window before `process_extraction_job` runs; the raised
    `ExtractionJobBusyError` must be the same skip, never a crash or a failure.
    """
    job_id = _halted_job(tmp_path, monkeypatch)
    monkeypatch.setattr(Store, "claim_pending_extraction_job", lambda self, jid: False)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _RefusingClient())

    assert cli.main(["sync"]) == 0  # skipped cleanly: no crash, no non-zero exit

    err = capsys.readouterr().err
    assert f"extraction job #{job_id} is already running" in err
    jobs = _jobs(tmp_path)
    assert [int(job["id"]) for job in jobs] == [job_id]  # no replacement job created
    assert jobs[0]["status"] == "pending"  # left untouched for its real owner


# --- D, F, G: the superseded job is dead, and all three readers agree --------


def _superseded_pending_job(tmp_path, monkeypatch) -> tuple[int, int, int]:
    """A source carrying an abandoned `pending` job plus a newer, finished one."""
    _ingest(tmp_path, monkeypatch)
    store = _store(tmp_path)
    source_id = int(store.sources()[0]["id"])
    artifact_id = int(store.latest_source_text_artifact(source_id)["id"])
    kwargs = dict(
        source_id=source_id,
        artifact_id=artifact_id,
        source_text=_body(),
        provider="anthropic",
        model="m",
        chunk_chars=60,
        chunk_overlap_chars=0,
    )
    stale_job_id = create_chunked_extraction_job(store, **kwargs)
    fresh_job_id = create_chunked_extraction_job(store, **kwargs)
    store.finish_extraction_job(fresh_job_id)
    assert store.get_extraction_job(stale_job_id)["status"] == "pending"
    store.close()
    return source_id, stale_job_id, fresh_job_id


def _app(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import verinote.web.app as webapp
    from verinote.config import Config

    recorder = _ThreadRecorder()
    monkeypatch.setattr(webapp, "threading", recorder)
    cfg = Config.for_root(tmp_path)
    return webapp.create_app(cfg), recorder


def test_launcher_does_not_revive_a_superseded_pending_job(tmp_path, monkeypatch):
    """Starting the UI must not re-run a source another job already finished.

    No HTTP request is made here — `create_app()` alone used to be enough. The
    launcher runs outside the request middleware, so this is a write (and a bill)
    triggered by nothing but opening the KB.
    """
    _superseded_pending_job(tmp_path, monkeypatch)

    _, recorder = _app(tmp_path, monkeypatch)

    assert recorder.started == []


def test_sources_page_stops_polling_for_a_superseded_pending_job(tmp_path, monkeypatch):
    """A dead `pending` row must not keep the page refreshing every 2 seconds."""
    from fastapi.testclient import TestClient

    _superseded_pending_job(tmp_path, monkeypatch)
    app, _ = _app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        body = client.get("/sources").text

    assert 'hx-trigger="every 2s"' not in body


def test_reanalyze_is_not_blocked_by_a_superseded_pending_job(tmp_path, monkeypatch):
    """The one source whose analysis is stuck must still be re-analysable."""
    from fastapi.testclient import TestClient

    source_id, _, _ = _superseded_pending_job(tmp_path, monkeypatch)
    app, recorder = _app(tmp_path, monkeypatch)
    assert recorder.started == []

    with TestClient(app) as client:
        response = client.post(
            f"/sources/{source_id}/reanalyze", follow_redirects=False
        )

    assert response.status_code == 303  # not the 409 "analysis already running"
    # `reanalyze_source` clears the source's old jobs, so the proof that a fresh
    # analysis was queued is the worker it launched — job ids are reused here.
    jobs = _jobs(tmp_path)
    assert [job["status"] for job in jobs] == ["pending"]
    assert recorder.started == [f"verinote-source-extract-{int(jobs[0]['id'])}"]


# --- H: a failed job is retried while it has budget, then given up on (#323) --


def _failed_retryable_job(tmp_path, *, source_text="alpha beta gamma", attempts=1):
    """A KB whose newest job is `failed` with one failed chunk at `attempts`.

    Everything else the planner compares — artifact (`None`), provider, model,
    chunk text — matches what `_plan` hands it below, so only the failed chunk's
    attempt count decides retry-vs-give-up.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/a.txt")
    job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        artifact_id=None,
        source_text=source_text,
        provider="fake",
        model="m",
    )
    chunk_id = int(store.source_chunks(job_id)[0]["id"])
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunk_id)
    store.mark_chunk_failed(chunk_id, "provider down")  # chunk -> failed
    store._conn.execute(
        "UPDATE source_chunks SET attempts = ? WHERE id = ?", (attempts, chunk_id)
    )
    # A job is only `failed` once terminalised; mid-run it stays `running` (#337),
    # so finish it to reach the genuine `failed` state the planner treats as a
    # retry/give-up candidate.
    store.finish_extraction_job(job_id)
    return store, source_id, job_id


def _plan(store, source_id, *, source_text="alpha beta gamma"):
    return plan_source_extraction(
        store,
        source_id=source_id,
        artifact_id=None,
        source_text=source_text,
        provider="fake",
        model="m",
    )


def test_plan_retries_a_failed_job_that_still_has_budget(tmp_path):
    store, source_id, job_id = _failed_retryable_job(tmp_path, attempts=1)
    plan = _plan(store, source_id)
    store.close()
    assert plan.retry_job_id == job_id
    assert plan.exhausted_job_id is None


def test_plan_gives_up_on_a_failed_job_whose_chunk_exhausted_its_attempts(tmp_path):
    """THE anti-spurious-run gate: a failed job whose only chunk has spent its whole
    budget surfaces as `exhausted`, never `retry`. If it reached `retry` instead,
    `cmd_sync` would claim it, reset nothing, and burn an empty run every sync —
    the loop #323 exists to break, just moved one layer down.
    """
    store, source_id, job_id = _failed_retryable_job(
        tmp_path, attempts=MAX_CHUNK_ATTEMPTS
    )
    plan = _plan(store, source_id)
    store.close()
    assert plan.exhausted_job_id == job_id
    assert plan.retry_job_id is None


def test_plan_rebuilds_a_failed_job_whose_source_changed_instead_of_retrying(tmp_path):
    """Staleness gates the retry branch, not just resume. A failed-with-retryable-
    chunk job whose source text no longer matches is rebuilt fresh — retrying it
    would re-send content a human may have just fixed between sync attempts (#323).
    """
    store, source_id, _ = _failed_retryable_job(tmp_path, attempts=1)
    plan = _plan(store, source_id, source_text="a completely different body now")
    store.close()
    # A fresh plan: nothing to continue, so cmd_sync builds a new job.
    assert plan.retry_job_id is None
    assert plan.exhausted_job_id is None
    assert plan.resume_job_id is None


def test_sync_gives_up_on_a_permanently_failing_chunk(tmp_path, monkeypatch, capsys):
    """End to end: a chunk that fails every attempt is retried in place until its
    budget is spent, then surfaced as a give-up — and the give-up sync opens no run
    and builds no new job, the empty-run loop #323 exists to break.
    """
    _ingest(tmp_path, monkeypatch, body=_body(("alpha",)))

    class _AlwaysFailingClient:
        name = "fake"

        def extract_facts(self, *, source_text: str, schema_hint: str = ""):
            raise LLMError("provider down")

    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _AlwaysFailingClient())

    # Each sync spends one attempt on the single chunk and reuses the one job; none
    # rebuilds it. After MAX_CHUNK_ATTEMPTS syncs the chunk has no budget left.
    for _ in range(MAX_CHUNK_ATTEMPTS):
        assert cli.main(["sync"]) == 1
    jobs = _jobs(tmp_path)
    assert len(jobs) == 1  # retried in place across every sync, never rebuilt
    job_id = int(jobs[0]["id"])
    assert int(jobs[0]["failed_chunks"]) == 1

    runs_before = _run_count(tmp_path)
    capsys.readouterr()  # drop the failing syncs' output

    # The give-up sync: the planner surfaces the job as exhausted, so cmd_sync skips
    # it before any claim — no worker runs, no run row opens, no fresh job appears.
    assert cli.main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "giving up on sources/doc.txt" in err
    assert _run_count(tmp_path) == runs_before  # no spurious empty run
    assert [int(job["id"]) for job in _jobs(tmp_path)] == [job_id]


# --- I: a crash is resumed by the next ORDINARY sync, with no flag (#488) -----


def test_a_crashed_sync_is_resumed_by_the_next_plain_sync(tmp_path, monkeypatch, capsys):
    """The whole point of writing the job row: `verinote sync`, and nothing else.

    An unmodelled exception used to leave the job `running`, and a `running` job
    is indistinguishable from one a live worker owns — so every later plain
    `sync` printed "already running" and skipped the source. The chunk was
    `failed` and, below `MAX_CHUNK_ATTEMPTS`, retryable; what could not reach it
    was the plain pass. Two things could still rewind that row, and neither is a
    plain sync: `verinote sync --recover`, and one `verinote ui` boot, whose
    `_resume_source_extraction_jobs` rolls a `running` job back to `pending`.
    There is no `--retry` flag, and there never was.

    Now the job comes to rest `failed` with a failed chunk, which is exactly the
    state `plan_source_extraction` calls a RETRY: the next ordinary sync claims
    the same job and spends one of `MAX_CHUNK_ATTEMPTS` on the chunk that failed.
    Two honest consequences of that, both measured below:

    * the failed chunk IS re-sent to the LLM. That is what a retry is. If it keeps
      failing, the third attempt exhausts the budget and the source is given up on
      (`test_a_failed_job_is_retried_until_its_chunk_runs_out_of_attempts` above).
    * chunks already `done` are NOT re-sent. The crash is on the THIRD chunk here
      precisely so that this is visible: two finished chunks stay finished.

    WHY `rc == 0` IS NOT THE ASSERTION. Before the fix the second sync also
    returned 0 — it skipped the source and reported "0 candidate(s) from 0
    source(s)", which is a successful run that did nothing. The marker lists are
    what tell a resume from a skip, and from a rebuild.
    """
    _ingest(tmp_path, monkeypatch)
    crashing = _RecordingClient(crash_on_call=3)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: crashing)

    with pytest.raises(ValueError):
        cli.main(["sync"])

    assert crashing.markers == ["alpha", "bravo", "charlie"]
    jobs = _jobs(tmp_path)
    assert len(jobs) == 1
    job_id = int(jobs[0]["id"])
    assert jobs[0]["status"] == "failed"  # #488: a terminal status, not `running`
    assert "ValueError" in jobs[0]["message"] and "boom" in jobs[0]["message"]
    store = _store(tmp_path)
    assert [c["status"] for c in store.source_chunks(job_id)] == [
        "done",
        "done",
        "failed",
        "pending",
        "pending",
        "pending",
    ]
    store.close()
    capsys.readouterr()  # drop the crashed run's output

    # ...and now an ordinary sync. No `--recover`, no flags at all.
    healthy = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: healthy)

    assert cli.main(["sync"]) == 0

    # THE ASSERTION THIS TEST EXISTS FOR: the finished chunks were not re-sent,
    # and the source was not skipped.
    assert healthy.markers == ["charlie", "delta", "echo", "foxtrot"]
    jobs = _jobs(tmp_path)
    assert [int(job["id"]) for job in jobs] == [job_id]  # resumed, not rebuilt
    assert jobs[0]["status"] == "done"

    store = _store(tmp_path)
    assert [f["subject"] for f in store.facts()] == list(MARKERS)
    store.close()

    out = capsys.readouterr().out
    assert "4 candidate(s) this run" in out
    assert f"resumed job #{job_id}: 6 candidate(s) in total" in out


# --- J: a `failed` job that finished chunks is continued, not rebuilt (#524) --

J_MARKERS = MARKERS[:3]
J_CHUNK_CHARS = 60


def _worker_marks_job_failed(store: Store, job_id: int, message: str) -> None:
    """Stand-in for the caller's broad `except`. NOT the code under test.

    A crash below the chunk loop gives `process_extraction_job` nothing to say
    about the job row, so it leaves the job `running`; the broad `except` a caller
    ends with is what writes `failed` over it. In this tree only `web/app.py` has
    one, so a CLI-driven test has to perform that write itself. Spelled out here
    rather than hidden behind a fixture, the idiom
    `test_chunk_claim_release.py::_worker_marks_job_failed` uses for the same
    reason — nothing in `verinote/pipeline` will do it.

    DELETE THIS WHEN #488 LANDS. PR #523 gives `cmd_sync` the same broad clause,
    at which point the sync below terminalises the job by itself and the correct
    change is to drop the call and assert the `failed` row with zero failed chunks
    that the CLI wrote — never to keep the stand-in so these lines stay green.
    """
    store.fail_extraction_job(job_id, message)


class _CrashBelowChunkAccounting:
    """Fail a pass where no chunk row can record that it happened.

    THE INJECTION POINT IS THE WHOLE POINT, not the exception. Failing the client
    puts the crash inside `_extract_chunk`, which marks the chunk `failed`; the
    job then reaches the planner's failed-chunk branch, which already resumes.
    `next_pending_chunk` raises before any claim, so no chunk changes status and
    no `attempts` is spent — the job terminalises as `failed` with `failed_chunks`
    zero while holding finished chunks, which is the state #524 is about.
    """

    def __init__(self, monkeypatch, *, before_chunk: int):
        self._before_chunk = before_chunk
        self.armed = True
        real = Store.next_pending_chunk

        def crashing(store, job_id):
            row = real(store, job_id)
            if self.armed and row is not None and int(row["chunk_index"]) == before_chunk:
                raise RuntimeError("crash below chunk accounting")
            return row

        monkeypatch.setattr(Store, "next_pending_chunk", crashing)

    def heal(self) -> None:
        self.armed = False


def _crashed_below_chunk_accounting(tmp_path, monkeypatch, *, before_chunk: int = 2):
    """Drive a real sync that dies below the chunk accounting and terminalise it.

    Built by the production pass rather than hand-written rows, so the fixture
    cannot drift away from the state a real crash leaves.
    """
    _ingest(tmp_path, monkeypatch)
    crash = _CrashBelowChunkAccounting(monkeypatch, before_chunk=before_chunk)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _RecordingClient())

    with pytest.raises(RuntimeError):
        cli.main(["sync"])

    jobs = _jobs(tmp_path)
    assert len(jobs) == 1
    job_id = int(jobs[0]["id"])
    store = _store(tmp_path)
    _worker_marks_job_failed(store, job_id, "analysis failed: RuntimeError: crash")
    # The edge state, asserted rather than assumed: `failed`, nothing charged to a
    # chunk, and finished chunks a rebuild would throw away.
    job = store.get_extraction_job(job_id)
    assert job["status"] == "failed"
    assert int(job["failed_chunks"]) == 0
    assert [c["status"] for c in store.source_chunks(job_id)] == (
        ["done"] * before_chunk + ["pending"] * (len(MARKERS) - before_chunk)
    )
    store.close()
    return job_id, crash


def _edge_state_job(tmp_path, *, done: int = 1):
    """A KB whose newest job is `failed`, has NO failed chunk, and `done` finished.

    The unit-level twin of `_crashed_below_chunk_accounting`. Everything the
    planner compares — artifact (`None`), provider, model, chunk text — matches
    what `_plan_edge` hands it below, so only the chunk rows decide.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/a.txt")
    job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        artifact_id=None,
        source_text=_body(J_MARKERS),
        provider="fake",
        model="m",
        chunk_chars=J_CHUNK_CHARS,
        chunk_overlap_chars=0,
    )
    assert len(store.source_chunks(job_id)) == len(J_MARKERS)
    store.mark_extraction_job_running(job_id)
    for row in store.source_chunks(job_id)[:done]:
        store.mark_chunk_running(int(row["id"]))
        store.mark_chunk_done(int(row["id"]), candidates=1)
    _worker_marks_job_failed(store, job_id, "analysis failed: RuntimeError: crash")
    assert int(store.get_extraction_job(job_id)["failed_chunks"]) == 0
    return store, source_id, job_id


def _plan_edge(store, source_id, **overrides):
    kwargs = {
        "source_id": source_id,
        "artifact_id": None,
        "source_text": _body(J_MARKERS),
        "provider": "fake",
        "model": "m",
        "chunk_chars": J_CHUNK_CHARS,
        "chunk_overlap_chars": 0,
    }
    kwargs.update(overrides)
    return plan_source_extraction(store, **kwargs)


def test_sync_resumes_a_failed_job_whose_crash_never_reached_a_chunk(
    tmp_path, monkeypatch
):
    """THE #524 ASSERTION: the finished chunks are not sent to the LLM a second time.

    A crash below the chunk accounting leaves `failed` with zero failed chunks, and
    planning used to read that as "rebuild fresh" — a new job whose first act is to
    re-extract every chunk the crashed pass had already paid for.
    """
    job_id, crash = _crashed_below_chunk_accounting(tmp_path, monkeypatch)
    crash.heal()
    healthy = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: healthy)

    assert cli.main(["sync"]) == 0

    # The load-bearing line: `alpha`/`bravo` are absent, so the two chunks the
    # crashed pass finished were kept rather than re-sent.
    assert healthy.markers == list(MARKERS[2:])
    jobs = _jobs(tmp_path)
    assert [int(job["id"]) for job in jobs] == [job_id]  # continued, not replaced
    assert jobs[0]["status"] == "done"

    store = _store(tmp_path)
    assert [f["subject"] for f in store.facts()] == list(MARKERS)
    store.close()


def test_sync_reports_the_continued_job_rather_than_a_fresh_one(
    tmp_path, monkeypatch, capsys
):
    """The stdout a user sees for the resumed pass, pinned.

    Not the guard against the branch regressing — the tests either side of this
    one catch that from the chunk text.

    WHAT SEPARATES THE TWO PASSES IS THE PARENTHETICAL, NOT THE NUMBER. Run this
    same scenario with the branch removed and the rebuild sends all six chunks to
    the LLM and still prints a flat `sources/doc.txt: 4 candidate(s)` — the same
    count this pass prints. `run_candidates` counts the candidates a run produced,
    and the two re-sent chunks produce none, because their facts already exist and
    dedupe away. That identity is why the re-send is silent, which is the issue's
    second complaint, and it is the reason a test that pinned only the number
    would pin nothing. Only the continued pass states the two scopes apart —
    `4 candidate(s) this run (resumed job #1: 6 candidate(s) in total)` — so the
    clause in parentheses is the whole visible difference and the only part worth
    holding.
    """
    job_id, crash = _crashed_below_chunk_accounting(tmp_path, monkeypatch)
    crash.heal()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _RecordingClient())
    capsys.readouterr()  # drop the crashed pass's output

    assert cli.main(["sync"]) == 0

    out = capsys.readouterr().out
    assert "4 candidate(s) this run" in out
    assert f"resumed job #{job_id}: 6 candidate(s) in total" in out


def test_plan_retries_a_failed_job_that_holds_a_finished_chunk(tmp_path):
    """Compared as a whole `ExtractionJobPlan` on purpose: asserting `retry_job_id`
    alone would also pass if `resume_job_id`/`exhausted_job_id`/`busy_job_id` came
    along with it, and each of those means something different to `cmd_sync`.

    `retry_job_id` and not `resume_job_id`, which is what the issue's wording
    suggests: `claim_pending_extraction_job`'s CAS is `WHERE status = 'pending'`,
    so on a `failed` job it matches nothing, raises `ExtractionJobBusyError`, and
    `cmd_sync` skips the source with "already running" and returns 0 — forever.
    """
    store, source_id, job_id = _edge_state_job(tmp_path, done=1)
    plan = _plan_edge(store, source_id)
    store.close()
    assert plan == ExtractionJobPlan(retry_job_id=job_id)


def test_plan_rebuilds_a_failed_job_that_finished_no_chunk(tmp_path):
    """The branch is scoped to the harm the issue names, and no wider.

    With nothing `done` there is no finished work for a rebuild to discard, and
    both routes cost the same number of LLM calls, so this stays a rebuild.
    """
    store, source_id, _ = _edge_state_job(tmp_path, done=0)
    plan = _plan_edge(store, source_id)
    store.close()
    assert plan == ExtractionJobPlan()


def test_plan_reads_the_chunk_rows_not_the_job_counter(tmp_path):
    """Pins WHICH source of truth decides, by injecting a disagreement.

    The desync below is hypothetical: `completed_chunks` is written by
    `_refresh_extraction_job` on every `mark_chunk_done`, and no production path
    leaves it lower than the `done` rows. The reason to read the rows is not that
    the counter lies — it is that the text gate above already read the rows, so
    the decision costs no second query and cannot straddle two snapshots, and that
    the sibling counter on the same row IS documented as untrustworthy after a
    job-level failure (`store/db.py`, `surface_stale_engine_facts`). Believing one
    counter and not the other in the same function would be the odd choice.
    """
    store, source_id, job_id = _edge_state_job(tmp_path, done=1)
    store._conn.execute(
        "UPDATE extraction_jobs SET completed_chunks = 0 WHERE id = ?", (job_id,)
    )
    plan = _plan_edge(store, source_id)
    store.close()
    assert plan == ExtractionJobPlan(retry_job_id=job_id)


def test_plan_still_gives_up_when_an_exhausted_chunk_sits_beside_a_finished_one(
    tmp_path,
):
    """ORDER IS LOAD-BEARING: the new branch lives BELOW the failed-chunk branch.

    Hoist it above and a job whose chunk has spent every attempt would be offered
    for retry again the moment any other chunk had finished — the give-up gate
    bypassed and #323's empty-run loop back, on the sources most likely to hit it.
    """
    store, source_id, job_id = _edge_state_job(tmp_path, done=1)
    failed_chunk = store.source_chunks(job_id)[1]
    store.mark_chunk_running(int(failed_chunk["id"]))
    store.mark_chunk_failed(int(failed_chunk["id"]), "provider down")
    store._conn.execute(
        "UPDATE source_chunks SET attempts = ? WHERE id = ?",
        (MAX_CHUNK_ATTEMPTS, int(failed_chunk["id"])),
    )
    # Without this the fixture could silently fail to produce a failed chunk and
    # the assertion below would pass through the new branch instead of the gate.
    assert int(store.get_extraction_job(job_id)["failed_chunks"]) == 1

    plan = _plan_edge(store, source_id)
    store.close()
    assert plan == ExtractionJobPlan(exhausted_job_id=job_id)


def _cancel(store, job_id):
    store._conn.execute(
        "UPDATE extraction_jobs SET status = 'canceled' WHERE id = ?", (job_id,)
    )


def _make_running(store, job_id):
    store._conn.execute(
        "UPDATE extraction_jobs SET status = 'running' WHERE id = ?", (job_id,)
    )


@pytest.mark.parametrize(
    ("case", "mutate", "overrides", "expected"),
    [
        ("canceled job", _cancel, {}, "empty"),
        ("running job", _make_running, {}, "busy"),
        ("artifact changed", None, {"artifact_id": 4242}, "empty"),
        ("body changed", None, {"source_text": _body(SECOND_MARKERS[:3])}, "empty"),
        ("chunk size changed", None, {"chunk_chars": 400}, "empty"),
        ("model changed", None, {"model": "another-model"}, "empty"),
    ],
)
def test_plan_does_not_continue_a_finished_chunk_job_that_is_stale_or_owned(
    tmp_path, case, mutate, overrides, expected
):
    """The new branch is fenced by every gate that already fenced the others.

    One condition is broken per case against the same fixture that
    `test_plan_retries_a_failed_job_that_holds_a_finished_chunk` proves returns
    `retry_job_id` — that test is this one's positive control, without which
    "returns an empty plan" could mean the fixture never built the edge state.

    A `done` job needs no case here: `test_sync_re_extracts_a_source_whose_job_
    already_finished` above already covers a finished job being rebuilt, and it
    goes red if the branch is hoisted to the top of the function.
    """
    store, source_id, job_id = _edge_state_job(tmp_path, done=1)
    if mutate is not None:
        mutate(store, job_id)
    plan = _plan_edge(store, source_id, **overrides)
    store.close()
    if expected == "busy":
        assert plan == ExtractionJobPlan(busy_job_id=job_id)
    else:
        assert plan == ExtractionJobPlan()


def test_a_reproducing_crash_below_chunk_accounting_stops_paying_the_llm(
    tmp_path, monkeypatch
):
    """The cost of a fault that keeps happening, measured over repeated syncs.

    Rebuilding also reset the attempt budget, so the same source paid for the same
    finished chunks on EVERY sync — 2, 4, 6 LLM calls over three passes on a
    six-chunk source. Continuing the job pays once.

    THE RUN COUNT BELOW IS A CHARACTERISATION, NOT A GUARANTEE. Nothing here
    terminates: this state charges no chunk, `failed_chunk_attempt_status` counts
    only `failed` chunks, and so the job is offered again every sync and burns an
    empty run doing it — at exactly the rate it burned one before this fix, which
    is why it is no regression and is out of scope for #524. Giving it an end
    needs a job-level failure count (#536), and when that lands `runs` will stop
    growing and this line MUST go red. Update it to the new ceiling then; do not
    restore the growth to keep it green.
    """
    job_id, crash = _crashed_below_chunk_accounting(tmp_path, monkeypatch)
    client = _RecordingClient()
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)
    runs = []
    for _ in range(3):
        with pytest.raises(RuntimeError):
            cli.main(["sync"])
        # Each pass leaves the job `running` and each pass's caller terminalises
        # it, the same write `_crashed_below_chunk_accounting` made — so the state
        # under test recurs rather than being reached once.
        store = _store(tmp_path)
        _worker_marks_job_failed(store, job_id, "analysis failed: RuntimeError: crash")
        store.close()
        runs.append(_run_count(tmp_path))

    # Not one further chunk reached the LLM across three more passes.
    assert client.markers == []
    assert [int(job["id"]) for job in _jobs(tmp_path)] == [job_id]
    assert runs == [2, 3, 4]  # CHARACTERISATION — see the docstring


def test_a_failed_job_that_finished_everything_terminalises_without_the_llm(
    tmp_path, monkeypatch
):
    """The edge state can also arrive with nothing left to do, and it must settle.

    A broad `except` that fires AFTER the chunk loop writes `failed` over a job
    whose chunks are all `done`. Continuing it claims the job, finds no pending
    chunk, and finishes: no LLM call, and the empty run it opens is opened ONCE,
    unlike the rebuild it replaces, which re-extracted every chunk.
    """
    _ingest(tmp_path, monkeypatch, body=_body(J_MARKERS))
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _RecordingClient())
    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    job_id = int(jobs[0]["id"])
    assert jobs[0]["status"] == "done"
    store = _store(tmp_path)
    _worker_marks_job_failed(store, job_id, "analysis failed: RuntimeError: crash")
    assert [c["status"] for c in store.source_chunks(job_id)] == ["done"] * len(J_MARKERS)
    store.close()
    runs_before = _run_count(tmp_path)

    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _RefusingClient())
    assert cli.main(["sync"]) == 0

    jobs = _jobs(tmp_path)
    assert [int(job["id"]) for job in jobs] == [job_id]
    assert jobs[0]["status"] == "done"
    assert _run_count(tmp_path) == runs_before + 1  # exactly one, then settled


# --- J2: the same state with a `running` chunk in it, and what it may cost ----


class _FailsOneChunk:
    """A provider that is down for exactly one chunk, named by its marker.

    `_RecordingClient(fail_on_call=N)` counts calls, which makes "the same chunk
    fails again" depend on how many chunks a pass happens to reach — and the
    passes below reach different numbers. Keying on the chunk's own text keeps
    the fault attached to the chunk across every pass, and `down` is what a
    recovered provider looks like.
    """

    name = "fake"

    def __init__(self, marker: str):
        self.marker = marker
        self.down = True
        self.seen: list[str] = []

    @property
    def markers(self) -> list[str]:
        return [text.split()[0] for text in self.seen]

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.seen.append(source_text)
        if self.down and source_text.startswith(self.marker):
            raise LLMError("provider down")
        return [ExtractedFact(source_text.split()[0], "seen_in", "source", 0.9)]


class _ReleaseWriteFails:
    """Lose the write that settles a claim, leaving the chunk `running`.

    THE INJECTION POINT IS THE WHOLE POINT, one layer below
    `_CrashBelowChunkAccounting`. The chunk's own LLM call fails, so
    `process_extraction_job`'s broad clause reaches its release point,
    `_release_claimed_chunk` — which settles the claim through
    `Store.mark_chunk_failed`. Failing THAT write is what leaves a chunk `running`
    under a job that then comes to rest: the store error escapes the pipeline
    without the chunk ever being settled, and the caller's broad `except` writes
    the job `failed`. `tests/test_sources_running_chunk_display.py` names the same
    route on a live KB. So the state reached here is the edge state of section J
    WITH a `running` chunk in it, which is the case the branch's cost argument has
    to answer for.
    """

    def __init__(self, monkeypatch):
        # Imported here, not in the module's import block: this section is the
        # only reader of it, and the class it needs is the error the store raises.
        import sqlite3

        self.error = sqlite3.OperationalError
        self.armed = True
        real = Store.mark_chunk_failed

        def failing(store, chunk_id, error):
            if self.armed:
                raise self.error("database is locked")
            return real(store, chunk_id, error)

        monkeypatch.setattr(Store, "mark_chunk_failed", failing)

    def heal(self) -> None:
        self.armed = False


def _j_chunk_states(store, job_id) -> list[tuple[str, int]]:
    return [(str(row["status"]), int(row["attempts"])) for row in store.source_chunks(job_id)]


def _pass_that_loses_the_release(tmp_path, release) -> int:
    """One sync that cannot record its chunk failure, terminalised by its caller.

    The `failed` write is `_worker_marks_job_failed`, for the reason recorded
    there: nothing in `verinote/pipeline` writes it, and today only `web/app.py`
    has the broad clause that does.
    """
    with pytest.raises(release.error):
        cli.main(["sync"])
    jobs = _jobs(tmp_path)
    assert len(jobs) == 1  # the job is continued, never rebuilt beside itself
    job_id = int(jobs[0]["id"])
    store = _store(tmp_path)
    _worker_marks_job_failed(
        store, job_id, "analysis failed: OperationalError: database is locked"
    )
    store.close()
    return job_id


def _release_write_failed(tmp_path, monkeypatch):
    """A KB whose newest job is the edge state, with a `running` chunk in it."""
    _ingest(tmp_path, monkeypatch, body=_body(J_MARKERS))
    release = _ReleaseWriteFails(monkeypatch)
    client = _FailsOneChunk(J_MARKERS[1])
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)
    job_id = _pass_that_loses_the_release(tmp_path, release)
    return job_id, client, release


def test_plan_continues_a_finished_chunk_job_that_also_holds_a_running_one(tmp_path):
    """A `running` chunk does NOT keep the job out of the new branch.

    The unit-level half of the pair: `_edge_state_job` is the same fixture
    `test_plan_retries_a_failed_job_that_holds_a_finished_chunk` uses, with one
    chunk claimed and never settled — the row a lost release leaves.
    `test_a_recurring_lost_release_does_not_spend_the_chunks_attempt_budget` below
    is what says a real pass produces this state rather than only this fixture.

    The branch is deliberately unguarded against `running`, and what pays for that
    is the refund in the claim, not an exclusion here: excluding it would send the
    job back to being rebuilt, throwing the finished chunk away again.
    """
    store, source_id, job_id = _edge_state_job(tmp_path, done=1)
    claimed = store.source_chunks(job_id)[1]
    store.mark_chunk_running(int(claimed["id"]))
    assert _j_chunk_states(store, job_id) == [("done", 1), ("running", 1), ("pending", 0)]
    assert int(store.get_extraction_job(job_id)["failed_chunks"]) == 0

    plan = _plan_edge(store, source_id)
    store.close()

    assert plan == ExtractionJobPlan(retry_job_id=job_id)


def test_a_recurring_lost_release_does_not_spend_the_chunks_attempt_budget(
    tmp_path, monkeypatch
):
    """THE COST OF THE `running` CASE: a lost release must not give up on a source.

    The claim rewinds a stray `running` chunk as it takes the job, and it refunds
    the attempt while doing so (`Store.claim_extraction_job_for_retry`). Keep the
    attempt instead and this exact sequence walks the chunk to `attempts` 1/2/3/4
    over four passes of a condition that is not the chunk's content; the first
    genuine content failure after the write heals then takes it to 5, past
    `MAX_CHUNK_ATTEMPTS`, and planning gives up on the source FOR GOOD — where the
    rebuild this branch replaced recovered it as soon as the provider came back.
    That is what the second half of this test buys: the budget assertion alone
    would still pass a fix that stopped counting but left the source stranded.

    The finished chunk's marker appearing exactly once across every pass is the
    #524 property holding in this state too — the reason the fix is the refund and
    not excluding `running` chunks from the branch, which recovers the source by
    rebuilding and pays the LLM for `alpha` on all four passes.
    """
    job_id, client, release = _release_write_failed(tmp_path, monkeypatch)
    budget = []
    for _ in range(3):
        assert _pass_that_loses_the_release(tmp_path, release) == job_id
        store = _store(tmp_path)
        budget.append(_j_chunk_states(store, job_id)[1][1])
        store.close()

    # Four passes of a host condition, no content budget spent on any of them.
    assert budget == [1, 1, 1]
    store = _store(tmp_path)
    assert _j_chunk_states(store, job_id) == [("done", 1), ("running", 1), ("pending", 0)]
    store.close()
    # The finished chunk was never re-sent, and only the failing one was retried.
    assert client.markers == [J_MARKERS[0]] + [J_MARKERS[1]] * 4

    # The write heals while the provider is still down: NOW the chunk fails for
    # real, and the failure it records is its first.
    release.heal()
    assert cli.main(["sync"]) == 1
    store = _store(tmp_path)
    assert _j_chunk_states(store, job_id) == [("done", 1), ("failed", 1), ("done", 1)]
    store.close()

    # And the provider comes back. The source is recovered, not given up on.
    client.down = False
    assert cli.main(["sync"]) == 0
    store = _store(tmp_path)
    assert _j_chunk_states(store, job_id) == [("done", 1), ("done", 2), ("done", 1)]
    store.close()
    assert [int(job["id"]) for job in _jobs(tmp_path)] == [job_id]
    assert sorted(_facts(tmp_path)) == sorted(J_MARKERS)


def _facts(tmp_path) -> list[str]:
    store = _store(tmp_path)
    try:
        return [str(fact["subject"]) for fact in store.facts()]
    finally:
        store.close()
