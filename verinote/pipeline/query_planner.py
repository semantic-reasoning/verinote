# SPDX-License-Identifier: MPL-2.0
"""Deterministic schema-aware query candidate planning."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from verinote.engine.terms import parse_term, term_compare_key
from verinote.pipeline.corroboration import canonical_relation, relation_label_matches
from verinote.pipeline.query_intent import ConjunctiveHop, QueryIntent, QueryIntentKind
from verinote.pipeline.query_schema import (
    EntityRef,
    QuerySchemaSnapshot,
    RelationSchema,
    SnapshotFact,
    TermRef,
)
from verinote.text import nfc as _nfc


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
    CONJUNCTIVE_TWO_HOP = "conjunctive_two_hop"
    CONJUNCTIVE_FILTER = "conjunctive_filter"


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
    elif intent.kind == QueryIntentKind.CONJUNCTIVE_LOOKUP:
        candidates, truncated = _conjunctive_two_hop_candidates(
            intent, snapshot, qid, max_candidates=bounds.max_candidates
        )
        return QueryCandidatePlan(
            qid=qid,
            candidates=candidates,
            truncated=truncated,
            reason=None if candidates else "no two-hop relation path matched the schema",
        )
    elif intent.kind == QueryIntentKind.CONJUNCTIVE_FILTER:
        candidates, truncated = _conjunctive_filter_candidates(
            intent, snapshot, qid, max_candidates=bounds.max_candidates
        )
        return QueryCandidatePlan(
            qid=qid,
            candidates=candidates,
            truncated=truncated,
            reason=None if candidates else "no conjunctive condition path matched the schema",
        )
    else:
        candidates = ()
        reason = f"unsupported intent kind: {intent.kind.value}"

    return _bounded_plan(qid, candidates, bounds, reason=reason)


def _conjunctive_two_hop_candidates(
    intent: QueryIntent,
    snapshot: QuerySchemaSnapshot,
    qid: int,
    *,
    max_candidates: int,
) -> tuple[tuple[QueryCandidate, ...], bool]:
    """Plan only fact-supported Entity -> Var -> Answer two-hop chains."""
    if len(intent.hops) != 2:
        return (), False
    if snapshot.join_facts_truncated:
        return (), True
    first_hop, second_hop = intent.hops
    requested_first = (_nfc(first_hop.relation.value),)
    requested_second = (_nfc(second_hop.relation.value),)
    candidates: list[QueryCandidate] = []
    second_by_subject: dict[str, dict[str, SnapshotFact]] = {}
    for fact in snapshot.join_facts:
        if _fact_relation_matches_any_requested(fact, requested_second, snapshot):
            second_by_subject.setdefault(fact.subject.key, {}).setdefault(
                fact.relation.executable, fact
            )
    seen_first_shapes: set[tuple[str, str, str]] = set()
    seen_candidate_shapes: set[tuple[str, str, str]] = set()
    for first in snapshot.join_facts:
        if not _entity_ref_matches(first.subject, first_hop.subject.value):
            continue
        if not _fact_relation_matches_any_requested(first, requested_first, snapshot):
            continue
        first_shape = (first.subject.executable, first.relation.executable, first.object.key)
        if first_shape in seen_first_shapes:
            continue
        seen_first_shapes.add(first_shape)
        for second in second_by_subject.get(first.object.key, {}).values():
            candidate_shape = (
                first.subject.executable,
                first.relation.executable,
                second.relation.executable,
            )
            if candidate_shape in seen_candidate_shapes:
                continue
            seen_candidate_shapes.add(candidate_shape)
            if len(candidates) >= max_candidates:
                return tuple(candidates), True
            candidates.append(
                QueryCandidate(
                    query_dl=_query_dl(
                        qid,
                        second_hop.object.value,
                        "relation("
                        f"{first.subject.executable}, {first.relation.executable}, {first_hop.object.value}), "
                        f"relation({first_hop.object.value}, {second.relation.executable}, {second_hop.object.value})",
                    ),
                    family=QueryCandidateFamily.CONJUNCTIVE_TWO_HOP,
                    direction=None,
                    relation_display=None,
                    relation_executable=None,
                    subject_executable=first.subject.executable,
                    object_executable=None,
                )
            )
    return tuple(candidates), False


def _conjunctive_filter_candidates(
    intent: QueryIntent,
    snapshot: QuerySchemaSnapshot,
    qid: int,
    *,
    max_candidates: int,
) -> tuple[tuple[QueryCandidate, ...], bool]:
    """Plan two fact-supported conditions with one shared answer variable."""
    if len(intent.conditions) != 2 or not intent.answer_var:
        return (), False
    if snapshot.join_facts_truncated:
        return (), True
    if _conjunctive_filter_conditions_are_semantic_duplicates(
        intent.conditions, intent.answer_var, snapshot
    ):
        # Alias expansion would turn these into the same predicate. Treating one
        # fact as evidence for both conditions would falsely verify a non-
        # independent conjunction, so do not evaluate a partial candidate set.
        return (), True

    condition_matches = [
        _conjunctive_condition_matches(condition, intent.answer_var, snapshot)
        for condition in intent.conditions
    ]
    if not condition_matches[0] or not condition_matches[1]:
        return (), False

    candidates: list[QueryCandidate] = []
    seen_shapes: set[tuple[tuple[str, str, str], tuple[str, str, str]]] = set()
    shared_answers = sorted(set(condition_matches[0]) & set(condition_matches[1]))
    for answer_key in shared_answers:
        first_shapes = condition_matches[0][answer_key]
        second_shapes = condition_matches[1][answer_key]
        for first_shape, first_fact in first_shapes.items():
            for second_shape, second_fact in second_shapes.items():
                shape = tuple(sorted((first_shape, second_shape)))
                if shape in seen_shapes:
                    continue
                seen_shapes.add(shape)
                if len(candidates) >= max_candidates:
                    return tuple(candidates), True
                atoms = sorted(
                    (
                        _conjunctive_condition_atom(first_fact, intent.conditions[0], intent.answer_var),
                        _conjunctive_condition_atom(second_fact, intent.conditions[1], intent.answer_var),
                    )
                )
                candidates.append(
                    QueryCandidate(
                        query_dl=_query_dl(qid, intent.answer_var, ", ".join(atoms)),
                        family=QueryCandidateFamily.CONJUNCTIVE_FILTER,
                        direction=None,
                        relation_display=None,
                        relation_executable=None,
                        subject_executable=None,
                        object_executable=None,
                    )
                )
    return tuple(candidates), False


def _conjunctive_filter_conditions_are_semantic_duplicates(
    conditions: tuple[ConjunctiveHop, ...],
    answer_var: str,
    snapshot: QuerySchemaSnapshot,
) -> bool:
    """Return whether alias policy makes both requested conditions identical."""
    aliases = {entry.alias: entry.canonical for entry in snapshot.relation_aliases}
    shapes = []
    for condition in conditions:
        answer_is_subject = (
            condition.subject.kind == "var" and condition.subject.value == answer_var
        )
        anchor = condition.object if answer_is_subject else condition.subject
        shapes.append(
            (
                _nfc(canonical_relation(condition.relation.value, aliases)),
                "subject" if answer_is_subject else "object",
                _nfc(anchor.value),
            )
        )
    return shapes[0] == shapes[1]


def _conjunctive_condition_matches(
    condition: ConjunctiveHop,
    answer_var: str,
    snapshot: QuerySchemaSnapshot,
) -> dict[str, dict[tuple[str, str, str], SnapshotFact]]:
    """Index observed condition facts by engine-equality answer key and rule shape."""
    # QueryIntent validation guarantees the concrete hop shape and endpoint roles.
    subject = condition.subject
    obj = condition.object
    answer_is_subject = subject.kind == "var" and subject.value == answer_var
    anchor = obj if answer_is_subject else subject
    requested = (_nfc(condition.relation.value),)
    aliases = {entry.alias: entry.canonical for entry in snapshot.relation_aliases}
    matches: dict[str, dict[tuple[str, str, str], SnapshotFact]] = {}
    for fact in snapshot.join_facts:
        if not _fact_relation_matches_any_requested(fact, requested, snapshot):
            continue
        answer_ref = fact.subject if answer_is_subject else fact.object
        anchor_ref = fact.object if answer_is_subject else fact.subject
        if not _entity_ref_matches(anchor_ref, anchor.value):
            continue
        shape = (
            _nfc(canonical_relation(fact.relation.display, aliases)),
            "subject" if answer_is_subject else "object",
            _term_ref_compare_key(anchor_ref),
        )
        answer_matches = matches.setdefault(_term_ref_compare_key(answer_ref), {})
        existing = answer_matches.get(shape)
        if existing is None or _snapshot_fact_representative_key(fact) < _snapshot_fact_representative_key(existing):
            answer_matches[shape] = fact
    return matches


def _snapshot_fact_representative_key(fact: SnapshotFact) -> tuple[str, str, str, int]:
    """Choose one stable raw fact for an alias-equivalent rule shape."""
    return (
        fact.relation.executable,
        fact.subject.executable,
        fact.object.executable,
        fact.fact_id,
    )


def _conjunctive_condition_atom(
    fact: SnapshotFact, condition: ConjunctiveHop, answer_var: str
) -> str:
    answer_is_subject = condition.subject.kind == "var" and condition.subject.value == answer_var
    if answer_is_subject:
        return f"relation({answer_var}, {fact.relation.executable}, {fact.object.executable})"
    return f"relation({fact.subject.executable}, {fact.relation.executable}, {answer_var})"


def _term_ref_compare_key(ref: TermRef) -> str:
    """Return the engine equality key without losing TermRef's stored kind.

    Re-parsing an executable term alone is ambiguous for an Atom-like value
    whose spelling also matches a Datalog variable. Leaf values compare by
    their displayed surface in the engine, while compound values intentionally
    retain their structural parser representation.
    """
    if ref.kind in {"Atom", "StringLit", "NumberLit"}:
        return f"s:{ref.display}"
    if ref.kind == "Var":
        return f"v:{ref.display}"
    return term_compare_key(parse_term(ref.executable))


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
