# SPDX-License-Identifier: Apache-2.0
"""Shared structured-output contract + parser used by every adapter.

Keeping the JSON schema and the parse-into-`ExtractedFact` logic in one place is
what makes the adapters thin: each provider only has to deliver text/JSON that
satisfies FACT_ARRAY_SCHEMA; `parse_facts` does the normalisation once.
"""

from __future__ import annotations

import json
from typing import Any

from verinote.llm.base import ExtractedFact, LLMError

# JSON Schema for a batch of extracted facts. Adapters pass this to whatever
# structured-output mechanism the provider offers (tool use / response_format /
# json mode) so the model is constrained to emit exactly this shape.
FACT_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["subject", "relation", "object", "confidence"],
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string", "minLength": 1},
        "relation": {"type": "string", "minLength": 1},
        "object": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "note": {"type": "string"},
    },
}

FACT_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["facts"],
    "additionalProperties": False,
    "properties": {"facts": {"type": "array", "items": FACT_OBJECT_SCHEMA}},
}

EXTRACTION_SYSTEM = (
    "You extract source-backed factual triples from a document. Return ONLY facts "
    "stated or directly entailed by the text. Each fact is a (subject, relation, "
    "object) triple with a confidence in [0,1]. Do not invent facts. Emit JSON "
    "matching the provided schema."
)


def parse_facts(raw: str | dict[str, Any]) -> list[ExtractedFact]:
    """Parse provider output (a JSON string or already-decoded dict) into facts."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        items = data["facts"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LLMError(f"extractor output did not match schema: {exc}") from exc

    facts: list[ExtractedFact] = []
    for item in items:
        try:
            facts.append(
                ExtractedFact(
                    subject=str(item["subject"]).strip(),
                    relation=str(item["relation"]).strip(),
                    object=str(item["object"]).strip(),
                    confidence=float(item["confidence"]),
                    note=str(item.get("note", "")).strip(),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError(f"malformed fact object {item!r}: {exc}") from exc
    return facts
