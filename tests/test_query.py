# SPDX-License-Identifier: MPL-2.0
import unicodedata

from verinote.llm.base import LLMError
from verinote.pipeline.query import (
    expand_query_relation_aliases,
    load_query,
    query_path,
    translate_questions,
)
from verinote.pipeline.corroboration import CorroborationPolicyError
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


def test_translate_korean_role_question_bypasses_llm(tmp_path):
    class FailingClient:
        def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
            raise AssertionError("deterministic role questions must not call the LLM")

    s = _store(tmp_path)
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
    assert f'answer_q{qid}(R) :- relation(S, R, "샘플인물").' in query_dl
    assert f'answer_q{qid}(R) :- relation(S, R, person("샘플인물")).' in query_dl
    assert load_query(s) == query_dl + "\n"


def test_load_query_expands_relation_aliases(tmp_path, fake_client):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `역할`\n", encoding="utf-8")
    qid = s.add_question("Find the sample person's role")
    client = fake_client(
        query=lambda question, qid: f'answer_q{qid}(V) :- relation("샘플인물", "role", V).'
    )

    translate_questions(s, client, root=tmp_path)

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


def test_review_required_question_is_flagged_not_in_draft(tmp_path, fake_client):
    s = _store(tmp_path)
    qid = s.add_question("What is the meaning of life?")
    client = fake_client(
        query=lambda question, qid: 'review_required("requires a synthetic relation")'
    )
    translate_questions(s, client, root=tmp_path)

    q = s.questions()[0]
    assert q["status"] == "review_required"
    assert q["reason"] == "requires a synthetic relation"
    assert q["query_dl"].startswith("review_required(")
    # review_required lines are tracked in the DB, not fed to the engine
    assert f"answer_q{qid}" not in (load_query(s) or "")
    assert "review_required" not in (load_query(s) or "")


def test_invalid_generated_query_requires_review_and_skips_draft(tmp_path, fake_client):
    s = _store(tmp_path)
    qid = s.add_question("What is the sample answer?")
    client = fake_client(query=lambda question, qid: "this is not datalog")

    results = translate_questions(s, client, root=tmp_path)

    q = s.questions()[0]
    assert results[0]["status"] == "review_required"
    assert results[0]["query_dl"].startswith("review_required(")
    assert "invalid query:" in results[0]["reason"]
    assert q["status"] == "review_required"
    assert q["reason"] == results[0]["reason"]
    assert q["query_dl"].startswith("review_required(")
    assert "this is not datalog" not in q["query_dl"]
    assert f"answer_q{qid}" not in (load_query(s) or "")
    assert load_query(s) == ""


def test_non_executable_question_states_store_reasons_and_skip_draft(tmp_path, fake_client):
    s = _store(tmp_path)
    no_answer_id = s.add_question("What is the sample answer?")
    ambiguous_id = s.add_question("Which sample item is current?")
    responses = iter(
        [
            'no_answer("no confirmed facts match")',
            'ambiguous("multiple sample entities match")',
        ]
    )
    client = fake_client(query=lambda question, qid: next(responses))

    results = translate_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": no_answer_id,
            "status": "no_answer",
            "query_dl": 'no_answer("no confirmed facts match")',
            "reason": "no confirmed facts match",
        },
        {
            "id": ambiguous_id,
            "status": "ambiguous",
            "query_dl": 'ambiguous("multiple sample entities match")',
            "reason": "multiple sample entities match",
        },
    ]
    rows = s.questions()
    assert [(q["status"], q["reason"]) for q in rows] == [
        ("no_answer", "no confirmed facts match"),
        ("ambiguous", "multiple sample entities match"),
    ]
    assert load_query(s) == ""


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
