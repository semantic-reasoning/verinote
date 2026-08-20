# SPDX-License-Identifier: MPL-2.0
"""What the job counters say when a chunk dies mid-insert. Now a guard, not a note.

THIS FILE CHANGED SIDES IN #482, DELIBERATELY. It arrived with #475 as a
CHARACTERISATION test: it recorded that a chunk which raised part-way through its
insert loop left facts in the KB that no counter accounted for, asserted
`candidate_count == 0`, and said in its own docstring that this described a defect
rather than a guarantee. It also said that fixing #482 must turn those assertions
RED, and that the correct response then was to update them to the counted
behaviour — never to restore the drift in order to keep them green.

That is what happened here. `mark_chunk_done` no longer accumulates the column;
`Store._refresh_extraction_job` recomputes it as
`SELECT COUNT(*) FROM facts WHERE job_id = ?`, so the fact this chunk wrote before
it died is counted the moment the chunk is released as `failed`. The assertion
below is `== 1` where it was `== 0`, and the file is now a NO-REGRESSION GUARD: it
fails if the counter ever goes back to being accumulated from what the pipeline
reports instead of counted from what the KB holds.

WHAT DID NOT CHANGE is `completed_chunks == 0`, and keeping it is the point. The
chunk genuinely did not complete. "Its facts are counted" and "it failed" are two
independent statements, both true; the old behaviour made them look like one by
letting the failure suppress the count.

The wider scenarios — the retry that used to make the shortfall permanent, the
locked fact-term sidecar, and the guard against someone adding a `status` filter
to the count — live in `tests/test_job_candidate_count_is_derived.py`. This file
keeps the narrow case #475 first noticed, so the two records stay findable from
each other.
"""

import pytest

from verinote.llm.base import ExtractedFact
from verinote.pipeline.extract import process_extraction_job
from verinote.store import Store


class _TwoFactClient:
    """Two facts for the one chunk, so the insert loop has a middle to die in."""

    name = "stub"

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        return [
            ExtractedFact("alpha", "seen_in", "source", 0.9),
            ExtractedFact("beta", "seen_in", "source", 0.9),
        ]


def test_facts_written_before_a_mid_chunk_failure_are_counted(tmp_path):
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    source_id = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=source_id, provider="fake", model="m", total_chunks=1
    )
    s.add_source_chunks(job_id=job_id, source_id=source_id, chunks=["alpha beta"])

    real_reconcile = s.reconcile_fact
    calls = {"n": 0}

    def _second_insert_raises(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("write failed mid-chunk")
        return real_reconcile(*args, **kwargs)

    s.reconcile_fact = _second_insert_raises

    with pytest.raises(RuntimeError):
        process_extraction_job(s, _TwoFactClient(), job_id=job_id)

    # The first fact is in the KB, with evidence, attributed to this run.
    assert [f["subject"] for f in s.facts()] == ["alpha"]
    # ...and the job counts it now. This assertion read `== 0` before #482.
    job = s.get_extraction_job(job_id)
    assert job["candidate_count"] == 1
    # The chunk still did not complete: counting its facts does not finish it.
    assert job["completed_chunks"] == 0
    # The claim, by contrast, is released — that part is #475's fix.
    chunk = s.source_chunks(job_id)[0]
    assert chunk["status"] == "failed"
    assert "RuntimeError" in chunk["error"]
    s.close()
