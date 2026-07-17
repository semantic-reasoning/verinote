# SPDX-License-Identifier: MPL-2.0

from verinote.llm.base import LLMError
from verinote.pipeline.ask import ask_question, search_source_excerpts
from verinote.pipeline.query import query_path
from verinote.store import Store


class DeterministicOnlyClient:
    name = "deterministic-only"

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        raise AssertionError("deterministic Ask path must not call intent LLM")

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        raise AssertionError("Ask must not call persistent direct Datalog translation")

    def answer_question(self, *, question: str, context: str) -> str:
        raise AssertionError("verified engine Ask path must not call fallback LLM")


class FallbackClient:
    name = "fallback"

    def __init__(self, *, answer: str = "UNVERIFIED synthetic answer", error=None):
        self.answer = answer
        self.error = error
        self.context = ""

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        return parse_query_intent(
            {
                "kind": "unknown_or_unsupported",
                "subject": None,
                "relation": None,
                "object": None,
                "relation_candidates": None,
                "operator": None,
                "value_type": None,
                "value": None,
                "reason": "unsupported synthetic question",
            }
        )

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        raise AssertionError("Ask fallback must not persist direct Datalog")

    def answer_question(self, *, question: str, context: str) -> str:
        self.context = context
        if self.error is not None:
            raise self.error
        return self.answer


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def test_ask_returns_verified_engine_answer_without_persisting(tmp_path):
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플인물", "역할", "검토자", status="confirmed", source_id=source_id)

    result = ask_question(
        store, DeterministicOnlyClient(), root=tmp_path, question="샘플인물의 역할은 무엇인가?"
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "검토자" in result.answer
    assert result.grounding_facts
    assert result.grounding_facts[0].answer == "검토자"
    assert result.grounding_facts[0].source == "sources/sample.txt"
    assert store.questions() == []
    assert not query_path(tmp_path).exists()


def test_ask_engine_answer_restates_triple_with_inline_source(tmp_path):
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플인물", "역할", "샘플역할", status="confirmed", source_id=source_id)

    result = ask_question(
        store, DeterministicOnlyClient(), root=tmp_path, question="샘플인물의 역할은 무엇인가?"
    )

    assert result.route == "engine"
    # factlog-style: the answer restates the verified triple, not a bare object,
    # and never leaks the internal q<id>: /report prefix.
    assert result.answer == "샘플인물, 역할, 샘플역할\n    ← sources/sample.txt"
    assert "q0:" not in result.answer


def test_ask_answers_generic_korean_attribute_question_from_engine(tmp_path):
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-project.txt")
    store.add_fact(
        "샘플프로젝트",
        "purpose",
        "샘플목표",
        status="confirmed",
        source_id=source_id,
    )

    result = ask_question(
        store, DeterministicOnlyClient(), root=tmp_path, question="샘플프로젝트의 목적은?"
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "샘플목표" in result.answer
    assert result.grounding_facts[0].source == "sources/sample-project.txt"


def test_ask_does_not_call_stale_fact_terms_verified(tmp_path):
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-project.txt")
    fid = store.add_fact(
        "샘플프로젝트",
        "purpose",
        "샘플목표",
        status="confirmed",
        source_id=source_id,
    )
    store._conn.execute(
        "UPDATE facts SET object = ?, term_token = ? WHERE id = ?",
        ("표시목표", "0" * 64, fid),
    )

    result = ask_question(
        store,
        FallbackClient(answer="UNVERIFIED fallback"),
        root=tmp_path,
        question="샘플프로젝트의 목적은?",
    )

    assert result.label != "VERIFIED — engine"
    assert result.route == "fallback"
    assert "stale DuckDB fact terms" in result.reason


def test_ask_verified_negative_only_for_explicit_no_answer_flow(tmp_path, monkeypatch):
    import verinote.pipeline.ask as ask_module

    store = _store(tmp_path)
    store.add_fact("샘플인물", "is_a", "person", status="confirmed")
    store.add_fact("샘플인물", "역할", "후보역할", status="candidate")
    monkeypatch.setattr(
        ask_module,
        "schema_aware_query_flow",
        lambda *args, **kwargs: (
            "no_answer",
            'no_answer("no confirmed facts match")',
            "no confirmed facts match",
        ),
    )

    result = ask_question(
        store, DeterministicOnlyClient(), root=tmp_path, question="샘플인물의 역할은 무엇인가?"
    )

    assert result.route == "engine"
    assert result.status == "no_answer"
    assert result.answer == "No confirmed facts match."
    assert "후보역할" not in result.answer


def test_ask_does_not_verify_negative_when_relation_candidate_is_missing(tmp_path):
    store = _store(tmp_path)
    store.add_fact("샘플조직", "is_a", "조직", status="confirmed")
    client = FallbackClient(answer="출처 탐색 결과를 확인해야 합니다.")

    result = ask_question(store, client, root=tmp_path, question="샘플조직의 임직원 수는?")

    assert result.route == "fallback"
    assert result.label == "UNVERIFIED — source exploration"
    assert result.status == "fallback"
    assert "No confirmed facts match." not in result.answer


def test_ask_fallback_uses_source_excerpts_and_grounding(tmp_path):
    source = tmp_path / "sources" / "sample.txt"
    source.parent.mkdir()
    source.write_text("샘플조직은 샘플서비스를 제공한다.", encoding="utf-8")
    store = _store(tmp_path)
    sid = store.add_source("sources/sample.txt")
    store.add_fact("샘플조직", "is_a", "조직", status="confirmed", source_id=sid)
    client = FallbackClient(answer="샘플조직은 샘플서비스를 제공한다고 볼 수 있습니다.")

    result = ask_question(store, client, root=tmp_path, question="샘플조직 설명해줘")

    assert result.route == "fallback"
    assert result.label == "UNVERIFIED — source exploration"
    assert "샘플서비스" in result.answer
    assert "sources/sample.txt" in client.context
    assert "샘플조직 | is_a | 조직" in client.context
    assert result.excerpts


def test_ask_fallback_survives_llm_answer_error(tmp_path):
    store = _store(tmp_path)
    client = FallbackClient(error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert result.route == "fallback"
    assert result.warning == "synthetic outage"
    assert "deterministic engine could not answer" in result.answer


def test_search_source_excerpts_reads_latest_text_artifact(tmp_path):
    store = _store(tmp_path)
    sid = store.add_source("sources/sample.pdf", kind="binary")
    artifact_path = tmp_path / "artifacts" / "sources" / str(sid) / "text.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("샘플문서는 샘플항목을 포함한다.", encoding="utf-8")
    store.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path=f"artifacts/sources/{sid}/text.txt",
        checksum="sha",
    )

    excerpts = search_source_excerpts(store, root=tmp_path, question="샘플항목")

    assert [item.path for item in excerpts] == [f"artifacts/sources/{sid}/text.txt"]


def test_ask_grounding_table_shows_a_comma_answer_in_its_source_form(tmp_path):
    """Ask's Answer cell is one value, not a comma-delimited list.

    `/report` joins a question's answers with `, `, so the answer renderer
    escapes a value's own surface comma as `\\,` (issue #167) -- otherwise one
    answer `검토자, 팀장` reads as two. Ask reuses `trace_query_answers()` for
    grounding (`AskGroundingFact.answer`, rendered as a single table cell in
    `web/templates/ask.html`), and there is no join to defend against: the cell
    holds exactly one value. Carrying the report's escape into it puts a
    backslash on screen that is in neither the source text nor the `object`
    column beside it, so the same fact contradicts itself across one row.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플인물", "역할", "검토자, 팀장", status="confirmed", source_id=source_id)

    result = ask_question(
        store, DeterministicOnlyClient(), root=tmp_path, question="샘플인물의 역할은 무엇인가?"
    )

    assert result.route == "engine"
    fact = result.grounding_facts[0]
    # The Answer cell and the Object cell beside it are the same value, and it
    # is the source's value: no report-join escape reaches this screen.
    assert fact.answer == "검토자, 팀장"
    assert fact.object == "검토자, 팀장"
