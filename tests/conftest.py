# SPDX-License-Identifier: MPL-2.0
"""Shared test fakes — a canned `LLMClient` so the pipeline is exercised offline."""

import pytest

from verinote.llm.base import ExtractedFact, LLMError


class FakeClient:
    """An `LLMClient` that returns canned facts (or raises) — no provider needed."""

    name = "fake"

    def __init__(self, facts=(), *, error: LLMError | None = None):
        self._facts = list(facts)
        self._error = error
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._facts)


@pytest.fixture
def fake_client():
    return FakeClient
