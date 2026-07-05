# SPDX-License-Identifier: MPL-2.0
"""Deterministic schema-aware query candidate planning."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata

from verinote.pipeline.corroboration import relation_label_matches
from verinote.pipeline.query_intent import QueryIntent, QueryIntentKind
from verinote.pipeline.query_schema import (
    EntityRef,
    QuerySchemaSnapshot,
    RelationSchema,
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


@dataclass(frozen=True)
class QueryCandidate:
    query_dl: str
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
    else:
        candidates = ()
        reason = f"unsupported intent kind: {intent.kind.value}"

    return _bounded_plan(qid, candidates, bounds, reason=reason)


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
                    relation_display=relation.relation.display,
                    relation_executable=relation.relation.executable,
                    subject_executable=subject.executable,
                    object_executable=None,
                )
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
                    relation_display=relation.relation.display,
                    relation_executable=relation.relation.executable,
                    subject_executable=None,
                    object_executable=obj.executable,
                )
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
    return _dedupe_candidates(candidates)


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


def _query_dl(qid: int, head_value: str, body: str) -> str:
    return f".decl answer_q{qid}(value: symbol)\nanswer_q{qid}({head_value}) :- {body}."


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
    return tuple(dict.fromkeys(candidates))


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)
