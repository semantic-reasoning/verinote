# SPDX-License-Identifier: MPL-2.0
"""Review workbench view models for deterministic trust signals."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata

from verinote.pipeline.corroboration import (
    canonical_relation,
    normalize_typed_value,
    store_functional_relations,
    store_relation_aliases,
    store_typed_relations,
    TypedRelationSpec,
)
from verinote.store import ENGINE_STATUSES, REVIEW_STATUSES, Store


@dataclass(frozen=True)
class WorkbenchFact:
    id: int
    subject: str
    relation: str
    object: str
    status: str
    source: str
    canonical_relation: str
    relation_alias: str
    typed_normalization: str


@dataclass(frozen=True)
class CorroboratedGroup:
    subject: str
    relation: str
    object: str
    sources: tuple[str, ...]
    facts: tuple[WorkbenchFact, ...]
    related_candidates: tuple[WorkbenchFact, ...]

    @property
    def source_count(self) -> int:
        return len(self.sources)


@dataclass(frozen=True)
class ConflictValue:
    object: str
    sources: tuple[str, ...]
    facts: tuple[WorkbenchFact, ...]
    typed_normalization: str

    @property
    def source_count(self) -> int:
        return len(self.sources)


@dataclass(frozen=True)
class ConflictGroup:
    subject: str
    relation: str
    values: tuple[ConflictValue, ...]
    related_candidates: tuple[WorkbenchFact, ...]


@dataclass(frozen=True)
class TrustWorkbench:
    corroborated: tuple[CorroboratedGroup, ...]
    conflicts: tuple[ConflictGroup, ...]


def trust_workbench(store: Store) -> TrustWorkbench:
    """Build source-backed review tasks without trusting model confidence."""
    aliases = store_relation_aliases(store)
    typed = store_typed_relations(store)
    single_valued = {canonical_relation(r, aliases) for r in store_functional_relations(store)}

    engine: dict[tuple[str, str, tuple[str, object]], list[WorkbenchFact]] = {}
    candidates: dict[tuple[str, str, tuple[str, object]], list[WorkbenchFact]] = {}
    by_subject_relation: dict[
        tuple[str, str], dict[tuple[str, object], list[WorkbenchFact]]
    ] = {}
    candidate_by_subject_relation: dict[tuple[str, str], list[WorkbenchFact]] = {}

    for row in store.facts():
        status = str(row["status"])
        source = str(row["source_path"] or "").strip()
        if not source:
            continue
        subject = str(row["subject"])
        relation = str(row["relation"])
        obj = str(row["object"])
        canonical = canonical_relation(relation, aliases)
        spec = _typed_spec(canonical, typed)
        object_key, normalization = _normalized_object_key(obj, spec)
        fact = WorkbenchFact(
            id=int(row["id"]),
            subject=subject,
            relation=relation,
            object=obj,
            status=status,
            source=source,
            canonical_relation=canonical,
            relation_alias=_relation_alias(relation, canonical),
            typed_normalization=normalization,
        )
        key = (subject, canonical, object_key)
        sr_key = (subject, canonical)
        if status in ENGINE_STATUSES:
            engine.setdefault(key, []).append(fact)
            if canonical in single_valued:
                by_subject_relation.setdefault(sr_key, {}).setdefault(object_key, []).append(fact)
        elif status in REVIEW_STATUSES:
            candidates.setdefault(key, []).append(fact)
            candidate_by_subject_relation.setdefault(sr_key, []).append(fact)

    corroborated = []
    for (subject, relation, object_key), facts in sorted(engine.items()):
        sources = tuple(sorted({fact.source for fact in facts}))
        if len(sources) < 2:
            continue
        representative = sorted({fact.object for fact in facts})[0]
        corroborated.append(
            CorroboratedGroup(
                subject=subject,
                relation=relation,
                object=representative,
                sources=sources,
                facts=tuple(sorted(facts, key=lambda fact: fact.id)),
                related_candidates=tuple(
                    sorted(
                        candidates.get((subject, relation, object_key), []),
                        key=lambda fact: fact.id,
                    )
                ),
            )
        )

    conflicts = []
    for (subject, relation), value_groups in sorted(by_subject_relation.items()):
        if len(value_groups) < 2:
            continue
        values = []
        for facts in value_groups.values():
            sources = tuple(sorted({fact.source for fact in facts}))
            representative = sorted({fact.object for fact in facts})[0]
            normalization = sorted(
                {fact.typed_normalization for fact in facts if fact.typed_normalization}
            )
            values.append(
                ConflictValue(
                    object=representative,
                    sources=sources,
                    facts=tuple(sorted(facts, key=lambda fact: fact.id)),
                    typed_normalization=normalization[0] if normalization else "",
                )
            )
        conflicts.append(
            ConflictGroup(
                subject=subject,
                relation=relation,
                values=tuple(sorted(values, key=lambda value: value.object)),
                related_candidates=tuple(
                    sorted(
                        candidate_by_subject_relation.get((subject, relation), []),
                        key=lambda fact: fact.id,
                    )
                ),
            )
        )

    return TrustWorkbench(
        corroborated=tuple(corroborated),
        conflicts=tuple(conflicts),
    )


def _typed_spec(
    relation: str, typed: dict[str, TypedRelationSpec]
) -> TypedRelationSpec | None:
    return typed.get(relation) or typed.get(unicodedata.normalize("NFC", relation))


def _normalized_object_key(
    obj: str, spec: TypedRelationSpec | None
) -> tuple[tuple[str, object], str]:
    if spec is None:
        return ("raw", obj), ""
    scalar = normalize_typed_value(spec.type, obj, spec.units)
    if scalar is None:
        return ("raw", obj), ""
    return ("scalar", scalar), f"{spec.alias}={scalar}"


def _relation_alias(relation: str, canonical: str) -> str:
    if unicodedata.normalize("NFC", relation) == canonical:
        return ""
    return f"{relation} -> {canonical}"
