# SPDX-License-Identifier: MPL-2.0

import pytest

import verinote.pipeline.ask as ask_module
from verinote.llm.base import LLMError
from verinote.pipeline.ask import ask_question, search_source_excerpts
from verinote.pipeline.query import _QueryFlowResult, query_path
from verinote.store import Store
from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError


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


class KoreanChainIntentClient:
    """Reads `X의 <relation>의 <relation>` as the two-hop lookup it is."""

    name = "korean-chain-intent"

    def __init__(self):
        self.intent_calls = 0
        self.schema_hints: list[str] = []

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        self.intent_calls += 1
        self.schema_hints.append(schema_hint)
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
                        "subject": {"kind": "entity", "value": "샘플프로젝트"},
                        "relation": {"kind": "relation", "value": "담당자"},
                        "object": {"kind": "var", "value": "M"},
                    },
                    {
                        "subject": {"kind": "var", "value": "M"},
                        "relation": {"kind": "relation", "value": "상사"},
                        "object": {"kind": "var", "value": "A"},
                    },
                ],
                "conditions": None,
                "answer_var": "A",
            }
        )

    def translate_query(self, **kwargs):
        raise AssertionError("Ask must not call direct Datalog translation")

    def answer_question(self, **kwargs):
        raise AssertionError("a verified chain answer must not call the fallback LLM")


def _chain_store(tmp_path) -> Store:
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-project.txt")
    store.add_fact("샘플프로젝트", "담당자", "샘플인물", status="confirmed", source_id=source_id)
    store.add_fact("샘플인물", "상사", "샘플상급자", status="confirmed", source_id=source_id)
    return store


@pytest.mark.parametrize(
    "question",
    ["샘플프로젝트의 담당자의 상사는 누구인가?", "샘플프로젝트의 담당자의상사는 누구인가?"],
)
def test_ask_answers_a_korean_chain_question_by_reinterpreting_an_empty_plan(
    tmp_path, question
):
    """A chained question must reach the model that can read it as two hops.

    The deterministic parser is schema-blind: it claims `X의 <label>?` and hands
    the whole chain over as one relation name. Nothing in the question says
    whether `담당자의 상사` is a relation the KB holds or a path through two --
    only the schema does, and it answers by planning no candidates at all. That
    empty plan is the signal to re-read the question.

    The second case has no space inside the chain, which is part of why this
    cannot be solved by looking for a particle in the label. The markers issue
    #432 proposes -- `의`/`와`/`과`/`중` -- fail in both directions. Bound to a
    word and followed by a space they decline ordinary multi-token relations
    (`회의 일정` and `협의 결과` on `의`, `성과 지표` on `과`) and still miss
    this chain. Narrowed to `의` and allowed to match without a space they do
    catch it, but then decline ordinary nouns that merely contain the syllable
    (`회의실`, `주의사항`, `편의점`). Which of the two any label is depends on
    the schema, and the parser cannot see it.
    """
    store = _chain_store(tmp_path)
    client = KoreanChainIntentClient()

    result = ask_question(store, client, root=tmp_path, question=question)

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "샘플상급자" in result.answer
    assert client.intent_calls == 1
    assert {fact.relation for fact in result.grounding_facts} == {"담당자", "상사"}


class EnglishChainIntentClient(KoreanChainIntentClient):
    """The same two-hop reading, for the English chain shapes."""

    name = "english-chain-intent"

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        self.intent_calls += 1
        self.schema_hints.append(schema_hint)
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
                        "subject": {"kind": "entity", "value": "Sample Project"},
                        "relation": {"kind": "relation", "value": "owner"},
                        "object": {"kind": "var", "value": "M"},
                    },
                    {
                        "subject": {"kind": "var", "value": "M"},
                        "relation": {"kind": "relation", "value": "manager"},
                        "object": {"kind": "var", "value": "A"},
                    },
                ],
                "conditions": None,
                "answer_var": "A",
            }
        )


@pytest.mark.parametrize(
    "question",
    [
        "What is the manager of the owner of Sample Project?",
        "What is Sample Project's owner's manager?",
    ],
)
def test_ask_answers_an_english_chain_question_by_reinterpreting_an_empty_plan(
    tmp_path, question
):
    """English chains reach the re-reading too, by two different routes.

    They are worth pinning together because they fail differently. The `of` form
    flattens the chain into the *label* (`manager of the owner`), the same defect
    as the Korean case. The possessive form does not: the regex takes
    `Sample Project's owner` as the **entity** and leaves a clean `manager`
    label, so it plans nothing because no such subject exists. One empty plan,
    two causes -- which is the argument for guarding at the plan rather than at
    any one parse rule.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-project.txt")
    store.add_fact(
        "Sample Project", "owner", "Sample Person", status="confirmed", source_id=source_id
    )
    store.add_fact(
        "Sample Person", "manager", "Sample Lead", status="confirmed", source_id=source_id
    )
    client = EnglishChainIntentClient()

    result = ask_question(store, client, root=tmp_path, question=question)

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "Sample Lead" in result.answer
    assert client.intent_calls == 1
    assert {fact.relation for fact in result.grounding_facts} == {"owner", "manager"}


def test_ask_gives_the_reinterpretation_the_observed_schema(tmp_path):
    """The re-reading is only worth making if the model can see the schema.

    Mapping the question's words onto relation labels the KB actually uses is
    the whole reason to ask a model at all -- without the hint it is guessing at
    exactly what the schema-blind parser already guessed at. Every other stub
    client in this file ignores the parameter, so nothing else would notice it
    going blank.
    """
    store = _chain_store(tmp_path)
    client = KoreanChainIntentClient()

    ask_question(
        store, client, root=tmp_path, question="샘플프로젝트의 담당자의 상사는 누구인가?"
    )

    assert len(client.schema_hints) == 1
    hint = client.schema_hints[0]
    assert "담당자" in hint
    assert "상사" in hint


@pytest.mark.parametrize("relation", ["회의 일정", "성과 지표"])
def test_ask_answers_a_multi_token_relation_without_a_provider_call(tmp_path, relation):
    """A label that names a real relation must never reach the re-reading.

    These are the labels that killed the parse-time approach: a rule looking for
    one of issue #432's markers (`의`/`와`/`과`/`중`) bound inside the label
    declines `회의 일정` on its `의` and `성과 지표` on its `과`, along with the
    chains. At the empty-plan boundary they are safe for free, because a
    label this subject really holds plans VALID and is never reconsidered --
    `DeterministicOnlyClient` raises on every provider entry point, so it is not
    consulted rather than merely not counted.

    The subject matters: a relation the KB holds for some *other* subject still
    plans nothing here and is re-read like any other empty plan. That is a cost,
    not a correctness problem, and it is what makes issue #434 worth doing.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-project.txt")
    store.add_fact("샘플프로젝트", relation, "샘플값", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question=f"샘플프로젝트의 {relation}은?",
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert "샘플값" in result.answer


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
        "_schema_aware_query_flow_result",
        lambda *args, **kwargs: _QueryFlowResult(
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


# --- the measure-unit caveat (#445) ----------------------------------------

_UNIT_WARNING = (
    "the question's counter is 개월; the verified value states 년. verinote "
    "shows stored values as recorded and applies no unit conversion"
)


class TwoHopMeasureIntentClient:
    """Reads `X의 <label>의 <label>은 몇 <counter>인가?` as two hops.

    The deterministic parser flattens the chain into one relation name, plans no
    candidates, and the empty plan is re-read by the model -- the same route
    `test_ask_answers_a_korean_chain_question_by_reinterpreting_an_empty_plan`
    pins, here with a measure tail on the end.
    """

    name = "two-hop-measure-intent"

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
                        "subject": {"kind": "entity", "value": "샘플사업"},
                        "relation": {"kind": "relation", "value": "하위단계"},
                        "object": {"kind": "var", "value": "M"},
                    },
                    {
                        "subject": {"kind": "var", "value": "M"},
                        "relation": {"kind": "relation", "value": "기간"},
                        "object": {"kind": "var", "value": "A"},
                    },
                ],
                "conditions": None,
                "answer_var": "A",
            }
        )

    def translate_query(self, **kwargs):
        raise AssertionError("Ask must not call direct Datalog translation")

    def answer_question(self, **kwargs):
        raise AssertionError("a verified chain answer must not call the fallback LLM")


def test_ask_warns_beside_a_verified_answer_stated_in_another_unit(tmp_path):
    """#445: the answer stands, and a caveat is shown beside it.

    `샘플사업의 기간은 몇 개월인가?` against a KB holding `(샘플사업, 기간, 2년)`
    has been answered `VERIFIED — engine` with `2년` since #442, which moved the
    whole measure-question family onto the engine. None of that changes here: the
    route, label, answer and reason are exactly what they were, and only the
    caveat is new. The assertion is on the whole sentence, because a caveat that
    named `MONTH` instead of `개월` would be worse than none.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    store.add_fact("샘플사업", "기간", "2년", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플사업의 기간은 몇 개월인가?",
    )

    assert result.route == "engine"
    assert result.label == "VERIFIED — engine"
    assert result.status == "translated"
    assert result.reason == "deterministic query matched confirmed/accepted facts"
    assert result.answer == "샘플사업, 기간, 2년\n    ← sources/sample-plan.txt"
    assert result.warning == _UNIT_WARNING
    # ask.html renders this slot as text, so the sentence carries no markup.
    assert "`" not in result.warning
    assert "*" not in result.warning


def test_ask_does_not_warn_when_the_verified_value_is_in_the_asked_unit(tmp_path):
    """The same question and the same shape of fact, stated in months."""
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    store.add_fact("샘플사업", "기간", "24개월", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플사업의 기간은 몇 개월인가?",
    )

    assert result.label == "VERIFIED — engine"
    assert result.warning is None


def test_ask_warns_on_the_answering_fact_of_a_two_hop_proof(tmp_path):
    """A two-hop proof lists an intermediate fact whose object is not the answer.

    Here the first fact's object is `샘플단계` and the second's is `2년`. Reading
    the caveat off the first fact in the trace would find no unit in `샘플단계`
    and go silent, so this pins the `_fold(object) == _fold(answer)` filter.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    store.add_fact("샘플사업", "하위단계", "샘플단계", status="confirmed", source_id=source_id)
    store.add_fact("샘플단계", "기간", "2년", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        TwoHopMeasureIntentClient(),
        root=tmp_path,
        question="샘플사업의 하위단계의 기간은 몇 개월인가?",
    )

    assert result.label == "VERIFIED — engine"
    assert [fact.object for fact in result.grounding_facts] == ["샘플단계", "2년"]
    assert result.warning == _UNIT_WARNING


def test_ask_keeps_the_trace_warning_for_a_multi_valued_answer(tmp_path):
    """Two facts on one relation are answered, and produce no source trace.

    That is not an unreachable state -- the question is answered and verified --
    so the caveat slot is already taken by the standing "no source trace" one,
    which wins because the unit caveat needs the trace to find the answering
    fact. This pins that the new branch did not displace it.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    store.add_fact("샘플사업", "기간", "2년", status="confirmed", source_id=source_id)
    store.add_fact("샘플사업", "기간", "18개월", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플사업의 기간은 몇 개월인가?",
    )

    assert result.label == "VERIFIED — engine"
    assert result.grounding_facts == ()
    assert result.warning == "source trace unavailable for this verified query shape"


def test_ask_warns_on_a_value_the_store_holds_in_decomposed_form(tmp_path):
    """The two sides of the answering-fact test arrive by different routes.

    `Store.add_fact` keeps the `object` column exactly as written, so a
    decomposed `2년` stays four code points there, while the trace composes the
    answer it renders through NFC. Without `_fold` on both sides no fact matches
    and this caveat silently never fires.
    """
    import unicodedata

    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    decomposed = unicodedata.normalize("NFD", "2년")
    store.add_fact("샘플사업", "기간", decomposed, status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플사업의 기간은 몇 개월인가?",
    )

    fact = result.grounding_facts[0]
    assert len(fact.object) == 4
    assert len(fact.answer) == 2
    assert fact.object != fact.answer
    assert result.warning == _UNIT_WARNING


def test_ask_is_silent_when_the_rendered_answer_escapes_the_stored_value(tmp_path):
    """Folding normalises; it does not un-escape, and the caveat fails silent.

    A stored `2년<TAB>` reaches the trace as object `'2년\\t'` and answer
    `'2년\\\\t'`, and NFC-plus-casefold does not reconcile those. No fact passes
    the answering-fact test, so no caveat is shown -- silence beside a correct
    answer, never a sentence about the wrong fact.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    store.add_fact("샘플사업", "기간", "2년\t", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플사업의 기간은 몇 개월인가?",
    )

    fact = result.grounding_facts[0]
    assert (fact.object, fact.answer) == ("2년\t", "2년\\t")
    assert result.label == "VERIFIED — engine"
    assert result.warning is None


def test_ask_does_not_warn_for_a_relation_literally_named_with_a_counter(tmp_path):
    """A KB may hold a relation named `몇 년`, and this question asks for it."""
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    store.add_fact("샘플사업", "몇 년", "24개월", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플사업의 몇 년인가?",
    )

    assert result.label == "VERIFIED — engine"
    assert result.answer == "샘플사업, 몇 년, 24개월\n    ← sources/sample-plan.txt"
    assert result.warning is None


def test_ask_does_not_warn_for_a_question_that_asked_in_no_unit(tmp_path):
    """An ordinary attribute question is untouched, whatever its value states."""
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플인물", "역할", "2년", status="confirmed", source_id=source_id)

    result = ask_question(
        store, DeterministicOnlyClient(), root=tmp_path, question="샘플인물의 역할은 무엇인가?"
    )

    assert result.label == "VERIFIED — engine"
    assert result.warning is None


def test_ask_warns_on_a_value_whose_case_differs_from_the_rendered_answer(tmp_path):
    """`_fold` is load-bearing on the ANSWER side too, not only the object side.

    The decomposed-value test above pins only half of it. There the object is
    NFD and the answer NFC, so folding the object alone is enough to make them
    meet -- drop the fold from the answer side and that test still passes.

    A mixed-case Latin value separates them: `3 Weeks` needs casefolding on
    whichever side is left unfolded, so this fails if either call goes. The
    caveat reports the folded spelling, which is why it says `weeks`.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample-plan.txt")
    store.add_fact("샘플사업", "기간", "3 Weeks", status="confirmed", source_id=source_id)

    result = ask_question(
        store,
        DeterministicOnlyClient(),
        root=tmp_path,
        question="샘플사업의 기간은 몇 개월인가?",
    )

    fact = result.grounding_facts[0]
    assert (fact.object, fact.answer) == ("3 Weeks", "3 Weeks")
    assert result.label == "VERIFIED — engine"
    assert result.warning == (
        "the question's counter is 개월; the verified value states weeks. "
        "verinote shows stored values as recorded and applies no unit conversion"
    )


# --- the fallback body names only sections the page renders (#438) ----------


def test_ask_fallback_body_promises_nothing_when_there_is_no_evidence(tmp_path):
    """With neither excerpt nor grounding fact, the body says so.

    `ask.html` gates the `Source excerpts` section on `result.excerpts`, so the
    old unconditional "Source excerpts are shown below." pointed at a section
    that was not on the page.

    The assertion is on the whole sentence rather than on the absence of the
    old one. `"Source excerpts" not in result.answer` would hold here for the
    right reason, but it would hold just as well on a regression that called
    the model and got prose back -- a check that cannot tell the fix from the
    bug. Naming the expected sentence can.
    """
    store = _store(tmp_path)
    client = FallbackClient(error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert result.route == "fallback"
    assert result.excerpts == ()
    assert result.grounding_facts == ()
    assert result.answer == (
        "The deterministic engine could not answer, and no source excerpt or "
        "verified grounding fact is shown below."
    )


def test_ask_fallback_body_names_the_grounding_table_when_no_excerpt_renders(tmp_path):
    """Grounding facts and no excerpt -- the row that was false before #438.

    The source row is registered but its file is absent from disk, so
    `search_source_excerpts` skips it and the page renders the
    `Verified grounding facts` table with no excerpts section beneath. The body
    must name the table that is there rather than the section that is not.

    A failed schema-aware re-read (#438) makes the fallback *route* common; it
    does not make this shape common -- whether an excerpt renders turns on the
    sources, independently of why the route was taken. What the two together
    mean is that the route reaches this shape more often, not that the shape
    follows from the failure.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플조직", "is_a", "조직", status="confirmed", source_id=source_id)
    client = FallbackClient(error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="샘플조직 설명해줘")

    assert result.route == "fallback"
    assert result.excerpts == ()
    assert result.grounding_facts
    assert result.answer == (
        "The deterministic engine could not answer. Verified grounding facts "
        "are shown below."
    )


def test_ask_fallback_body_still_names_the_excerpts_when_both_render(tmp_path):
    """Both collections populated: the sentence this route has always shown.

    Naming one present section is enough, and excerpts win. Swapping the
    helper's branch order would make this row name the grounding table while
    the excerpts section renders below it -- true of the page, but a needless
    change to text that was never false.

    **An unchanged-row pin: this passes on the parent commit by design.** The
    old sentence was already true wherever an excerpt renders, so this row is
    here to fail a fix that rewrites text it had no reason to touch, not to
    demonstrate one. Measured: swapping the helper's branch order fails this
    test and nothing else in the suite -- the render tripwire in
    `tests/test_ask_verdict.py` accepts either present section by design.
    """
    source = tmp_path / "sources" / "sample.txt"
    source.parent.mkdir()
    source.write_text("샘플조직은 샘플서비스를 제공한다.", encoding="utf-8")
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플조직", "is_a", "조직", status="confirmed", source_id=source_id)
    client = FallbackClient(error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="샘플조직 설명해줘")

    assert result.route == "fallback"
    assert result.excerpts
    assert result.grounding_facts
    assert result.answer == (
        "The deterministic engine could not answer. Source excerpts are shown below."
    )


def test_ask_fallback_body_names_the_grounding_table_when_the_model_returns_nothing(
    tmp_path,
):
    """The empty-answer guard is a second writer of this sentence.

    `_fallback_answer` substitutes the body twice -- once when
    `answer_question` raises and once when it returns an empty string. Reverting
    either call site alone reintroduces the false promise on that site's own
    population, and only this test covers the empty-string site: here the
    provider was consulted and answered with nothing, so `warning` stays unset.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플조직", "is_a", "조직", status="confirmed", source_id=source_id)
    client = FallbackClient(answer="")

    result = ask_question(store, client, root=tmp_path, question="샘플조직 설명해줘")

    assert result.route == "fallback"
    assert result.warning is None
    assert result.excerpts == ()
    assert result.grounding_facts
    assert result.answer == (
        "The deterministic engine could not answer. Verified grounding facts "
        "are shown below."
    )


# The docstring on `_fallback_answer_body` argues from what `ask.html` renders and
# from what `search_source_excerpts` declines to read. The rendering half is
# re-derived in tests/test_ask_verdict.py, which already renders `ask.html`; this
# module keeps the half that needs no template.


def test_search_source_excerpts_compares_nothing_from_a_missing_or_undecodable_source(
    tmp_path, monkeypatch
):
    """The absent excerpt in the body's last branch is not a failed comparison.

    `search_source_excerpts` passes over a registered source whose file is gone
    and one whose bytes are not UTF-8, so `_best_excerpt` -- the only thing that
    compares source text against the question -- never sees them. That is why
    the last branch says what the page shows rather than "nothing matched".

    The spy would sit at zero for a vacuous reason if the question produced no
    patterns, so the control at the end stores the same sentence as UTF-8 and
    requires exactly one comparison to happen.

    **This is a tripwire on a docstring claim, not coverage of a fix.** It
    passes on the parent commit by design: it guards `_fallback_answer_body`'s
    reasoning about *why* an excerpt can be absent, and that reasoning rests on
    behaviour no change here touches. Read a green run on the parent as the
    intended result, not as a test that pins nothing.

    It goes red when the skipping stops. Measured: the undecodable half falls to
    dropping `except UnicodeDecodeError` on its own; the missing-file half needs
    both the `is_file()` guard and `except OSError` gone, because either one
    alone still keeps the read away from `_best_excerpt`.
    """
    compared: list[str] = []
    real_best_excerpt = ask_module._best_excerpt

    def spy(text, patterns):
        compared.append(text)
        return real_best_excerpt(text, patterns)

    monkeypatch.setattr(ask_module, "_best_excerpt", spy)

    store = _store(tmp_path)
    (tmp_path / "sources").mkdir()
    store.add_source("sources/gone.txt")
    (tmp_path / "sources" / "euckr.txt").write_bytes(
        "샘플조직은 샘플서비스를 제공한다.".encode("euc-kr")
    )
    store.add_source("sources/euckr.txt")

    assert search_source_excerpts(store, root=tmp_path, question="샘플조직 설명해줘") == []
    assert compared == []

    (tmp_path / "sources" / "readable.txt").write_text(
        "샘플조직은 샘플서비스를 제공한다.", encoding="utf-8"
    )
    store.add_source("sources/readable.txt")

    excerpts = search_source_excerpts(store, root=tmp_path, question="샘플조직 설명해줘")

    assert [item.path for item in excerpts] == ["sources/readable.txt"]
    assert len(compared) == 1


# --- an excerpt means the source matched, not that it was readable (#468) ---


_UNRELATED_SOURCE_TEXT = "샘플기관은 샘플절차를 안내한다."


def _seed_three_matching_and_one_unrelated_source(tmp_path):
    """Four registered, readable sources scoring 2, 1, 1 and 0 on one question.

    The question these fixtures are built for is `샘플조직의 역할은 무엇인가?`,
    whose patterns are `샘플조직의`, `역할은` and `무엇인가`. `roles.txt`
    carries the first two, `history.txt` and `notice.txt` only the first, and
    `unrelated.txt` none of them.

    The file names and the texts are load-bearing rather than decorative.
    `store.sources()` reads `ORDER BY path`, so the three matching sources
    arrive at the sort as `history.txt`, `notice.txt`, `roles.txt`, which
    disagrees with their score order of `roles.txt`, `history.txt`,
    `notice.txt` -- and is not the reverse of it either, so reading the paths
    backwards does not recover the right answer any more than reading them
    forwards does. `notice.txt` is the long one for the same reason: its
    excerpt outruns `roles.txt`'s, so ranking by excerpt length instead of by
    score does not recover it either. A rename, or a rewrite that shortened
    that text, would put one of those orders back in step and make the
    ordering test vacuous, so that test asserts these properties rather than
    trusting this docstring for them.
    """
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "roles.txt").write_text("샘플조직의 역할은 샘플서비스 검토이다.", encoding="utf-8")
    (sources / "history.txt").write_text("샘플조직의 연혁은 다음과 같다.", encoding="utf-8")
    (sources / "notice.txt").write_text(
        "샘플조직의 안내문은 여러 항목으로 나뉘어 있으며 자세한 내용은 별도 문서에 있다.",
        encoding="utf-8",
    )
    (sources / "unrelated.txt").write_text(_UNRELATED_SOURCE_TEXT, encoding="utf-8")
    store = _store(tmp_path)
    store.add_source("sources/roles.txt")
    store.add_source("sources/history.txt")
    store.add_source("sources/notice.txt")
    store.add_source("sources/unrelated.txt")
    return store


def test_search_source_excerpts_drops_a_source_it_read_and_matched_nothing_in(
    tmp_path, monkeypatch
):
    """Being readable is not enough: `if score:` drops a source that scored zero.

    `search_source_excerpts` appends a match only when `_best_excerpt` returned
    a non-zero score. Without that gate a registered, readable source the
    question never touched still becomes an `AskExcerpt` -- carrying the empty
    string `_best_excerpt` returns when it found no pattern -- and the page
    lists that file under a blank excerpt.

    The test above this one makes the neighbouring point in the opposite
    direction: there the source never reaches `_best_excerpt` at all, because
    it is missing from disk or is not UTF-8. Here it is read and compared and
    dropped afterwards. The spy is what makes that difference observable, and
    it is also what keeps this test from rotting: were the fixture to stop
    writing `unrelated.txt` or stop registering it, the exclusion below would
    still hold, for a reason that has nothing to do with the gate. The spy
    fails in that case instead of passing quietly.

    The set equality is exact for this fixture only. Four sources cannot reach
    the `MAX_EXCERPTS` truncation, and no two of them resolve to the same path,
    so the `seen_paths` dedupe never drops one either. Outside those bounds the
    result is not simply "every registered source that scored".

    The neighbouring mutant `if excerpt:` is equivalent rather than uncaught,
    and the reason is a property of `_TOKEN` (`verinote/pipeline/ask.py:39`)
    rather than of this fixture. A falsy `score` means `best_pos < 0` and the
    early `return "", 0`, so only the other direction is open: a truthy score
    carrying an empty excerpt needs `text` to be entirely whitespace while
    some pattern is still found in `_fold(text)`. Neither half is reachable.
    All 29 whitespace code points, and every pair and triple of them, keep
    `_fold(s)` whitespace -- their combining classes are zero so nothing
    composes across them, none of them is the target of a composition,
    casefold maps none of them, and the two that NFC does rewrite (`U+2000`,
    `U+2001`) go to other whitespace. And each of the 32,303 code points
    `_TOKEN` can match folds to something non-empty and non-whitespace both
    alone and doubled, so no token, whatever its length, folds to something an
    all-whitespace `folded` could contain. A widened `_TOKEN` -- `\\S+`, say --
    would split the two gates apart, and what it would let through is exactly
    #468's defect: an `AskExcerpt` carrying an empty excerpt string.

    Not covered here: the order of what comes back (the next test), the
    truncation, the dedupe, and the window arithmetic inside `_best_excerpt`.
    """
    compared: list[str] = []
    real_best_excerpt = ask_module._best_excerpt

    def spy(text, patterns):
        compared.append(text)
        return real_best_excerpt(text, patterns)

    monkeypatch.setattr(ask_module, "_best_excerpt", spy)

    store = _seed_three_matching_and_one_unrelated_source(tmp_path)

    excerpts = search_source_excerpts(
        store, root=tmp_path, question="샘플조직의 역할은 무엇인가?"
    )

    assert _UNRELATED_SOURCE_TEXT in compared
    assert {item.path for item in excerpts} == {
        "sources/roles.txt",
        "sources/history.txt",
        "sources/notice.txt",
    }


def test_search_source_excerpts_puts_the_stronger_match_first(tmp_path):
    """Three matches come back best-first, not in the order the store read them.

    `sorted(matches, key=lambda item: (-item.score, item.path))` is the whole
    of that promise. Drop `-item.score` from the key, or the `sorted` call, or
    flip the sign, and what shows through is `store.sources()`' own
    `ORDER BY path`, which this fixture deliberately arranges to disagree with
    the score order.

    The three assertions above the ordering one are that arrangement, not
    decoration, and they read no score at all. Each takes a cheaper key the
    ordering assertion would otherwise be satisfied by -- paths ascending,
    paths descending, excerpt length descending -- and holds that it produces
    some other list. A rename that put one of those keys back in step
    (`a_roles.txt`, `notice.txt`, `z_history.txt`), or a shorter notice text,
    would leave the ordering assertion passing under a broken key, and a
    premise fails there instead, on the shipped code as much as on a mutant.

    Two mutations of that same key survive these tests, and the gap is named
    here rather than implied away. Dropping the `item.path` tie-break to
    `key=(-item.score,)` changes nothing even though two of these sources do
    score equally: `store.sources()` reads `ORDER BY path`, so equal scores
    arrive already in path order and a stable sort leaves them there whether
    or not the key says so. The `[:limit]` slice is likewise unpinned, since
    showing it needs more matching sources than `MAX_EXCERPTS`. Both are
    tracked in #533 rather than left as a claim here that goes quietly stale
    the day someone pins one of them.
    """
    store = _seed_three_matching_and_one_unrelated_source(tmp_path)

    excerpts = search_source_excerpts(
        store, root=tmp_path, question="샘플조직의 역할은 무엇인가?"
    )

    order = [item.path for item in excerpts]
    by_path = sorted(excerpts, key=lambda item: item.path)
    by_length = sorted(excerpts, key=lambda item: (-len(item.excerpt), item.path))
    assert [item.path for item in by_path] != order
    assert [item.path for item in reversed(by_path)] != order
    assert [item.path for item in by_length] != order
    assert order == [
        "sources/roles.txt",
        "sources/history.txt",
        "sources/notice.txt",
    ]


def test_ask_hands_the_page_the_excerpts_already_ordered(tmp_path):
    """The order has to survive into `AskResult.excerpts` to reach a reader.

    `ask.html` loops over `result.excerpts` as it receives them, so nothing
    downstream re-sorts and nothing upstream of this tuple is visible on the
    page. `_fallback_answer` makes its own call to `search_source_excerpts`;
    the test above cannot see a caller that re-collected the result on the way
    out, and this one runs the assembled route end to end instead.

    Same fixture, same three premises: the matching sources are named, and
    their texts sized, so that neither path order nor excerpt length can
    reproduce the list the scores ask for.
    """
    store = _seed_three_matching_and_one_unrelated_source(tmp_path)
    client = FallbackClient(error=LLMError("synthetic outage"))

    result = ask_question(
        store, client, root=tmp_path, question="샘플조직의 역할은 무엇인가?"
    )

    assert result.route == "fallback"
    order = [item.path for item in result.excerpts]
    by_path = sorted(result.excerpts, key=lambda item: item.path)
    by_length = sorted(result.excerpts, key=lambda item: (-len(item.excerpt), item.path))
    assert [item.path for item in by_path] != order
    assert [item.path for item in reversed(by_path)] != order
    assert [item.path for item in by_length] != order
    assert order == [
        "sources/roles.txt",
        "sources/history.txt",
        "sources/notice.txt",
    ]


def test_ask_body_promises_nothing_when_the_only_source_matched_nothing(tmp_path):
    """The body's last branch, reached through a source the gate dropped.

    Reaching it takes both collections empty, and every other run that lands
    there lands there without reading a source at all: wrap
    `_fallback_answer_body` and `_best_excerpt` for one full suite run,
    resetting the read count per test, and this is the only run that arrives
    at the branch with a read behind it. The rest leave `excerpts` empty
    because no source text was ever compared against the question, so the gate
    is not what emptied it for them. This row is the assembled pipeline
    reaching the branch the remaining way: a source that was registered, found,
    read, compared against the question, and kept out by `if score:` alone.

    A list of those runs grows with the file and says nothing about the one
    that gets added next; the property does, and the measurement above is how
    a reader checks whether it still holds.

    That is the distinction the helper's own docstring turns on -- it says the
    branch reports what the page shows rather than why, *because* an excerpt
    can be absent from text that was never read. This row supplies the other
    half, where the text was read.

    No fact is confirmed here, so the grounding table is empty too and the
    sentence has to promise neither section.
    """
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "unrelated.txt").write_text(
        _UNRELATED_SOURCE_TEXT, encoding="utf-8"
    )
    store = _store(tmp_path)
    store.add_source("sources/unrelated.txt")
    client = FallbackClient(error=LLMError("synthetic outage"))

    result = ask_question(
        store, client, root=tmp_path, question="샘플조직의 역할은 무엇인가?"
    )

    assert result.route == "fallback"
    assert result.excerpts == ()
    assert result.grounding_facts == ()
    assert result.answer == (
        "The deterministic engine could not answer, and no source excerpt or "
        "verified grounding fact is shown below."
    )


# --- Ask does not re-request a provider that just failed (#438) -------------


class RecordingClient:
    """Records the provider methods it is asked for, in order.

    The suppression under test is invisible to an answer-level assertion: a
    fallback answer built after a failed `answer_question` and one built without
    calling it at all can render identically, and `_fallback_answer` swallows
    `LLMError`. Only the call list distinguishes them, so these tests assert on
    it rather than on the result.
    """

    name = "recording"

    def __init__(self, *, intent_error=None, answer_error=None, answer="샘플 모델 답변"):
        self.calls: list[str] = []
        self.intent_error = intent_error
        self.answer_error = answer_error
        self.answer = answer

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        from verinote.pipeline.query_intent import parse_query_intent

        self.calls.append("extract_query_intent")
        if self.intent_error is not None:
            raise self.intent_error
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
        self.calls.append("translate_query")
        raise AssertionError("Ask must not call direct Datalog translation")

    def answer_question(self, *, question: str, context: str) -> str:
        self.calls.append("answer_question")
        if self.answer_error is not None:
            raise self.answer_error
        return self.answer


def _seed_rich(tmp_path):
    """A confirmed fact whose source text answers the question."""
    source = tmp_path / "sources" / "sample.txt"
    source.parent.mkdir()
    source.write_text(
        "샘플조직은 샘플서비스를 운영하며 샘플조직의 역할 논의가 진행 중이다.", encoding="utf-8"
    )
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플조직", "is_a", "조직", status="confirmed", source_id=source_id)
    return store


def _seed_grounding_only(tmp_path):
    """A confirmed fact whose source text does not answer the question."""
    source = tmp_path / "sources" / "sample.txt"
    source.parent.mkdir()
    source.write_text("샘플조직은 샘플서비스를 제공한다.", encoding="utf-8")
    store = _store(tmp_path)
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("샘플조직", "is_a", "조직", status="confirmed", source_id=source_id)
    return store


def test_ask_sends_one_request_when_intent_extraction_fails(tmp_path):
    """T1 -- the shape #438 reports: one failed call, not two.

    The deterministic parser does not support this question, so the flow asks
    the provider to read it; that call raises, the flow reports
    `provider_failed`, and Ask must not then ask the same provider to compose an
    answer. Asserted on the call list because the rendered answer is the same
    either way.
    """
    store = _store(tmp_path)
    client = RecordingClient(intent_error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert client.calls == ["extract_query_intent"]
    assert result.route == "fallback"


def test_ask_sends_one_request_when_reinterpretation_fails_and_keeps_the_evidence(tmp_path):
    """T2 -- the other reachable site, and the evidence survives the skip.

    Here the deterministic parser supports the question but plans nothing, so
    the flow asks the provider to re-read it; that is a different construction
    site from T1's with the same verdict. The excerpts and grounding facts are
    built without a model, so suppressing the request must not cost them.
    """
    store = _seed_rich(tmp_path)
    client = RecordingClient(intent_error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="샘플조직의 역할은 무엇인가?")

    assert client.calls == ["extract_query_intent"]
    assert result.excerpts
    assert result.grounding_facts


@pytest.mark.parametrize("status", ["review_required", "ambiguous", "translated"])
def test_ask_suppresses_on_the_flag_and_not_on_the_status(tmp_path, monkeypatch, status):
    """T3 -- consumer-side tripwire: the guard reads the flag, nothing else.

    Ask suppresses on `provider_failed` alone. Conjoining a status would let a
    future construction site with a different one fall through to a second
    request, so these rows inject statuses the flag does not naturally pair
    with and require the suppression to hold anyway. That is also why they are
    injected: among the constructions that carry a literal status, the flag
    pairs only with `review_required` and `translation_failed` today.

    This pins the consumer half only. The producer half -- that every
    provider-failure exit sets the flag -- is pinned in tests/test_query.py.
    """
    store = _store(tmp_path)
    client = RecordingClient()
    monkeypatch.setattr(
        ask_module,
        "_schema_aware_query_flow_result",
        lambda *args, **kwargs: _QueryFlowResult(
            status, None, "llm error: synthetic outage", False, True
        ),
    )

    result = ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert client.calls == []
    assert result.route == "fallback"


def test_ask_says_no_usable_reading_rather_than_that_the_provider_failed(tmp_path):
    """T4 -- the warning is true on both halves of the flag, and unformatted.

    `provider_failed` covers a request that failed *and* a request that
    succeeded with an unusable payload, so the notice claims only that no usable
    reading arrived. The literal is spelled out here rather than imported: a
    test that imports the constant it asserts cannot fail when the constant is
    reworded.
    """
    store = _store(tmp_path)
    client = RecordingClient(intent_error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert result.warning == (
        "verinote did not get a usable reading of the question from the provider "
        "and did not send it another request, so no model-composed answer is shown"
    )
    # ask.html renders this slot as text, so the sentence carries no markup.
    assert "`" not in result.warning
    assert "*" not in result.warning


def test_ask_still_reports_a_consulted_provider_as_consulted(tmp_path):
    """T5 -- over-reach guard; passes on the parent commit by design.

    Here the provider read the question successfully and only the *answering*
    call failed, so there is no provider-failure verdict and the fallback model
    was rightly asked. It exists to fail a suppression that fires too widely,
    not to demonstrate one: make the skip unconditional and both assertions go.
    """
    store = _store(tmp_path)
    client = RecordingClient(answer_error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert client.calls == ["extract_query_intent", "answer_question"]
    assert result.warning == "synthetic outage"


def test_ask_does_not_treat_a_fact_term_error_as_a_provider_failure(tmp_path, monkeypatch):
    """T6 -- over-reach guard: a flow that raised is not a provider failure.

    When the flow itself raises, it reported nothing at all -- including no
    verdict about the provider, which was never asked. The fallback model is
    still worth asking, so this path must keep calling it.

    **There is no parent-commit run of this to compare against**: it patches
    `_schema_aware_query_flow_result`, the name `ask.py` binds only from this
    commit, so on the parent the patch target does not exist and the test errors
    rather than reporting on behaviour. It earns its place against over-reach,
    not absence -- passing `provider_skipped=True` at this call site is caught
    here and, measured, nowhere else.
    """
    store = _store(tmp_path)
    client = RecordingClient()

    def _raise(*args, **kwargs):
        raise DuckDBFactTermStoreError("synthetic fact-term failure")

    monkeypatch.setattr(ask_module, "_schema_aware_query_flow_result", _raise)

    ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert client.calls == ["answer_question"]


def test_ask_names_the_grounding_table_on_the_skipped_path(tmp_path):
    """T7 -- the body stays true on the shape this fix makes common.

    Suppressing the model makes the no-prose body the usual outcome rather than
    a rare one, so the sentence commit 1 fixed has to be right here too.
    """
    store = _seed_grounding_only(tmp_path)
    client = RecordingClient(intent_error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="샘플조직의 역할은 무엇인가?")

    assert client.calls == ["extract_query_intent"]
    assert result.excerpts == ()
    assert result.grounding_facts
    assert result.answer == (
        "The deterministic engine could not answer. Verified grounding facts "
        "are shown below."
    )


def test_ask_keeps_the_provider_error_in_the_reason_when_it_skips(tmp_path):
    """T8 -- the specific error is still reachable, one line below the notice.

    The notice deliberately does not name the failure, so `reason` is the only
    place the actual provider error survives; `ask.html` renders it directly
    beneath the warning.

    **This passes on the parent commit**, and that is not a defect in it: the
    reason is composed by the flow and is the same whether or not Ask goes on to
    ask the model, so no assertion about it can separate the two. What it guards
    is the new branch not substituting something of its own -- measured, it is
    the only test that catches a constant reason on the skipped path.
    """
    store = _store(tmp_path)
    client = RecordingClient(intent_error=LLMError("synthetic outage"))

    result = ask_question(store, client, root=tmp_path, question="지원하지 않는 질문")

    assert "llm error" in result.reason
    assert "synthetic outage" in result.reason
