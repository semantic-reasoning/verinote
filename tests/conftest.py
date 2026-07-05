# SPDX-License-Identifier: MPL-2.0
"""Shared test fakes — a canned `LLMClient` so the pipeline is exercised offline."""

import pytest

from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline.query_intent import parse_query_intent


class FakeClient:
    """An `LLMClient` that returns canned facts (or raises) — no provider needed."""

    name = "fake"

    def __init__(self, facts=(), *, error: LLMError | None = None, query=None, intent=None):
        self._facts = list(facts)
        self._error = error
        # query: callable(question, qid) -> Datalog line; default answers an is_a.
        self._query = query
        self._intent = intent
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._facts)

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._query is not None:
            return self._query(question, qid)
        return f'answer_q{qid}(O) :- relation("{question}", "is_a", O).'

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        self.calls += 1
        if self._error is not None:
            raise self._error
        raw = self._intent(question) if callable(self._intent) else self._intent
        if raw is None:
            raw = {
                "kind": "unknown_or_unsupported",
                "subject": None,
                "relation": None,
                "object": None,
                "relation_candidates": None,
                "operator": None,
                "value_type": None,
                "value": None,
                "reason": "not configured",
            }
        return parse_query_intent(raw)


@pytest.fixture
def fake_client():
    return FakeClient


def query_intent_payload(
    kind,
    *,
    subject=None,
    relation=None,
    object=None,
    relation_candidates=(),
    operator=None,
    value_type=None,
    value=None,
    reason=None,
):
    def target(kind, value):
        return None if value is None else {"kind": kind, "value": value}

    return {
        "kind": kind,
        "subject": target("entity", subject),
        "relation": target("relation", relation),
        "object": target("entity", object),
        "relation_candidates": list(relation_candidates),
        "operator": operator,
        "value_type": value_type,
        "value": value,
        "reason": reason,
    }


@pytest.fixture
def intent_payload():
    return query_intent_payload
