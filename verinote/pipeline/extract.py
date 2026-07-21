# SPDX-License-Identifier: MPL-2.0
"""Extract candidate facts from sources and persist them as `candidate` rows.

`extract_source` handles one source; `sync_sources` wraps a batch in a single
`runs` row so the whole pass can later be inspected or retired as a unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
import unicodedata
from typing import Iterable

from verinote.engine.terms import Compound, TermParseError, parse_term
from verinote.llm.base import ExtractedFact, LLMClient, LLMError
from verinote.pipeline.chunk import chunk_text
from verinote.pipeline.corroboration import (
    canonical_relation,
    CorroborationPolicyError,
    store_relation_aliases,
)
from verinote.pipeline.normalize import normalize_for_extraction
from verinote.pipeline.policy_state import PolicyMissingError, assert_writable
from verinote.prompts import PromptError, render_prompt
from verinote.store import Store
from verinote.store.fact_input import structural_term


class ExtractionJobBusyError(Exception):
    """Another worker owns this extraction job; the caller must back off.

    Deliberately NOT an `LLMError` or `PolicyMissingError`: the web worker turns
    those into `fail_extraction_job`, a write that would corrupt a job another
    worker legitimately owns. This says "someone else has it — touch nothing."
    """

    def __init__(self, job_id: int):
        super().__init__(f"extraction job {job_id} is already owned by another worker")
        self.job_id = job_id


_NORMALIZATION_BRIDGE_RELATIONS = {
    "주체",
    "subject",
    "entity",
    "normalized",
    "normalized_as",
    "canonical",
    "canonical_form",
}
_KEY_VALUE_RELATIONS = {"값", "value", "has_value", "label_value"}
_STANDARD_ASCII_RELATIONS = {"value"}
_COPULA_OBJECTS = {"입니다", "이다", "임", "있습니다", "없습니다", "합니다"}
_TYPED_LITERAL_FUNCTORS = {"amount", "date", "number", "ordinal"}
_UNIT_ONLY_OBJECTS = {"%", "％", "건", "개", "곳", "명", "년", "원", "조", "억", "만"}
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_HAN_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII_RELATION_RE = re.compile(r"^[A-Za-z0-9_ -]+$")
_COMPACT_SEP_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_METRIC_OBJECT_RE = re.compile(
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:%|％|명|곳|건|년|개|원|조|억|만))|"
    r"(?:\d+\s*조(?:\s*\d[\d,]*\s*억)?)|"
    r"(?:\d[\d,]*\s*억)"
)
_RECORD_SPLIT_RE = re.compile(r"[\n\r]+|[。.!?;；]")
_ROLE_CUE_RE = re.compile(r"원문:|대표|대표이사|CTO|CEO|CFO|담당자|발표자|총괄|소속")
# Generic role-designation relations (``샘플인물 역할 샘플직책`` — the SUBJECT holds a
# role, the OBJECT is the role/title). The default policy aliases 역할/직책/직위 to
# ``role`` but deliberately leaves 대표/대표이사 alone (their object is an org or a
# person, not a title — see policy_defaults), so both the aliased and the raw
# labels are listed here; matching is done on the canonicalized relation so an
# aliased KB and a bare KB behave the same. Role-*named* relations (``샘플조직
# 샘플직책 샘플인물``, where the title itself is the relation) are a different, valid
# shape and are deliberately NOT matched here.
_ROLE_DESIGNATION_RELATIONS = {
    "역할", "직책", "직위", "직함", "대표", "대표이사", "role", "title", "position"
}
_ROLE_DESIGNATION_RELATIONS_CF = {relation.casefold() for relation in _ROLE_DESIGNATION_RELATIONS}


def extract_source(
    store: Store,
    client: LLMClient,
    *,
    source_path: str,
    source_text: str,
    schema_hint: str = "",
    run_id: int | None = None,
) -> int:
    """Run extraction for one source; reconcile candidates. Returns count inserted.

    Newly extracted facts land as `candidate` — they only become engine input
    after passing the human review gate (see the web review queue). Each fact
    cites its `source` and (when given) the `run` that produced it.

    Re-extraction reconciles rather than blind-inserting: a triple already stored
    for this source (in any status, a human's rejection included) is left as it
    is, so a re-sync never resurrects a rejected fact (#160). The returned count
    is the rows actually inserted, not the number extracted — the run summary
    must not claim candidates for triples that were suppressed.
    """
    analysis_text = normalize_for_extraction(source_text)
    facts = _extract_chunk_facts(
        client,
        source_text=analysis_text,
        schema_hint=schema_hint,
        root=store.db_path.parent,
    )
    # The write boundary, mirroring `_extract_chunk`'s per-chunk gate. The CLI's
    # start-of-command check cannot see a policy deleted *after* the command began,
    # and `sync_sources` loops one LLM call per source — so a policy that vanishes
    # mid-batch must stop the *next* source's inserts. Placed after the LLM call and
    # before the first DB write, this keeps every source extracted from a halt point
    # out of a KB whose rules no longer exist, while sources written before the halt
    # stay (the run row is left for inspection, as with `LLMError`).
    assert_writable(store)
    aliases = _relation_aliases_or_error(store)
    rows = _candidate_rows(facts, analysis_text, relation_aliases=aliases)

    source_id = store.add_source(source_path)
    inserted = 0
    for subject, relation, obj, f in rows:
        result = store.reconcile_fact(
            subject,
            relation,
            obj,
            status="candidate",
            confidence=f.confidence,
            source_id=source_id,
            run_id=run_id,
            note=f.note,
        )
        if not result.created:
            continue
        store.add_fact_evidence(
            fact_id=result.fact_id,
            source_id=source_id,
            evidence_kind="chunk",
            locator="source",
            snippet=analysis_text,
        )
        inserted += 1
    return inserted


def _chunks_for(
    source_text: str,
    *,
    chunk_chars: int | None = None,
    chunk_overlap_chars: int | None = None,
) -> list[str]:
    """The chunks this config would split `source_text` into, right now.

    Shared by job creation and `plan_source_extraction` so the resume check
    compares a job's persisted chunks against text produced the *same* way. Two
    call sites chunking "the same" text with hand-copied kwargs is how a resume
    predicate silently starts approving jobs whose chunk boundaries have moved.
    """
    kwargs = {}
    if chunk_chars is not None:
        kwargs["max_chars"] = chunk_chars
    if chunk_overlap_chars is not None:
        kwargs["overlap_chars"] = chunk_overlap_chars
    return chunk_text(normalize_for_extraction(source_text), **kwargs)


def latest_source_job_ids(jobs: Iterable) -> dict[int, int]:
    """Newest extraction job id per source, derived from the job rows themselves.

    "This job has been superseded" is not a state worth storing — it is exactly
    "a newer job exists for the same source", and a stored copy can drift out of
    step with the rows it summarises while a derived one cannot. `sources_with_
    counts` already reads a source's current analysis through the same `MAX(id)`
    lens; this is that judgement, in Python, for callers that already hold the
    rows.
    """
    latest: dict[int, int] = {}
    for job in jobs:
        source_id = int(job["source_id"])
        job_id = int(job["id"])
        if job_id > latest.get(source_id, 0):
            latest[source_id] = job_id
    return latest


def is_live_extraction_job(job, latest_job_ids: dict[int, int]) -> bool:
    """True when `job` is unfinished AND still its source's newest job.

    An unfinished job that has been superseded is dead work: a `pending` row
    nothing will ever process, left behind by a re-analysis that replaced it.
    Treating it as live makes the UI poll forever, keeps the Sources page saying
    "analysing", blocks the re-analyse button with a 409, and — worst — lets the
    launcher start a second extraction over a source another job already
    finished. One predicate so all four answers agree.
    """
    if job["status"] not in {"pending", "running"}:
        return False
    return int(job["id"]) == latest_job_ids.get(int(job["source_id"]))


# The total attempt budget for a single chunk before its job gives up. `attempts`
# counts every `mark_chunk_running` — the initial pass plus each auto-retry — so a
# chunk with `attempts >= MAX_CHUNK_ATTEMPTS` has spent the whole budget and its
# job is surfaced as exhausted rather than retried forever (#323). The web retry
# button is a human override and ignores this cap (`max_attempts=None`).
MAX_CHUNK_ATTEMPTS = 3


@dataclass(frozen=True)
class ExtractionJobPlan:
    """What a sync pass should do about one source's newest extraction job.

    At most one field is set. `resume_job_id` picks a rolled-back `pending` job
    back up where it left off; `retry_job_id` re-runs a `failed` job that still has
    retry budget, resetting its failed chunks through the atomic claim;
    `exhausted_job_id` names a `failed` job that has given up — a chunk has failed
    every attempt, so the pass skips it instead of spinning the same dead chunk
    every sync (#323); `busy_job_id` says another process owns it and this pass must
    keep its hands off. No field set means start a fresh job.
    """

    resume_job_id: int | None = None
    retry_job_id: int | None = None
    exhausted_job_id: int | None = None
    busy_job_id: int | None = None


def plan_source_extraction(
    store: Store,
    *,
    source_id: int,
    artifact_id: int | None,
    source_text: str,
    provider: str | None,
    model: str | None,
    chunk_chars: int | None = None,
    chunk_overlap_chars: int | None = None,
    max_chunk_attempts: int = MAX_CHUNK_ATTEMPTS,
) -> ExtractionJobPlan:
    """Decide whether a source's newest job resumes, retries, gives up, or is stale.

    A job rolled back to `pending` (see `_halt_extraction_job`) still holds every
    chunk it finished, and the machinery to carry on already works —
    `next_pending_chunk` skips `done` chunks, `reset_running_chunks` reclaims
    in-flight ones, `candidate_count` accumulates. What was missing was anyone
    asking for it, so every sync started from chunk zero and paid for the same
    LLM calls twice.

    Resuming or retrying is only honest when the job still describes the work in
    front of us, so all of the following must hold:

    * the job is `pending` or `failed` — `done`/`canceled` are decided outcomes,
      and a `running` job may belong to a live worker (see below);
    * it is the source's newest job — an older one has been superseded;
    * its artifact is the artifact we are about to extract. Artifacts are
      content-addressed (`UNIQUE(source_id, kind, checksum)`), so a changed body
      is a different `artifact_id`;
    * its persisted chunks are exactly what this config would produce now. This
      one carries most of the weight: it re-checks the body *and* catches a
      changed chunk size, overlap, or normalisation rule, any of which would make
      the finished chunks cover different text than the pending ones assume;
    * its provider and model match the ones this run would use. Resume across a
      model change and the job row ends up mis-describing its own output.

    These staleness gates guard the retry branch too, not just resume: a human can
    fix the source text, chunk config, or model between one sync and the next, and
    re-running a `failed` job against content it no longer describes is exactly as
    wrong as resuming a `pending` one — either way the job is stale and rebuilt
    fresh, which is why the gates run before the failed-chunk handling below.

    A `failed` job that is NOT stale is decided by its failed chunks. A failed
    chunk is invisible to resume (`reset_running_chunks` rewinds only `running`,
    `next_pending_chunk` claims only `pending`), so the job is re-run through the
    atomic retry claim, which resets the failed chunks under the same lock that
    takes ownership. But the moment ANY failed chunk has spent its whole attempt
    budget (`attempts >= max_chunk_attempts`) the job has given up: it is surfaced
    as `exhausted` so the pass SKIPS it. Checking exhaustion BEFORE offering a
    retry is the anti-spurious-run gate — retrying an all-exhausted job would claim
    it, reset nothing, and burn an empty run every sync, which is exactly the loop
    #323 exists to break. The web retry button stays the human escape hatch: it
    resets every failed chunk regardless of attempt count.

    A `running` job is neither resumed nor replaced. It may belong to a live UI
    worker, and resuming would have `reset_running_chunks` yank that worker's
    in-flight chunk back into the queue — the same chunk sent to the LLM twice.
    """
    jobs = store.source_extraction_jobs()
    latest_job_ids = latest_source_job_ids(jobs)
    latest_id = latest_job_ids.get(source_id)
    if latest_id is None:
        return ExtractionJobPlan()
    job = store.get_extraction_job(latest_id)
    if job is None:
        return ExtractionJobPlan()
    if job["status"] == "running":
        return ExtractionJobPlan(busy_job_id=latest_id)
    if job["status"] not in {"pending", "failed"}:
        return ExtractionJobPlan()
    job_artifact_id = None if job["artifact_id"] is None else int(job["artifact_id"])
    if job_artifact_id != artifact_id:
        return ExtractionJobPlan()
    if (job["provider"], job["model"]) != (provider, model):
        return ExtractionJobPlan()
    persisted = [str(row["text"]) for row in store.source_chunks(latest_id)]
    current = _chunks_for(
        source_text,
        chunk_chars=chunk_chars,
        chunk_overlap_chars=chunk_overlap_chars,
    )
    if persisted != current:
        return ExtractionJobPlan()
    if int(job["failed_chunks"]) > 0:
        _failed, exhausted = store.failed_chunk_attempt_status(
            latest_id, max_attempts=max_chunk_attempts
        )
        if exhausted > 0:
            return ExtractionJobPlan(exhausted_job_id=latest_id)
        return ExtractionJobPlan(retry_job_id=latest_id)
    if job["status"] != "pending":
        # `failed` with no chunk currently failed is an edge state (e.g. a human
        # reset them out of band) — nothing to resume or retry, so rebuild fresh.
        return ExtractionJobPlan()
    return ExtractionJobPlan(resume_job_id=latest_id)


def create_chunked_extraction_job(
    store: Store,
    *,
    source_id: int,
    artifact_id: int | None = None,
    source_text: str,
    provider: str | None,
    model: str | None,
    chunk_chars: int | None = None,
    chunk_overlap_chars: int | None = None,
) -> int:
    """Create a durable extraction job and its source chunks."""
    chunks = _chunks_for(
        source_text,
        chunk_chars=chunk_chars,
        chunk_overlap_chars=chunk_overlap_chars,
    )
    job_id = store.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider=provider,
        model=model,
        total_chunks=len(chunks),
        message=f"Queued: 0/{len(chunks)} chunk(s) complete",
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=chunks)
    if not chunks:
        store.finish_extraction_job(job_id)
    return job_id


@dataclass(frozen=True)
class ChunkedExtractionResult:
    """Outcome of processing one persisted extraction job.

    TWO SCOPES, KEPT APART — the same separation `_halt_extraction_job` insists on
    for its summary. `candidates`/`completed_chunks`/`failed_chunks` are the
    *job's* totals, cumulative across every resume; `run_candidates`/`run_chunks`
    are what *this* call did. Once a job can be resumed the two diverge, and a
    caller that prints the job total as "this run extracted N candidates" is
    reporting work it did not do.
    """

    job_id: int
    candidates: int = 0
    completed_chunks: int = 0
    failed_chunks: int = 0
    run_candidates: int = 0
    run_chunks: int = 0


def process_extraction_job(
    store: Store,
    client: LLMClient,
    *,
    job_id: int,
    schema_hint: str = "",
    retry: bool = False,
    retry_max_attempts: int | None = None,
) -> ChunkedExtractionResult:
    """Process pending chunks for one durable extraction job.

    Raises `PolicyMissingError` if this KB's recorded logic policy file is gone —
    at the start, before each chunk is claimed, and again at the write boundary in
    `_extract_chunk`. A job is long-running (one LLM call per chunk) and the CLI's
    start-of-command check cannot see a policy deleted *after* the job began, so
    the check has to live next to the write itself.

    On a mid-job halt the job is rolled back to `pending` (see
    `_halt_extraction_job`) before the error propagates. Left `running`, the job row
    would simply be false — the job is not running, nothing is processing it — and a
    KB that lies about its own state is what this change exists to stop. It would
    also make recovery depend on the web launcher: `_resume_source_extraction_jobs`
    is the only thing that revives a `running` job (and only when someone next
    starts the UI, by first rolling it back to `pending` so the ownership claim
    below can take it). `pending` is the state that says what is true and that
    every resume path already understands.
    """
    assert_writable(store)
    job = store.get_extraction_job(job_id)
    if job is None:
        raise LLMError(f"missing extraction job: {job_id}")
    source = store.get_source(int(job["source_id"]))
    if source is None:
        raise LLMError(f"missing source for extraction job: {job_id}")

    claimed = (
        store.claim_extraction_job_for_retry(job_id, max_attempts=retry_max_attempts)
        if retry
        else store.claim_pending_extraction_job(job_id)
    )
    if not claimed:
        # Another worker owns this job (a concurrent `verinote sync`, a second UI
        # worker, or the startup resume loop). It may have a chunk in flight, so
        # we must NOT reset its chunks — backing off is the whole point (#240). A
        # `running` job reaches here only via the resume loop, which rolls a
        # crashed job back to `pending` first, so this claim then succeeds. In
        # retry mode the same CAS resets the failed chunks and takes ownership in
        # one locked step, so a racer that loses it here likewise wrote nothing —
        # the manual button and an auto-retry can share a job_id safely (#323).
        raise ExtractionJobBusyError(job_id)
    run_id = store.add_run(provider=job["provider"], model=job["model"])

    candidates = 0
    run_chunks = 0
    try:
        while chunk := store.next_pending_chunk(job_id):
            # Pre-claim gate. Claiming a chunk (`status='running'`, `attempts + 1`)
            # is itself a write, so it must not happen on a halted KB — the check
            # belongs *before* `mark_chunk_running`, not after it.
            assert_writable(store)
            running = store.mark_chunk_running(int(chunk["id"]))
            if running is None:
                continue
            try:
                inserted = _extract_chunk(
                    store,
                    client,
                    source_id=int(source["id"]),
                    source_text=str(running["text"]),
                    run_id=run_id,
                    job_id=job_id,
                    artifact_id=(
                        int(job["artifact_id"]) if job["artifact_id"] is not None else None
                    ),
                    chunk_id=int(running["id"]),
                    schema_hint=schema_hint,
                )
            except LLMError as exc:
                store.mark_chunk_failed(int(running["id"]), str(exc))
                continue
            candidates += inserted
            store.mark_chunk_done(int(running["id"]), candidates=inserted)
            run_chunks += 1
    except PolicyMissingError:
        # The policy vanished mid-job — either before this chunk was claimed (the
        # gate above) or between its LLM call and its first insert (the boundary in
        # `_extract_chunk`). Rewind so the KB is recoverable, then let the error out.
        _halt_extraction_job(
            store,
            job_id=job_id,
            run_id=run_id,
            source_path=str(source["path"]),
            run_candidates=candidates,
            run_chunks=run_chunks,
        )
        raise

    store.finish_extraction_job(job_id)
    final = store.get_extraction_job(job_id)
    summary = (
        f"{source['path']}: {final['completed_chunks']}/{final['total_chunks']} "
        f"chunk(s), {final['candidate_count']} candidate(s), "
        f"{final['failed_chunks']} failed"
    )
    store.set_run_summary(run_id, summary)
    return ChunkedExtractionResult(
        job_id=job_id,
        candidates=int(final["candidate_count"]),
        completed_chunks=int(final["completed_chunks"]),
        failed_chunks=int(final["failed_chunks"]),
        run_candidates=candidates,
        run_chunks=run_chunks,
    )


def _halt_extraction_job(
    store: Store,
    *,
    job_id: int,
    run_id: int,
    source_path: str,
    run_candidates: int,
    run_chunks: int,
) -> None:
    """Rewind a job whose KB went halted mid-flight, and record what really happened.

    The summary must carry the REAL counts. It is tempting to write a constant here
    ("halted; no candidate facts were written") — and it is a lie the moment any
    chunk completed before the halt. Those candidate facts exist, they carry a
    `run_id` pointing at *this* run, and the provenance page would then tell the
    user that the run which produced the facts in front of them wrote nothing. A
    change that exists to stop the KB lying about its state must not plant a new
    lie, so every number below is read back from the KB rather than assumed.

    TWO DIFFERENT SCOPES, AND THEY MUST STAY GRAMMATICALLY APART. `completed`/
    `total` are the *job's* progress, cumulative across every resume; `run_chunks`
    and `run_candidates` are what *this* run did. Phrase them as one clause
    ("halted after 2/3 chunk(s), 1 candidate(s) written by this run") and a resumed
    job reads as "this run did 2 chunks and wrote 1 candidate" when this run may
    have done exactly one chunk. A sentence that has to be cross-checked against the
    job row to be understood is not a fix for a KB that misreports itself.
    """
    job = store.get_extraction_job(job_id)
    completed = int(job["completed_chunks"]) if job is not None else 0
    total = int(job["total_chunks"]) if job is not None else 0
    store.rollback_extraction_job(
        job_id,
        f"Halted: this KB's policy file is missing. Rolled back to pending at "
        f"{completed}/{total} chunk(s). Restore the policy file (or run "
        f"`verinote policy reset --force`), then re-run the analysis.",
    )
    store.set_run_summary(
        run_id,
        f"{source_path}: halted because this KB's policy file went missing; "
        f"job rolled back to pending at job progress {completed}/{total} chunk(s); "
        f"this run wrote {run_candidates} candidate(s) from {run_chunks} chunk(s)",
    )


def _extract_chunk(
    store: Store,
    client: LLMClient,
    *,
    source_id: int,
    source_text: str,
    run_id: int,
    job_id: int,
    artifact_id: int | None = None,
    chunk_id: int | None = None,
    schema_hint: str = "",
) -> int:
    facts = _extract_chunk_facts(
        client,
        source_text=source_text,
        schema_hint=schema_hint,
        root=store.db_path.parent,
    )
    # The write boundary. Re-checked for *every* chunk, after the LLM call and
    # before the first insert: the policy file can vanish while a job is running
    # (a job is many slow LLM calls), and a check done only at job start would let
    # every chunk after the deletion land in a KB whose rules no longer exist.
    assert_writable(store)
    aliases = _relation_aliases_or_error(store)
    rows = _candidate_rows(facts, source_text, relation_aliases=aliases)
    inserted = 0
    for subject, relation, obj, f in rows:
        result = store.reconcile_fact(
            subject,
            relation,
            obj,
            status="candidate",
            confidence=f.confidence,
            source_id=source_id,
            run_id=run_id,
            job_id=job_id,
            note=f.note,
        )
        if not result.created:
            # A non-superseded dedupe hit re-anchors the existing fact at this
            # run's artifact so a later staleness check can tell a fact this run
            # re-observed from one an edit silently dropped; a superseded hit is
            # left to reconcile_fact's suppression event, never re-anchored.
            if result.matched_status != "superseded" and artifact_id is not None:
                store.note_fact_reobserved(
                    fact_id=result.fact_id,
                    source_id=source_id,
                    artifact_id=artifact_id,
                    job_id=job_id,
                    chunk_id=chunk_id,
                    snippet=source_text,
                )
            continue
        store.add_fact_evidence(
            fact_id=result.fact_id,
            source_id=source_id,
            artifact_id=artifact_id,
            job_id=job_id,
            chunk_id=chunk_id,
            evidence_kind="chunk",
            locator="chunk",
            snippet=source_text,
        )
        inserted += 1
    return inserted


def _extract_chunk_facts(
    client: LLMClient, *, source_text: str, schema_hint: str = "", root=None
) -> list[ExtractedFact]:
    facts = client.extract_facts(source_text=source_text, schema_hint=schema_hint)
    if _ROLE_CUE_RE.search(source_text) is None:
        return facts
    focused_schema_hint = _focused_role_schema_hint(schema_hint, root=root)
    try:
        facts.extend(
            client.extract_facts(
                source_text=source_text,
                schema_hint=focused_schema_hint,
            )
        )
    except LLMError:
        pass
    return facts


def _focused_role_schema_hint(schema_hint: str, *, root=None) -> str:
    if root is None:
        root = "."
    try:
        focused_role_prompt = render_prompt(root, "focused-role-extraction")
    except PromptError as exc:
        raise LLMError(str(exc)) from exc
    if not schema_hint:
        return focused_role_prompt
    return f"{schema_hint}\n{focused_role_prompt}"


def _relation_aliases_or_error(store: Store) -> dict[str, str]:
    try:
        return store_relation_aliases(store)
    except CorroborationPolicyError as exc:
        raise LLMError(str(exc)) from exc


def _candidate_rows(
    facts: list[ExtractedFact],
    source_text: str,
    *,
    relation_aliases: dict[str, str] | None = None,
) -> list[tuple[object, object, object, ExtractedFact]]:
    rows = []
    aliases = relation_aliases or {}
    role_bearers = _role_bearer_subjects(facts, aliases)
    try:
        for f in facts:
            f = _canonical_fact(f)
            if f is None:
                continue
            if _is_normalization_bridge(f, aliases):
                continue
            if _is_reversed_role_designation(f, role_bearers, aliases):
                continue
            if _has_unbacked_han_translation(f, source_text, aliases):
                continue
            if _has_unbacked_ascii_relation(f, source_text, aliases):
                continue
            if _has_unsupported_metric_subject(f, source_text):
                continue
            rows.append(
                (
                    _extracted_value(f.subject, f.subject_kind),
                    _extracted_value(f.relation, f.relation_kind),
                    _extracted_value(f.object, f.object_kind),
                    f,
                )
            )
    except TermParseError as exc:
        raise LLMError(f"malformed extracted structural term: {exc}") from exc
    return rows


def _canonical_fact(f: ExtractedFact) -> ExtractedFact | None:
    """Normalize shallow fact shapes and drop malformed S-P-O fragments.

    Relation aliases are applied at read time only (`engine_input`), never here:
    overwriting the source's own label at write time destroys it irrecoverably
    and defeats the `relation_raw` preservation, UI alias badge, and alias-file
    re-decisions that read-time normalization exists to provide (#252). Each
    later legitimacy filter canonicalizes the relation for its own decision, so
    dropping the write-time merge keeps their behavior identical while storage
    keeps the raw label. The `값`->`value` rewrite below is a separate, hardcoded
    shape normalization with no read-time handler, so it stays.
    """
    if _is_bad_spo_shape(f):
        return None
    if f.relation_kind == "string" and f.relation.strip() in _KEY_VALUE_RELATIONS:
        return replace(f, relation="value")
    return f


def _is_bad_spo_shape(f: ExtractedFact) -> bool:
    relation = f.relation.strip()
    obj = f.object.strip()
    if _slot_is_typed_literal(f.subject, f.subject_kind):
        return True
    if _slot_is_typed_literal(f.relation, f.relation_kind):
        return True
    if f.object_kind == "string" and obj in _COPULA_OBJECTS:
        return True
    if f.object_kind == "string" and obj in _UNIT_ONLY_OBJECTS:
        return True
    if f.relation_kind == "string" and _compact_text(relation).endswith("여부"):
        return True
    return False


def _slot_is_typed_literal(value: str, _kind: str) -> bool:
    try:
        term = parse_term(value)
    except TermParseError:
        return False
    return isinstance(term, Compound) and term.functor in _TYPED_LITERAL_FUNCTORS


def _is_normalization_bridge(
    f: ExtractedFact, relation_aliases: dict[str, str]
) -> bool:
    if f.relation_kind != "string":
        return False
    # Canonicalize first so an alias key still matches the bridge set (#252): the
    # relation is now stored raw, so this filter no longer receives it canonical.
    relation = canonical_relation(f.relation.strip(), relation_aliases).lower()
    if relation not in _NORMALIZATION_BRIDGE_RELATIONS:
        return False
    return f.subject_kind == "term" or f.object_kind == "term"


def _norm_entity(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _is_role_designation_relation(
    relation: str, relation_kind: str, aliases: dict[str, str]
) -> bool:
    """True when ``relation`` canonicalizes to a generic role-designation label."""
    if relation_kind != "string":
        return False
    canonical = canonical_relation(_norm_entity(relation), aliases)
    return canonical.strip().casefold() in _ROLE_DESIGNATION_RELATIONS_CF


def _role_bearer_subjects(
    facts: list[ExtractedFact], aliases: dict[str, str]
) -> set[str]:
    """Entities extracted as the SUBJECT of a role designation in this batch.

    Such an entity holds a role, so it is a person/role-bearer — it must not also
    appear as the OBJECT of a role designation. This co-occurrence signal is what
    distinguishes ``샘플조직 역할 샘플인물`` (reversed) from ``샘플인물 역할 샘플직책``
    (correct); it depends on both facts landing in the same extraction batch.
    """
    return {
        _norm_entity(f.subject)
        for f in facts
        if f.subject_kind == "string"
        and _is_role_designation_relation(f.relation, f.relation_kind, aliases)
    }


def _is_reversed_role_designation(
    f: ExtractedFact, role_bearers: set[str], aliases: dict[str, str]
) -> bool:
    """Drop a role designation whose direction is reversed (``org role person``).

    The single, low-false-positive signal is that the OBJECT is itself a
    role-bearer — it appears as the SUBJECT of a role designation in this batch, so
    it is a person who holds a role and cannot also BE the role value of something
    else. This is the only signal that stays correct once the policy collapses
    대표/역할/직책 into one ``role`` relation: a plain org-subject check would also
    drop the valid ``조직 대표 사람`` (org's representative), which shares the same
    canonical shape. Role-*named* relations such as ``샘플조직 샘플직책 샘플인물`` are a
    valid shape and are not matched here — only the generic role designations are.
    """
    if not _is_role_designation_relation(f.relation, f.relation_kind, aliases):
        return False
    return f.object_kind == "string" and _norm_entity(f.object) in role_bearers


def _has_unbacked_han_translation(
    f: ExtractedFact, source_text: str, relation_aliases: dict[str, str]
) -> bool:
    """Drop likely Chinese/Hanja translations hallucinated from Korean sources.

    Only the relation is canonicalized before the check (#252): subject and
    object are never aliased, so they must be tested on their raw source labels.
    """
    if _HANGUL_RE.search(source_text) is None:
        return False
    relation = canonical_relation(f.relation.strip(), relation_aliases)
    return any(
        _has_han_run_not_in_source(value, source_text)
        for value in (f.subject, relation, f.object)
    )


def _has_unbacked_ascii_relation(
    f: ExtractedFact, source_text: str, relation_aliases: dict[str, str]
) -> bool:
    """Drop English/snake_case relation labels hallucinated from Korean sources."""
    if _HANGUL_RE.search(source_text) is None:
        return False
    # Canonicalize first so an aliased ASCII key (e.g. `founded`) is checked as
    # its policy-backed canonical, not dropped as unbacked (#252): the relation is
    # now stored raw, so this filter no longer receives it already canonical.
    relation = canonical_relation(f.relation.strip(), relation_aliases)
    allowed_ascii = _policy_backed_ascii_relations(relation_aliases)
    if relation.casefold() in allowed_ascii:
        return False
    if _ASCII_RELATION_RE.fullmatch(relation) is None:
        return False
    return _compact_text(relation) not in _compact_text(source_text)


def _policy_backed_ascii_relations(relation_aliases: dict[str, str]) -> set[str]:
    allowed = {item.casefold() for item in _STANDARD_ASCII_RELATIONS}
    for canonical in relation_aliases.values():
        relation = unicodedata.normalize("NFC", canonical).strip()
        if _ASCII_RELATION_RE.fullmatch(relation):
            allowed.add(relation.casefold())
    return allowed


def _has_unsupported_metric_subject(f: ExtractedFact, source_text: str) -> bool:
    """Drop numeric facts whose subject is absent from the local evidence record."""
    if _METRIC_OBJECT_RE.search(f.object) is None:
        return False
    subject = _compact_text(f.subject)
    if not subject:
        return True
    for record in _metric_evidence_records(f, source_text):
        if subject in _compact_text(record):
            return False
    return True


def _metric_evidence_records(f: ExtractedFact, source_text: str) -> list[str]:
    records = []
    compact_object = _compact_text(f.object)
    compact_relation = _compact_text(f.relation)
    for record in _RECORD_SPLIT_RE.split(source_text):
        compact_record = _compact_text(record)
        if compact_object and compact_object in compact_record:
            records.append(record)
        elif compact_relation and compact_relation in compact_record:
            records.append(record)
    return records


def _compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return _COMPACT_SEP_RE.sub("", normalized).casefold()


def _has_han_run_not_in_source(value: str, source_text: str) -> bool:
    return any(match.group(0) not in source_text for match in _HAN_RUN_RE.finditer(value))


def _extracted_value(value: str, kind: str) -> object:
    if kind == "term":
        return structural_term(value)
    return value


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one `sync_sources` pass over a batch of sources."""

    run_id: int
    per_source: list[tuple[str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(n for _, n in self.per_source)


def sync_sources(
    store: Store,
    client: LLMClient,
    sources: Iterable[tuple[str, str]],
    *,
    provider: str | None,
    model: str | None,
    schema_hint: str = "",
) -> SyncResult:
    """Extract a batch of `(source_path, source_text)` pairs under one run.

    Opens a `runs` row (recording provider/model), links every produced fact to
    it, then writes a one-line summary. Any `LLMError` raised by the client
    propagates to the caller — the partial run row is left for inspection.
    """
    run_id = store.add_run(provider=provider, model=model)
    per_source: list[tuple[str, int]] = []
    for source_path, source_text in sources:
        n = extract_source(
            store,
            client,
            source_path=source_path,
            source_text=source_text,
            schema_hint=schema_hint,
            run_id=run_id,
        )
        per_source.append((source_path, n))
    result = SyncResult(run_id=run_id, per_source=per_source)
    store.set_run_summary(
        run_id, f"{len(per_source)} source(s), {result.total} candidate(s)"
    )
    return result
