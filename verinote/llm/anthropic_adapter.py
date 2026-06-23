# SPDX-License-Identifier: MPL-2.0
"""Anthropic adapter. Forces structured output via tool use."""

from __future__ import annotations

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError
from verinote.llm.schema import EXTRACTION_SYSTEM, FACT_ARRAY_SCHEMA, parse_facts


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("anthropic SDK not installed; `pip install verinote[anthropic]`") from exc

        client = anthropic.Anthropic(api_key=self.cfg.api_key, base_url=self.cfg.base_url)
        tool = {
            "name": "emit_facts",
            "description": "Return the extracted facts.",
            "input_schema": FACT_ARRAY_SCHEMA,
        }
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=4096,
                system=EXTRACTION_SYSTEM + ("\n" + schema_hint if schema_hint else ""),
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_facts"},
                messages=[{"role": "user", "content": source_text}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise LLMError(f"anthropic request failed: {exc}") from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return parse_facts(block.input)
        raise LLMError("anthropic response contained no tool_use block")
