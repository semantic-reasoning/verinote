# SPDX-License-Identifier: MPL-2.0
"""Ollama adapter — fully local, no cloud vendor. Uses Ollama's JSON format mode.

This adapter is the proof that anti-lock-in is real: with a local model the whole
pipeline runs offline, and the wirelog verifier still guarantees correctness.
"""

from __future__ import annotations

import json
import urllib.request

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError
from verinote.llm.schema import EXTRACTION_SYSTEM, FACT_ARRAY_SCHEMA, parse_facts


class OllamaAdapter:
    name = "ollama"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or "http://localhost:11434").rstrip("/")

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        payload = {
            "model": self.cfg.model,
            "stream": False,
            # Ollama accepts a JSON schema in `format` to constrain output.
            "format": FACT_ARRAY_SCHEMA,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM + ("\n" + schema_hint if schema_hint else "")},
                {"role": "user", "content": source_text},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - local trusted endpoint
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

        return parse_facts(body.get("message", {}).get("content", ""))
