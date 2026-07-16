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
from verinote.pipeline.policy_state import assert_writable
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


def apply_auto_accept_recommendations(
    store: Store, *, exclude_fact_ids: Iterable[int] = ()
) -> list[AcceptRecommendation]:
    """Promote every eligible candidate to `accepted` — never on a halted KB.

    `exclude_fact_ids` holds facts a human has just decided on in this same
    request. The rule still runs for everything else, because that decision may
    have unblocked siblings; it just doesn't get to re-decide the fact the human
    aimed at. Without this, demoting a corroborated fact to the review tier hands
    it straight back to the rule that promotes corroborated review-tier facts.

    A halted KB already stops this today, but only by COINCIDENCE: `_engine` builds
    its single-valued set from `store_functional_relations`, which calls
    `load_policy`, which raises on a KB whose policy file is gone. The refusal is
    therefore a side effect of what the recommendation engine happens to read — and
    the day functional relations move into a table, or the policy gets cached, that
    side effect evaporates while this remains a write entrypoint that stamps
    `status='accepted'` on facts no rule was ever applied to.

    So the refusal is made this function's OWN, asked of the same predicate every
    other write entrypoint asks (#194), before a single row is read. The extraction
    worker calls this the instant a job finishes, in the same `with Store(...)`, so
    a policy deleted after the final chunk's write boundary lands exactly here.
    """
    assert_writable(store)
    excluded = {int(fact_id) for fact_id in exclude_fact_ids}
    applied = []
    for recommendation in accept_recommendations(store).values():
        if not recommendation.eligible or recommendation.fact_id in excluded:
            continue
        # Eligibility came off a snapshot; `auto_accept_fact` re-checks the tier
        # at the write and returns None if a human decided first. Their decision
        # wins, and the audit event below is skipped along with the write.
        if store.auto_accept_fact(recommendation.fact_id, rule_name=RULE_NAME) is None:
            continue
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
