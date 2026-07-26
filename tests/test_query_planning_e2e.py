# SPDX-License-Identifier: MPL-2.0

import verinote.cli as cli
from verinote.pipeline.query import load_query, query_path, translate_questions
from verinote.pipeline.verify import verify
from verinote.store import Store


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _target(kind: str, value: str | None) -> dict | None:
    return None if value is None else {"kind": kind, "value": value}


def _intent(
    kind: str,
    *,
    subject: str | None = None,
    relation: str | None = None,
    object: str | None = None,
    relation_candidates: tuple[str, ...] = (),
    reason: str | None = None,
) -> dict:
    return {
        "kind": kind,
        "subject": _target("entity", subject),
        "relation": _target("relation", relation),
        "object": _target("entity", object),
        "relation_candidates": list(relation_candidates),
        "operator": None,
        "value_type": None,
        "value": None,
        "reason": reason,
    }


class IntentOnlyClient:
    name = "intent-only"

    def __init__(self, intent):
        self.intent = intent
        self.intent_calls = 0
        self.direct_datalog_calls = 0

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        self.intent_calls += 1
        raw = self.intent(question) if callable(self.intent) else self.intent
        return parse_query_intent(raw)

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        self.direct_datalog_calls += 1
        raise AssertionError("planner path must not call direct Datalog fallback")


def test_translate_planner_persists_query_file_and_verify_answers(tmp_path):
    store = _store(tmp_path)
    store.add_fact(
        "샘플인물",
        "직책",
        "샘플역할",
        status="confirmed",
    )
    qid = store.add_question("샘플인물 관련 직책 값을 찾아라")
    client = IntentOnlyClient(
        _intent(
            "lookup_object",
            subject="샘플인물",
            relation="직책",
        )
    )

    results = translate_questions(store, client, root=tmp_path)
    report = verify(store)

    assert client.intent_calls == 1
    assert client.direct_datalog_calls == 0
    assert results[0]["status"] == "translated"
    question = store.questions()[0]
    assert question["status"] == "translated"
    assert question["reason"] == ""
    assert (
        f'answer_q{qid}(O) :- relation("샘플인물", "직책", O).'
    ) in question["query_dl"]
    assert '"role"' not in question["query_dl"]
    assert "has_role" not in question["query_dl"]
    written_query = query_path(tmp_path).read_text(encoding="utf-8")
    loaded_query = load_query(store)
    assert written_query in loaded_query
    assert f'answer_q{qid}(O) :- relation("샘플인물", "role", O).' in loaded_query
    assert report.engine_available is True
    assert report.ok is True
    assert report.answers == ["q1: 샘플역할"]


def test_translate_relation_discovery_persists_relation_answer(tmp_path):
    store = _store(tmp_path)
    store.add_fact(
        "Sample Entity",
        "synthetic_relation",
        "Sample Value",
        status="confirmed",
    )
    qid = store.add_question("How is Sample Entity related?")
    client = IntentOnlyClient(
        _intent(
            "discover_entity_relations",
            subject="Sample Entity",
        )
    )

    results = translate_questions(store, client, root=tmp_path)
    report = verify(store)

    assert results[0]["status"] == "translated"
    question = store.questions()[0]
    assert question["status"] == "translated"
    assert (
        f'answer_q{qid}("synthetic_relation") :- '
        'relation("Sample Entity", "synthetic_relation", O).'
    ) in question["query_dl"]
    assert report.ok is True
    assert report.answers == ["q1: synthetic_relation"]


def test_translate_relation_discovery_relation_hint_prefers_direct_lookup(tmp_path):
    store = _store(tmp_path)
    store.add_fact(
        "Sample Entity",
        "provides",
        "Sample Value",
        status="confirmed",
    )
    store.add_fact(
        "Sample Entity",
        "owns",
        "Other Value",
        status="confirmed",
    )
    qid = store.add_question("Synthetic relation hint coverage?")
    client = IntentOnlyClient(
        _intent(
            "discover_entity_relations",
            subject="Sample Entity",
            relation="provides",
        )
    )

    results = translate_questions(store, client, root=tmp_path)
    report = verify(store)

    assert results[0]["status"] == "translated"
    question = store.questions()[0]
    assert (
        f'answer_q{qid}(O) :- relation("Sample Entity", "provides", O).'
    ) in question["query_dl"]
    assert "ambiguous" not in question["query_dl"]
    assert report.ok is True
    assert report.answers == ["q1: Sample Value"]


def test_translate_relation_discovery_low_signal_relation_requires_review(tmp_path):
    store = _store(tmp_path)
    store.add_fact(
        "Sample Entity",
        "source",
        "Sample Value",
        status="confirmed",
    )
    store.add_question("How is Sample Entity related?")
    client = IntentOnlyClient(
        _intent(
            "discover_entity_relations",
            subject="Sample Entity",
        )
    )

    results = translate_questions(store, client, root=tmp_path)

    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == "relation label requires review: source"
    question = store.questions()[0]
    assert question["status"] == "review_required"
    assert question["query_dl"] == 'review_required("relation label requires review: source")'
    assert load_query(store) == ""


def test_cli_query_uses_planner_and_reports_stable_row_outcome(
    tmp_path, monkeypatch, capsys
):
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    store.add_fact(
        "Synthetic CLI Subject",
        "synthetic_cli_relation",
        "Synthetic CLI Answer",
        status="confirmed",
    )
    store.close()
    client = IntentOnlyClient(
        _intent(
            "lookup_object",
            subject="Synthetic CLI Subject",
            relation="synthetic_cli_relation",
        )
    )
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    rc = cli.main(["query", "Which synthetic CLI value is recorded?"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translated - Executable query is ready." in out
    assert "translated 1 question(s)" in out
    assert client.intent_calls == 1
    assert client.direct_datalog_calls == 0

    store = Store(tmp_path / "kb.sqlite")
    question = store.questions()[0]
    assert question["status"] == "translated"
    assert question["reason"] == ""
    assert (
        'answer_q1(O) :- relation("Synthetic CLI Subject", '
        '"synthetic_cli_relation", O).'
    ) in question["query_dl"]
    assert query_path(tmp_path).read_text(encoding="utf-8") == question["query_dl"] + "\n"

    report = verify(store)
    assert report.engine_available is True
    assert report.answers == ["q1: Synthetic CLI Answer"]


def test_web_translate_marks_malformed_intent_visibly_failed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import verinote.web.app as webapp
    from verinote.config import Config
    from verinote.web import create_app

    class MalformedIntentClient:
        name = "malformed-intent"

        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            from verinote.pipeline.query_intent import parse_query_intent

            return parse_query_intent({"kind": "lookup_object"})

        def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
            raise AssertionError("planner path must not call direct Datalog fallback")

    monkeypatch.setattr(webapp, "get_client", lambda cfg: MalformedIntentClient())
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    client = TestClient(create_app(cfg))
    store = client.app.state.store
    store.add_question("Which synthetic malformed value is recorded?")

    response = client.post("/questions/translate", follow_redirects=True)

    assert response.status_code == 200
    question = store.questions()[0]
    assert question["status"] == "translation_failed"
    assert question["status"] != "pending"
    assert "query intent output did not match schema:" in question["reason"]
    assert "question-translation_failed" in response.text
    assert "Translation failed" in response.text
    assert "query intent output did not match schema:" in response.text
    assert query_path(tmp_path).read_text(encoding="utf-8") == ""


def test_source_relation_name_is_preserved_when_alias_matches_canonical(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `role` -> `synthetic_role_canonical`\n",
        encoding="utf-8",
    )
    store.add_fact(
        "Synthetic Person",
        "synthetic_role_canonical",
        "Synthetic Reviewer",
        status="confirmed",
    )
    qid = store.add_question("Which synthetic role does the person have?")
    client = IntentOnlyClient(
        _intent("lookup_object", subject="Synthetic Person", relation="role")
    )

    translate_questions(store, client, root=tmp_path)
    report = verify(store)

    query_dl = store.questions()[0]["query_dl"]
    assert (
        f'answer_q{qid}(O) :- relation("Synthetic Person", '
        '"synthetic_role_canonical", O).'
    ) in query_dl
    assert '"role"' not in query_dl
    assert query_path(tmp_path).read_text(encoding="utf-8") == query_dl + "\n"
    assert report.engine_available is True
    assert report.answers == ["q1: Synthetic Reviewer"]


def test_planner_selects_subject_direction_from_observed_facts(tmp_path):
    store = _store(tmp_path)
    store.add_fact(
        "Synthetic Record",
        "synthetic_author",
        "Synthetic Person",
        status="confirmed",
    )
    qid = store.add_question("Which synthetic record names the person as author?")
    client = IntentOnlyClient(
        _intent(
            "lookup_subject",
            relation="synthetic_author",
            object="Synthetic Person",
        )
    )

    translate_questions(store, client, root=tmp_path)
    report = verify(store)

    query_dl = store.questions()[0]["query_dl"]
    assert (
        f'answer_q{qid}(S) :- relation(S, "synthetic_author", '
        '"Synthetic Person").'
    ) in query_dl
    assert (
        f'answer_q{qid}(O) :- relation("Synthetic Person", '
        '"synthetic_author", O).'
    ) not in query_dl
    assert report.engine_available is True
    assert report.answers == ["q1: Synthetic Record"]


def test_relation_alias_and_canonical_relation_names_are_honored(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `synthetic_raw_label` -> `synthetic_canonical_label`\n",
        encoding="utf-8",
    )
    store.add_fact(
        "Synthetic Alias Subject",
        "synthetic_canonical_label",
        "Synthetic Alias Answer",
        status="confirmed",
    )
    store.add_fact(
        "Synthetic Canonical Subject",
        "synthetic_canonical_label",
        "Synthetic Canonical Answer",
        status="confirmed",
    )
    store.add_question("Which value is recorded through the raw label?")
    store.add_question("Which value is recorded through the canonical label?")
    client = IntentOnlyClient(
        lambda question: (
            _intent(
                "lookup_object",
                subject="Synthetic Alias Subject",
                relation="synthetic_raw_label",
            )
            if "raw label" in question
            else _intent(
                "lookup_object",
                subject="Synthetic Canonical Subject",
                relation="synthetic_canonical_label",
            )
        )
    )

    results = translate_questions(store, client, root=tmp_path)
    report = verify(store)

    assert [result["status"] for result in results] == ["translated", "translated"]
    assert client.direct_datalog_calls == 0
    query_dl = query_path(tmp_path).read_text(encoding="utf-8")
    assert '"synthetic_canonical_label"' in query_dl
    assert '"synthetic_raw_label"' not in query_dl
    assert report.engine_available is True
    assert report.answers == [
        "q1: Synthetic Alias Answer",
        "q2: Synthetic Canonical Answer",
    ]


def test_unsupported_intent_is_distinct_from_translation_failed(tmp_path):
    class NoAnswerThenInvalidIntentClient:
        name = "no-answer-then-invalid-intent"

        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            from verinote.pipeline.query_intent import parse_query_intent

            if "missing subject" in question:
                return parse_query_intent(
                    _intent(
                        "unknown_or_unsupported",
                        reason="synthetic fallback coverage",
                    )
                )
            return parse_query_intent({"kind": "lookup_object"})

        def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
            raise AssertionError("ordinary translation must not call direct Datalog fallback")

    store = _store(tmp_path)
    store.add_fact("Synthetic Subject", "synthetic_relation", "Value", status="confirmed")
    store.add_question("Which value is recorded for the missing subject?")
    store.add_question("Which value has invalid model output?")

    results = translate_questions(store, NoAnswerThenInvalidIntentClient(), root=tmp_path)

    assert [result["status"] for result in results] == [
        "review_required",
        "translation_failed",
    ]
    assert results[0]["query_dl"] == 'review_required("synthetic fallback coverage")'
    assert results[0]["reason"] == "synthetic fallback coverage"
    assert results[1]["query_dl"] is None
    assert "query intent output did not match schema:" in results[1]["reason"]
    rows = store.questions()
    assert [row["status"] for row in rows] == ["review_required", "translation_failed"]
    assert all(row["status"] != "pending" for row in rows)
    assert query_path(tmp_path).read_text(encoding="utf-8") == ""


def _typed_compare_payload(
    *,
    relation: str,
    operator: str = ">=",
    value_type: str = "amount",
    value: str = 'amount(1, "credit")',
    relation_candidates: tuple[str, ...] = (),
) -> dict:
    payload = _intent("compare_typed_value", subject="Synthetic Company", relation=relation)
    payload.update(
        operator=operator,
        value_type=value_type,
        value=value,
        relation_candidates=list(relation_candidates),
    )
    return payload


def test_translate_typed_comparison_groups_alias_facts_and_keeps_raw_objects(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `gross_sales` -> `revenue`\n",
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "- revenue : amount as revenue_scalar (credit=10)\n",
        encoding="utf-8",
    )
    store.add_fact("Synthetic Company", "gross_sales", 'amount(2, "credit")', status="confirmed")
    store.add_fact("Synthetic Company", "revenue", 'amount(3, "credit")', status="confirmed")
    qid = store.add_question("Is Synthetic Company revenue at least one credit?")

    results = translate_questions(
        store,
        IntentOnlyClient(_typed_compare_payload(relation="gross_sales")),
        root=tmp_path,
    )

    assert results[0]["status"] == "translated"
    query = store.questions()[0]["query_dl"]
    assert (
        f'answer_q{qid}("amount(2, \\"credit\\")") :- '
        'relation("Synthetic Company", "gross_sales", "amount(2, \\"credit\\")").'
    ) in query
    assert (
        f'answer_q{qid}("amount(3, \\"credit\\")") :- '
        'relation("Synthetic Company", "revenue", "amount(3, \\"credit\\")").'
    ) in query
    assert "<=" not in query and ">=" not in query


def test_translate_typed_comparison_reports_distinct_canonical_candidates_as_ambiguous(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- revenue : amount as revenue_scalar (credit=10)\n"
        "- cost : amount as cost_scalar (credit=10)\n",
        encoding="utf-8",
    )
    store.add_fact("Synthetic Company", "revenue", 'amount(2, "credit")', status="confirmed")
    store.add_fact("Synthetic Company", "cost", 'amount(3, "credit")', status="confirmed")
    store.add_question("Is Synthetic Company revenue or cost at least one credit?")

    results = translate_questions(
        store,
        IntentOnlyClient(
            _typed_compare_payload(relation="revenue", relation_candidates=("cost",))
        ),
        root=tmp_path,
    )

    assert results[0]["status"] == "ambiguous"
    assert results[0]["reason"] == "multiple query candidates returned conflicting answers"


def test_translate_typed_comparison_fails_closed_for_invalid_or_truncated_exact_facts(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- metric : number as metric_scalar\n",
        encoding="utf-8",
    )
    store.add_fact("Synthetic Company", "metric", "number(not-a-number)", status="confirmed")
    store.add_question("Is Synthetic Company metric at least ten?")

    invalid = translate_questions(
        store,
        IntentOnlyClient(
            _typed_compare_payload(relation="metric", value_type="number", value="number(10)")
        ),
        root=tmp_path,
    )
    assert invalid[0]["status"] == "review_required"

    for index in range(51):
        store.add_fact("Synthetic Company", "metric", f"number({index})", status="confirmed")
    store.add_question("Is Synthetic Company metric at least ten again?")
    truncated = translate_questions(
        store,
        IntentOnlyClient(
            _typed_compare_payload(relation="metric", value_type="number", value="number(10)")
        ),
        root=tmp_path,
    )
    assert truncated[-1]["status"] == "review_required"
    assert truncated[-1]["reason"] == "too many query candidates matched the schema"


def test_translate_typed_comparison_nonmatching_valid_fact_is_no_answer(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- metric : number as metric_scalar\n",
        encoding="utf-8",
    )
    store.add_fact("Synthetic Company", "metric", "number(9)", status="confirmed")
    store.add_question("Is Synthetic Company metric above ten?")

    results = translate_questions(
        store,
        IntentOnlyClient(
            _typed_compare_payload(
                relation="metric",
                operator=">",
                value_type="number",
                value="number(10)",
            )
        ),
        root=tmp_path,
    )

    assert results[0]["status"] == "no_answer"
    assert results[0]["reason"] == "no confirmed facts match"


def test_translate_typed_comparison_malformed_requested_relation_blocks_other_candidate(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- revenue : number as revenue_scalar\n"
        "- cost : number as cost_scalar\n",
        encoding="utf-8",
    )
    store.add_fact("Synthetic Company", "revenue", "number(20)", status="confirmed")
    store.add_fact("Synthetic Company", "cost", "number(not-a-number)", status="confirmed")
    store.add_question("Is Synthetic Company revenue or cost at least ten?")

    results = translate_questions(
        store,
        IntentOnlyClient(
            _typed_compare_payload(
                relation="revenue",
                relation_candidates=("cost",),
                value_type="number",
                value="number(10)",
            )
        ),
        root=tmp_path,
    )

    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == "typed comparison has malformed or uncomparable evidence"


def test_translate_typed_comparison_type_mismatch_blocks_no_answer(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- revenue : number as revenue_scalar\n"
        "- released_on : date as release_date\n",
        encoding="utf-8",
    )
    store.add_fact("Synthetic Company", "revenue", "number(9)", status="confirmed")
    store.add_fact("Synthetic Company", "released_on", "date(2024, 7, 3)", status="confirmed")
    store.add_question("Is Synthetic Company revenue or release date above ten?")

    results = translate_questions(
        store,
        IntentOnlyClient(
            _typed_compare_payload(
                relation="revenue",
                relation_candidates=("released_on",),
                value_type="number",
                value="number(10)",
            )
        ),
        root=tmp_path,
    )

    assert results[0]["status"] == "review_required"
    assert results[0]["reason"] == "typed comparison relation has incompatible typed spec"
