# SPDX-License-Identifier: Apache-2.0
"""OpenAI adapter. Forces structured output via response_format json_schema."""

from __future__ import annotations

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError
from verinote.llm.schema import EXTRACTION_SYSTEM, FACT_ARRAY_SCHEMA, parse_facts


class OpenAIAdapter:
    name = "openai"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def extract_facts(
        self, *, source_text: str, schema_hint: str = ""
    ) -> list[ExtractedFact]:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError(
                "openai SDK not installed; `pip install verinote[openai]`"
            ) from exc

        # base_url makes this work against any OpenAI-compatible endpoint too.
        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url)
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {
                        "role": "system",
                        "content": EXTRACTION_SYSTEM
                        + ("\n" + schema_hint if schema_hint else ""),
                    },
                    {"role": "user", "content": source_text},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "facts",
                        "schema": FACT_ARRAY_SCHEMA,
                        "strict": True,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise LLMError(f"openai request failed: {exc}") from exc

        return parse_facts(resp.choices[0].message.content or "")
