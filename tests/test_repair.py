# SPDX-License-Identifier: MPL-2.0
import builtins

import pytest

from verinote.engine.terms import Compound, StringLit
from verinote.llm.base import LLMError
from verinote.pipeline.query import load_query
from verinote.pipeline.repair import repair_questions
from verinote.store import Store


def _store_with_review_required(tmp_path):
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    qid = s.add_question("Where was Sample Person born?")
    s.set_question_query(
        qid, 'review_required("Where was Sample Person born?")', "review_required"
    )
    return s, qid


def test_repair_accepts_engine_valid_planned_query(tmp_path, fake_client, intent_payload):
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    s.set_question_query(qid, s.questions()[0]["query_dl"], "review_required", "stale")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="born_in"
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("repair must not call direct Datalog before the planner")
    )
    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"
    assert s.questions()[0]["reason"] == ""
    assert f"answer_q{qid}" in (load_query(s) or "")


def test_repair_accepts_relation_discovery_planned_query(
    tmp_path, fake_client, intent_payload
):
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact("Sample Entity", "synthetic_relation", "Sample Value", status="confirmed")
    qid = s.add_question("Synthetic relation discovery repair?")
    s.set_question_query(
        qid,
        'review_required("Synthetic relation discovery repair?")',
        "review_required",
    )
    client = fake_client(
        intent=intent_payload(
            "discover_entity_relations",
            subject="Sample Entity",
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("repair must not call direct Datalog for planner-supported paths")
    )

    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    question = s.questions()[0]
    assert question["status"] == "translated"
    assert (
        f'answer_q{qid}("synthetic_relation") :- '
        'relation("Sample Entity", "synthetic_relation", O).'
    ) in question["query_dl"]
    assert load_query(s) == question["query_dl"] + "\n"


def test_repair_planner_review_required_does_not_call_direct_fallback(
    tmp_path, fake_client, intent_payload
):
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact("Sample Entity", "source", "Sample Value", status="confirmed")
    qid = s.add_question("Synthetic relation discovery repair?")
    s.set_question_query(
        qid,
        'review_required("Synthetic relation discovery repair?")',
        "review_required",
    )
    client = fake_client(
        intent=intent_payload(
            "discover_entity_relations",
            subject="Sample Entity",
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("planner-supported repair must not call direct Datalog fallback")
    )

    results = repair_questions(
        s,
        client,
        root=tmp_path,
        allow_direct_datalog_fallback=True,
    )

    assert results == [
        {
            "id": qid,
            "accepted": False,
            "reason": "relation label requires review: source",
        }
    ]
    question = s.questions()[0]
    assert question["status"] == "review_required"
    assert question["query_dl"] == 'review_required("relation label requires review: source")'
    assert load_query(s) == ""


def test_repair_fallback_accepts_duckdb_supported_compound_query(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact(
        "Sample Person",
        "has_role",
        Compound(
            "role",
            (Compound("person", (StringLit("Sample Person"),)), StringLit("Lead")),
        ),
        status="confirmed",
    )
    client = fake_client(
        query=lambda q, i: (
            f'answer_q{i}(S) :- relation(S, "has_role", '
            'role(person("Sample Person"), "Lead")).'
        )
    )
    results = repair_questions(
        s, client, root=tmp_path, allow_direct_datalog_fallback=True
    )

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"


def test_repair_fallback_accepts_valid_proposal_without_pyrewire(
    tmp_path, monkeypatch, fake_client
):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyrewire":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        query=lambda q, i: f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).'
    )

    results = repair_questions(
        s, client, root=tmp_path, allow_direct_datalog_fallback=True
    )

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"


def test_repair_rejects_engine_invalid_proposal(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    # references an undeclared predicate -> engine rejects, question untouched
    client = fake_client(query=lambda q, i: f"answer_q{i}(O) :- bogus(O).")
    results = repair_questions(
        s, client, root=tmp_path, allow_direct_datalog_fallback=True
    )

    assert results[0]["accepted"] is False
    assert "bogus" in results[0]["reason"]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["reason"] == results[0]["reason"]
    assert f"answer_q{qid}" not in (load_query(s) or "")


def test_repair_rejects_duckdb_unsupported_compound_query(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(
        query=lambda q, i: (
            f'answer_q{i}(person(O)) :- relation("Sample Person", "born_in", O).'
        )
    )
    results = repair_questions(
        s, client, root=tmp_path, allow_direct_datalog_fallback=True
    )

    assert results[0]["accepted"] is False
    assert "variable-bearing compound" in results[0]["reason"]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["reason"] == results[0]["reason"]
    assert f"answer_q{qid}" not in (load_query(s) or "")


def test_repair_rejects_unsupported_intent(tmp_path, fake_client, intent_payload):
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="still unsupported")
    )
    results = repair_questions(
        s, client, root=tmp_path, allow_direct_datalog_fallback=False
    )

    assert results == [{"id": qid, "accepted": False, "reason": "still unsupported"}]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["query_dl"] == 'review_required("still unsupported")'
    assert q["reason"] == "still unsupported"
    assert "review_required" not in (load_query(s) or "")


def test_repair_persists_no_answer_lifecycle_outcome(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="born_in"
        )
    )

    def no_rows(store, plan):
        assert plan.candidates
        return QueryCandidateSetEvaluation(
            plan=plan, outcome=QueryCandidateSetOutcome.NO_ANSWER
        )

    monkeypatch.setattr("verinote.pipeline.query.evaluate_query_candidate_plan", no_rows)
    results = repair_questions(s, client, root=tmp_path)

    assert results == [
        {"id": qid, "accepted": False, "reason": "no confirmed facts match"}
    ]
    q = s.questions()[0]
    assert q["status"] == "no_answer"
    assert q["query_dl"] == 'no_answer("no confirmed facts match")'
    assert q["reason"] == "no confirmed facts match"
    assert "no_answer" not in (load_query(s) or "")


def test_repair_persists_ambiguous_lifecycle_outcome(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="born_in"
        )
    )

    def ambiguous(store, plan):
        assert plan.candidates
        return QueryCandidateSetEvaluation(
            plan=plan, outcome=QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING
        )

    monkeypatch.setattr("verinote.pipeline.query.evaluate_query_candidate_plan", ambiguous)
    results = repair_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": qid,
            "accepted": False,
            "reason": "multiple query candidates returned conflicting answers",
        }
    ]
    q = s.questions()[0]
    assert q["status"] == "ambiguous"
    assert (
        q["query_dl"]
        == 'ambiguous("multiple query candidates returned conflicting answers")'
    )
    assert q["reason"] == "multiple query candidates returned conflicting answers"
    assert "ambiguous" not in (load_query(s) or "")


def test_repair_default_runs_direct_datalog_fallback(
    tmp_path, fake_client, intent_payload
):
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).',
    )
    # No flag passed: exercises the production default wiring.
    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"
    assert f"answer_q{qid}" in (load_query(s) or "")


def test_repair_rejects_fallback_answering_a_different_question(
    tmp_path, fake_client, intent_payload
):
    """A snippet that answers some other question must not repair this one."""
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: (
            ".decl answer_q999(value: symbol)\n"
            'answer_q999(O) :- relation("Sample Person", "born_in", O).'
        ),
    )
    results = repair_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": qid,
            "accepted": False,
            "reason": f"invalid query: answer predicate must be answer_q{qid}, "
            "got answer_q999",
        }
    ]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert "answer_q999" not in (load_query(s) or "")


@pytest.mark.parametrize(
    "name, snippet",
    [
        # One case per place a foreign answer predicate can appear, each isolated
        # so it exercises a single arm of the guard.
        (
            "declaration",
            ".decl answer_q999(value: symbol)\n"
            'answer_q{qid}(O) :- relation("Sample Person", "born_in", O).',
        ),
        (
            "rule head",
            'answer_q{qid}(O) :- relation("Sample Person", "born_in", O).\n'
            'answer_q999(O) :- relation("Sample Person", "born_in", O).',
        ),
        (
            "fact",
            'answer_q{qid}(O) :- relation("Sample Person", "born_in", O).\n'
            'answer_q999("Sample Place").',
        ),
    ],
)
def test_repair_rejects_a_foreign_answer_predicate_anywhere(
    tmp_path, fake_client, intent_payload, name, snippet
):
    """Each spot a foreign answer predicate can hide is rejected on its own."""
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: snippet.format(qid=i),
    )
    results = repair_questions(s, client, root=tmp_path)

    # The guard's own reason, not a downstream `unknown predicate` rejection.
    assert results == [
        {
            "id": qid,
            "accepted": False,
            "reason": f"invalid query: answer predicate must be answer_q{qid}, "
            "got answer_q999",
        }
    ]
    assert s.questions()[0]["status"] == "review_required"
    assert "answer_q999" not in (load_query(s) or "")


def test_repair_rejects_fallback_answering_extra_questions(
    tmp_path, fake_client, intent_payload
):
    """Answering this question does not license answering others in the same snippet."""
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: (
            f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).\n'
            ".decl answer_q999(value: symbol)\n"
            'answer_q999(O) :- relation("Sample Person", "born_in", O).'
        ),
    )
    results = repair_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": qid,
            "accepted": False,
            "reason": f"invalid query: answer predicate must be answer_q{qid}, "
            "got answer_q999",
        }
    ]
    assert s.questions()[0]["status"] == "review_required"
    assert "answer_q999" not in (load_query(s) or "")


@pytest.mark.parametrize(
    "declared, claim",
    [
        ('no_answer("nothing in the KB")', "nothing in the KB"),
        ('ambiguous("two readings")', "two readings"),
    ],
)
def test_repair_does_not_let_the_model_retire_a_review_flag(
    tmp_path, fake_client, intent_payload, declared, claim
):
    """The model saying `no_answer`/`ambiguous` must not retire the review flag.

    These statuses are durable and no command re-picks them, so promoting an
    unvalidated model claim would close the question for good.
    """
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: declared,
    )
    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": False, "reason": results[0]["reason"]}]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    # The claim is recorded, attributed to the model rather than to the engine.
    assert claim in q["reason"]
    assert "unvalidated model claim" in q["reason"]
    assert declared not in (load_query(s) or "")


def test_repair_model_no_answer_claim_stays_repairable(
    tmp_path, fake_client, intent_payload
):
    """A rejected model claim must leave the question repairable on a later run."""
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    giving_up = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: 'no_answer("nothing in the KB")',
    )
    repair_questions(s, giving_up, root=tmp_path)
    assert s.questions()[0]["status"] == "review_required"

    # A later run with a model that produces a real query still repairs it.
    working = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).',
    )
    results = repair_questions(s, working, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"
    assert f"answer_q{qid}" in (load_query(s) or "")


def test_repair_llm_error_costs_two_provider_calls(tmp_path, fake_client):
    """A provider outage costs two calls: intent extraction, then the fallback.

    Pinned so the default-on fallback's cost during an outage or rate-limit is a
    deliberate choice rather than an accident.
    """
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(error=LLMError("provider unavailable"))
    repair_questions(s, client, root=tmp_path)

    assert client.calls == 2


def test_repair_persists_llm_error_reason(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    original_query = s.questions()[0]["query_dl"]
    client = fake_client(error=LLMError("provider unavailable"))
    results = repair_questions(s, client, root=tmp_path)

    assert results == [
        {"id": qid, "accepted": False, "reason": "llm error: provider unavailable"}
    ]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["query_dl"] == original_query
    assert q["reason"] == "llm error: provider unavailable"
