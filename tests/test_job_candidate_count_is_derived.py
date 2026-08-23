# SPDX-License-Identifier: MPL-2.0
"""#482: the job's candidate tally is COUNTED from the fact rows, not accumulated.

`candidate_count` used to be accumulated: `mark_chunk_done` added each chunk's
reported insert count to the column. That runs AFTER `_extract_chunk` returns, so
a chunk whose insert loop raised part-way had already written facts that nothing
counted -- and the shortfall was permanent, because a retry re-ran the chunk and
`reconcile_fact` deduped those same facts (`if not result.created`), returning a
smaller number the second time round. Measured on the pre-fix code with three
facts and a failure on the second: the job came to rest `done` holding three fact
rows while reporting `candidate_count = 2`.

The column is now `SELECT COUNT(*) FROM facts WHERE job_id = ?`, recomputed by
`Store._refresh_extraction_job`. Counting rows cannot drift from the rows.

The last test here is a GUARD, not a scenario: the count deliberately carries no
`status` predicate, because the column records what a job CREATED and must not
shrink as reviewers work through the queue. A future tidy-up that adds
`AND status = 'candidate'` looks like a tightening and is a silent change of
meaning; that test is what stops it.

Fixtures are synthetic throughout (AGENTS.md): `s_alpha`/`s_beta`/`s_gamma`.
"""

import pytest

from verinote.llm.base import ExtractedFact
from verinote.pipeline.extract import process_extraction_job
from verinote.store import Store
from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreLockedError


class _ThreeFactClient:
    """Three facts for the one chunk, so the insert loop has a middle to die in."""

    name = "stub"

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        return [
            ExtractedFact("s_alpha", "seen_in", "source", 0.9),
            ExtractedFact("s_beta", "seen_in", "source", 0.9),
            ExtractedFact("s_gamma", "seen_in", "source", 0.9),
        ]


def _one_chunk_job(store, *, chunks=("alpha beta gamma",)):
    source_id = store.add_source("sources/a.txt")
    job_id = store.create_extraction_job(
        source_id=source_id, provider="fake", model="m", total_chunks=len(chunks)
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=list(chunks))
    return job_id


def _fact_rows_for_job(store, job_id):
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM facts WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    )


def _raise_on_nth_reconcile(store, n, exc):
    """Let the first (n-1) reconciles land, then raise from inside the insert loop."""
    real = store.reconcile_fact
    calls = {"n": 0}

    def _patched(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            raise exc
        return real(*args, **kwargs)

    store.reconcile_fact = _patched
    return real


def test_a_partial_chunk_and_its_retry_agree_with_the_fact_rows(tmp_path):
    """The #482 reproduction: a retry must not record the deduped short count.

    Pre-fix this asserted 2 == 3 and failed, which is the point of the test.
    """
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    job_id = _one_chunk_job(s)

    real = _raise_on_nth_reconcile(s, 2, RuntimeError("write failed mid-chunk"))
    with pytest.raises(RuntimeError):
        process_extraction_job(s, _ThreeFactClient(), job_id=job_id)

    # What the real callers (`cli.py`, `web/app.py`) do with an escaped exception.
    s.fail_extraction_job(job_id, "RuntimeError: write failed mid-chunk")
    s.reconcile_fact = real
    process_extraction_job(s, _ThreeFactClient(), job_id=job_id, retry=True)

    job = s.get_extraction_job(job_id)
    # The literal pins the scenario; the equality pins the invariant.
    assert _fact_rows_for_job(s, job_id) == 3
    assert job["candidate_count"] == 3
    assert job["candidate_count"] == _fact_rows_for_job(s, job_id)
    s.close()


def test_a_partial_chunk_publishes_the_facts_it_did_write(tmp_path):
    """The fact that landed before the raise is counted immediately, not eventually.

    `_release_claimed_chunk` -> `mark_chunk_failed` -> `_refresh_extraction_job` is
    the route. The chunk is still `failed` and still incomplete: "the facts are
    counted" and "the chunk did not finish" are two independent, both-true
    statements about this job.
    """
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    job_id = _one_chunk_job(s)

    _raise_on_nth_reconcile(s, 2, RuntimeError("write failed mid-chunk"))
    with pytest.raises(RuntimeError):
        process_extraction_job(s, _ThreeFactClient(), job_id=job_id)

    job = s.get_extraction_job(job_id)
    assert [f["subject"] for f in s.facts()] == ["s_alpha"]
    assert job["candidate_count"] == 1
    assert job["completed_chunks"] == 0
    assert s.source_chunks(job_id)[0]["status"] == "failed"
    s.close()


def test_a_sidecar_lock_mid_chunk_leaves_the_job_count_true(tmp_path):
    """A locked fact-term sidecar strikes INSIDE the loop, and rolls the job back.

    `add_fact` writes the DuckDB term, so `DuckDBFactTermStoreLockedError` is
    reachable mid-loop (#169) -- unlike `PolicyMissingError`, whose
    `assert_writable` gate sits ahead of the loop. `rollback_extraction_job` is
    then the one rewind a part-written chunk reaches without passing
    `_refresh_extraction_job`, which is why it recounts the column itself.
    """
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    job_id = _one_chunk_job(s, chunks=("alpha beta",))

    _raise_on_nth_reconcile(
        s, 2, DuckDBFactTermStoreLockedError("held by another process")
    )
    with pytest.raises(DuckDBFactTermStoreLockedError):
        process_extraction_job(s, _ThreeFactClient(), job_id=job_id)

    job = s.get_extraction_job(job_id)
    assert job["status"] == "pending"
    assert job["candidate_count"] == 1
    assert job["candidate_count"] == _fact_rows_for_job(s, job_id)
    s.close()


def test_a_sidecar_lock_summary_counts_the_fact_it_wrote(tmp_path):
    """The run summary must not say "0 candidate(s)" over a fact it just wrote.

    `_back_off_from_locked_sidecar`'s sibling `_halt_extraction_job` promises the
    numbers in its summary are "read back from the KB rather than assumed", and
    names `run_chunks` as its one exception; the per-run CANDIDATE tally is not
    that exception, and was assumed all the same, via `candidates += inserted`.
    Measured pre-fix: this summary read "this run wrote 0 candidate(s) from 0
    chunk(s)" while one fact carrying that `run_id` sat in the KB.
    """
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    job_id = _one_chunk_job(s, chunks=("alpha beta",))

    _raise_on_nth_reconcile(
        s, 2, DuckDBFactTermStoreLockedError("held by another process")
    )
    with pytest.raises(DuckDBFactTermStoreLockedError):
        process_extraction_job(s, _ThreeFactClient(), job_id=job_id)

    run_id = int(s.facts()[0]["run_id"])
    summary = s.get_run(run_id)["summary"]
    assert "this run wrote 1 candidate(s)" in summary
    assert "0 candidate(s)" not in summary
    s.close()


def test_a_reviewed_fact_does_not_shrink_the_count(tmp_path):
    """GUARD: the count carries no `status` predicate, and must not grow one.

    `candidate_count` records what the job CREATED. Adding
    `AND status = 'candidate'` would make a finished job's number fall as a
    reviewer works through the queue -- the job's own history rewriting itself
    under the reader, which is the failure mode #482 exists to remove.
    """
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    job_id = _one_chunk_job(s)
    process_extraction_job(s, _ThreeFactClient(), job_id=job_id)

    before = s.get_extraction_job(job_id)["candidate_count"]
    assert before == 3

    fact_id = int(s.facts()[0]["id"])
    s.toggle_review(fact_id)
    s.accept_fact(fact_id)
    assert {f["status"] for f in s.facts()} != {"candidate"}

    s._refresh_extraction_job(job_id)
    assert s.get_extraction_job(job_id)["candidate_count"] == before
    s.close()
