# SPDX-License-Identifier: MPL-2.0
"""Ollama adapter — fully local, no cloud vendor. Uses Ollama's JSON format mode.

This adapter is the proof that anti-lock-in is real: with a local model the whole
pipeline runs offline, and the DuckDB-backed verifier still guarantees correctness.
"""

from __future__ import annotations

import json
import urllib.request

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError
from verinote.llm.schema import (
    QUERY_INTENT_SCHEMA,
    QUERY_INTENT_SYSTEM,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
    query_system,
)
from verinote.pipeline.query_intent import QueryIntent, parse_query_intent


OLLAMA_FACT_ARRAY_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["subject", "relation", "object", "confidence", "note"],
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string"},
            "relation": {"type": "string"},
            "object": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "note": {"type": "string"},
        },
    },
}

OLLAMA_EXTRACTION_SYSTEM = (
    "Extract explicit source-backed factual triples from the document chunk. "
    "Return a JSON array only. Each item must have string fields subject, "
    "relation, object, note and numeric confidence. Extract many small facts "
    "from each sentence, bullet, or table row, up to {max_facts} facts for this chunk. "
    "Write each fact as a semantic subject-predicate-object statement: subject is "
    "the entity being described, relation is a concise predicate, and object is "
    "the related entity or value. Normalize subjects and objects to concise "
    "source-language fact terms instead of copying whole source phrases. Preserve "
    "the source language, script, and named-entity spelling for subjects and "
    "objects; do not translate, romanize, summarize, or invent entities. For "
    "relations, prefer concise English canonical predicates when schema/policy "
    "aliases or obvious stable predicates support them; otherwise keep the "
    "concise source-language relation label. Put the exact original supporting "
    "phrase in note when a fact is normalized, compacted, corrected, "
    "or derived from layout. Skip facts that cannot be expressed with a non-empty "
    "subject, relation, and object, or where the relation is only inferred from "
    "co-occurrence in the same chunk. For numeric, percentage, count, date, or "
    "money facts, the subject must appear in the same local evidence record; do "
    "not reuse a subject from a previous or overlapping chunk. For key-value or "
    "label-value text, do not emit generic predicates like `값`; use relation "
    "`value` when no clearer owner/predicate is available. Do not use `is_a` "
    "unless the object is a class, category, or type of the subject. Do not use "
    "sentence endings such as `입니다` as objects, and do not emit question or "
    "judgment predicates ending in `여부`."
)


class OllamaAdapter:
    name = "ollama"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or "http://localhost:11434").rstrip("/")

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        system = OLLAMA_EXTRACTION_SYSTEM.format(
            max_facts=self.cfg.extraction_max_facts_per_chunk
        )
        system += "\n" + schema_hint if schema_hint else ""
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            # Ollama local models are much more reliable with flat string slots.
            "format": OLLAMA_FACT_ARRAY_SCHEMA,
            "options": {"temperature": 0, "num_predict": 1800},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": source_text},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

        try:
            return parse_facts(body.get("message", {}).get("content", ""))
        except LLMError as exc:
            message = str(exc)
            if (
                "malformed fact object" in message
                or "extractor output did not match schema" in message
            ):
                return []
            raise

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        system = query_system(qid) + ("\n" + schema_hint if schema_hint else "")
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            "format": QUERY_SCHEMA,
            "options": {"temperature": 0, "num_predict": 512},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

        return parse_query(body.get("message", {}).get("content", ""))

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> QueryIntent:
        system = QUERY_INTENT_SYSTEM + ("\n" + schema_hint if schema_hint else "")
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            "format": QUERY_INTENT_SCHEMA,
            "options": {"temperature": 0, "num_predict": 512},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

        return parse_query_intent(body.get("message", {}).get("content", ""))
