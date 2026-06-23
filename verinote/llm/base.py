# SPDX-License-Identifier: Apache-2.0
"""The provider-agnostic LLM contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Any provider-side or parsing failure, normalised across adapters."""


@dataclass(frozen=True)
class ExtractedFact:
    """One candidate fact the extractor proposes from a source.

    Mirrors the `facts` columns the store will persist. The wirelog verifier and
    the human review gate decide whether it ever becomes engine input.
    """

    subject: str
    relation: str
    object: str
    confidence: float
    note: str = ""


@runtime_checkable
class LLMClient(Protocol):
    """Every provider adapter implements this; callers depend only on it."""

    name: str

    def extract_facts(
        self, *, source_text: str, schema_hint: str = ""
    ) -> list[ExtractedFact]:
        """Extract source-backed candidate facts from `source_text`.

        Adapters MUST force structured output (a JSON array of fact objects) and
        parse it into `ExtractedFact`s, raising `LLMError` on any provider error
        or schema violation so the caller can retry deterministically.
        """
        ...
