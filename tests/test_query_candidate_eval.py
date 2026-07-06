# SPDX-License-Identifier: MPL-2.0

import verinote.pipeline.query_candidate_eval as query_candidate_eval
import verinote.pipeline.query_quality_policy as query_quality_policy
from verinote.engine import CheckReport
from verinote.pipeline.query_candidate_eval import (
    QueryCandidateOutcome,
    QueryCandidateSetOutcome,
    evaluate_query_candidate,
    evaluate_query_candidate_plan,
)
from verinote.pipeline.query_planner import QueryCandidate, QueryCandidatePlan
from verinote.pipeline.query_planner import QueryCandidateDirection, QueryCandidateFamily
from verinote.store import Store


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _candidate(
    query_dl: str,
    *,
    family: QueryCandidateFamily = QueryCandidateFamily.MANUAL_DRAFT,
    direction: QueryCandidateDirection | None = None,
    relation_display: str | None = None,
    relation_executable: str | None = None,
) -> QueryCandidate:
    return QueryCandidate(
        query_dl=query_dl,
        family=family,
        direction=direction,
        relation_display=relation_display,
        relation_executable=relation_executable,
        subject_executable=None,
        object_executable=None,
    )


def _lookup(qid: int, subject: str, relation: str) -> QueryCandidate:
    return _candidate(
        f'.decl answer_q{qid}(value: symbol)\n'
        f'answer_q{qid}(O) :- relation("{subject}", "{relation}", O).',
        family=QueryCandidateFamily.DIRECT_OBJECT_LOOKUP,
        direction=QueryCandidateDirection.SUBJECT_TO_OBJECT,
    )


def _relation_discovery(
    qid: int, relation: str, *, display: str | None = None
) -> QueryCandidate:
    relation_display = display or relation
    return _candidate(
        f'.decl answer_q{qid}(value: symbol)\n'
        f'answer_q{qid}("{relation}") :- '
        f'relation("Sample Entity", "{relation}", "Sample Value").',
        family=QueryCandidateFamily.SUBJECT_RELATION_DISCOVERY,
        direction=QueryCandidateDirection.SUBJECT_TO_RELATION,
        relation_display=relation_display,
        relation_executable=f'"{relation}"',
    )


def _object_relation_discovery(qid: int, relation: str) -> QueryCandidate:
    return _candidate(
        f'.decl answer_q{qid}(value: symbol)\n'
        f'answer_q{qid}("{relation}") :- '
        f'relation("Sample Source", "{relation}", "Sample Entity").',
        family=QueryCandidateFamily.OBJECT_RELATION_DISCOVERY,
        direction=QueryCandidateDirection.OBJECT_TO_RELATION,
        relation_display=relation,
        relation_executable=f'"{relation}"',
    )


def _plan(*candidates: QueryCandidate, qid: int = 1) -> QueryCandidatePlan:
    return QueryCandidatePlan(qid=qid, candidates=tuple(candidates))


def test_evaluate_query_candidate_rejects_invalid_syntax(tmp_path):
    store = _store(tmp_path)

    evaluation = evaluate_query_candidate(store, _candidate("this is not datalog"))

    assert evaluation.outcome == QueryCandidateOutcome.INVALID
    assert evaluation.validation_reason
    assert evaluation.report is None


def test_evaluate_query_candidate_rejects_unsupported_predicate(tmp_path):
    store = _store(tmp_path)
    candidate = _candidate(".decl answer_q1(value: symbol)\nanswer_q1(O) :- bogus(O).")

    evaluation = evaluate_query_candidate(store, candidate)

    assert evaluation.outcome == QueryCandidateOutcome.INVALID
    assert "bogus" in evaluation.validation_reason


def test_evaluate_query_candidate_no_rows_is_no_answer(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "born_in", "Sample City", status="confirmed")

    evaluation = evaluate_query_candidate(store, _lookup(1, "Other Person", "born_in"))

    assert evaluation.outcome == QueryCandidateOutcome.NO_ANSWER
    assert evaluation.answers == ()
    assert evaluation.report is not None
    assert evaluation.report.ok is True


def test_evaluate_query_candidate_answers_from_confirmed_and_accepted(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "role", "Confirmed Role", status="confirmed")
    store.add_fact("Sample Person", "role", "Accepted Role", status="accepted")

    evaluation = evaluate_query_candidate(store, _lookup(1, "Sample Person", "role"))

    assert evaluation.outcome == QueryCandidateOutcome.VALID_WITH_ANSWERS
    assert evaluation.answers == ("q1: Accepted Role, Confirmed Role",)


def test_evaluate_query_candidate_keeps_empty_string_answer(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "note", "", status="confirmed")

    evaluation = evaluate_query_candidate(store, _lookup(1, "Sample Person", "note"))

    assert evaluation.outcome == QueryCandidateOutcome.VALID_WITH_ANSWERS
    assert evaluation.answers == ("q1: ",)


def test_evaluate_query_candidate_ignores_candidate_and_needs_review_facts(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "role", "Candidate Role", status="candidate")
    store.add_fact("Sample Person", "role", "Needs Review Role", status="needs_review")

    evaluation = evaluate_query_candidate(store, _lookup(1, "Sample Person", "role"))

    assert evaluation.outcome == QueryCandidateOutcome.NO_ANSWER


def test_evaluate_query_candidate_reports_engine_unavailable(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def fake_run_check(*args, **kwargs):
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text="DuckDB is not installed",
            findings=["ERROR engine error: DuckDB is not installed"],
            engine_available=False,
        )

    monkeypatch.setattr(query_candidate_eval, "run_check_duckdb", fake_run_check)

    evaluation = evaluate_query_candidate(store, _lookup(1, "Sample Person", "role"))

    assert evaluation.outcome == QueryCandidateOutcome.ENGINE_POLICY_ERROR
    assert evaluation.report is not None
    assert evaluation.report.engine_available is False


def test_evaluate_query_candidate_reports_validation_engine_unavailable(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    monkeypatch.setattr(
        query_candidate_eval,
        "validate_query",
        lambda query_dl: (False, "ERROR engine error: DuckDB is not installed"),
    )

    evaluation = evaluate_query_candidate(store, _lookup(1, "Sample Person", "role"))

    assert evaluation.outcome == QueryCandidateOutcome.ENGINE_POLICY_ERROR
    assert evaluation.report is not None
    assert evaluation.report.engine_available is False


def test_evaluate_query_candidate_reports_backend_error(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def fake_run_check(*args, **kwargs):
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text="backend error",
            findings=["ERROR engine error: backend error"],
        )

    monkeypatch.setattr(query_candidate_eval, "run_check_duckdb", fake_run_check)

    evaluation = evaluate_query_candidate(store, _lookup(1, "Sample Person", "role"))

    assert evaluation.outcome == QueryCandidateOutcome.ENGINE_POLICY_ERROR
    assert evaluation.report is not None
    assert evaluation.report.findings == ["ERROR engine error: backend error"]


def test_evaluate_query_candidate_uses_minimal_relation_policy(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Org", "established_on", "2020", status="confirmed")
    store.add_fact("Sample Org", "established_on", "2021", status="confirmed")
    store.add_fact("Sample Person", "role", "Reviewer", status="confirmed")

    evaluation = evaluate_query_candidate(store, _lookup(1, "Sample Person", "role"))

    assert evaluation.outcome == QueryCandidateOutcome.VALID_WITH_ANSWERS
    assert evaluation.answers == ("q1: Reviewer",)
    assert evaluation.report is not None
    assert evaluation.report.findings == []


def test_evaluate_query_candidate_plan_expands_relation_aliases_without_mutating_selected(
    tmp_path,
):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `역할`\n", encoding="utf-8")
    store.add_fact("Sample Person", "역할", "Reviewer", status="confirmed")
    candidate = _lookup(1, "Sample Person", "role")

    evaluation = evaluate_query_candidate_plan(store, _plan(candidate))

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.answers == ("q1: Reviewer",)
    assert evaluation.selected == candidate
    assert evaluation.selected.query_dl == candidate.query_dl
    assert '"역할"' not in evaluation.selected.query_dl


def test_evaluate_query_candidate_plan_reports_alias_policy_error(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("not a valid alias entry\n", encoding="utf-8")

    evaluation = evaluate_query_candidate_plan(
        store,
        _plan(_lookup(1, "Sample Person", "role")),
    )

    assert evaluation.outcome == QueryCandidateSetOutcome.ENGINE_POLICY_ERROR
    assert len(evaluation.evaluations) == 1
    assert evaluation.evaluations[0].outcome == QueryCandidateOutcome.ENGINE_POLICY_ERROR
    assert evaluation.evaluations[0].report is not None
    assert "relation-aliases.md" in evaluation.evaluations[0].report.text


def test_evaluate_query_candidate_plan_reports_alias_expansion_cap_as_policy_error(
    tmp_path,
):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "\n".join(f"- `r{i}` -> `canonical_{i}`" for i in range(7)) + "\n",
        encoding="utf-8",
    )
    body = ", ".join(f'relation(X{i}, "r{i}", X{i + 1})' for i in range(7))
    candidate = _candidate(
        ".decl answer_q1(value: symbol)\n" f"answer_q1(X7) :- {body}."
    )

    evaluation = evaluate_query_candidate_plan(store, _plan(candidate))

    assert evaluation.outcome == QueryCandidateSetOutcome.ENGINE_POLICY_ERROR
    assert evaluation.evaluations[0].outcome == QueryCandidateOutcome.ENGINE_POLICY_ERROR
    assert evaluation.evaluations[0].report is not None
    assert "query alias expansion exceeds" in evaluation.evaluations[0].report.text


def test_evaluate_query_candidate_plan_dedupes_mixed_raw_and_canonical_answers(
    tmp_path,
):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `역할`\n", encoding="utf-8")
    store.add_fact("Sample Person", "role", "Reviewer", status="confirmed")
    store.add_fact("Sample Person", "역할", "Reviewer", status="confirmed")

    evaluation = evaluate_query_candidate_plan(
        store, _plan(_lookup(1, "Sample Person", "role"))
    )

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.answers == ("q1: Reviewer",)


def test_evaluate_query_candidate_plan_empty(tmp_path):
    store = _store(tmp_path)

    evaluation = evaluate_query_candidate_plan(store, _plan())

    assert evaluation.outcome == QueryCandidateSetOutcome.EMPTY
    assert evaluation.evaluations == ()
    assert evaluation.selected is None


def test_evaluate_query_candidate_plan_selects_first_same_answer_candidate(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "role", "Reviewer", status="confirmed")
    first = _lookup(1, "Sample Person", "role")
    second = _candidate(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Person", "role", O), O != "Other".'
    )

    evaluation = evaluate_query_candidate_plan(store, _plan(first, second))

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.selected == first
    assert evaluation.answers == ("q1: Reviewer",)


def test_evaluate_query_candidate_plan_preserves_candidate_metadata(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "role", "Reviewer", status="confirmed")
    candidate = _lookup(1, "Sample Person", "role")

    evaluation = evaluate_query_candidate_plan(store, _plan(candidate))

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.evaluations[0].candidate is candidate
    assert evaluation.evaluations[0].candidate.family == (
        QueryCandidateFamily.DIRECT_OBJECT_LOOKUP
    )
    assert evaluation.evaluations[0].candidate.direction == (
        QueryCandidateDirection.SUBJECT_TO_OBJECT
    )
    assert evaluation.selected is candidate
    assert evaluation.selected.family == QueryCandidateFamily.DIRECT_OBJECT_LOOKUP
    assert evaluation.selected.direction == QueryCandidateDirection.SUBJECT_TO_OBJECT


def test_relation_discovery_allowed_label_can_translate(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Entity", "synthetic_relation", "Sample Value", status="confirmed")
    candidate = _relation_discovery(1, "synthetic_relation")

    evaluation = evaluate_query_candidate_plan(store, _plan(candidate))

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.selected is candidate
    assert evaluation.answers == ("q1: synthetic_relation",)


def test_relation_discovery_denied_label_requires_review(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Entity", "source", "Sample Value", status="confirmed")
    candidate = _relation_discovery(1, "source")

    candidate_evaluation = evaluate_query_candidate(store, candidate)
    plan_evaluation = evaluate_query_candidate_plan(store, _plan(candidate))

    assert candidate_evaluation.outcome == QueryCandidateOutcome.REVIEW_REQUIRED
    assert candidate_evaluation.answers == ()
    assert candidate_evaluation.review_reason == "relation label requires review: source"
    assert plan_evaluation.outcome == QueryCandidateSetOutcome.REVIEW_REQUIRED
    assert plan_evaluation.outcome != QueryCandidateSetOutcome.NO_ANSWER
    assert plan_evaluation.answers == ()


def test_relation_discovery_denied_label_normalization_requires_review(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    nfd_source = "sourc" + "é"
    nfc_source = "sourcé"
    monkeypatch.setattr(
        query_quality_policy,
        "LOW_SIGNAL_RELATION_LABELS",
        frozenset({nfc_source}),
    )
    store.add_fact("Sample Entity", nfc_source, "Sample Value", status="confirmed")
    candidate = _relation_discovery(1, nfc_source, display=f"  {nfd_source.upper()}  ")

    evaluation = evaluate_query_candidate_plan(store, _plan(candidate))

    assert evaluation.outcome == QueryCandidateSetOutcome.REVIEW_REQUIRED
    assert evaluation.evaluations[0].review_reason == (
        f"relation label requires review: {nfc_source}"
    )

    store.add_fact("Sample Entity", "SOURCE", "Sample Value", status="confirmed")
    monkeypatch.setattr(
        query_quality_policy,
        "LOW_SIGNAL_RELATION_LABELS",
        frozenset({"source"}),
    )
    source_candidate = _relation_discovery(2, "SOURCE", display="  SOURCE  ")
    source_evaluation = evaluate_query_candidate_plan(
        store,
        QueryCandidatePlan(qid=2, candidates=(source_candidate,)),
    )

    assert source_evaluation.outcome == QueryCandidateSetOutcome.REVIEW_REQUIRED
    assert source_evaluation.evaluations[0].review_reason == (
        "relation label requires review: source"
    )


def test_relation_discovery_mixed_denied_and_allowed_selects_allowed(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Entity", "source", "Sample Value", status="confirmed")
    store.add_fact(
        "Sample Entity", "synthetic_relation", "Sample Value", status="confirmed"
    )
    denied = _relation_discovery(1, "source")
    allowed = _relation_discovery(1, "synthetic_relation")

    evaluation = evaluate_query_candidate_plan(store, _plan(denied, allowed))

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.selected is allowed
    assert evaluation.answers == ("q1: synthetic_relation",)


def test_relation_discovery_direct_answer_wins_over_discovery_answer(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Entity", "role", "Direct Value", status="confirmed")
    store.add_fact(
        "Sample Entity", "synthetic_relation", "Sample Value", status="confirmed"
    )
    direct = _lookup(1, "Sample Entity", "role")
    discovery = _relation_discovery(1, "synthetic_relation")

    evaluation = evaluate_query_candidate_plan(store, _plan(discovery, direct))

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.selected is direct
    assert evaluation.answers == ("q1: Direct Value",)


def test_relation_discovery_selects_single_answering_family_without_direct_answer(
    tmp_path,
):
    store = _store(tmp_path)
    store.add_fact(
        "Sample Entity", "synthetic_relation", "Sample Value", status="confirmed"
    )
    no_answer_direct = _lookup(1, "Sample Entity", "missing_relation")
    discovery = _relation_discovery(1, "synthetic_relation")

    evaluation = evaluate_query_candidate_plan(
        store,
        _plan(no_answer_direct, discovery),
    )

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert evaluation.selected is discovery
    assert evaluation.answers == ("q1: synthetic_relation",)


def test_relation_discovery_subject_and_object_families_are_ambiguous(tmp_path):
    store = _store(tmp_path)
    store.add_fact(
        "Sample Entity", "subject_relation", "Sample Value", status="confirmed"
    )
    store.add_fact(
        "Sample Source", "object_relation", "Sample Entity", status="confirmed"
    )
    subject_discovery = _relation_discovery(1, "subject_relation")
    object_discovery = _object_relation_discovery(1, "object_relation")

    evaluation = evaluate_query_candidate_plan(
        store,
        _plan(subject_discovery, object_discovery),
    )

    assert evaluation.outcome == QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING
    assert evaluation.selected is subject_discovery
    assert evaluation.answers == ("q1: subject_relation",)


def test_relation_discovery_same_family_conflicting_answers_are_ambiguous(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Entity", "first_relation", "Sample Value", status="confirmed")
    store.add_fact("Sample Entity", "second_relation", "Sample Value", status="confirmed")
    first = _relation_discovery(1, "first_relation")
    second = _relation_discovery(1, "second_relation")

    evaluation = evaluate_query_candidate_plan(store, _plan(first, second))

    assert evaluation.outcome == QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING
    assert evaluation.selected is first
    assert evaluation.answers == ("q1: first_relation",)


def test_relation_discovery_missing_relation_label_requires_review(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Entity", "synthetic_relation", "Sample Value", status="confirmed")
    candidate = _candidate(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1("synthetic_relation") :- '
        'relation("Sample Entity", "synthetic_relation", "Sample Value").',
        family=QueryCandidateFamily.SUBJECT_RELATION_DISCOVERY,
        direction=QueryCandidateDirection.SUBJECT_TO_RELATION,
    )

    evaluation = evaluate_query_candidate_plan(store, _plan(candidate))

    assert evaluation.outcome == QueryCandidateSetOutcome.REVIEW_REQUIRED
    assert evaluation.evaluations[0].review_reason == (
        "relation discovery candidate lacks a relation label"
    )


def test_relation_quality_policy_does_not_block_direct_lookups(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "source", "Sample Document", status="confirmed")
    direct_object = _lookup(1, "Sample Person", "source")
    direct_relation = _candidate(
        '.decl answer_q2(value: symbol)\n'
        'answer_q2(R) :- relation("Sample Person", R, "Sample Document").',
        family=QueryCandidateFamily.DIRECT_RELATION_LOOKUP,
        direction=QueryCandidateDirection.SUBJECT_OBJECT_TO_RELATION,
        relation_display="source",
        relation_executable='"source"',
    )

    object_evaluation = evaluate_query_candidate_plan(store, _plan(direct_object))
    relation_evaluation = evaluate_query_candidate_plan(
        store,
        QueryCandidatePlan(qid=2, candidates=(direct_relation,)),
    )

    assert object_evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert object_evaluation.answers == ("q1: Sample Document",)
    assert relation_evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert relation_evaluation.answers == ("q2: source",)


def test_evaluate_query_candidate_plan_marks_conflicting_answer_sets_ambiguous(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "role", "Reviewer", status="confirmed")
    store.add_fact("Sample Person", "title", "Editor", status="confirmed")
    role = _lookup(1, "Sample Person", "role")
    title = _lookup(1, "Sample Person", "title")

    evaluation = evaluate_query_candidate_plan(store, _plan(role, title))

    assert evaluation.outcome == QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING
    assert evaluation.selected == role
    assert evaluation.answers == ("q1: Reviewer",)


def test_evaluate_query_candidate_plan_invalid_and_no_answer_rollups(tmp_path):
    store = _store(tmp_path)
    invalid = _candidate(".decl answer_q1(value: symbol)\nanswer_q1(O) :- bogus(O).")

    invalid_evaluation = evaluate_query_candidate_plan(store, _plan(invalid))
    mixed_evaluation = evaluate_query_candidate_plan(
        store, _plan(invalid, _lookup(1, "Sample Person", "role"))
    )

    assert invalid_evaluation.outcome == QueryCandidateSetOutcome.INVALID
    assert mixed_evaluation.outcome == QueryCandidateSetOutcome.NO_ANSWER


def test_evaluate_query_candidate_has_no_persistence_side_effects(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "role", "Reviewer", status="confirmed")
    before_facts = [dict(row) for row in store.facts()]
    before_questions = [dict(row) for row in store.questions()]

    evaluation = evaluate_query_candidate_plan(
        store, _plan(_lookup(1, "Sample Person", "role"))
    )

    assert evaluation.outcome == QueryCandidateSetOutcome.VALID
    assert [dict(row) for row in store.facts()] == before_facts
    assert [dict(row) for row in store.questions()] == before_questions
    assert not (tmp_path / "facts" / "query.dl").exists()
