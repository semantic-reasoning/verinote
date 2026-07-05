# SPDX-License-Identifier: MPL-2.0

import verinote.pipeline.query_candidate_eval as query_candidate_eval
from verinote.engine import CheckReport
from verinote.pipeline.query_candidate_eval import (
    QueryCandidateOutcome,
    QueryCandidateSetOutcome,
    evaluate_query_candidate,
    evaluate_query_candidate_plan,
)
from verinote.pipeline.query_planner import QueryCandidate, QueryCandidatePlan
from verinote.store import Store


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _candidate(query_dl: str) -> QueryCandidate:
    return QueryCandidate(
        query_dl=query_dl,
        relation_display=None,
        relation_executable=None,
        subject_executable=None,
        object_executable=None,
    )


def _lookup(qid: int, subject: str, relation: str) -> QueryCandidate:
    return _candidate(
        f'.decl answer_q{qid}(value: symbol)\n'
        f'answer_q{qid}(O) :- relation("{subject}", "{relation}", O).'
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
