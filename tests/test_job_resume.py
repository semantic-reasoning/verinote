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
from verinote.llm.base import ExtractedFact
from verinote.pipeline import create_chunked_extraction_job
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

    def __init__(self, *, delete_policy_on_call: int | None = None, policy_path=None):
        self.seen: list[str] = []
        self._delete_on = delete_policy_on_call
        self._policy_path = policy_path

    @property
    def markers(self) -> list[str]:
        return [text.split()[0] for text in self.seen]

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.seen.append(source_text)
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
    # Not one chunk was claimed: resuming would have `reset_running_chunks` pull
    # the other process's in-flight chunk back and send it to the LLM twice.
    assert {chunk["status"] for chunk in chunks} == {"pending"}
    assert {int(chunk["attempts"]) for chunk in chunks} == {0}
    store.close()
    err = capsys.readouterr().err
    assert f"extraction job #{job_id} is already running" in err


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
