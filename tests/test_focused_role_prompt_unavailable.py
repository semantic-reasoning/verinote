# SPDX-License-Identifier: MPL-2.0
"""An unreadable focused-role prompt costs the chunk nothing (#544).

THE DISTINCTION THESE TESTS PIN. A prompt override the machine cannot read is a
condition of the host (a file it cannot decode), not of the chunk's content.
#539 normalised that read failure to an `LLMError`, and the chunk loop treats an
`LLMError` as a per-chunk, content-attributable failure: it releases the claim as
`failed` (spending a retry attempt) and `continue`s to the next chunk. So one
corrupt override burned one retry attempt AND one provider call per chunk, and
`MAX_CHUNK_ATTEMPTS` of those passes gave up on the source for good — over a file
the user can fix (#269's rule, measured in #544).

The fix routes it the way the two siblings route their conditions
(`PolicyMissingError`, `DuckDBFactTermStoreLockedError`): as a
`PromptUnavailableError` the chunk loop re-raises WITHOUT charging (the claim is
rewound, the attempt refunded) and the outer handler rolls the job back to
`pending` around it. And the hint is resolved before any provider call, so no
call is spent either.

THREE OUTCOMES MUST STAY APART, and the tests below separate them on purpose:
`failed` (the pre-#544 LLMError path, which charges), `pending` with the attempt
kept (a clause that rewinds but does not refund — the arrangement that still gives
up on a source), and `pending` with the attempt refunded (what is wanted).
"""

import pytest

from verinote.llm.base import ExtractedFact
from verinote.pipeline.extract import (
    MAX_CHUNK_ATTEMPTS,
    ExtractionJobPlan,
    create_chunked_extraction_job,
    plan_source_extraction,
    process_extraction_job,
)
from verinote.prompts import PromptUnavailableError
from verinote.store import Store

# A role-cue source: the focused-role pass is owed to every chunk, so a broken
# override is hit on the very first chunk. The cues are English (CTO/CEO/CFO)
# on purpose: `normalize_for_extraction` only rewrites Korean role+name pairs, so
# the source text survives to the chunks verbatim and the split is deterministic
# (exactly one clean chunk per line), the same shape `test_chunk_claim_sidecar_lock`
# pins with "alpha/beta/gamma".
SOURCE_TEXT = "alpha CTO\n\nbeta CEO\n\ngamma CFO"
CHUNK_CHARS = 9
CHUNK_OVERLAP_CHARS = 0
PROVIDER = "fake"
MODEL = "m"


class _ChunkClient:
    """One fact per chunk, and a call counter so a wasted call is visible."""

    name = "stub"

    def __init__(self):
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls += 1
        return [ExtractedFact(source_text.strip(), "seen_in", "source", 0.9)]


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
    assert [c["text"] for c in store.source_chunks(job_id)] == [
        "alpha CTO",
        "beta CEO",
        "gamma CFO",
    ]
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


def _broken_override(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"focused role \xff\xfe and a bad byte")


def test_a_broken_focused_role_override_requeues_the_chunk_and_rewinds_the_job(
    tmp_path,
):
    """All four outcomes of the release, in the one place they have to agree.

    The chunk goes back `pending` with its attempt refunded, the job is rolled
    back to `pending`, the plan resumes (not rebuild, not give-up), and the
    provider made ZERO calls — the hint surfaced before any call. The
    `resume_job_id` assertion is the one that catches a half-fix: charge the chunk
    (the pre-#544 LLMError path) and the plan is no longer a clean resume.
    """
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)
    _broken_override(tmp_path / "policy" / "prompts" / "focused-role-extraction.md")
    client = _ChunkClient()

    with pytest.raises(PromptUnavailableError):
        process_extraction_job(s, client, job_id=job_id)

    assert _chunk_states(s, job_id) == [("pending", 0), ("pending", 0), ("pending", 0)]
    assert s.source_chunks(job_id)[0]["error"] == ""
    assert s.get_extraction_job(job_id)["status"] == "pending"
    assert _plan(s, source_id) == ExtractionJobPlan(resume_job_id=job_id)
    assert client.calls == 0, "the broken hint must surface before any provider call"
    s.close()


def test_a_broken_focused_role_override_records_no_chunk_failure(tmp_path):
    """The requeue is not a chunk failure a reader has to be told about.

    `mark_chunk_failed` leaves a `chunk_failed` row someone may later query; this
    leaves none. What IS recorded is the job rollback, so the pause is not silent.
    """
    s = _store(tmp_path)
    _source_id, job_id = _three_chunk_job(s)
    _broken_override(tmp_path / "policy" / "prompts" / "focused-role-extraction.md")

    with pytest.raises(PromptUnavailableError):
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
    assert "focused-role extraction prompt could not be loaded" in job["message"]
    s.close()


def test_a_broken_focused_role_override_leaves_the_whole_budget_for_a_real_failure(
    tmp_path,
):
    """The refund, isolated: broken-override passes must not bring a real failure
    closer to giving up.

    Three broken-override passes first — the number of times the error invites the
    operator to fix the file before `MAX_CHUNK_ATTEMPTS` would be gone — and then
    the chunk starts failing for real. It must still get its full three attempts.

    THIS IS THE TEST THAT SEPARATES the refund from a bare rewind. Requeue without
    the attempt refund and the three broken passes leave the chunk at three
    attempts, so the FIRST genuine failure reaches `attempts = 4 >=
    MAX_CHUNK_ATTEMPTS` and the source is abandoned on its first real error.
    """
    s = _store(tmp_path)
    source_id, job_id = _three_chunk_job(s)
    override = tmp_path / "policy" / "prompts" / "focused-role-extraction.md"
    _broken_override(override)

    for _ in range(3):
        with pytest.raises(PromptUnavailableError):
            process_extraction_job(s, _ChunkClient(), job_id=job_id)
        assert _chunk_states(s, job_id)[0] == ("pending", 0)
        assert _plan(s, source_id) == ExtractionJobPlan(resume_job_id=job_id)

    # The override is fixed; the chunk now fails on its own content (a non-
    # LLMError, so the loop releases it as failed), and the budget is spent one
    # attempt per pass exactly as if the broken passes had never happened.
    override.unlink()
    for expected_attempts, expected_plan in (
        (1, ExtractionJobPlan(retry_job_id=job_id)),
        (2, ExtractionJobPlan(retry_job_id=job_id)),
        (3, ExtractionJobPlan(exhausted_job_id=job_id)),
    ):
        with pytest.raises(ValueError):
            process_extraction_job(
                s,
                _FailsChunk(),
                job_id=job_id,
                retry=expected_attempts > 1,
                retry_max_attempts=MAX_CHUNK_ATTEMPTS if expected_attempts > 1 else None,
            )
        s.fail_extraction_job(job_id, "analysis failed: boom")
        assert _chunk_states(s, job_id)[0] == ("failed", expected_attempts)
        assert _plan(s, source_id) == expected_plan

    s.close()


class _FailsChunk:
    """A client whose every call raises a content failure (not an availability one)."""

    name = "stub"

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        raise ValueError("boom")
