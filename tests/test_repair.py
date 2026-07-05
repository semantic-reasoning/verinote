# SPDX-License-Identifier: MPL-2.0
import builtins

from verinote.llm.base import LLMError
from verinote.pipeline.query import load_query
from verinote.pipeline.repair import repair_questions
from verinote.store import Store


def _store_with_review_required(tmp_path):
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    qid = s.add_question("Where was Ada born?")
    s.set_question_query(qid, 'review_required("Where was Ada born?")', "review_required")
    return s, qid


def test_repair_accepts_engine_valid_proposal(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    s.set_question_query(qid, s.questions()[0]["query_dl"], "review_required", "stale")
    client = fake_client(query=lambda q, i: f'answer_q{i}(O) :- relation("Ada", "born_in", O).')
    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"
    assert s.questions()[0]["reason"] == ""
    assert f"answer_q{qid}" in (load_query(s) or "")


def test_repair_accepts_duckdb_supported_compound_query(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(
        query=lambda q, i: (
            f'answer_q{i}(S) :- relation(S, "has_role", role(person("Ada"), "PI")).'
        )
    )
    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"


def test_repair_accepts_valid_proposal_without_pyrewire(tmp_path, monkeypatch, fake_client):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyrewire":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(query=lambda q, i: f'answer_q{i}(O) :- relation("Ada", "born_in", O).')

    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"


def test_repair_rejects_engine_invalid_proposal(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    # references an undeclared predicate -> engine rejects, question untouched
    client = fake_client(query=lambda q, i: f"answer_q{i}(O) :- bogus(O).")
    results = repair_questions(s, client, root=tmp_path)

    assert results[0]["accepted"] is False
    assert "bogus" in results[0]["reason"]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["reason"] == results[0]["reason"]
    assert f"answer_q{qid}" not in (load_query(s) or "")


def test_repair_rejects_duckdb_unsupported_compound_query(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(
        query=lambda q, i: f'answer_q{i}(person(O)) :- relation("Ada", "born_in", O).'
    )
    results = repair_questions(s, client, root=tmp_path)

    assert results[0]["accepted"] is False
    assert "variable-bearing compound" in results[0]["reason"]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["reason"] == results[0]["reason"]
    assert f"answer_q{qid}" not in (load_query(s) or "")


def test_repair_rejects_still_unanswerable(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(query=lambda q, i: 'review_required("still nope")')
    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": False, "reason": "still nope"}]
    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["query_dl"] == 'review_required("still nope")'
    assert q["reason"] == "still nope"
    assert "review_required" not in (load_query(s) or "")


def test_repair_persists_no_answer_lifecycle_outcome(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(query=lambda q, i: 'no_answer("no confirmed facts match")')
    results = repair_questions(s, client, root=tmp_path)

    assert results == [
        {"id": qid, "accepted": False, "reason": "no confirmed facts match"}
    ]
    q = s.questions()[0]
    assert q["status"] == "no_answer"
    assert q["query_dl"] == 'no_answer("no confirmed facts match")'
    assert q["reason"] == "no confirmed facts match"
    assert "no_answer" not in (load_query(s) or "")


def test_repair_persists_ambiguous_lifecycle_outcome(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    client = fake_client(query=lambda q, i: 'ambiguous("multiple sample entities match")')
    results = repair_questions(s, client, root=tmp_path)

    assert results == [
        {"id": qid, "accepted": False, "reason": "multiple sample entities match"}
    ]
    q = s.questions()[0]
    assert q["status"] == "ambiguous"
    assert q["query_dl"] == 'ambiguous("multiple sample entities match")'
    assert q["reason"] == "multiple sample entities match"
    assert "ambiguous" not in (load_query(s) or "")


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
