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


def _store_with_deterministic_planner_empty(tmp_path):
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    qid = s.add_question("What is Sample Person's birth place?")
    s.set_question_query(
        qid,
        'review_required("What is Sample Person\'s birth place?")',
        "review_required",
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


def test_repair_fallback_schema_hint_is_advisory_and_engine_rejects_invalid_proposal(
    tmp_path, fake_client
):
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact(
        "Synthetic Private Subject",
        "synthetic_relation",
        "Synthetic Private Object",
        status="confirmed",
    )
    client = fake_client()
    schema_hints = []

    def translate_query(*, question: str, qid: int, schema_hint: str = "") -> str:
        schema_hints.append(schema_hint)
        return (
            f'answer_q{qid}(O) :- relation("Synthetic Private Subject", '
            '"synthetic_relation", O), bogus(O).'
        )

    client.translate_query = translate_query

    results = repair_questions(s, client, root=tmp_path)

    assert results[0]["id"] == qid
    assert results[0]["accepted"] is False
    assert "bogus" in results[0]["reason"]
    assert len(schema_hints) == 1
    hint = schema_hints[0]
    assert "Observed relations:" in hint
    assert "synthetic_relation" in hint
    assert "Synthetic Private Subject" not in hint
    assert "Synthetic Private Object" not in hint
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


def test_repair_reaches_direct_datalog_after_a_declining_reinterpretation(
    tmp_path, fake_client
):
    """A re-reading that declines must not cost the question its rescue.

    A deterministically supported intent that plans nothing stays
    `review_required` with the direct-Datalog fallback still permitted, and
    `/repair` uses that permission to translate the question. The schema-aware
    reinterpretation now runs first; when it produces nothing executable, the
    question has to arrive at the fallback exactly as it does today.
    """
    s, qid = _store_with_deterministic_planner_empty(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        query=lambda q, i: f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).',
    )
    # No intent override: the fake client answers `unknown_or_unsupported`, so
    # the model declines to re-read the question.
    # No flag passed: exercises the production default wiring.
    results = repair_questions(s, client, root=tmp_path)

    assert client.calls == 2  # the reinterpretation, then the rescue
    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"
    assert f"answer_q{qid}" in (load_query(s) or "")


@pytest.mark.parametrize(
    "retry_outcome_name",
    ["NO_ANSWER", "AMBIGUOUS_CONFLICTING", "REVIEW_REQUIRED", "EMPTY"],
)
def test_repair_reaches_direct_datalog_after_any_declining_reinterpretation(
    tmp_path, fake_client, intent_payload, monkeypatch, retry_outcome_name
):
    """No re-reading verdict short of an executable query may close the question.

    `no_answer` and `ambiguous` are terminal -- nothing re-picks them -- and the
    repair gate forwards only `review_required` to the direct-Datalog fallback.
    So letting a re-reading's verdict replace the deterministic result would take
    away the rescue that translates this question today, on a reading of the
    question the deterministic layer never understood. `review_required` and a
    second `empty` lose the rescue a different way: the re-reading's result is
    built with `deterministic_intent_supported=False`, so it carries
    `allow_direct_datalog_fallback=False` where the deterministic one carried
    True.

    Parametrized so each outcome is independently falsifiable: admitting just one
    of them into the accept set must fail here, not be masked by the others.
    """
    from verinote.pipeline.query_candidate_eval import (
        QueryCandidateSetEvaluation,
        QueryCandidateSetOutcome,
    )

    s, qid = _store_with_deterministic_planner_empty(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="born_in"
        ),
        query=lambda q, i: f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).',
    )

    def declining(store, plan):
        # The deterministic pass plans nothing, so it must see a real EMPTY. The
        # re-reading's intent names a relation this KB holds, so its plan really
        # does carry a candidate and the verdict below is a verdict over one.
        if not plan.candidates:
            return QueryCandidateSetEvaluation(
                plan=plan, outcome=QueryCandidateSetOutcome.EMPTY
            )
        return QueryCandidateSetEvaluation(
            plan=plan, outcome=getattr(QueryCandidateSetOutcome, retry_outcome_name)
        )

    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan", declining
    )

    results = repair_questions(s, client, root=tmp_path)

    assert client.calls == 2  # the reinterpretation, then the rescue
    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"


def test_repair_reaches_direct_datalog_after_a_truncated_reinterpretation(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    """A re-reading whose own plan truncates must not close the question either.

    Truncation reports no outcome at all, so it is the one member of the
    disposition domain the outcome parametrization above cannot reach -- it
    happens before evaluation, so patching the evaluator cannot produce it. The
    consequence of admitting it is the same as for the others: the truncated
    result is built with `deterministic_intent_supported=False`, so it carries
    `allow_direct_datalog_fallback=False` and the question loses its rescue.
    """
    from verinote.pipeline.query_planner import (
        QueryCandidatePlan,
        plan_query_candidates,
    )

    real_plan_query_candidates = plan_query_candidates
    s, qid = _store_with_deterministic_planner_empty(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="born_in"
        ),
        query=lambda q, i: f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).',
    )
    planned = {"count": 0}

    def truncate_only_the_reinterpretation(intent, snapshot, **kwargs):
        plan = real_plan_query_candidates(intent, snapshot, **kwargs)
        planned["count"] += 1
        if planned["count"] == 1:
            return plan  # the deterministic pass: a genuinely empty plan
        return QueryCandidatePlan(
            qid=plan.qid, candidates=plan.candidates, truncated=True
        )

    monkeypatch.setattr(
        "verinote.pipeline.query.plan_query_candidates",
        truncate_only_the_reinterpretation,
    )

    results = repair_questions(s, client, root=tmp_path)

    assert planned["count"] == 2
    assert client.calls == 2  # the reinterpretation, then the rescue
    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"


def test_repair_surfaces_an_engine_error_found_by_the_reinterpretation(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    """An engine or policy error is a failure signal, not a declined reading.

    It is the one non-VALID outcome the re-reading may return in place of the
    deterministic result, because hiding it behind "no query candidates matched
    the schema" would report a broken engine as an ordinary miss -- and because
    spending the direct-Datalog rescue on an engine that is already erroring
    buys nothing.
    """
    from verinote.pipeline.query_candidate_eval import (
        QueryCandidateSetEvaluation,
        QueryCandidateSetOutcome,
    )

    s, _qid = _store_with_deterministic_planner_empty(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="born_in"
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("an erroring engine must not be handed a direct Datalog draft")
    )

    def engine_error(store, plan):
        if not plan.candidates:
            return QueryCandidateSetEvaluation(
                plan=plan, outcome=QueryCandidateSetOutcome.EMPTY
            )
        return QueryCandidateSetEvaluation(
            plan=plan, outcome=QueryCandidateSetOutcome.ENGINE_POLICY_ERROR
        )

    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan", engine_error
    )

    results = repair_questions(s, client, root=tmp_path)

    assert client.calls == 1
    assert results[0]["accepted"] is False
    assert results[0]["reason"].startswith("engine/policy error:")
    assert s.questions()[0]["status"] == "review_required"


def test_repair_disabled_fallback_keeps_deterministic_planner_empty_under_review(
    tmp_path, fake_client
):
    s, qid = _store_with_deterministic_planner_empty(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client()
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("disabled fallback must not call direct Datalog")
    )

    results = repair_questions(
        s, client, root=tmp_path, allow_direct_datalog_fallback=False
    )

    # The flag scopes the direct-Datalog fallback and nothing else. The
    # reinterpretation belongs to translation, so it still runs.
    assert client.calls == 1
    assert results == [
        {
            "id": qid,
            "accepted": False,
            "reason": (
                'relation "birth place" is not in the schema or its aliases (a policy/relation-aliases.md entry would map it)'
            ),
        }
    ]
    assert s.questions()[0]["status"] == "review_required"


def test_repair_provider_error_during_reinterpretation_skips_direct_fallback(
    tmp_path, fake_client
):
    """An outage must not be answered with a second request to the same provider.

    `_prepare_repair_question` consults `allow_direct_datalog_fallback` before it
    looks at `provider_failed`, so the reinterpretation's failure has to switch
    that flag off itself or the fallback fires into the same outage.
    """
    s, _qid = _store_with_deterministic_planner_empty(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(error=LLMError("synthetic outage"))
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("a failed provider must not be called again for this question")
    )

    results = repair_questions(s, client, root=tmp_path)

    assert client.calls == 1
    assert results[0]["accepted"] is False
    assert 'relation "birth place" is not in the schema or its aliases (a policy/relation-aliases.md entry would map it)' in results[0]["reason"]
    assert "synthetic outage" in results[0]["reason"]
    assert s.questions()[0]["status"] == "review_required"


def test_repair_llm_supported_planner_empty_does_not_call_direct_fallback(
    tmp_path, fake_client, intent_payload
):
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="missing_relation"
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("LLM-supported planner-empty repair must not call direct Datalog")
    )

    results = repair_questions(s, client, root=tmp_path)

    assert client.calls == 1
    assert results == [
        {
            "id": qid,
            "accepted": False,
            "reason": (
                'relation "missing_relation" is not in the schema or its aliases (a policy/relation-aliases.md entry would map it)'
            ),
        }
    ]
    assert s.questions()[0]["status"] == "review_required"


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


@pytest.mark.parametrize(
    "declared, claim",
    [
        ('review_required("model says so")', "model says so"),
        # Malformed variants reach a second branch that also sets the reason.
        ("review_required model says so", "model says so"),
    ],
)
def test_repair_attributes_a_model_declared_review_flag(
    tmp_path, fake_client, intent_payload, declared, claim
):
    """Keeping the flag at the model's request is fine; silently adopting its
    words as the reason is not — the engine never checked them."""
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: declared,
    )
    results = repair_questions(s, client, root=tmp_path)

    assert results[0]["accepted"] is False
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["reason"] == f"unvalidated model claim: review_required: {claim}"


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


def test_repair_llm_error_costs_one_provider_call(tmp_path, fake_client):
    """A provider outage costs one call: the fallback must not retry the outage.

    The direct-Datalog fallback exists for questions planning reports it cannot
    *support*. An `LLMError` says nothing about support — it says the provider
    never answered — so a second call to that same provider is both wrong and
    twice the cost per question during an outage or rate limit.
    """
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(error=LLMError("provider unavailable"))
    repair_questions(s, client, root=tmp_path)

    assert client.calls == 1


def test_repair_unsupported_intent_still_reaches_fallback(
    tmp_path, fake_client, intent_payload
):
    """An LLM-confirmed unsupported intent still costs the fallback call."""
    s, qid = _store_with_review_required(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(
        intent=intent_payload("unknown_or_unsupported", reason="planner cannot map"),
        query=lambda q, i: f'answer_q{i}(O) :- relation("Sample Person", "born_in", O).',
    )
    results = repair_questions(s, client, root=tmp_path)

    assert client.calls == 2
    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"


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
