# SPDX-License-Identifier: MPL-2.0
"""Conservative deterministic accept recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import unicodedata

from verinote.pipeline.corroboration import (
    canonical_relation,
    normalize_typed_value,
    store_functional_relations,
    store_relation_aliases,
    store_typed_relations,
    TypedRelationSpec,
)
from verinote.store import Store, is_engine_input, is_review_eligible, review_statuses

RULE_NAME = "corroborated_no_conflict"


@dataclass(frozen=True)
class AcceptRecommendation:
    fact_id: int
    eligible: bool
    reasons: tuple[str, ...]
    support_sources: tuple[str, ...]
    support_fact_ids: tuple[int, ...]
    canonical_relation: str
    typed_normalization: str


def accept_recommendation(store: Store, fact_id: int) -> AcceptRecommendation | None:
    fact = store.get_fact(fact_id)
    if fact is None:
        return None
    return _engine(store).recommend(fact)


def accept_recommendations(store: Store) -> dict[int, AcceptRecommendation]:
    engine = _engine(store)
    recommendations = {}
    for fact in store.facts(statuses=review_statuses()):
        recommendations[int(fact["id"])] = engine.recommend(fact)
    return recommendations


def accept_recommendations_for(
    store: Store, fact_ids: Iterable[int]
) -> dict[int, AcceptRecommendation]:
    engine = _engine(store)
    recommendations = {}
    for fact_id in fact_ids:
        fact = store.get_fact(int(fact_id))
        if fact is None:
            continue
        recommendations[int(fact["id"])] = engine.recommend(fact)
    return recommendations


def apply_auto_accept_recommendations(store: Store) -> list[AcceptRecommendation]:
    applied = []
    for recommendation in accept_recommendations(store).values():
        if not recommendation.eligible:
            continue
        store.set_status(
            recommendation.fact_id,
            "accepted",
            action="auto_accepted",
            actor="rule",
            rule_name=RULE_NAME,
        )
        store.add_fact_event(
            fact_id=recommendation.fact_id,
            event_type="auto_accept_applied",
            actor="rule",
            rule_name=RULE_NAME,
            after={
                "support_sources": list(recommendation.support_sources),
                "support_fact_ids": list(recommendation.support_fact_ids),
                "canonical_relation": recommendation.canonical_relation,
                "typed_normalization": recommendation.typed_normalization,
            },
        )
        applied.append(recommendation)
    return applied


@dataclass(frozen=True)
class _FactView:
    id: int
    subject: str
    relation: str
    object: str
    status: str
    source: str
    job_id: int | None
    canonical_relation: str
    object_key: tuple[str, object]
    typed_normalization: str


class _RecommendationEngine:
    def __init__(
        self,
        store: Store,
        aliases: dict[str, str],
        typed: dict[str, TypedRelationSpec],
        single_valued: set[str],
    ) -> None:
        self.store = store
        self.aliases = aliases
        self.typed = typed
        self.single_valued = single_valued
        self.facts = [_view_fact(row, aliases, typed) for row in store.facts()]

    def recommend(self, fact_row) -> AcceptRecommendation:
        target = _view_fact(fact_row, self.aliases, self.typed)
        reasons = []
        if not is_review_eligible(target.status):
            reasons.append("not_review_candidate")
        if not target.source:
            reasons.append("source_missing")
        if not _job_done(self.store, target.job_id):
            reasons.append("source_analysis_incomplete")
        if target.status == "superseded" or _was_rejected(self.store, target.id):
            reasons.append("previously_rejected_or_superseded")

        support = self._supporting_facts(target)
        support_sources = tuple(sorted({fact.source for fact in support if fact.source}))
        if len(support_sources) < 2:
            reasons.append("insufficient_distinct_source_support")
        if any(not _job_done(self.store, fact.job_id) for fact in support):
            if "source_analysis_incomplete" not in reasons:
                reasons.append("source_analysis_incomplete")
        if self._has_single_valued_conflict(target):
            reasons.append("single_valued_conflict")

        return AcceptRecommendation(
            fact_id=target.id,
            eligible=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            support_sources=support_sources,
            support_fact_ids=tuple(sorted(fact.id for fact in support)),
            canonical_relation=target.canonical_relation,
            typed_normalization=target.typed_normalization,
        )

    def _supporting_facts(self, target: _FactView) -> list[_FactView]:
        return [
            fact
            for fact in self.facts
            if (is_review_eligible(fact.status) or is_engine_input(fact.status))
            and fact.subject == target.subject
            and fact.canonical_relation == target.canonical_relation
            and fact.object_key == target.object_key
            and fact.source
        ]

    def _has_single_valued_conflict(self, target: _FactView) -> bool:
        if target.canonical_relation not in self.single_valued:
            return False
        for fact in self.facts:
            if not is_engine_input(fact.status):
                continue
            if fact.subject != target.subject:
                continue
            if fact.canonical_relation != target.canonical_relation:
                continue
            if fact.object_key != target.object_key:
                return True
        return False


def _engine(store: Store) -> _RecommendationEngine:
    aliases = store_relation_aliases(store)
    typed = store_typed_relations(store)
    single_valued = {canonical_relation(r, aliases) for r in store_functional_relations(store)}
    return _RecommendationEngine(store, aliases, typed, single_valued)


def _view_fact(
    row, aliases: dict[str, str], typed: dict[str, TypedRelationSpec]
) -> _FactView:
    relation = str(row["relation"])
    canonical = canonical_relation(relation, aliases)
    spec = _typed_spec(canonical, typed)
    object_key, typed_normalization = _object_key(str(row["object"]), spec)
    return _FactView(
        id=int(row["id"]),
        subject=str(row["subject"]),
        relation=relation,
        object=str(row["object"]),
        status=str(row["status"]),
        source=str(row["source_path"] or "").strip(),
        job_id=int(row["job_id"]) if row["job_id"] is not None else None,
        canonical_relation=canonical,
        object_key=object_key,
        typed_normalization=typed_normalization,
    )


def _typed_spec(
    relation: str, typed: dict[str, TypedRelationSpec]
) -> TypedRelationSpec | None:
    return typed.get(relation) or typed.get(unicodedata.normalize("NFC", relation))


def _object_key(
    obj: str, spec: TypedRelationSpec | None
) -> tuple[tuple[str, object], str]:
    if spec is None:
        return ("raw", obj), ""
    scalar = normalize_typed_value(spec.type, obj, spec.units)
    if scalar is None:
        return ("raw", obj), ""
    return ("scalar", scalar), f"{spec.alias}={scalar}"


def _job_done(store: Store, job_id: int | None) -> bool:
    if job_id is None:
        return False
    job = store.get_extraction_job(job_id)
    return job is not None and job["status"] == "done"


def _was_rejected(store: Store, fact_id: int) -> bool:
    return any(event["event_type"] == "rejected" for event in store.fact_events(fact_id))
