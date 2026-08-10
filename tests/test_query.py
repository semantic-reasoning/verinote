# SPDX-License-Identifier: MPL-2.0
import ast
import unicodedata
import os
import stat
import threading
from pathlib import Path

import pytest

from verinote.llm.base import LLMError
from verinote.pipeline.query import (
    expand_query_relation_aliases,
    load_query,
    query_path,
    query_schema_hint,
    translate_questions,
    write_query_file,
)
import verinote.pipeline.query as query_module
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


def _translated_question(store: Store, text: str, rule: str) -> int:
    qid = store.add_question(text)
    store.set_question_query(qid, rule, "translated")
    return qid


def test_query_file_publishers_from_independent_stores_serialize(tmp_path, monkeypatch):
    first = _store(tmp_path)
    second = Store(tmp_path / "kb.sqlite")
    second.init_schema()
    _translated_question(first, "Synthetic one?", ".decl answer_q1()\nanswer_q1().")
    _translated_question(first, "Synthetic two?", ".decl answer_q2()\nanswer_q2().")

    entered_replace = threading.Event()
    release_replace = threading.Event()
    original_replace = os.replace

    def paused_replace(source, destination):
        entered_replace.set()
        assert release_replace.wait(2)
        original_replace(source, destination)

    monkeypatch.setattr("verinote.pipeline.query.os.replace", paused_replace)
    first_done = threading.Event()
    second_done = threading.Event()
    t1 = threading.Thread(target=lambda: (write_query_file(first, tmp_path), first_done.set()))
    t2 = threading.Thread(target=lambda: (write_query_file(second, tmp_path), second_done.set()))
    t1.start()
    assert entered_replace.wait(2)
    t2.start()
    assert not second_done.wait(0.1)
    release_replace.set()
    t1.join(2)
    t2.join(2)
    assert first_done.is_set() and second_done.is_set()
    expected = "\n".join(q["query_dl"] for q in second.questions() if q["status"] == "translated") + "\n"
    assert query_path(tmp_path).read_text(encoding="utf-8") == expected


def test_query_file_readers_keep_old_complete_output_while_staging(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")
    path = query_path(tmp_path)
    path.parent.mkdir()
    path.write_text("old complete output\n", encoding="utf-8")
    entered_replace = threading.Event()
    release_replace = threading.Event()
    original_replace = os.replace

    def paused_replace(source, destination):
        entered_replace.set()
        assert release_replace.wait(2)
        original_replace(source, destination)

    monkeypatch.setattr("verinote.pipeline.query.os.replace", paused_replace)
    thread = threading.Thread(target=lambda: write_query_file(store, tmp_path))
    thread.start()
    assert entered_replace.wait(2)
    assert path.read_text(encoding="utf-8") == "old complete output\n"
    release_replace.set()
    thread.join(2)
    assert not thread.is_alive()


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_query_file_staging_or_replace_failure_keeps_existing_output_and_cleans_temp(
    tmp_path, monkeypatch, failure
):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")
    path = query_path(tmp_path)
    path.parent.mkdir()
    path.write_text("old complete output\n", encoding="utf-8")
    if failure == "fsync":
        monkeypatch.setattr("verinote.pipeline.query.os.fsync", lambda *_: (_ for _ in ()).throw(OSError("synthetic")))
    else:
        monkeypatch.setattr("verinote.pipeline.query.os.replace", lambda *_: (_ for _ in ()).throw(OSError("synthetic")))

    with pytest.raises(OSError, match="synthetic"):
        write_query_file(store, tmp_path)

    assert path.read_text(encoding="utf-8") == "old complete output\n"
    assert not list(path.parent.glob(".query.dl.*"))


def test_query_file_preserves_existing_mode(tmp_path):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")
    path = query_path(tmp_path)
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o640)

    write_query_file(store, tmp_path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_query_file_uses_0644_for_new_file(tmp_path):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")

    path = write_query_file(store, tmp_path)

    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_query_file_mode_does_not_require_fchmod(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")
    monkeypatch.delattr(query_module.os, "fchmod", raising=False)

    path = write_query_file(store, tmp_path)

    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_query_file_applies_mode_before_staged_file_fsync(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")
    calls = []
    original_chmod = os.chmod
    original_fsync = os.fsync

    def record_chmod(*args):
        calls.append("chmod")
        return original_chmod(*args)

    def record_fsync(*args):
        calls.append("fsync")
        return original_fsync(*args)

    monkeypatch.setattr(query_module.os, "chmod", record_chmod)
    monkeypatch.setattr(query_module.os, "fsync", record_fsync)

    write_query_file(store, tmp_path)

    assert calls.index("chmod") < calls.index("fsync")


def test_directory_fsync_is_called_after_query_file_replacement(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")
    calls = []
    monkeypatch.setattr(query_module, "_fsync_directory", lambda path: calls.append(path))

    write_query_file(store, tmp_path)

    assert calls == [query_path(tmp_path).parent]


def test_directory_fsync_skips_platforms_without_directory_open_support(tmp_path, monkeypatch):
    monkeypatch.delattr(query_module.os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(query_module.os, "open", lambda *_: pytest.fail("directory must not be opened"))

    query_module._fsync_directory(tmp_path)


def test_directory_fsync_ignores_unsupported_sync_error(tmp_path, monkeypatch):
    closed = []
    monkeypatch.setattr(query_module.os, "open", lambda *_: 42)
    monkeypatch.setattr(
        query_module.os,
        "fsync",
        lambda _: (_ for _ in ()).throw(OSError(query_module.errno.EINVAL, "unsupported")),
    )
    monkeypatch.setattr(query_module.os, "close", closed.append)

    query_module._fsync_directory(tmp_path)

    assert closed == [42]


def test_directory_fsync_propagates_supported_platform_error(tmp_path, monkeypatch):
    monkeypatch.setattr(query_module.os, "open", lambda *_: 42)
    monkeypatch.setattr(
        query_module.os,
        "fsync",
        lambda _: (_ for _ in ()).throw(OSError(query_module.errno.EIO, "synthetic")),
    )
    monkeypatch.setattr(query_module.os, "close", lambda _: None)

    with pytest.raises(OSError, match="synthetic"):
        query_module._fsync_directory(tmp_path)


def test_directory_fsync_failure_reports_failed_publish_after_replace(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _translated_question(store, "Synthetic?", ".decl answer_q1()\nanswer_q1().")
    path = query_path(tmp_path)
    path.parent.mkdir()
    path.write_text("old complete output\n", encoding="utf-8")
    monkeypatch.setattr(
        query_module,
        "_fsync_directory",
        lambda _: (_ for _ in ()).throw(OSError("synthetic directory fsync failure")),
    )

    with pytest.raises(OSError, match="directory fsync failure"):
        write_query_file(store, tmp_path)

    assert path.read_text(encoding="utf-8") == ".decl answer_q1()\nanswer_q1().\n"


def test_translate_survives_a_reason_on_a_well_classified_intent(
    tmp_path, fake_client, intent_payload
):
    """A correctly classified intent that also carries a reason must translate.

    Issue #237: providers routinely fill `reason` (the schema requires the key)
    while classifying the question correctly. The intent validator used to treat
    that as an off-schema answer, so every such question failed translation even
    though the subject and relation were right there.
    """
    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = s.add_question("What is Sample Subject?")
    client = fake_client(
        intent=intent_payload(
            "lookup_object",
            subject="Sample Subject",
            relation="is_a",
            reason="the question names the subject and the relation",
        )
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["id"] == qid
    assert results[0]["status"] == "translated"
    assert (
        f'answer_q{qid}(O) :- relation("Sample Subject", "is_a", O).'
        in s.questions()[0]["query_dl"]
    )


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
    loaded_query = load_query(s)
    assert f'answer_q{qid}(O) :- relation("샘플인물", "역할", O).' in loaded_query
    assert f'answer_q{qid}(O) :- relation("샘플인물", "role", O).' in loaded_query


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
    s.add_fact("샘플조직", "provides", "샘플서비스", status="confirmed")
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
    assert f'answer_q{qid}(O) :- relation("샘플조직", "provides", O).' in query_dl
    loaded_query = load_query(s)
    assert f'answer_q{qid}(O) :- relation("샘플조직", "provides", O).' in loaded_query


def test_translate_korean_purpose_question_bypasses_llm(tmp_path):
    class FailingClient:
        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            raise AssertionError("deterministic purpose questions must not call intent LLM")

        def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
            raise AssertionError("deterministic purpose questions must not call direct Datalog")

    s = _store(tmp_path)
    s.add_fact("샘플프로젝트", "purpose", "샘플목표", status="confirmed")
    qid = s.add_question("샘플프로젝트의 목적은?")

    results = translate_questions(s, FailingClient(), root=tmp_path)

    assert results[0]["status"] == "translated"
    query_dl = s.questions()[0]["query_dl"]
    assert f'answer_q{qid}(O) :- relation("샘플프로젝트", "purpose", O).' in query_dl
    assert "목적" not in query_dl
    loaded_query = load_query(s)
    assert f'answer_q{qid}(O) :- relation("샘플프로젝트", "purpose", O).' in loaded_query


def test_canonical_purpose_question_answers_legacy_korean_relation_fact(tmp_path):
    class FailingClient:
        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            raise AssertionError("deterministic purpose questions must not call intent LLM")

        def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
            raise AssertionError("deterministic purpose questions must not call direct Datalog")

    s = _store(tmp_path)
    qid = s.add_question("What is Sample Project's purpose?")
    s.add_fact("Sample Project", "목적", "Sample Goal", status="confirmed")

    results = translate_questions(s, FailingClient(), root=tmp_path)

    assert results[0]["status"] == "translated"
    query_dl = s.questions()[0]["query_dl"]
    assert f'answer_q{qid}(O) :- relation("Sample Project", "목적", O).' in query_dl
    loaded_query = load_query(s)
    assert f'answer_q{qid}(O) :- relation("Sample Project", "purpose", O).' in loaded_query


def test_canonical_query_answers_legacy_alias_relation_fact(
    tmp_path, fake_client, intent_payload
):
    s = _store(tmp_path)
    s.add_fact("샘플조직", "제공 요소", "샘플서비스", status="confirmed")
    qid = s.add_question("What does the sample organization provide?")
    client = fake_client(
        intent=intent_payload(
            "lookup_object",
            subject="샘플조직",
            relation="provides",
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("schema-aware alias query must not call direct Datalog")
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["status"] == "translated"
    query_dl = s.questions()[0]["query_dl"]
    assert f'answer_q{qid}(O) :- relation("샘플조직", "제공 요소", O).' in query_dl
    loaded_query = load_query(s)
    assert f'answer_q{qid}(O) :- relation("샘플조직", "provides", O).' in loaded_query


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
    policy.mkdir(exist_ok=True)
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


def test_expand_query_relation_aliases_expands_canonical_to_raw_aliases():
    query_dl = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Sample Organization", "provides", O).\n'
    )

    expanded = expand_query_relation_aliases(query_dl, {"제공 요소": "provides"})

    assert (
        'answer_q1(O) :- relation("Sample Organization", "provides", O).'
        in expanded
    )
    assert (
        'answer_q1(O) :- relation("Sample Organization", "제공 요소", O).'
        in expanded
    )


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


def test_query_intent_errors_are_catchable(tmp_path, fake_client):
    intent_client = fake_client(intent="not json")
    with pytest.raises(LLMError, match="query intent output was not JSON"):
        intent_client.extract_query_intent(question="What is the sample answer?")


def test_translation_never_calls_direct_datalog_fallback(
    tmp_path, fake_client, intent_payload
):
    s = _store(tmp_path)
    qid = s.add_question("What is the sample answer?")
    client = fake_client(
        intent=intent_payload(
            "unknown_or_unsupported", reason="requires a synthetic relation"
        )
    )
    client.translate_query = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("ordinary translation must not call direct Datalog fallback")
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results == [
        {
            "id": qid,
            "status": "review_required",
            "query_dl": 'review_required("requires a synthetic relation")',
            "reason": "requires a synthetic relation",
        }
    ]


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
    # The relation `is_a` exists; the subject does not. The reason says which,
    # because "no query candidates matched the schema" sent the reader looking
    # for a missing relation that is right there.
    assert results[0]["reason"] == 'entity "Missing Subject" is not in the knowledge base'
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


def test_truncated_candidate_plan_is_review_required_without_evaluation(
    tmp_path, fake_client, intent_payload, monkeypatch
):
    from verinote.pipeline.query_planner import (
        QueryCandidate,
        QueryCandidateFamily,
        QueryCandidatePlan,
    )

    s = _store(tmp_path)
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = s.add_question("What is Sample Subject?")
    client = fake_client(intent=intent_payload("lookup_object", subject="Sample Subject", relation="is_a"))
    candidate = QueryCandidate(
        query_dl='.decl answer_q0(value: symbol)\nanswer_q0(O) :- relation("Sample Subject", "is_a", O).',
        family=QueryCandidateFamily.DIRECT_OBJECT_LOOKUP,
        direction=None,
        relation_display=None,
        relation_executable=None,
        subject_executable=None,
        object_executable=None,
    )
    monkeypatch.setattr(
        "verinote.pipeline.query.plan_query_candidates",
        lambda *args, **kwargs: QueryCandidatePlan(qid=qid, candidates=(candidate,), truncated=True),
    )
    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("truncated plan must not be evaluated")),
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == "too many query candidates matched the schema"


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


def test_supported_planner_review_required_does_not_call_direct_datalog(
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


def test_query_schema_hint_lists_canonical_relation_before_aliases(tmp_path):
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    s.add_fact("Sample Subject", "제공 요소", "Synthetic Answer", status="confirmed")

    hint = query_schema_hint(build_query_schema_snapshot(s))

    assert "- provides (aliases:" in hint
    assert "제공 요소" in hint
    assert "Sample Subject" not in hint
    assert "Synthetic Answer" not in hint


def test_query_schema_hint_includes_typed_comparison_type_and_amount_units(tmp_path):
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- revenue : amount as revenue_scalar (credit=10, token=100)\n",
        encoding="utf-8",
    )
    s.add_fact("Synthetic Company", "revenue", 'amount(2, "credit")', status="confirmed")

    hint = query_schema_hint(build_query_schema_snapshot(s))

    assert "typed: amount; units: credit=10, token=100" in hint
    assert "Synthetic Company" not in hint


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


_DETERMINISTIC_QUESTION = "What is Sample Person's birth place?"


@pytest.mark.parametrize(
    "outcome_name",
    ["NO_ANSWER", "AMBIGUOUS_CONFLICTING", "REVIEW_REQUIRED", "ENGINE_POLICY_ERROR"],
)
def test_engine_verdicts_are_not_reinterpreted(
    tmp_path, fake_client, monkeypatch, outcome_name
):
    """Only an empty plan may be re-read; every engine verdict stands.

    An empty plan is the schema declining to build anything, so it says nothing
    about the facts. Each of these outcomes is the engine's answer over
    candidates that were actually built, and re-reading the question until an
    answer changes is how a system talks itself out of a verdict it already has.

    Parametrized rather than looped so that a change re-litigating only one
    outcome still fails: a single test asserting all four would let three pass
    for the fourth.
    """
    from verinote.pipeline.query_candidate_eval import (
        QueryCandidateSetEvaluation,
        QueryCandidateSetOutcome,
    )

    s = _store(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    s.add_question(_DETERMINISTIC_QUESTION)
    client = fake_client()
    client.extract_query_intent = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError(f"{outcome_name} is an engine verdict and must not be re-read")
    )
    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan",
        lambda store, plan: QueryCandidateSetEvaluation(
            plan=plan, outcome=getattr(QueryCandidateSetOutcome, outcome_name)
        ),
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["status"] != "translated"
    assert client.calls == 0


def test_truncated_plan_is_not_reinterpreted(tmp_path, fake_client, monkeypatch):
    """A truncated plan reports no outcome at all, so it cannot reach the re-read.

    Truncation means too many candidates matched, the opposite of the empty plan
    the re-reading exists for. It also returns before the engine runs, so there
    is no verdict to compare against -- which is why the planner helper answers
    `None` rather than folding it into `EMPTY`.
    """
    from verinote.pipeline.query_planner import (
        QueryCandidate,
        QueryCandidateFamily,
        QueryCandidatePlan,
    )

    s = _store(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    qid = s.add_question(_DETERMINISTIC_QUESTION)
    client = fake_client()
    client.extract_query_intent = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("a truncated plan must not be re-read")
    )
    candidate = QueryCandidate(
        query_dl=f".decl answer_q{qid}(value: symbol)\nanswer_q{qid}(O) :- relation(\"Sample Person\", \"born_in\", O).",
        family=QueryCandidateFamily.DIRECT_OBJECT_LOOKUP,
        direction=None,
        relation_display=None,
        relation_executable=None,
        subject_executable=None,
        object_executable=None,
    )
    monkeypatch.setattr(
        "verinote.pipeline.query.plan_query_candidates",
        lambda *args, **kwargs: QueryCandidatePlan(
            qid=qid, candidates=(candidate,), truncated=True
        ),
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == "too many query candidates matched the schema"
    assert client.calls == 0


def test_llm_first_path_makes_exactly_one_intent_call(
    tmp_path, fake_client, intent_payload
):
    """A question the parser declined is already the model's reading.

    Re-reading it would ask the same provider the same question twice. The gate
    is the deterministic parser's support, not the empty plan alone -- this
    fixture plans empty, so a trigger that dropped that half would call twice.
    """
    s = _store(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    s.add_question("Tell me something about Sample Person, synthetically speaking")
    client = fake_client(
        intent=intent_payload(
            "lookup_object", subject="Sample Person", relation="missing_relation"
        )
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == (
        'relation "missing_relation" is not in the schema or its aliases (a policy/relation-aliases.md entry would map it)'
    )
    assert client.calls == 1


def test_reinterpretation_llm_error_declines_the_direct_datalog_fallback(
    tmp_path, fake_client
):
    """A failed re-reading must not be laundered, and must not invite a retry.

    `_prepare_repair_question` checks `allow_direct_datalog_fallback` before it
    checks `provider_failed`, so leaving the permission on would send a second
    request to the provider that has just failed.
    """
    s = _store(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    client = fake_client(error=LLMError("synthetic outage"))

    flow = query_module._schema_aware_query_flow_result(
        s,
        client,
        qid=1,
        question=_DETERMINISTIC_QUESTION,
        llm_error_status="review_required",
    )

    assert flow.status == "review_required"
    assert 'relation "birth place" is not in the schema or its aliases (a policy/relation-aliases.md entry would map it)' in flow.reason
    assert "synthetic outage" in flow.reason
    assert flow.provider_failed is True
    assert flow.allow_direct_datalog_fallback is False


def _diagnosis_store(tmp_path):
    s = _store(tmp_path)
    s.add_fact("샘플프로젝트", "purpose", "샘플목표", status="confirmed")
    s.add_fact("샘플조직", "is_a", "조직", status="confirmed")
    return s


def _plan_for(store, *, subject, relation, qid=1):
    from verinote.pipeline.query_intent import (
        IntentTarget,
        QueryIntent,
        QueryIntentKind,
    )
    from verinote.pipeline.query_planner import plan_query_candidates
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", subject),
        relation=IntentTarget("relation", relation),
    )
    snapshot = build_query_schema_snapshot(store, exact_entities=(subject,))
    return plan_query_candidates(intent, snapshot, qid=qid)


@pytest.mark.parametrize(
    ("subject", "relation", "relation_in_schema", "entity_in_kb"),
    [
        ("샘플프로젝트", "담당자", False, True),   # (a) relation absent
        ("없는조직", "purpose", True, False),      # (b) entity absent
        ("없는조직", "담당자", False, False),      # (a+b) both absent
        ("샘플조직", "purpose", True, True),       # (c) both known, no fact joins them
    ],
)
def test_empty_lookup_plan_reports_which_half_is_missing(
    tmp_path, subject, relation, relation_in_schema, entity_in_kb
):
    """One empty plan, three remedies -- so it has to say which one applies.

    (a+b) is listed because it is a real case, not a corner: the parser can
    mis-split a question so that neither half survives, and a diagnosis that
    picked one winner would send the user to fix half the problem.
    """
    plan = _plan_for(_diagnosis_store(tmp_path), subject=subject, relation=relation)

    assert plan.candidates == ()
    assert plan.diagnosis is not None
    assert plan.diagnosis.relation_in_schema is relation_in_schema
    assert plan.diagnosis.entity_in_kb is entity_in_kb


def test_relation_membership_is_read_from_the_complete_label_set(tmp_path):
    """Membership must not be answered from the bounded, rendered relation list.

    `snapshot.relations` is capped at `max_relations` because it is rendered
    into the model's hint and into candidate generation. Answering "does this
    relation exist?" from that cap reports a relation the KB holds as absent,
    which would tell the user to add an alias for something already there --
    and, once the re-read is gated on this field, would spend a provider call
    on it too.
    """
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    for index in range(120):
        s.add_fact("샘플주체", f"관계{index:03d}", f"값{index:03d}", status="confirmed")
    snapshot = build_query_schema_snapshot(s, exact_entities=("없는주체",))
    assert snapshot.relations_truncated  # the rendered list really is capped

    # A relation past the cap, asked about with a subject that does not hold it.
    plan = _plan_for(s, subject="없는주체", relation="관계119")

    assert plan.candidates == ()
    assert plan.diagnosis is not None
    assert plan.diagnosis.relation_in_schema is True
    assert plan.diagnosis.entity_in_kb is False


def test_diagnosis_is_skipped_when_the_snapshot_carries_no_membership_sets(tmp_path):
    """Absence is unknown, not false, when the sets were never built.

    A snapshot assembled by hand has no complete membership data. Treating that
    as "nothing exists" would emit a confidently wrong reason; the planner
    reports no diagnosis instead and the caller keeps its old wording.
    """
    from verinote.pipeline.query_intent import (
        IntentTarget,
        QueryIntent,
        QueryIntentKind,
    )
    from verinote.pipeline.query_planner import plan_query_candidates
    from verinote.pipeline.query_schema import QuerySchemaSnapshot

    bare = QuerySchemaSnapshot(
        relations=(),
        relations_truncated=False,
        relation_aliases=(),
        typed_relations=(),
        exact_entity_facts=(),
        exact_entity_facts_truncated=False,
        fact_count=0,
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "샘플조직"),
        relation=IntentTarget("relation", "purpose"),
    )

    plan = plan_query_candidates(intent, bare, qid=1)

    assert plan.candidates == ()
    assert plan.diagnosis is None


def test_known_entity_with_no_such_fact_is_reported_as_neither_half_missing(
    tmp_path, fake_client, intent_payload
):
    """Case (c) reads differently from the two absences, because nothing is wrong.

    The user has no alias to add and no spelling to fix; the KB simply does not
    hold the fact. Saying so is the difference between an instruction and a
    dead end.
    """
    s = _diagnosis_store(tmp_path)
    s.add_question("샘플조직의 목적은?")
    client = fake_client(
        intent=intent_payload("lookup_object", subject="샘플조직", relation="purpose")
    )

    results = translate_questions(s, client, root=tmp_path)

    assert results[0]["status"] == "review_required"
    # The question also carries its un-stripped josa reading (#431), which no KB
    # holds, so `any_unmatched` fires and the resolved reading is named. It is
    # the user's own word, so the message stays true -- but see #441: the
    # "a word was substituted" signal is now close to always-on for this shape.
    assert results[0]["reason"] == (
        'entity "샘플조직" is in the knowledge base and relation "목적" '
        "resolved, but no confirmed fact joins them"
    )


def test_reason_names_only_the_endpoint_the_knowledge_base_is_missing(tmp_path):
    """A question naming two entities must not accuse the one the KB holds.

    `entity_in_kb` is a single bool over both endpoints, so "how are A and B
    related?" with only B misspelled makes it False for the pair. Rendering that
    as "entity A, B is not in the knowledge base" states something false about
    A -- in the very message added to stop the reason being unhelpful. The
    diagnosis therefore carries the absent endpoints, not just the verdict.
    """
    from verinote.pipeline.query import _empty_plan_reason
    from verinote.pipeline.query_intent import (
        IntentTarget,
        QueryIntent,
        QueryIntentKind,
    )
    from verinote.pipeline.query_planner import plan_query_candidates
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    s.add_fact("샘플조직", "is_a", "조직", status="confirmed")
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_RELATION,
        subject=IntentTarget("entity", "샘플조직"),
        object=IntentTarget("entity", "없는것"),
    )
    snapshot = build_query_schema_snapshot(s, exact_entities=("샘플조직", "없는것"))

    plan = plan_query_candidates(intent, snapshot, qid=1)

    assert plan.candidates == ()
    assert plan.diagnosis is not None
    assert plan.diagnosis.entity_in_kb is False
    assert plan.diagnosis.absent_entities == ("없는것",)
    reason = _empty_plan_reason(plan, intent)
    assert reason == 'entity "없는것" is not in the knowledge base'
    assert "샘플조직" not in reason


def test_a_typed_relation_alias_counts_as_being_in_the_schema(tmp_path):
    """The planner matches a typed alias, so membership must see it too.

    `_relation_matches_any` observes a relation's typed-spec name and alias,
    which are declared in `policy/typed-relations.md` and appear in no fact. A
    membership set built from facts alone calls such an alias absent, and the
    reason then tells the user to add it to `policy/relation-aliases.md` -- a
    remedy for a problem they do not have, prescribed about a relation the
    planner can already match.
    """
    from verinote.pipeline.query_intent import (
        IntentTarget,
        QueryIntent,
        QueryIntentKind,
    )
    from verinote.pipeline.query_planner import (
        _matching_relations,
        _requested_relations_in_schema,
    )
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    (tmp_path / "policy").mkdir(exist_ok=True)
    (tmp_path / "policy" / "typed-relations.md").write_text(
        "- `가격`: amount as price (원=1)\n", encoding="utf-8"
    )
    s.add_fact("샘플제품", "가격", "1000", status="confirmed")
    snapshot = build_query_schema_snapshot(s, exact_entities=("다른제품",))
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "다른제품"),
        relation=IntentTarget("relation", "price"),
    )

    # The planner does match it; membership must not disagree.
    assert _matching_relations(intent, snapshot)
    assert _requested_relations_in_schema(("price",), snapshot) == (("price",), False)


def test_an_entity_seen_only_as_an_object_is_in_the_knowledge_base(tmp_path):
    """Being in the KB is not the same as being some relation's subject.

    `entity_in_kb` exists to catch a name the KB has never heard of. An entity
    that appears only on the object side has been heard of, so calling it absent
    would report a spelling problem for a correctly spelled name -- and the real
    finding, that this subject has no such fact, would be lost.
    """
    plan = _plan_for(
        _kb_with_object_only_entity(tmp_path), subject="김철수", relation="owner"
    )

    assert plan.candidates == ()
    assert plan.diagnosis is not None
    assert plan.diagnosis.entity_in_kb is True
    assert plan.diagnosis.absent_entities == ()


def _kb_with_object_only_entity(tmp_path):
    s = _store(tmp_path)
    s.add_fact("프로젝트A", "owner", "김철수", status="confirmed")
    return s


def test_membership_sets_carry_every_spelling_the_matcher_compares_against(tmp_path):
    """The sets must mirror the matcher's spellings, not a convenient subset.

    `_matching_entities` compares an intent's value against a term's `display`,
    `executable` and `key`, and `_relation_matches_any` does the same for a
    relation. A membership set holding only the displayed surface therefore says
    "absent" for a spelling the planner would have matched -- the divergence
    that made a typed alias read as missing. Pinning the spellings keeps the two
    from drifting apart again.
    """
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    s.add_fact("샘플조직", "is_a", "조직", status="confirmed")
    snapshot = build_query_schema_snapshot(s)
    relation = snapshot.relations[0].relation
    subject = snapshot.relations[0].subjects[0]

    assert snapshot.all_relation_labels is not None
    assert {relation.display, relation.executable, relation.key} <= snapshot.all_relation_labels
    assert snapshot.all_entity_surfaces is not None
    assert {subject.display, subject.executable, subject.key} <= snapshot.all_entity_surfaces
    # The three really are different spellings, or this test would pass vacuously.
    assert len({relation.display, relation.executable, relation.key}) == 3


def _kb_where_the_join_search_truncates(tmp_path):
    """A hub relation and a busy entity, so both bounded views are capped."""
    s = _store(tmp_path)
    for index in range(150):  # past max_entities_per_side on purpose.subjects
        s.add_fact(f"주체{index:03d}", "purpose", f"목표{index:03d}", status="confirmed")
    for index in range(60):  # past max_exact_entity_facts, sorting before "purpose"
        s.add_fact("힣타겟", f"aaa{index:03d}", f"값{index:03d}", status="confirmed")
    s.add_fact("힣타겟", "purpose", "진짜목표", status="confirmed")
    return s


def test_no_fact_joins_them_is_not_claimed_when_the_search_was_truncated(tmp_path):
    """Emptiness is read from bounded views, so it cannot always be trusted.

    Membership comes from complete sets, but candidate generation does not: a
    relation's subject list and the exact-fact list are both capped. On a hub
    relation the joining fact can sit just past a cap, and the planner comes up
    empty while the fact exists. Saying "no confirmed fact joins them" there is
    a false statement about the KB's own contents -- worse than the vague string
    it replaced, which was at least true.
    """
    from verinote.pipeline.query import _empty_plan_reason
    from verinote.pipeline.query_intent import (
        IntentTarget,
        QueryIntent,
        QueryIntentKind,
    )
    from verinote.pipeline.query_planner import plan_query_candidates
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _kb_where_the_join_search_truncates(tmp_path)
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "힣타겟"),
        relation=IntentTarget("relation", "purpose"),
    )
    snapshot = build_query_schema_snapshot(s, exact_entities=("힣타겟",))
    # The fixture really does truncate, or this test proves nothing.
    assert snapshot.exact_entity_facts_truncated
    plan = plan_query_candidates(intent, snapshot, qid=1)
    assert plan.candidates == ()
    # ...and the fact it would have to have found is really there.
    assert any(
        row["subject"] == "힣타겟" and row["relation"] == "purpose"
        for row in s.facts(statuses=["confirmed"])
    )

    assert plan.diagnosis is not None
    assert plan.diagnosis.join_search_complete is False
    assert _empty_plan_reason(plan, intent) == "no query candidates matched the schema"


def test_no_fact_joins_them_is_claimed_when_the_search_was_complete(tmp_path):
    """The guard must not swallow the case it was built to report.

    On a KB small enough for every view to be whole, emptiness really does prove
    the fact is absent, and that is the one reading of the three that tells the
    user nothing needs fixing.
    """
    plan = _plan_for(_diagnosis_store(tmp_path), subject="샘플조직", relation="purpose")

    assert plan.diagnosis is not None
    assert plan.diagnosis.join_search_complete is True


def test_case_c_names_the_reading_only_when_a_requested_label_was_dropped(tmp_path):
    """Name the substitution when there is one, and stay quiet when there is not.

    `_relation_requests` merges `intent.relation` with `relation_candidates`, and
    an LLM-supplied candidate need not be an alias sibling of the relation it
    accompanies. When only the sibling resolves, "the requested relation
    resolved" hides that the question was read as a different word. When every
    label resolves they are one relation under alias policy -- always so for the
    sets the deterministic parser invents -- and singling one out would show the
    user a word they may never have typed.
    """
    from verinote.pipeline.query import _empty_plan_reason
    from verinote.pipeline.query_intent import (
        IntentTarget,
        QueryIntent,
        QueryIntentKind,
    )
    from verinote.pipeline.query_planner import plan_query_candidates
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    s.add_fact("홍길동", "purpose", "샘플목표", status="confirmed")
    s.add_fact("샘플조직", "is_a", "조직", status="confirmed")
    partial = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "샘플조직"),
        relation=IntentTarget("relation", "담당자"),
        # `목적` resolves to the `purpose` relation, so its canonical differs
        # from its spelling -- which is what makes the display rule testable.
        relation_candidates=("목적",),
    )
    snapshot = build_query_schema_snapshot(s, exact_entities=("샘플조직",))

    plan = plan_query_candidates(partial, snapshot, qid=1)

    assert plan.candidates == ()
    assert plan.diagnosis is not None
    # `담당자` is absent and `목적` resolved, so the reading is disclosed --
    # spelled as the question spelled it, not as `purpose`, which is neither the
    # requested word nor a label this KB would show for it.
    assert plan.diagnosis.any_unmatched is True
    assert plan.diagnosis.matched_relations == ("목적",)
    assert _empty_plan_reason(plan, partial) == (
        'entity "샘플조직" is in the knowledge base and relation "목적" '
        "resolved, but no confirmed fact joins them"
    )


def test_no_fact_joins_them_needs_evidence_not_merely_an_untruncated_list(tmp_path):
    """An empty exact-fact list is not a completed search; it is no search.

    `not truncated` is also true of a list that was never populated, so a
    snapshot built without `exact_entities` would report the join search as
    exhaustive with no backstop behind it and claim a fact absent on no
    evidence. A diagnosed entity that is in the KB has at least one fact, so an
    empty list here means the snapshot was built for something else.
    """
    from verinote.pipeline.query import _empty_plan_reason
    from verinote.pipeline.query_intent import (
        IntentTarget,
        QueryIntent,
        QueryIntentKind,
    )
    from verinote.pipeline.query_planner import plan_query_candidates
    from verinote.pipeline.query_schema import build_query_schema_snapshot

    s = _store(tmp_path)
    s.add_fact("샘플주체", "관계000", "값000", status="confirmed")
    s.add_fact("다른주체", "관계001", "값001", status="confirmed")
    # 샘플주체 is in the KB and 관계001 is in the schema, but they do not join --
    # the (c) shape, which is the one that needs evidence.
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "샘플주체"),
        relation=IntentTarget("relation", "관계001"),
    )
    # Built WITHOUT exact_entities: the list is empty and un-truncated.
    snapshot = build_query_schema_snapshot(s)
    assert snapshot.exact_entity_facts == ()
    assert snapshot.exact_entity_facts_truncated is False

    plan = plan_query_candidates(intent, snapshot, qid=1)

    assert plan.diagnosis is not None
    assert plan.diagnosis.join_search_complete is False
    assert "no confirmed fact joins them" not in _empty_plan_reason(plan, intent)


def test_every_provider_failure_exit_reports_the_provider_failed():
    """Producer-side tripwire for `provider_failed` (#438).

    Ask suppresses its fallback request on this flag, so a handler that builds a
    `_QueryFlowResult` after an `LLMError` and forgets the keyword silently
    re-opens the defect: the dataclass defaults it to `False`, and the consumer
    cannot tell a provider that did not fail from one nobody reported. The
    consumer half -- that Ask reads the flag and does not condition on status --
    is pinned in tests/test_ask.py.

    The offender set is re-derived from the AST on every run rather than
    compared against a list of known sites, so this carries no count and a new
    module or handler is covered the day it is written. It swept the whole
    package deliberately: `_QueryFlowResult` lives in one module today, and a
    second one importing the private name is exactly the drift worth catching.

    What would make this vacuous: no construction inside any `except LLMError:`
    handler, so the loop finds nothing to judge. The companion assertion below
    requires the sweep to have examined at least one.
    """
    package = Path(__file__).resolve().parent.parent / "verinote"
    offenders = []
    examined = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type
            names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
            if not any(getattr(n, "id", getattr(n, "attr", None)) == "LLMError" for n in names):
                continue
            for child in ast.walk(node):
                if not (
                    isinstance(child, ast.Call)
                    and getattr(child.func, "id", None) == "_QueryFlowResult"
                ):
                    continue
                examined.append(f"{path.name}:{child.lineno}")
                flagged = any(
                    kw.arg == "provider_failed"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in child.keywords
                )
                if not flagged:
                    offenders.append(f"{path.name}:{child.lineno}")

    assert examined, (
        "no `_QueryFlowResult` was built inside an `except LLMError:` handler, so "
        "this sweep judged nothing -- it has been made vacuous, not satisfied"
    )
    assert offenders == [], (
        "these provider-failure exits build a flow result without "
        f"provider_failed=True, so Ask cannot tell they failed: {offenders}"
    )
