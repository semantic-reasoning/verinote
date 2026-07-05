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
    EXTRACTION_SYSTEM,
    FACT_ARRAY_SCHEMA,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
    query_system,
)


class OllamaAdapter:
    name = "ollama"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or "http://localhost:11434").rstrip("/")

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        system = EXTRACTION_SYSTEM + ("\n" + schema_hint if schema_hint else "")
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            # Ollama accepts a JSON schema in `format` to constrain output.
            "format": FACT_ARRAY_SCHEMA,
            "options": {"temperature": 0},
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

        return parse_facts(body.get("message", {}).get("content", ""))

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        system = query_system(qid) + ("\n" + schema_hint if schema_hint else "")
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            "format": QUERY_SCHEMA,
            "options": {"temperature": 0},
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
