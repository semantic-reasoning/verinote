# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.query import load_query, query_path, translate_questions
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_translate_persists_query_and_writes_file(tmp_path, fake_client):
    s = _store(tmp_path)
    qid = s.add_question("What is Ada?")
    results = translate_questions(s, fake_client(), root=tmp_path)

    assert results[0]["id"] == qid
    assert results[0]["status"] == "translated"
    # the question row now carries a generated answer rule + its .decl
    q = s.questions()[0]
    assert q["status"] == "translated"
    assert f".decl answer_q{qid}" in q["query_dl"]
    assert "answer_q%d(O) :- relation(" % qid in q["query_dl"]
    # and the engine draft file was written
    draft = query_path(tmp_path)
    assert draft.is_file()
    assert load_query(s) == draft.read_text(encoding="utf-8")
    assert f"answer_q{qid}" in load_query(s)


def test_review_required_question_is_flagged_not_in_draft(tmp_path, fake_client):
    s = _store(tmp_path)
    qid = s.add_question("What is the meaning of life?")
    client = fake_client(query=lambda question, qid: f'review_required("{question}")')
    translate_questions(s, client, root=tmp_path)

    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["query_dl"].startswith("review_required(")
    # review_required lines are tracked in the DB, not fed to the engine
    assert f"answer_q{qid}" not in (load_query(s) or "")
    assert "review_required" not in (load_query(s) or "")


def test_translate_only_touches_pending(tmp_path, fake_client):
    s = _store(tmp_path)
    s.add_question("q1")
    translate_questions(s, fake_client(), root=tmp_path)
    # second run with no new pending questions returns nothing
    again = translate_questions(s, fake_client(), root=tmp_path)
    assert again == []
