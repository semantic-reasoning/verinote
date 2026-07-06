# SPDX-License-Identifier: MPL-2.0
"""Deterministic schema-aware query candidate planning."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import unicodedata

from verinote.pipeline.corroboration import relation_label_matches
from verinote.pipeline.query_intent import QueryIntent, QueryIntentKind
from verinote.pipeline.query_schema import (
    EntityRef,
    QuerySchemaSnapshot,
    RelationSchema,
    SnapshotFact,
    TermRef,
)


@dataclass(frozen=True)
class QueryPlannerBounds:
    max_candidates: int = 32

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or self.max_candidates < 0
        ):
            raise ValueError("max_candidates must be a non-negative integer")


class QueryCandidateFamily(StrEnum):
    DIRECT_OBJECT_LOOKUP = "direct_object_lookup"
    DIRECT_SUBJECT_LOOKUP = "direct_subject_lookup"
    DIRECT_RELATION_LOOKUP = "direct_relation_lookup"
    SUBJECT_RELATION_DISCOVERY = "subject_relation_discovery"
    OBJECT_RELATION_DISCOVERY = "object_relation_discovery"
    EXACT_FACT_FALLBACK = "exact_fact_fallback"
    MANUAL_DRAFT = "manual_draft"


class QueryCandidateDirection(StrEnum):
    SUBJECT_TO_OBJECT = "subject_to_object"
    OBJECT_TO_SUBJECT = "object_to_subject"
    SUBJECT_TO_RELATION = "subject_to_relation"
    OBJECT_TO_RELATION = "object_to_relation"
    SUBJECT_OBJECT_TO_RELATION = "subject_object_to_relation"


@dataclass(frozen=True)
class QueryCandidate:
    query_dl: str
    family: QueryCandidateFamily
    direction: QueryCandidateDirection | None
    relation_display: str | None
    relation_executable: str | None
    subject_executable: str | None
    object_executable: str | None


@dataclass(frozen=True)
class QueryCandidatePlan:
    qid: int
    candidates: tuple[QueryCandidate, ...]
    truncated: bool = False
    reason: str | None = None


def plan_query_candidates(
    intent: QueryIntent,
    snapshot: QuerySchemaSnapshot,
    *,
    qid: int,
    bounds: QueryPlannerBounds = QueryPlannerBounds(),
) -> QueryCandidatePlan:
    """Return bounded deterministic Datalog candidates for a structured intent."""
    if isinstance(qid, bool) or not isinstance(qid, int) or qid < 0:
        raise ValueError("qid must be a non-negative integer")

    if intent.kind == QueryIntentKind.LOOKUP_OBJECT:
        candidates = _lookup_object_candidates(intent, snapshot, qid)
        reason = None
    elif intent.kind == QueryIntentKind.LOOKUP_SUBJECT:
        candidates = _lookup_subject_candidates(intent, snapshot, qid)
        reason = None
    elif intent.kind == QueryIntentKind.LOOKUP_RELATION:
        candidates = _lookup_relation_candidates(intent, snapshot, qid)
        reason = None
    elif intent.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS:
        candidates = _discover_entity_relation_candidates(intent, snapshot, qid)
        reason = None if candidates else "no relation discovery candidates matched the schema"
    else:
        candidates = ()
        reason = f"unsupported intent kind: {intent.kind.value}"

    return _bounded_plan(qid, candidates, bounds, reason=reason)


def _discover_entity_relation_candidates(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot, qid: int
) -> tuple[QueryCandidate, ...]:
    if intent.subject is None:
        return ()
    candidates: list[QueryCandidate] = []
    requested = _relation_requests(intent)
    if requested:
        direct_candidates = list(_lookup_object_candidates(intent, snapshot, qid))
        if direct_candidates:
            return tuple(direct_candidates)
        candidates.extend(_discover_from_relation_examples(intent, snapshot, qid, requested))
        candidates.extend(_discover_from_exact_facts(intent, snapshot, qid, requested))
        return _dedupe_candidates(candidates)
    candidates.extend(_discover_from_relation_examples(intent, snapshot, qid, requested))
    candidates.extend(_discover_from_exact_facts(intent, snapshot, qid, requested))
    return _dedupe_candidates(candidates)


def _discover_from_relation_examples(
    intent: QueryIntent,
    snapshot: QuerySchemaSnapshot,
    qid: int,
    requested: tuple[str, ...],
) -> list[QueryCandidate]:
    if intent.subject is None:
        return []
    candidates: list[QueryCandidate] = []
    for relation in snapshot.relations:
        if requested and not _relation_matches_any(relation, requested):
            continue
        for subject in _matching_entities(relation.subjects, intent.subject.value):
            candidates.append(
                _subject_relation_discovery_candidate(qid, relation.relation, subject)
            )
        for obj in _matching_entities(relation.objects, intent.subject.value):
            candidates.append(
                _object_relation_discovery_candidate(qid, relation.relation, obj)
            )
    return candidates


def _discover_from_exact_facts(
    intent: QueryIntent,
    snapshot: QuerySchemaSnapshot,
    qid: int,
    requested: tuple[str, ...],
) -> list[QueryCandidate]:
    if intent.subject is None:
        return []
    candidates: list[QueryCandidate] = []
    for fact in snapshot.exact_entity_facts:
        if requested and not _fact_relation_matches_any_requested(fact, requested, snapshot):
            continue
        if fact.matched_side in {"subject", "both"} and _entity_ref_matches(
            fact.subject, intent.subject.value
        ):
            candidates.append(
                _subject_relation_discovery_candidate(
                    qid,
                    fact.relation,
                    _entity_from_term_ref(fact.subject),
                )
            )
        if fact.matched_side in {"object", "both"} and _entity_ref_matches(
            fact.object, intent.subject.value
        ):
            candidates.append(
                _object_relation_discovery_candidate(
                    qid,
                    fact.relation,
                    _entity_from_term_ref(fact.object),
                )
            )
    return candidates


def _subject_relation_discovery_candidate(
    qid: int, relation: TermRef, subject: EntityRef
) -> QueryCandidate:
    return QueryCandidate(
        query_dl=_query_dl(
            qid,
            _answer_label(relation),
            f"relation({subject.executable}, {relation.executable}, O)",
        ),
        family=QueryCandidateFamily.SUBJECT_RELATION_DISCOVERY,
        direction=QueryCandidateDirection.SUBJECT_TO_RELATION,
        relation_display=relation.display,
        relation_executable=relation.executable,
        subject_executable=subject.executable,
        object_executable=None,
    )


def _object_relation_discovery_candidate(
    qid: int, relation: TermRef, obj: EntityRef
) -> QueryCandidate:
    return QueryCandidate(
        query_dl=_query_dl(
            qid,
            _answer_label(relation),
            f"relation(S, {relation.executable}, {obj.executable})",
        ),
        family=QueryCandidateFamily.OBJECT_RELATION_DISCOVERY,
        direction=QueryCandidateDirection.OBJECT_TO_RELATION,
        relation_display=relation.display,
        relation_executable=relation.executable,
        subject_executable=None,
        object_executable=obj.executable,
    )


def _lookup_object_candidates(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot, qid: int
) -> tuple[QueryCandidate, ...]:
    if intent.subject is None:
        return ()
    candidates: list[QueryCandidate] = []
    for relation in _matching_relations(intent, snapshot):
        for subject in _matching_entities(relation.subjects, intent.subject.value):
            query_dl = _query_dl(
                qid,
                "O",
                f"relation({subject.executable}, {relation.relation.executable}, O)",
            )
            candidates.append(
                QueryCandidate(
                    query_dl=query_dl,
                    family=QueryCandidateFamily.DIRECT_OBJECT_LOOKUP,
                    direction=QueryCandidateDirection.SUBJECT_TO_OBJECT,
                    relation_display=relation.relation.display,
                    relation_executable=relation.relation.executable,
                    subject_executable=subject.executable,
                    object_executable=None,
                )
            )
    candidates.extend(
        _lookup_object_exact_fact_candidates(intent, snapshot, qid)
    )
    return _dedupe_candidates(candidates)


def _lookup_subject_candidates(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot, qid: int
) -> tuple[QueryCandidate, ...]:
    if intent.object is None:
        return ()
    candidates: list[QueryCandidate] = []
    for relation in _matching_relations(intent, snapshot):
        for obj in _matching_entities(relation.objects, intent.object.value):
            query_dl = _query_dl(
                qid,
                "S",
                f"relation(S, {relation.relation.executable}, {obj.executable})",
            )
            candidates.append(
                QueryCandidate(
                    query_dl=query_dl,
                    family=QueryCandidateFamily.DIRECT_SUBJECT_LOOKUP,
                    direction=QueryCandidateDirection.OBJECT_TO_SUBJECT,
                    relation_display=relation.relation.display,
                    relation_executable=relation.relation.executable,
                    subject_executable=None,
                    object_executable=obj.executable,
                )
            )
    candidates.extend(
        _lookup_subject_exact_fact_candidates(intent, snapshot, qid)
    )
    return _dedupe_candidates(candidates)


def _lookup_relation_candidates(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot, qid: int
) -> tuple[QueryCandidate, ...]:
    subject_value = intent.subject.value if intent.subject is not None else None
    object_value = intent.object.value if intent.object is not None else None
    candidates: list[QueryCandidate] = []

    for relation in snapshot.relations:
        subjects = (
            _matching_entities(relation.subjects, subject_value)
            if subject_value is not None
            else (None,)
        )
        objects = (
            _matching_entities(relation.objects, object_value)
            if object_value is not None
            else (None,)
        )
        for subject in subjects:
            for obj in objects:
                body = _lookup_relation_body(subject, obj)
                candidates.append(
                    QueryCandidate(
                        query_dl=_query_dl(qid, "R", body),
                        family=QueryCandidateFamily.DIRECT_RELATION_LOOKUP,
                        direction=_lookup_relation_direction(subject, obj),
                        relation_display=None,
                        relation_executable=None,
                        subject_executable=(
                            subject.executable if subject is not None else None
                        ),
                        object_executable=(
                            obj.executable if obj is not None else None
                        ),
                    )
                )
    candidates.extend(_lookup_relation_exact_fact_candidates(intent, snapshot, qid))
    return _dedupe_candidates(candidates)


def _lookup_object_exact_fact_candidates(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot, qid: int
) -> list[QueryCandidate]:
    if intent.subject is None:
        return []
    candidates: list[QueryCandidate] = []
    for fact in snapshot.exact_entity_facts:
        if fact.matched_side not in {"subject", "both"}:
            continue
        if not _entity_ref_matches(fact.subject, intent.subject.value):
            continue
        if not _fact_relation_matches_any(fact, intent, snapshot):
            continue
        candidates.append(
            QueryCandidate(
                query_dl=_query_dl(
                    qid,
                    "O",
                    f"relation({fact.subject.executable}, {fact.relation.executable}, O)",
                ),
                family=QueryCandidateFamily.EXACT_FACT_FALLBACK,
                direction=QueryCandidateDirection.SUBJECT_TO_OBJECT,
                relation_display=fact.relation.display,
                relation_executable=fact.relation.executable,
                subject_executable=fact.subject.executable,
                object_executable=None,
            )
        )
    return candidates


def _lookup_subject_exact_fact_candidates(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot, qid: int
) -> list[QueryCandidate]:
    if intent.object is None:
        return []
    candidates: list[QueryCandidate] = []
    for fact in snapshot.exact_entity_facts:
        if fact.matched_side not in {"object", "both"}:
            continue
        if not _entity_ref_matches(fact.object, intent.object.value):
            continue
        if not _fact_relation_matches_any(fact, intent, snapshot):
            continue
        candidates.append(
            QueryCandidate(
                query_dl=_query_dl(
                    qid,
                    "S",
                    f"relation(S, {fact.relation.executable}, {fact.object.executable})",
                ),
                family=QueryCandidateFamily.EXACT_FACT_FALLBACK,
                direction=QueryCandidateDirection.OBJECT_TO_SUBJECT,
                relation_display=fact.relation.display,
                relation_executable=fact.relation.executable,
                subject_executable=None,
                object_executable=fact.object.executable,
            )
        )
    return candidates


def _lookup_relation_exact_fact_candidates(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot, qid: int
) -> list[QueryCandidate]:
    subject_value = intent.subject.value if intent.subject is not None else None
    object_value = intent.object.value if intent.object is not None else None
    candidates: list[QueryCandidate] = []
    for fact in snapshot.exact_entity_facts:
        subject = (
            _entity_from_term_ref(fact.subject)
            if subject_value is not None
            and _entity_ref_matches(fact.subject, subject_value)
            else None
        )
        obj = (
            _entity_from_term_ref(fact.object)
            if object_value is not None
            and _entity_ref_matches(fact.object, object_value)
            else None
        )
        if subject_value is not None and subject is None:
            continue
        if object_value is not None and obj is None:
            continue
        candidates.append(
            QueryCandidate(
                query_dl=_query_dl(qid, "R", _lookup_relation_body(subject, obj)),
                family=QueryCandidateFamily.EXACT_FACT_FALLBACK,
                direction=_lookup_relation_direction(subject, obj),
                relation_display=None,
                relation_executable=None,
                subject_executable=subject.executable if subject is not None else None,
                object_executable=obj.executable if obj is not None else None,
            )
        )
    return candidates


def _lookup_relation_direction(
    subject: EntityRef | None, obj: EntityRef | None
) -> QueryCandidateDirection:
    if subject is not None and obj is not None:
        return QueryCandidateDirection.SUBJECT_OBJECT_TO_RELATION
    if subject is not None:
        return QueryCandidateDirection.SUBJECT_TO_RELATION
    if obj is not None:
        return QueryCandidateDirection.OBJECT_TO_RELATION
    raise ValueError("lookup_relation requires at least one endpoint")


def _lookup_relation_body(subject: EntityRef | None, obj: EntityRef | None) -> str:
    if subject is not None and obj is not None:
        return f"relation({subject.executable}, R, {obj.executable})"
    if subject is not None:
        return f"relation({subject.executable}, R, O)"
    if obj is not None:
        return f"relation(S, R, {obj.executable})"
    raise ValueError("lookup_relation requires at least one endpoint")


def _matching_relations(
    intent: QueryIntent, snapshot: QuerySchemaSnapshot
) -> tuple[RelationSchema, ...]:
    wanted = _relation_requests(intent)
    if not wanted:
        return ()
    return tuple(
        relation
        for relation in snapshot.relations
        if _relation_matches_any(relation, wanted)
    )


def _relation_requests(intent: QueryIntent) -> tuple[str, ...]:
    values: list[str] = []
    if intent.relation is not None:
        values.append(intent.relation.value)
    values.extend(intent.relation_candidates)
    return tuple(dict.fromkeys(_nfc(value) for value in values))


def _relation_matches_any(relation: RelationSchema, wanted: tuple[str, ...]) -> bool:
    aliases = {entry.alias: entry.canonical for entry in relation.aliases}
    observed = [
        _nfc(relation.relation.display),
        _nfc(relation.relation.executable),
        _nfc(relation.canonical_relation),
    ]
    for alias in relation.aliases:
        observed.append(_nfc(alias.alias))
        observed.append(_nfc(alias.canonical))
    if relation.typed is not None:
        observed.append(_nfc(relation.typed.relation))
        observed.append(_nfc(relation.typed.alias))
    return any(
        relation_label_matches(observed_value, wanted_value, aliases)
        for observed_value in observed
        for wanted_value in wanted
    )


def _matching_entities(
    entities: tuple[EntityRef, ...], value: str | None
) -> tuple[EntityRef, ...]:
    if value is None:
        return entities
    wanted = _nfc(value)
    return tuple(
        entity
        for entity in entities
        if wanted in {
            _nfc(entity.display),
            _nfc(entity.executable),
            _nfc(entity.key),
        }
    )


def _entity_ref_matches(ref: TermRef, value: str) -> bool:
    wanted = _nfc(value)
    return wanted in {_nfc(ref.display), _nfc(ref.executable), _nfc(ref.key)}


def _fact_relation_matches_any(
    fact: SnapshotFact, intent: QueryIntent, snapshot: QuerySchemaSnapshot
) -> bool:
    wanted = _relation_requests(intent)
    if not wanted:
        return False
    return _fact_relation_matches_any_requested(fact, wanted, snapshot)


def _fact_relation_matches_any_requested(
    fact: SnapshotFact, wanted: tuple[str, ...], snapshot: QuerySchemaSnapshot
) -> bool:
    aliases = {entry.alias: entry.canonical for entry in snapshot.relation_aliases}
    observed = (_nfc(fact.relation.display), _nfc(fact.relation.executable))
    return any(
        relation_label_matches(observed_value, wanted_value, aliases)
        for observed_value in observed
        for wanted_value in wanted
    )


def _entity_from_term_ref(ref: TermRef) -> EntityRef:
    return EntityRef(
        display=ref.display,
        executable=ref.executable,
        kind=ref.kind,
        key=ref.key,
        fact_count=1,
    )


def _query_dl(qid: int, head_value: str, body: str) -> str:
    return f".decl answer_q{qid}(value: symbol)\nanswer_q{qid}({head_value}) :- {body}."


def _answer_label(relation: TermRef) -> str:
    return relation.executable if relation.kind != "StringLit" else relation.executable


def _bounded_plan(
    qid: int,
    candidates: tuple[QueryCandidate, ...],
    bounds: QueryPlannerBounds,
    *,
    reason: str | None,
) -> QueryCandidatePlan:
    bounded = candidates[: bounds.max_candidates]
    return QueryCandidatePlan(
        qid=qid,
        candidates=bounded,
        truncated=len(candidates) > bounds.max_candidates,
        reason=reason,
    )


def _dedupe_candidates(candidates: list[QueryCandidate]) -> tuple[QueryCandidate, ...]:
    deduped: dict[tuple[str, str | None, str | None, str | None, str | None], QueryCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.query_dl,
            candidate.relation_display,
            candidate.relation_executable,
            candidate.subject_executable,
            candidate.object_executable,
        )
        deduped.setdefault(key, candidate)
    return tuple(deduped.values())


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)
