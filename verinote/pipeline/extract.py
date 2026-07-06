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

from verinote.engine.terms import TermParseError
from verinote.llm.base import ExtractedFact, LLMClient, LLMError
from verinote.pipeline.chunk import chunk_text
from verinote.pipeline.corroboration import (
    canonical_relation,
    CorroborationPolicyError,
    store_relation_aliases,
)
from verinote.pipeline.normalize import normalize_for_extraction
from verinote.prompts import PromptError, render_prompt
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
_KEY_VALUE_RELATIONS = {"값", "value", "has_value", "label_value"}
_STANDARD_ASCII_RELATIONS = {"value"}
_COPULA_OBJECTS = {"입니다", "이다", "임", "있습니다", "없습니다", "합니다"}
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
    analysis_text = normalize_for_extraction(source_text)
    facts = _extract_chunk_facts(
        client,
        source_text=analysis_text,
        schema_hint=schema_hint,
        root=store.db_path.parent,
    )
    aliases = _relation_aliases_or_error(store)
    rows = _candidate_rows(facts, analysis_text, relation_aliases=aliases)

    source_id = store.add_source(source_path)
    for subject, relation, obj, f in rows:
        fact_id = store.add_fact(
            subject,
            relation,
            obj,
            status="candidate",
            confidence=f.confidence,
            source_id=source_id,
            run_id=run_id,
            note=f.note,
        )
        store.add_fact_evidence(
            fact_id=fact_id,
            source_id=source_id,
            evidence_kind="chunk",
            locator="source",
            snippet=analysis_text,
        )
    return len(rows)


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
    kwargs = {}
    if chunk_chars is not None:
        kwargs["max_chars"] = chunk_chars
    if chunk_overlap_chars is not None:
        kwargs["overlap_chars"] = chunk_overlap_chars
    analysis_text = normalize_for_extraction(source_text)
    chunks = chunk_text(analysis_text, **kwargs)
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
    aliases = _relation_aliases_or_error(store)
    rows = _candidate_rows(facts, source_text, relation_aliases=aliases)
    inserted = 0
    for subject, relation, obj, f in rows:
        if store.fact_exists_for_source(
            source_id=source_id, subject=subject, relation=relation, obj=obj
        ):
            continue
        fact_id = store.add_fact(
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
        store.add_fact_evidence(
            fact_id=fact_id,
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
    try:
        for f in facts:
            f = _canonical_fact(f, aliases)
            if f is None:
                continue
            if _is_normalization_bridge(f):
                continue
            if _has_unbacked_han_translation(f, source_text):
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


def _canonical_fact(
    f: ExtractedFact, relation_aliases: dict[str, str]
) -> ExtractedFact | None:
    """Normalize shallow fact shapes and drop malformed S-P-O fragments."""
    if _is_bad_spo_shape(f):
        return None
    if f.relation_kind == "string" and f.relation.strip() in _KEY_VALUE_RELATIONS:
        return replace(f, relation="value")
    if f.relation_kind == "string":
        relation = canonical_relation(f.relation.strip(), relation_aliases)
        if relation != f.relation:
            return replace(f, relation=relation)
    return f


def _is_bad_spo_shape(f: ExtractedFact) -> bool:
    relation = f.relation.strip()
    obj = f.object.strip()
    if f.object_kind == "string" and obj in _COPULA_OBJECTS:
        return True
    if f.relation_kind == "string" and _compact_text(relation).endswith("여부"):
        return True
    return False


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


def _has_unbacked_ascii_relation(
    f: ExtractedFact, source_text: str, relation_aliases: dict[str, str]
) -> bool:
    """Drop English/snake_case relation labels hallucinated from Korean sources."""
    if _HANGUL_RE.search(source_text) is None:
        return False
    relation = f.relation.strip()
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
