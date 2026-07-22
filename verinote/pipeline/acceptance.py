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
    """The rule's reconciler: one policy snapshot, two guarded writes — never on a halted KB.

    Promotes every eligible review-tier candidate to `accepted` AND retracts every
    rule-accepted fact whose corroboration basis has lapsed (fewer than two
    distinct source witnesses) back to `needs_review`. Both directions belong to
    the rule alone: `auto_accept_fact` only touches review-tier rows and
    `auto_retract_fact` only touches `accepted` — the rule's own tier — so a
    human's `confirmed`/`superseded` is structurally unreachable from here.

    Both passes are judged off snapshots taken before any write in this call (and
    so identical — nothing writes between them), which makes the outcome
    order-independent: the two guarded writes touch disjoint rows (review-tier vs
    `accepted`), and a fact retracted in this pass is still `accepted` in the
    snapshot, so a review-tier rival of a retracted value stays blocked by it
    (#287) and its promotion is deferred to the next cascade rather than racing
    this one.

    `exclude_fact_ids` holds facts a human has just decided on in this same
    request. The rule still runs for everything else, because that decision may
    have unblocked siblings; it just doesn't get to re-decide the fact the human
    aimed at, in either direction. Without this, demoting a corroborated fact to
    the review tier hands it straight back to the rule that promotes corroborated
    review-tier facts.

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
    # Retraction is judged off a snapshot taken before any promotion write, so
    # both passes see the same pre-write state. That is what makes the pass
    # order-independent: a fact retracted below is still `accepted` in this
    # snapshot, so a review-tier rival stays blocked by it (#287) and its
    # promotion defers to the next cascade rather than racing this one.
    #
    # Promotion keeps flowing through the module-level `accept_recommendations`
    # (its own read-only pre-write snapshot, identical because nothing writes
    # between the two reads) rather than reusing this engine directly. That is
    # deliberate: it is the seam the cross-connection race guard is exercised
    # through, where a reject can be injected between the snapshot and the write.
    retraction_engine = _engine(store)

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

    # Retraction walks the snapshot taken above — before any write of this pass —
    # so the statuses it reads are the ones promotion was judged against.
    for view in retraction_engine.facts:
        if view.status != "accepted" or view.id in excluded:
            continue
        if not retraction_engine.basis_lapsed(view):
            continue
        # `auto_retract_fact` only touches `accepted`, so a human decision that
        # landed after the snapshot wins the guarded UPDATE (returns None) and
        # the audit event is skipped with it.
        if store.auto_retract_fact(view.id, rule_name=RULE_NAME) is None:
            continue
        remaining = retraction_engine._supporting_facts(view)
        store.add_fact_event(
            fact_id=view.id,
            event_type="auto_accept_retracted",
            actor="rule",
            rule_name=RULE_NAME,
            after={
                "reasons": ["insufficient_distinct_source_support"],
                "remaining_support_sources": sorted(
                    {fact.source for fact in remaining if fact.source}
                ),
                "remaining_support_fact_ids": sorted(fact.id for fact in remaining),
                "canonical_relation": view.canonical_relation,
            },
        )
    return applied


@dataclass(frozen=True)
class _FactView:
    id: int
    subject: str
    relation: str
    object: str
    status: str
    stale: bool
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
        # #343: `reject_fact` -> `superseded` is the sole record of a human
        # rejection, and it is the ONLY writer of that status, so the superseded
        # rows are an exact, cheap proxy for "a human rejected this value". We key
        # the shadow on the CANONICAL (source, subject, relation, value) rather than
        # the literal relation label `reconcile_fact` deduped on: a later
        # re-extraction that renames the value to an aliased-synonym relation lands
        # a fresh fact_id with no rejection history of its own, and the literal-token
        # dedup misses it. The key is SOURCE-scoped on purpose -- #343 deliberately
        # leaves open whether enough independent later corroboration should ever
        # override one source's earlier rejection, so this veto reaches only
        # re-labelings of the SAME source's rejected value, never another source's
        # independent proposal of it.
        self._rejected_keys = frozenset(
            (f.source, f.subject, f.canonical_relation, f.object_key)
            for f in self.facts
            if f.status == "superseded"
        )

    def recommend(self, fact_row) -> AcceptRecommendation:
        target = _view_fact(fact_row, self.aliases, self.typed)
        reasons = []
        if not is_review_eligible(target.status):
            reasons.append("not_review_candidate")
        if not target.source:
            reasons.append("source_missing")
        if not _job_done(self.store, target.job_id):
            reasons.append("source_analysis_incomplete")
        if (
            target.status == "superseded"
            or _was_rejected(self.store, target.id)
            or self._reject_shadowed(target)
        ):
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
        # A fact the staleness sweep demoted must never re-promote ITSELF, even
        # when its value keeps other live witnesses -- the _supporting_facts
        # exclusion alone leaks here, because excluding the stale fact from its own
        # support set still leaves >=2 other distinct sources (#329 Gap 1).
        if target.stale and target.status == "needs_review":
            reasons.append("stale_citation")

        return AcceptRecommendation(
            fact_id=target.id,
            eligible=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            support_sources=support_sources,
            support_fact_ids=tuple(sorted(fact.id for fact in support)),
            canonical_relation=target.canonical_relation,
            typed_normalization=target.typed_normalization,
        )

    def _reject_shadowed(self, fact: _FactView) -> bool:
        """True when `fact` re-labels a value THIS source has had rejected (#343).

        Keyed on the canonical (source, subject, relation, value), so it catches a
        re-extraction that renamed the relation to an aliased synonym -- the case
        `reconcile_fact`'s literal-triple dedup misses, which lets a rejected value
        return under a clean fact_id. Source-scoped: it never vetoes a different
        source's independent proposal of the same value.
        """
        return (
            fact.source,
            fact.subject,
            fact.canonical_relation,
            fact.object_key,
        ) in self._rejected_keys

    def _supporting_facts(self, target: _FactView) -> list[_FactView]:
        return [
            fact
            for fact in self.facts
            if (is_review_eligible(fact.status) or is_engine_input(fact.status))
            # A fact the staleness sweep demoted (its source text no longer
            # supports it) stays review-eligible by status but must not corroborate
            # any value while it awaits a human -- #329. Conditioning on
            # needs_review keeps the flag inert the moment a human confirms or
            # rejects, so no explicit clear is owed on those transitions.
            and not (fact.stale and fact.status == "needs_review")
            # #343 (witness half): a still-review-eligible fact that merely re-labels
            # THIS source's rejected value must not pad another candidate's
            # corroboration count. Scoped to review-eligible on purpose -- once a
            # human confirms it (engine tier via `accept_fact`) that is a deliberate
            # decision the fix must never discount, so a `confirmed` witness of an
            # earlier-rejected-then-reaccepted value keeps counting.
            and not (is_review_eligible(fact.status) and self._reject_shadowed(fact))
            and fact.subject == target.subject
            and fact.canonical_relation == target.canonical_relation
            and fact.object_key == target.object_key
            and fact.source
        ]

    def basis_lapsed(self, target: _FactView) -> bool:
        """True when a rule-accepted fact no longer has 2+ distinct source witnesses.

        The exact complement of promotion's `insufficient_distinct_source_support`
        threshold, and deliberately COUNT-ONLY: no `_job_done` gate, no
        `source_missing` gate. Those job-based signals flap while a source is being
        re-extracted — a fresh same-triple candidate with a still-RUNNING job joins
        the support set the instant it lands — so gating retraction on them would
        yank a healthy accepted fact mid-analysis. The distinct-source count is
        job-independent and monotone under transient extraction: it falls only when
        support is DURABLY lost (a reject→superseded, an amend-away, a deleted
        source), which is exactly when a rule-accepted value should return to
        review. Mirroring promotion's threshold exactly also forecloses thrash — a
        fact retracted here fails the same `< 2` test if re-evaluated for promotion,
        so it cannot oscillate.
        """
        return len({fact.source for fact in self._supporting_facts(target) if fact.source}) < 2

    def _has_single_valued_conflict(self, target: _FactView) -> bool:
        """True when another fact witnesses a rival value on a functional relation.

        A rival is admitted by EXACTLY the filter `_supporting_facts` uses to
        admit a witness FOR the value: review-eligible or engine-tier, and
        source-backed. The symmetry is the point (#287). Scanning only the engine
        tier here let a fact corroborate a value while being unable to speak
        against a contradicting one, so two mutually-contradictory corroborated
        candidates never saw each other and BOTH auto-promoted — leaving the KB
        failing its own functional_conflict policy with no human in the loop.

        With the filters aligned, any contested value is withheld: neither rival
        is eligible, in either evaluation order, so nothing promotes. Withholding
        all of them is the rule's charter, not timidity — promoting either would
        silently adjudicate an evidential conflict, and "oldest id wins" was
        rejected precisely because a deterministic rule must not pick a truth. The
        `fact.id == target.id` guard keeps a fact from counting as its own rival.

        The deadlock is a human's to break, and the path is already wired:
        rejecting the wrong value supersedes it (it then witnesses on neither
        tier), which unblocks the survivor on the next `_maybe_apply_auto_accept`
        cascade that every review decision triggers. Residual limitation, out of
        scope here: a second DB connection could still race this snapshot — the
        write-time re-check in `auto_accept_fact` re-reads only the engine tier,
        so two candidates promoted concurrently on separate connections are not
        caught by it. Closing that needs write-time atomicity, not this filter.
        """
        if target.canonical_relation not in self.single_valued:
            return False
        for fact in self.facts:
            if fact.id == target.id:
                continue
            if not (
                (is_review_eligible(fact.status) or is_engine_input(fact.status))
                and fact.source
            ):
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
        stale=bool(row["stale"]),
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
