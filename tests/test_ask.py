# SPDX-License-Identifier: MPL-2.0

import verinote.pipeline.ask as ask_module
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


class TwoHopIntentClient:
    name = "two-hop-intent"

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        return parse_query_intent(
            {
                "kind": "conjunctive_lookup",
                "subject": None,
                "relation": None,
                "object": None,
                "relation_candidates": None,
                "operator": None,
                "value_type": None,
                "value": None,
                "reason": None,
                "hops": [
                    {
                        "subject": {"kind": "entity", "value": "Ada"},
                        "relation": {"kind": "relation", "value": "assigned_to"},
                        "object": {"kind": "var", "value": "M"},
                    },
                    {
                        "subject": {"kind": "var", "value": "M"},
                        "relation": {"kind": "relation", "value": "purpose"},
                        "object": {"kind": "var", "value": "A"},
                    },
                ],
                "conditions": None,
                "answer_var": "A",
            }
        )

    def translate_query(self, **kwargs):
        raise AssertionError("two-hop Ask must not call direct Datalog translation")

    def answer_question(self, **kwargs):
        raise AssertionError("verified two-hop Ask must not call fallback LLM")


class ThreeHopIntentClient:
    name = "three-hop-intent"

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        return parse_query_intent(
            {
                "kind": "conjunctive_three_hop_lookup",
                "subject": None,
                "relation": None,
                "object": None,
                "relation_candidates": None,
                "operator": None,
                "value_type": None,
                "value": None,
                "reason": None,
                "hops": None,
                "conditions": None,
                "chain_hops": [
                    {
                        "subject": {"kind": "entity", "value": "Example Org"},
                        "relation": {"kind": "relation", "value": "owns"},
                        "object": {"kind": "var", "value": "M"},
                    },
                    {
                        "subject": {"kind": "var", "value": "M"},
                        "relation": {"kind": "relation", "value": "runs"},
                        "object": {"kind": "var", "value": "N"},
                    },
                    {
                        "subject": {"kind": "var", "value": "N"},
                        "relation": {"kind": "relation", "value": "purpose"},
                        "object": {"kind": "var", "value": "A"},
                    },
                ],
                "answer_var": "A",
            }
        )

    def translate_query(self, **kwargs):
        raise AssertionError("three-hop Ask must not call direct Datalog translation")

    def answer_question(self, **kwargs):
        raise AssertionError("verified three-hop Ask must not call fallback LLM")


class FallbackThreeHopIntentClient(ThreeHopIntentClient):
    def answer_question(self, **kwargs):
        return "UNVERIFIED synthetic fallback"


class ConjunctiveFilterIntentClient:
    name = "conjunctive-filter-intent"

    def __init__(
        self,
        *,
        role_relation: str = "role",
        affiliation_relation: str = "affiliation",
        conditions: tuple[dict[str, object], dict[str, object]] | None = None,
    ):
        self.role_relation = role_relation
        self.affiliation_relation = affiliation_relation
        self.conditions = conditions

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        return parse_query_intent(
            {
                "kind": "conjunctive_filter",
                "subject": None,
                "relation": None,
                "object": None,
                "relation_candidates": None,
                "operator": None,
                "value_type": None,
                "value": None,
                "reason": None,
                "hops": None,
                "conditions": list(
                    self.conditions
                    or (
                        {
                            "subject": {"kind": "var", "value": "A"},
                            "relation": {"kind": "relation", "value": self.role_relation},
                            "object": {"kind": "entity", "value": "Engineer"},
                        },
                        {
                            "subject": {"kind": "entity", "value": "Research Team"},
                            "relation": {
                                "kind": "relation",
                                "value": self.affiliation_relation,
                            },
                            "object": {"kind": "var", "value": "A"},
                        },
                    )
                ),
                "answer_var": "A",
            }
        )

    def translate_query(self, **kwargs):
        raise AssertionError("conjunctive-filter Ask must not call direct Datalog translation")

    def answer_question(self, **kwargs):
        raise AssertionError("verified conjunctive-filter Ask must not call fallback LLM")


class FallbackConjunctiveFilterIntentClient(ConjunctiveFilterIntentClient):
    def answer_question(self, **kwargs):
        return "UNVERIFIED synthetic fallback"


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


def test_ask_returns_verified_two_hop_answer_with_both_sources(tmp_path):
    store = _store(tmp_path)
    first_source = store.add_source("sources/assignment.txt")
    second_source = store.add_source("sources/purpose.txt")
    store.add_fact("Ada", "assigned_to", "Project", status="confirmed", source_id=first_source)
    store.add_fact("Project", "purpose", "Research", status="confirmed", source_id=second_source)

    result = ask_question(
        store, TwoHopIntentClient(), root=tmp_path, question="Resolve the linked outcome for this case."
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "Research" in result.answer
    assert {fact.source for fact in result.grounding_facts} == {
        "sources/assignment.txt",
        "sources/purpose.txt",
    }


def test_ask_returns_verified_three_hop_answer_with_all_sources(tmp_path):
    store = _store(tmp_path)
    first_source = store.add_source("sources/ownership.txt")
    second_source = store.add_source("sources/operations.txt")
    third_source = store.add_source("sources/purpose.txt")
    store.add_fact("Example Org", "owns", "Program", status="confirmed", source_id=first_source)
    store.add_fact("Program", "runs", "Project", status="confirmed", source_id=second_source)
    store.add_fact("Project", "purpose", "Research", status="confirmed", source_id=third_source)

    result = ask_question(
        store, ThreeHopIntentClient(), root=tmp_path, question="Resolve the synthetic three-hop outcome."
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert result.engine_answers == ("q0: Research",)
    assert {
        (fact.subject, fact.relation, fact.object, fact.source)
        for fact in result.grounding_facts
    } == {
        ("Example Org", "owns", "Program", "sources/ownership.txt"),
        ("Program", "runs", "Project", "sources/operations.txt"),
        ("Project", "purpose", "Research", "sources/purpose.txt"),
    }


def test_ask_fails_closed_when_three_hop_trace_is_unavailable(tmp_path, monkeypatch):
    store = _store(tmp_path)
    first_source = store.add_source("sources/ownership.txt")
    second_source = store.add_source("sources/operations.txt")
    third_source = store.add_source("sources/purpose.txt")
    store.add_fact("Example Org", "owns", "Program", status="confirmed", source_id=first_source)
    store.add_fact("Program", "runs", "Project", status="confirmed", source_id=second_source)
    store.add_fact("Project", "purpose", "Research", status="confirmed", source_id=third_source)
    monkeypatch.setattr(ask_module, "trace_query_answers", lambda *_args, **_kwargs: ())

    result = ask_question(
        store,
        FallbackThreeHopIntentClient(),
        root=tmp_path,
        question="Resolve the synthetic three-hop outcome without a trace.",
    )

    assert result.route == "fallback"
    assert result.label != "VERIFIED — engine"
    assert result.engine_answers == ()
    assert result.grounding_facts == ()
    assert result.reason == "three-hop query source trace is incomplete"


def test_ask_three_hop_trace_uses_the_evaluated_fact_snapshot(tmp_path, monkeypatch):
    store = _store(tmp_path)
    first_source = store.add_source("sources/ownership.txt")
    second_source = store.add_source("sources/operations.txt")
    third_source = store.add_source("sources/purpose.txt")
    injected_source = store.add_source("sources/injected.txt")
    store.add_fact("Example Org", "owns", "Program", status="confirmed", source_id=first_source)
    store.add_fact("Program", "runs", "Project", status="confirmed", source_id=second_source)
    store.add_fact("Project", "purpose", "Research", status="confirmed", source_id=third_source)

    original_trace = ask_module.trace_query_answers

    def inject_fact_before_trace(*args, **kwargs):
        store.add_fact(
            "Project",
            "purpose",
            "Research",
            status="confirmed",
            source_id=injected_source,
        )
        return original_trace(*args, **kwargs)

    monkeypatch.setattr(ask_module, "trace_query_answers", inject_fact_before_trace)

    result = ask_question(
        store,
        ThreeHopIntentClient(),
        root=tmp_path,
        question="Resolve the synthetic three-hop outcome from one execution snapshot.",
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert {fact.source for fact in result.grounding_facts} == {
        "sources/ownership.txt",
        "sources/operations.txt",
        "sources/purpose.txt",
    }
    assert all(fact.source != "sources/injected.txt" for fact in result.grounding_facts)


def test_ask_returns_only_shared_conjunctive_filter_answer_with_both_sources(tmp_path):
    store = _store(tmp_path)
    role_source = store.add_source("sources/roles.txt")
    affiliation_source = store.add_source("sources/affiliations.txt")
    store.add_fact("Ada", "role", "Engineer", status="confirmed", source_id=role_source)
    store.add_fact(
        "Research Team", "affiliation", "Ada", status="confirmed", source_id=affiliation_source
    )
    store.add_fact("Bryn", "role", "Engineer", status="confirmed", source_id=role_source)
    store.add_fact(
        "Research Team", "affiliation", "Cato", status="confirmed", source_id=affiliation_source
    )

    result = ask_question(
        store,
        ConjunctiveFilterIntentClient(),
        root=tmp_path,
        question="Which synthetic person has both requested conditions?",
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "Ada" in result.answer
    assert "Bryn" not in result.answer
    assert "Cato" not in result.answer
    assert {
        (fact.subject, fact.relation, fact.object, fact.source)
        for fact in result.grounding_facts
    } == {
        ("Ada", "role", "Engineer", "sources/roles.txt"),
        ("Research Team", "affiliation", "Ada", "sources/affiliations.txt"),
    }
    assert {fact.source for fact in result.grounding_facts} == {
        "sources/roles.txt",
        "sources/affiliations.txt",
    }


def test_ask_conjunctive_filter_matches_alias_requested_relations_with_both_sources(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `position` -> `role`\n- `member_of` -> `affiliation`\n",
        encoding="utf-8",
    )
    role_source = store.add_source("sources/roles.txt")
    affiliation_source = store.add_source("sources/affiliations.txt")
    store.add_fact("Ada", "role", "Engineer", status="confirmed", source_id=role_source)
    store.add_fact(
        "Research Team", "affiliation", "Ada", status="confirmed", source_id=affiliation_source
    )
    store.add_fact("Bryn", "role", "Engineer", status="confirmed", source_id=role_source)
    store.add_fact(
        "Research Team", "affiliation", "Cato", status="confirmed", source_id=affiliation_source
    )

    result = ask_question(
        store,
        ConjunctiveFilterIntentClient(
            role_relation="position", affiliation_relation="member_of"
        ),
        root=tmp_path,
        question="Which synthetic person has both aliased conditions?",
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert result.engine_answers == ("q0: Ada",)
    assert {
        (fact.subject, fact.relation, fact.object, fact.source)
        for fact in result.grounding_facts
    } == {
        ("Ada", "role", "Engineer", "sources/roles.txt"),
        ("Research Team", "affiliation", "Ada", "sources/affiliations.txt"),
    }
    assert {fact.source for fact in result.grounding_facts} == {
        "sources/roles.txt",
        "sources/affiliations.txt",
    }


def test_ask_conjunctive_filter_rejects_alias_equivalent_conditions(tmp_path):
    store = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `position` -> `role`\n", encoding="utf-8"
    )
    source = store.add_source("sources/positions.txt")
    store.add_fact("Ada", "position", "Engineer", status="confirmed", source_id=source)

    result = ask_question(
        store,
        FallbackConjunctiveFilterIntentClient(
            conditions=(
                {
                    "subject": {"kind": "var", "value": "A"},
                    "relation": {"kind": "relation", "value": "position"},
                    "object": {"kind": "entity", "value": "Engineer"},
                },
                {
                    "subject": {"kind": "var", "value": "A"},
                    "relation": {"kind": "relation", "value": "role"},
                    "object": {"kind": "entity", "value": "Engineer"},
                },
            )
        ),
        root=tmp_path,
        question="Which synthetic person matches both aliased conditions?",
    )

    assert result.label != "VERIFIED — engine"
    assert result.engine_answers == ()
    assert result.grounding_facts == ()


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


def test_ask_answers_a_who_question_from_the_engine_without_a_provider_call(tmp_path):
    """`X의 <relation>는 누구인가?` is answerable from the KB, and for free.

    The relation is stored under the exact label the question names, so nothing
    here needs a model: the deterministic reading alone should reach the engine.
    `DeterministicOnlyClient` raises on every provider entry point, so this fails
    if the interrogative leaks into the relation candidate and the planner falls
    through to the UNVERIFIED source-exploration route.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-project.txt")
    store.add_fact(
        "샘플프로젝트", "담당자", "샘플인물", status="confirmed", source_id=source_id
    )

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플프로젝트의 담당자는 누구인가?",
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "샘플인물" in result.answer
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
