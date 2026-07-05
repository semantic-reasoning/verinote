# SPDX-License-Identifier: MPL-2.0
"""Dry-run evaluation for deterministic query planner candidates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from verinote.engine import CheckReport, validate_query
from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.pipeline.corroboration import CorroborationPolicyError, store_relation_aliases
from verinote.pipeline.query import expand_query_relation_aliases
from verinote.pipeline.query_planner import QueryCandidate, QueryCandidatePlan

RELATION_DECL = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"


class QueryCandidateOutcome(Enum):
    INVALID = "invalid"
    ENGINE_POLICY_ERROR = "engine_policy_error"
    NO_ANSWER = "no_answer"
    VALID_WITH_ANSWERS = "valid_with_answers"


class QueryCandidateSetOutcome(Enum):
    EMPTY = "empty"
    INVALID = "invalid"
    ENGINE_POLICY_ERROR = "engine_policy_error"
    NO_ANSWER = "no_answer"
    VALID = "valid"
    AMBIGUOUS_CONFLICTING = "ambiguous_conflicting"


@dataclass(frozen=True)
class QueryCandidateEvaluation:
    candidate: QueryCandidate
    outcome: QueryCandidateOutcome
    answers: tuple[str, ...] = ()
    validation_reason: str | None = None
    report: CheckReport | None = None


@dataclass(frozen=True)
class QueryCandidateSetEvaluation:
    plan: QueryCandidatePlan
    outcome: QueryCandidateSetOutcome
    evaluations: tuple[QueryCandidateEvaluation, ...] = ()
    selected: QueryCandidate | None = None
    answers: tuple[str, ...] = ()


def evaluate_query_candidate(
    store,
    candidate: QueryCandidate,
    *,
    aliases: Mapping[str, str] | None = None,
) -> QueryCandidateEvaluation:
    """Validate and dry-run one planner candidate without mutating stored query state."""
    ok, reason = validate_query(candidate.query_dl)
    if not ok:
        if _validation_engine_error(reason):
            return QueryCandidateEvaluation(
                candidate=candidate,
                outcome=QueryCandidateOutcome.ENGINE_POLICY_ERROR,
                validation_reason=reason,
                report=_engine_error_report(reason),
            )
        return QueryCandidateEvaluation(
            candidate=candidate,
            outcome=QueryCandidateOutcome.INVALID,
            validation_reason=reason,
        )

    try:
        query_dl = expand_query_relation_aliases(candidate.query_dl, dict(aliases or {}))
        report = run_check_duckdb(
            store.engine_fact_terms(),
            policy_dl=RELATION_DECL,
            query_dl=query_dl,
        )
    except CorroborationPolicyError as exc:
        report = _engine_error_report(str(exc))
    except Exception as exc:
        report = CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"query candidate dry-run error: {exc}",
            findings=[f"ERROR engine error: {exc}"],
        )

    if not report.engine_available or not report.ok or report.errors:
        return QueryCandidateEvaluation(
            candidate=candidate,
            outcome=QueryCandidateOutcome.ENGINE_POLICY_ERROR,
            report=report,
        )
    answers = tuple(dict.fromkeys(report.answers))
    if not answers:
        return QueryCandidateEvaluation(
            candidate=candidate,
            outcome=QueryCandidateOutcome.NO_ANSWER,
            report=report,
        )
    return QueryCandidateEvaluation(
        candidate=candidate,
        outcome=QueryCandidateOutcome.VALID_WITH_ANSWERS,
        answers=answers,
        report=report,
    )


def evaluate_query_candidate_plan(store, plan: QueryCandidatePlan) -> QueryCandidateSetEvaluation:
    """Evaluate a candidate plan and choose a deterministic non-ambiguous candidate."""
    if not plan.candidates:
        return QueryCandidateSetEvaluation(plan=plan, outcome=QueryCandidateSetOutcome.EMPTY)

    try:
        aliases = store_relation_aliases(store)
    except CorroborationPolicyError as exc:
        report = _engine_error_report(str(exc))
        evaluations = tuple(
            QueryCandidateEvaluation(
                candidate=candidate,
                outcome=QueryCandidateOutcome.ENGINE_POLICY_ERROR,
                report=report,
            )
            for candidate in plan.candidates
        )
        return QueryCandidateSetEvaluation(
            plan=plan,
            outcome=QueryCandidateSetOutcome.ENGINE_POLICY_ERROR,
            evaluations=evaluations,
        )
    evaluations = tuple(
        evaluate_query_candidate(store, candidate, aliases=aliases)
        for candidate in plan.candidates
    )

    if any(
        evaluation.outcome == QueryCandidateOutcome.ENGINE_POLICY_ERROR
        for evaluation in evaluations
    ):
        return QueryCandidateSetEvaluation(
            plan=plan,
            outcome=QueryCandidateSetOutcome.ENGINE_POLICY_ERROR,
            evaluations=evaluations,
        )

    answering = tuple(
        evaluation
        for evaluation in evaluations
        if evaluation.outcome == QueryCandidateOutcome.VALID_WITH_ANSWERS
    )
    if not answering:
        if all(
            evaluation.outcome == QueryCandidateOutcome.INVALID
            for evaluation in evaluations
        ):
            outcome = QueryCandidateSetOutcome.INVALID
        else:
            outcome = QueryCandidateSetOutcome.NO_ANSWER
        return QueryCandidateSetEvaluation(
            plan=plan,
            outcome=outcome,
            evaluations=evaluations,
        )

    answer_sets = {evaluation.answers for evaluation in answering}
    selected = answering[0]
    if len(answer_sets) > 1:
        return QueryCandidateSetEvaluation(
            plan=plan,
            outcome=QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING,
            evaluations=evaluations,
            selected=selected.candidate,
            answers=selected.answers,
        )
    return QueryCandidateSetEvaluation(
        plan=plan,
        outcome=QueryCandidateSetOutcome.VALID,
        evaluations=evaluations,
        selected=selected.candidate,
        answers=selected.answers,
    )

def _validation_engine_error(reason: str) -> bool:
    normalized = reason.lower()
    return "duckdb is not installed" in normalized or "engine error" in normalized


def _engine_error_report(reason: str) -> CheckReport:
    engine_available = "duckdb is not installed" not in reason.lower()
    return CheckReport(
        ok=False,
        errors=1,
        warnings=0,
        text=f"query candidate validation error: {reason}",
        findings=[f"ERROR engine error: {reason}"],
        engine_available=engine_available,
    )
