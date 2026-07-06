# SPDX-License-Identifier: MPL-2.0
import unicodedata

import pytest

from verinote.llm.base import LLMError
from verinote.pipeline.query import (
    expand_query_relation_aliases,
    load_query,
    query_path,
    query_schema_hint,
    translate_questions,
)
from verinote.pipeline.corroboration import CorroborationPolicyError
from verinote.pipeline.query_intent import deterministic_query_intent
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_translate_persists_query_and_writes_file(tmp_path, fake_client, intent_payload):
    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = s.add_question("What is Sample Subject?")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Subject", relation="is_a"
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("schema-aware translation must not call direct Datalog")
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["id"] == qid
    assert results[0]["status"] == "translated"
    # the question row now carries a generated answer rule + its .decl
    q = s.questions()[0]
    assert q["status"] == "translated"
    assert f".decl answer_q{qid}" in q["query_dl"]
    assert (
        f'answer_q{qid}(O) :- relation("Sample Subject", "is_a", O).'
        in q["query_dl"]
    )
    # and the engine draft file was written
    draft = query_path(tmp_path)
    assert draft.is_file()
    assert load_query(s) == draft.read_text(encoding="utf-8")
    assert f"answer_q{qid}" in load_query(s)


def test_translate_korean_role_question_bypasses_llm(tmp_path):
    class FailingClient:
        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            raise AssertionError("deterministic role questions must not call intent LLM")

        def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
            raise AssertionError("deterministic role questions must not call direct Datalog")

    s = _store(tmp_path)
    s.add_fact("샘플인물", "역할", "검토자", status="confirmed")
    qid = s.add_question("샘플인물의 역할은 무엇인가?")
    results = translate_questions(s, FailingClient(), root=tmp_path)

    assert results == [
        {
            "id": qid,
            "status": "translated",
            "query_dl": s.questions()[0]["query_dl"],
            "reason": "",
        }
    ]
    query_dl = s.questions()[0]["query_dl"]
    assert f'answer_q{qid}(O) :- relation("샘플인물", "역할", O).' in query_dl
    assert "has_role" not in query_dl
    assert "person(" not in query_dl
    assert load_query(s) == query_dl + "\n"


def test_korean_role_question_is_parsed_as_structured_intent():
    intent = deterministic_query_intent("샘플인물의 역할은 무엇인가?")

    assert intent.kind.value == "lookup_object"
    assert intent.subject is not None
    assert intent.subject.value == "샘플인물"
    assert intent.relation_candidates == ("역할", "직책", "직위")


def test_translate_korean_provide_question_bypasses_llm(tmp_path):
    class FailingClient:
        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            raise AssertionError("deterministic provide questions must not call intent LLM")

        def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
            raise AssertionError("deterministic provide questions must not call direct Datalog")

    s = _store(tmp_path)
    s.add_fact("샘플조직", "제공 요소", "샘플서비스", status="confirmed")
    qid = s.add_question("샘플조직이 제공하는 것은?")

    results = translate_questions(s, FailingClient(), root=tmp_path)

    assert results == [
        {
            "id": qid,
            "status": "translated",
            "query_dl": s.questions()[0]["query_dl"],
            "reason": "",
        }
    ]
    query_dl = s.questions()[0]["query_dl"]
    assert f'answer_q{qid}(O) :- relation("샘플조직", "제공 요소", O).' in query_dl
    assert load_query(s) == query_dl + "\n"


def test_translate_retries_translation_failed_questions(tmp_path, fake_client, intent_payload):
    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = s.add_question("What is Sample Subject?")
    s.set_question_query(qid, None, "translation_failed", "provider returned invalid schema")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Subject", relation="is_a"
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("schema-aware retry must not call direct Datalog")
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": qid,
            "status": "translated",
            "query_dl": s.questions()[0]["query_dl"],
            "reason": "",
        }
    ]
    assert s.questions()[0]["status"] == "translated"
    assert "provider returned invalid schema" not in s.questions()[0]["reason"]


def test_load_query_expands_relation_aliases(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `역할`\n", encoding="utf-8")
    qid = s.add_question("Find the sample person's role")
    s.set_question_query(
        qid,
        f'.decl answer_q{qid}(value: symbol)\n'
        f'answer_q{qid}(V) :- relation("샘플인물", "role", V).',
        "translated",
    )

    from verinote.pipeline.query import write_query_file

    write_query_file(s, tmp_path)

    stored_query = s.questions()[0]["query_dl"]
    assert f'answer_q{qid}(V) :- relation("샘플인물", "role", V).' in stored_query
    assert f'answer_q{qid}(V) :- relation("샘플인물", "역할", V).' not in stored_query
    loaded_query = load_query(s)
    assert f'answer_q{qid}(V) :- relation("샘플인물", "role", V).' in loaded_query
    assert f'answer_q{qid}(V) :- relation("샘플인물", "역할", V).' in loaded_query


def test_expand_query_relation_aliases_handles_atoms_and_combinations():
    query_dl = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("샘플인물", role, X), relation(X, "title", O).\n'
    )

    expanded = expand_query_relation_aliases(query_dl, {"role": "역할", "title": "직함"})

    assert 'answer_q1(O) :- relation("샘플인물", role, X), relation(X, "title", O).' in expanded
    assert 'answer_q1(O) :- relation("샘플인물", "역할", X), relation(X, "title", O).' in expanded
    assert 'answer_q1(O) :- relation("샘플인물", role, X), relation(X, "직함", O).' in expanded
    assert 'answer_q1(O) :- relation("샘플인물", "역할", X), relation(X, "직함", O).' in expanded


def test_expand_query_relation_aliases_does_not_expand_variable_relations():
    query_dl = ".decl answer_q1(value: symbol)\n" 'answer_q1(R) :- relation("샘플인물", R, O).\n'

    assert expand_query_relation_aliases(query_dl, {"role": "역할"}) == query_dl


def test_expand_query_relation_aliases_normalizes_query_relation_names():
    decomposed = unicodedata.normalize("NFD", "역할")
    query_dl = (
        ".decl answer_q1(value: symbol)\n"
        f'answer_q1(O) :- relation("샘플인물", "{decomposed}", O).\n'
    )

    expanded = expand_query_relation_aliases(query_dl, {"역할": "role"})

    assert 'answer_q1(O) :- relation("샘플인물", "role", O).' in expanded


def test_expand_query_relation_aliases_does_not_duplicate_existing_canonical_rule():
    query_dl = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Sample Person", "role", O).\n'
        'answer_q1(O) :- relation("Sample Person", "역할", O).\n'
    )

    expanded = expand_query_relation_aliases(query_dl, {"role": "역할"})

    assert (
        expanded.count('answer_q1(O) :- relation("Sample Person", "역할", O).')
        == 1
    )
    assert (
        expanded.count('answer_q1(O) :- relation("Sample Person", "role", O).')
        == 1
    )


def test_expand_query_relation_aliases_does_not_expand_canonical_back_to_raw():
    query_dl = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Sample Person", "역할", O).\n'
    )

    expanded = expand_query_relation_aliases(query_dl, {"role": "역할"})

    assert expanded == query_dl


def test_expand_query_relation_aliases_caps_combinations():
    body = ", ".join(f'relation(X{i}, "r{i}", X{i + 1})' for i in range(7))
    query_dl = ".decl answer_q1(value: symbol)\n" f"answer_q1(O) :- {body}.\n"
    aliases = {f"r{i}": f"canonical_{i}" for i in range(7)}

    try:
        expand_query_relation_aliases(query_dl, aliases)
    except CorroborationPolicyError as exc:
        assert "query alias expansion exceeds" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected CorroborationPolicyError")


def test_review_required_question_is_flagged_not_in_draft(tmp_path, fake_client, intent_payload):
    s = _store(tmp_path)
    qid = s.add_question("What is the meaning of life?")
    client = fake_client(
        intent=intent_payload(
            "unknown_or_unsupported", reason="requires a synthetic relation"
        )
    )
    translate_questions(s, client, root=tmp_path)

    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["reason"] == "requires a synthetic relation"
    assert q["query_dl"].startswith("review_required(")
    # review_required lines are tracked in the DB, not fed to the engine
    assert f"answer_q{qid}" not in (load_query(s) or "")
    assert "review_required" not in (load_query(s) or "")


def test_invalid_intent_output_fails_translation_and_skips_draft(tmp_path, fake_client):
    s = _store(tmp_path)
    qid = s.add_question("What is the sample answer?")
    client = fake_client(intent={"kind": "lookup_object"})

    results = translate_questions(s, client, root=tmp_path)

    q = s.questions()[0]
    assert results[0]["status"] == "translation_failed"
    assert results[0]["query_dl"] is None
    assert "query intent output did not match schema:" in results[0]["reason"]
    assert q["status"] == "translation_failed"
    assert q["reason"] == results[0]["reason"]
    assert q["query_dl"] is None
    assert f"answer_q{qid}" not in (load_query(s) or "")
    assert load_query(s) == ""


def test_query_intent_errors_are_catchable_and_separate_from_datalog_translation(
    tmp_path, fake_client
):
    intent_client = fake_client(intent="not json")
    with pytest.raises(LLMError, match="query intent output was not JSON"):
        intent_client.extract_query_intent(question="What is the sample answer?")

    s = _store(tmp_path)
    qid = s.add_question("What is the sample answer?")
    datalog_client = fake_client(query=lambda question, qid: "this is not datalog")

    results = translate_questions(
        s, datalog_client, root=tmp_path, allow_direct_datalog_fallback=True
    )

    assert results[0]["id"] == qid
    assert results[0]["status"] == "review_required"
    assert "invalid query:" in results[0]["reason"]


def test_planner_no_candidates_requires_review(tmp_path, fake_client, intent_payload):
    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = s.add_question("What is the sample answer?")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Missing Subject", relation="is_a"
        )
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["id"] == qid
    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == "no query candidates matched the schema"
    assert load_query(s) == ""


def test_planned_executable_without_rows_becomes_no_answer(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = s.add_question("What is Sample Subject?")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Subject", relation="is_a"
        )
    )

    def no_rows(store, plan):
        assert plan.candidates
        return QueryCandidateSetEvaluation(
            plan=plan, outcome=QueryCandidateSetOutcome.NO_ANSWER
        )

    monkeypatch.setattr("verinote.pipeline.query.evaluate_query_candidate_plan", no_rows)

    results = translate_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": qid,
            "status": "no_answer",
            "query_dl": 'no_answer("no confirmed facts match")',
            "reason": "no confirmed facts match",
        }
    ]
    assert load_query(s) == ""


def test_quality_policy_review_required_outcome_is_persisted(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateOutcome
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = s.add_question("What is Sample Subject?")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Subject", relation="is_a"
        )
    )

    def review_required(store, plan):
        assert plan.candidates
        return QueryCandidateSetEvaluation(
            plan=plan,
            outcome=QueryCandidateSetOutcome.REVIEW_REQUIRED,
            evaluations=(
                QueryCandidateEvaluation(
                    candidate=plan.candidates[0],
                    outcome=QueryCandidateOutcome.REVIEW_REQUIRED,
                    review_reason="relation label requires review: source",
                ),
            ),
        )

    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan", review_required
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": qid,
            "status": "review_required",
            "query_dl": 'review_required("relation label requires review: source")',
            "reason": "relation label requires review: source",
        }
    ]
    assert load_query(s) == ""


def test_supported_planner_review_required_does_not_call_direct_fallback(
    tmp_path, fake_client, intent_payload
):
    s = _store(tmp_path)
    s.add_fact("Sample Entity", "source", "Sample Value", status="confirmed")
    qid = s.add_question("Synthetic planner-supported review?")
    client = fake_client(
        intent=intent_payload(
            "discover_entity_relations",
            subject="Sample Entity",
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("planner-supported review must not call direct Datalog fallback")
    )

    results = translate_questions(
        s,
        client,
        root=tmp_path,
        allow_direct_datalog_fallback=True,
    )

    assert results == [
        {
            "id": qid,
            "status": "review_required",
            "query_dl": 'review_required("relation label requires review: source")',
            "reason": "relation label requires review: source",
        }
    ]
    assert load_query(s) == ""


def test_quality_policy_review_reason_wins_over_invalid_candidate_reason(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateOutcome
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    s.add_question("What is Sample Subject?")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Subject", relation="is_a"
        )
    )

    def review_required(store, plan):
        assert plan.candidates
        invalid = QueryCandidateEvaluation(
            candidate=plan.candidates[0],
            outcome=QueryCandidateOutcome.INVALID,
            validation_reason="unsupported predicate: bogus",
        )
        denied = QueryCandidateEvaluation(
            candidate=plan.candidates[0],
            outcome=QueryCandidateOutcome.REVIEW_REQUIRED,
            review_reason="relation label requires review: source",
        )
        return QueryCandidateSetEvaluation(
            plan=plan,
            outcome=QueryCandidateSetOutcome.REVIEW_REQUIRED,
            evaluations=(invalid, denied),
        )

    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan", review_required
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == "relation label requires review: source"
    assert results[0]["query_dl"] == (
        'review_required("relation label requires review: source")'
    )


def test_query_schema_hint_is_bounded_schema_only(tmp_path):
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")

    hint = query_schema_hint(build_query_schema_snapshot(s))

    assert "Observed relations:" in hint
    assert "is_a" in hint
    assert "Sample Subject" not in hint
    assert "Synthetic Answer" not in hint


def test_translate_persists_llm_error_as_translation_failed(tmp_path, fake_client):
    s = _store(tmp_path)
    qid = s.add_question("What is the sample answer?")

    results = translate_questions(
        s, fake_client(error=LLMError("provider unavailable")), root=tmp_path
    )

    assert results == [
        {
            "id": qid,
            "status": "translation_failed",
            "query_dl": None,
            "reason": "provider unavailable",
        }
    ]
    q = s.questions()[0]
    assert q["status"] == "translation_failed"
    assert q["reason"] == "provider unavailable"
    assert q["query_dl"] is None
    assert load_query(s) == ""


def test_translate_only_touches_pending(tmp_path, fake_client):
    s = _store(tmp_path)
    s.add_question("q1")
    translate_questions(s, fake_client(), root=tmp_path)
    # second run with no new pending questions returns nothing
    again = translate_questions(s, fake_client(), root=tmp_path)
    assert again == []
