# SPDX-License-Identifier: MPL-2.0
"""Deterministic trust summary read model for facts."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata
from typing import Any

from verinote.engine.terms import render_term
from verinote.pipeline.corroboration import (
    CompetingValue,
    canonical_relation,
    normalize_typed_value,
    store_relation_aliases,
    store_single_valued_conflicts,
    store_typed_relations,
    TypedRelationSpec,
)
from verinote.store import ENGINE_STATUSES, REVIEW_STATUSES, Store


@dataclass(frozen=True)
class FactTriple:
    subject: str
    relation: str
    object: str


@dataclass(frozen=True)
class SourceMetadata:
    id: int
    path: str
    kind: str


@dataclass(frozen=True)
class RunMetadata:
    id: int
    provider: str | None
    model: str | None
    summary: str


@dataclass(frozen=True)
class JobMetadata:
    id: int
    status: str
    provider: str | None
    model: str | None
    artifact_path: str | None
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    message: str


@dataclass(frozen=True)
class EvidenceAnchor:
    id: int
    source_id: int
    source_path: str
    artifact_id: int | None
    artifact_path: str | None
    job_id: int | None
    chunk_id: int | None
    chunk_index: int | None
    chunk_status: str | None
    evidence_kind: str
    start_offset: int | None
    end_offset: int | None
    locator: str
    snippet: str


@dataclass(frozen=True)
class SupportSummary:
    source_count: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ConflictValueSummary:
    object: str
    source_count: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ConflictSummary:
    subject: str
    relation: str
    values: tuple[ConflictValueSummary, ...]


@dataclass(frozen=True)
class TypedValueSummary:
    relation: str
    type: str
    alias: str
    normalized_value: int | None


@dataclass(frozen=True)
class AuditEntry:
    id: int
    event_type: str
    actor: str
    source_id: int | None
    job_id: int | None
    chunk_id: int | None
    rule_name: str
    at: str

    @property
    def action(self) -> str:
        return self.event_type


@dataclass(frozen=True)
class FactTrustSummary:
    fact_id: int
    display: FactTriple
    canonical_terms: FactTriple | None
    status: str
    review_eligible: bool
    engine_input: bool
    confidence: float
    note: str
    source: SourceMetadata | None
    run: RunMetadata | None
    job: JobMetadata | None
    evidence: tuple[EvidenceAnchor, ...]
    support: SupportSummary
    canonical_relation: str
    typed_value: TypedValueSummary | None
    conflict: ConflictSummary | None
    audit: tuple[AuditEntry, ...]
    trust_labels: tuple[str, ...]

    @property
    def source_backed(self) -> bool:
        return bool(self.evidence)

    @property
    def conflicted(self) -> bool:
        return self.conflict is not None


def fact_trust_summary(store: Store, fact_id: int) -> FactTrustSummary | None:
    """Return deterministic trust metadata for one fact, or None if missing."""
    fact = store.get_fact(fact_id)
    if fact is None:
        return None

    display = FactTriple(
        subject=str(fact["subject"]),
        relation=str(fact["relation"]),
        object=str(fact["object"]),
    )
    aliases = store_relation_aliases(store)
    typed = store_typed_relations(store)
    relation = canonical_relation(display.relation, aliases)
    support = _support_summary(store, display, aliases, typed)
    conflict = _conflict_summary(store, display.subject, relation)
    typed_value = _typed_value_summary(typed, relation, display.object)
    evidence = tuple(_evidence_anchor(row) for row in store.fact_evidence(fact_id))
    status = str(fact["status"])

    return FactTrustSummary(
        fact_id=int(fact["id"]),
        display=display,
        canonical_terms=_canonical_terms(store, fact_id),
        status=status,
        review_eligible=status in REVIEW_STATUSES,
        engine_input=status in ENGINE_STATUSES,
        confidence=float(fact["confidence"]),
        note=str(fact["note"]),
        source=_source_metadata(store, fact["source_id"]),
        run=_run_metadata(store, fact["run_id"]),
        job=_job_metadata(store, fact["job_id"]),
        evidence=evidence,
        support=support,
        canonical_relation=relation,
        typed_value=typed_value,
        conflict=conflict,
        audit=tuple(
            AuditEntry(
                id=int(row["id"]),
                event_type=str(row["event_type"]),
                actor=str(row["actor"]),
                source_id=_optional_int(row["source_id"]),
                job_id=_optional_int(row["job_id"]),
                chunk_id=_optional_int(row["chunk_id"]),
                rule_name=str(row["rule_name"]),
                at=str(row["at"]),
            )
            for row in store.fact_events(fact_id)
        ),
        trust_labels=_trust_labels(
            status=status,
            evidence=evidence,
            support=support,
            conflict=conflict,
        ),
    )


def _support_summary(
    store: Store,
    display: FactTriple,
    aliases: dict[str, str],
    typed: dict[str, TypedRelationSpec],
) -> SupportSummary:
    relation = canonical_relation(display.relation, aliases)
    object_key = _object_key(relation, display.object, typed)
    sources: set[str] = set()
    for row in store.facts(statuses=ENGINE_STATUSES):
        if str(row["subject"]) != display.subject:
            continue
        row_relation = canonical_relation(str(row["relation"]), aliases)
        if row_relation != relation:
            continue
        if _object_key(row_relation, str(row["object"]), typed) != object_key:
            continue
        source_path = str(row["source_path"] or "").strip()
        if source_path:
            sources.add(source_path)
    return SupportSummary(source_count=len(sources), sources=tuple(sorted(sources)))


def _conflict_summary(
    store: Store, subject: str, relation: str
) -> ConflictSummary | None:
    for item in store_single_valued_conflicts(store):
        if item.subject == subject and item.relation == relation:
            return ConflictSummary(
                subject=item.subject,
                relation=item.relation,
                values=tuple(_conflict_value(value) for value in item.values),
            )
    return None


def _conflict_value(value: CompetingValue) -> ConflictValueSummary:
    return ConflictValueSummary(
        object=value.object,
        source_count=value.source_count,
        sources=value.sources,
    )


def _typed_value_summary(
    typed: dict[str, Any], relation: str, obj: str
) -> TypedValueSummary | None:
    spec = _typed_spec(typed, relation)
    if spec is None:
        return None
    return TypedValueSummary(
        relation=relation,
        type=spec.type,
        alias=spec.alias,
        normalized_value=normalize_typed_value(spec.type, obj, spec.units),
    )


def _object_key(
    relation: str, obj: str, typed: dict[str, TypedRelationSpec]
) -> tuple[str, object]:
    spec = _typed_spec(typed, relation)
    if spec is not None:
        scalar = normalize_typed_value(spec.type, obj, spec.units)
        if scalar is not None:
            return ("scalar", scalar)
    return ("raw", obj)


def _typed_spec(
    typed: dict[str, TypedRelationSpec], relation: str
) -> TypedRelationSpec | None:
    return typed.get(relation) or typed.get(unicodedata.normalize("NFC", relation))


def _canonical_terms(store: Store, fact_id: int) -> FactTriple | None:
    terms = store.get_fact_terms(fact_id)
    if terms is None:
        return None
    return FactTriple(
        subject=render_term(terms[0]),
        relation=render_term(terms[1]),
        object=render_term(terms[2]),
    )


def _source_metadata(store: Store, source_id: object) -> SourceMetadata | None:
    if source_id is None:
        return None
    row = store.get_source(int(source_id))
    if row is None:
        return None
    return SourceMetadata(id=int(row["id"]), path=str(row["path"]), kind=str(row["kind"]))


def _run_metadata(store: Store, run_id: object) -> RunMetadata | None:
    if run_id is None:
        return None
    row = store.get_run(int(run_id))
    if row is None:
        return None
    return RunMetadata(
        id=int(row["id"]),
        provider=row["provider"],
        model=row["model"],
        summary=str(row["summary"]),
    )


def _job_metadata(store: Store, job_id: object) -> JobMetadata | None:
    if job_id is None:
        return None
    row = store.get_extraction_job_detail(int(job_id))
    if row is None:
        return None
    return JobMetadata(
        id=int(row["id"]),
        status=str(row["status"]),
        provider=row["provider"],
        model=row["model"],
        artifact_path=row["artifact_path"],
        total_chunks=int(row["total_chunks"]),
        completed_chunks=int(row["completed_chunks"]),
        failed_chunks=int(row["failed_chunks"]),
        message=str(row["message"]),
    )


def _evidence_anchor(row: Any) -> EvidenceAnchor:
    return EvidenceAnchor(
        id=int(row["id"]),
        source_id=int(row["source_id"]),
        source_path=str(row["source_path"]),
        artifact_id=_optional_int(row["artifact_id"]),
        artifact_path=row["artifact_path"],
        job_id=_optional_int(row["job_id"]),
        chunk_id=_optional_int(row["chunk_id"]),
        chunk_index=_optional_int(row["chunk_index"]),
        chunk_status=row["chunk_status"],
        evidence_kind=str(row["evidence_kind"]),
        start_offset=_optional_int(row["start_offset"]),
        end_offset=_optional_int(row["end_offset"]),
        locator=str(row["locator"]),
        snippet=str(row["snippet"]),
    )


def _trust_labels(
    *,
    status: str,
    evidence: tuple[EvidenceAnchor, ...],
    support: SupportSummary,
    conflict: ConflictSummary | None,
) -> tuple[str, ...]:
    labels = ["source_backed" if evidence else "evidence_missing"]
    if support.source_count == 0:
        labels.append("unsupported")
    elif support.source_count == 1:
        labels.append("single_source")
    else:
        labels.append("corroborated")
    if conflict is not None:
        labels.append("conflicted")
    if status in ENGINE_STATUSES:
        labels.append("reviewed")
    if status == "superseded":
        labels.append("superseded")
    return tuple(labels)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
