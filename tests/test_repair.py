# SPDX-License-Identifier: MPL-2.0
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
    client = fake_client(query=lambda q, i: f'answer_q{i}(O) :- relation("Ada", "born_in", O).')
    results = repair_questions(s, client, root=tmp_path)

    assert results == [{"id": qid, "accepted": True, "reason": ""}]
    assert s.questions()[0]["status"] == "translated"
    assert f"answer_q{qid}" in (load_query(s) or "")


def test_repair_rejects_engine_invalid_proposal(tmp_path, fake_client):
    s, qid = _store_with_review_required(tmp_path)
    # references an undeclared predicate -> engine rejects, question untouched
    client = fake_client(query=lambda q, i: f"answer_q{i}(O) :- bogus(O).")
    results = repair_questions(s, client, root=tmp_path)

    assert results[0]["accepted"] is False
    assert "bogus" in results[0]["reason"]
    assert s.questions()[0]["status"] == "review_required"
    assert f"answer_q{qid}" not in (load_query(s) or "")


def test_repair_rejects_still_unanswerable(tmp_path, fake_client):
    s, _qid = _store_with_review_required(tmp_path)
    client = fake_client(query=lambda q, i: 'review_required("still nope")')
    results = repair_questions(s, client, root=tmp_path)

    assert results[0]["accepted"] is False
    assert "cannot express" in results[0]["reason"]
    assert s.questions()[0]["status"] == "review_required"
