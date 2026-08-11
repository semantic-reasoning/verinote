# SPDX-License-Identifier: MPL-2.0
"""Characterisation: what the job counters say when a chunk dies mid-insert.

CHARACTERISATION, NOT A GUARANTEE. This file records current behaviour so that a
later change to it is visible, and it describes a known defect. It makes no claim
that the behaviour below is correct and must not be read as a no-regression
guard.

`_extract_chunk` writes its candidate facts one at a time and only returns the
count once every one of them has landed; `mark_chunk_done` — which adds that
count to the job's `candidate_count` — runs after it. So a chunk that raises
part-way through its insert loop leaves facts in the KB that no counter accounts
for: the facts are real, carry this run's `run_id`, and are visible on the
provenance pages, while the job reports `candidate_count = 0`.

The chunk itself is released as `failed` (#475), which is the part that is fixed.
The counter drift is not.

FOLLOW-UP ISSUE: #482

That issue owns the drift; this file only records it. #482 says in its own text
that fixing it must turn the assertions below RED, and this docstring says the
same thing from the other side, so neither can be read without finding the other.
When they do go red, the correct response is to update them to the new, counted
behaviour — never to restore the drift in order to keep them green.
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


def test_facts_written_before_a_mid_chunk_failure_are_not_counted(tmp_path):
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
    # ...and no counter knows about it. This is the defect being characterised.
    job = s.get_extraction_job(job_id)
    assert job["candidate_count"] == 0
    assert job["completed_chunks"] == 0
    # The claim, by contrast, is released — that part is #475's fix.
    chunk = s.source_chunks(job_id)[0]
    assert chunk["status"] == "failed"
    assert "RuntimeError" in chunk["error"]
    s.close()
