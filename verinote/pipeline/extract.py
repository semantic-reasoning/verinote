# SPDX-License-Identifier: MPL-2.0
"""Extract candidate facts from sources and persist them as `candidate` rows.

`extract_source` handles one source; `sync_sources` wraps a batch in a single
`runs` row so the whole pass can later be inspected or retired as a unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

from verinote.engine.terms import TermParseError
from verinote.llm.base import ExtractedFact, LLMClient, LLMError
from verinote.pipeline.chunk import chunk_text
from verinote.store import Store
from verinote.store.fact_input import structural_term


_NORMALIZATION_BRIDGE_RELATIONS = {
    "주체",
    "subject",
    "entity",
    "normalized",
    "normalized_as",
    "canonical",
    "canonical_form",
}
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_HAN_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def extract_source(
    store: Store,
    client: LLMClient,
    *,
    source_path: str,
    source_text: str,
    schema_hint: str = "",
    run_id: int | None = None,
) -> int:
    """Run extraction for one source; insert candidates. Returns count inserted.

    Newly extracted facts land as `candidate` — they only become engine input
    after passing the human review gate (see the web review queue). Each fact
    cites its `source` and (when given) the `run` that produced it.
    """
    facts = client.extract_facts(source_text=source_text, schema_hint=schema_hint)
    rows = _candidate_rows(facts, source_text)

    source_id = store.add_source(source_path)
    for subject, relation, obj, f in rows:
        store.add_fact(
            subject,
            relation,
            obj,
            status="candidate",
            confidence=f.confidence,
            source_id=source_id,
            run_id=run_id,
            note=f.note,
        )
    return len(rows)


def create_chunked_extraction_job(
    store: Store,
    *,
    source_id: int,
    source_text: str,
    provider: str | None,
    model: str | None,
) -> int:
    """Create a durable extraction job and its source chunks."""
    chunks = chunk_text(source_text)
    job_id = store.create_extraction_job(
        source_id=source_id,
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
    """Outcome of processing one persisted extraction job."""

    job_id: int
    candidates: int = 0
    completed_chunks: int = 0
    failed_chunks: int = 0


def process_extraction_job(
    store: Store,
    client: LLMClient,
    *,
    job_id: int,
    schema_hint: str = "",
) -> ChunkedExtractionResult:
    """Process pending chunks for one durable extraction job."""
    job = store.get_extraction_job(job_id)
    if job is None:
        raise LLMError(f"missing extraction job: {job_id}")
    source = store.get_source(int(job["source_id"]))
    if source is None:
        raise LLMError(f"missing source for extraction job: {job_id}")

    store.reset_running_chunks(job_id)
    store.mark_extraction_job_running(job_id)
    run_id = store.add_run(provider=job["provider"], model=job["model"])

    candidates = 0
    while chunk := store.next_pending_chunk(job_id):
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
                schema_hint=schema_hint,
            )
        except LLMError as exc:
            store.mark_chunk_failed(int(running["id"]), str(exc))
            continue
        candidates += inserted
        store.mark_chunk_done(int(running["id"]), candidates=inserted)

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
    )


def _extract_chunk(
    store: Store,
    client: LLMClient,
    *,
    source_id: int,
    source_text: str,
    run_id: int,
    job_id: int,
    schema_hint: str = "",
) -> int:
    facts = client.extract_facts(source_text=source_text, schema_hint=schema_hint)
    rows = _candidate_rows(facts, source_text)
    inserted = 0
    for subject, relation, obj, f in rows:
        if store.fact_exists_for_source(
            source_id=source_id, subject=subject, relation=relation, obj=obj
        ):
            continue
        store.add_fact(
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
        inserted += 1
    return inserted


def _candidate_rows(
    facts: list[ExtractedFact], source_text: str
) -> list[tuple[object, object, object, ExtractedFact]]:
    rows = []
    try:
        for f in facts:
            if _is_normalization_bridge(f):
                continue
            if _has_unbacked_han_translation(f, source_text):
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


def _is_normalization_bridge(f: ExtractedFact) -> bool:
    relation = f.relation.strip().lower()
    relation_kind = f.relation_kind
    if relation_kind != "string" or relation not in _NORMALIZATION_BRIDGE_RELATIONS:
        return False
    return f.subject_kind == "term" or f.object_kind == "term"


def _has_unbacked_han_translation(f: ExtractedFact, source_text: str) -> bool:
    """Drop likely Chinese/Hanja translations hallucinated from Korean sources."""
    if _HANGUL_RE.search(source_text) is None:
        return False
    return any(
        _has_han_run_not_in_source(value, source_text)
        for value in (f.subject, f.relation, f.object)
    )


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
