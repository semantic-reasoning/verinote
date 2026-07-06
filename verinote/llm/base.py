# SPDX-License-Identifier: MPL-2.0
"""The provider-agnostic LLM contract."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from verinote.pipeline.query_intent import QueryIntent

FactSlotKind = Literal["string", "term"]


class LLMError(RuntimeError):
    """Any provider-side or parsing failure, normalised across adapters."""


@dataclass(frozen=True)
class ExtractedFact:
    """One candidate fact the extractor proposes from a source.

    Mirrors the `facts` columns the store will persist. The DuckDB-backed
    verifier and the human review gate decide whether it ever becomes engine input.
    """

    subject: str
    relation: str
    object: str
    confidence: float
    note: str = ""
    _: KW_ONLY
    subject_kind: FactSlotKind = "string"
    relation_kind: FactSlotKind = "string"
    object_kind: FactSlotKind = "string"

    def __post_init__(self) -> None:
        for name in ("subject_kind", "relation_kind", "object_kind"):
            value = getattr(self, name)
            if value not in {"string", "term"}:
                raise ValueError(f"{name} must be 'string' or 'term', got {value!r}")


@runtime_checkable
class LLMClient(Protocol):
    """Every provider adapter implements this; callers depend only on it."""

    name: str

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        """Extract source-backed candidate facts from `source_text`.

        Adapters MUST force structured output (a JSON array of fact objects) and
        parse it into `ExtractedFact`s, raising `LLMError` on any provider error
        or schema violation so the caller can retry deterministically.
        """
        ...

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        """Translate an NL `question` into a single Datalog query line.

        Return a rule whose head is ``answer_q<qid>(V)`` binding one answer
        variable over ``relation/3``. When no executable rule is appropriate,
        return a durable non-executable outcome:
        ``review_required("reason")``, ``no_answer("reason")``, or
        ``ambiguous("reason")``. Raise `LLMError` on provider/parse error.
        """
        ...

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> "QueryIntent":
        """Extract a constrained query intent from `question`.

        This structured-output boundary is separate from Datalog translation.
        Adapters raise `LLMError` for malformed or schema-invalid intent output.
        """
        ...

    def answer_question(self, *, question: str, context: str) -> str:
        """Answer a free-form question from caller-provided context only.

        This is used by the read-only Ask workflow after deterministic engine
        routing cannot produce an executable answer. Adapters return plain text
        and raise `LLMError` for provider failures.
        """
        ...
