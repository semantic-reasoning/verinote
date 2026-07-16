# SPDX-License-Identifier: MPL-2.0
"""OpenAI adapter. Forces structured output via response_format json_schema."""

from __future__ import annotations

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError
from verinote.llm.schema import (
    FACT_ARRAY_SCHEMA,
    QUERY_INTENT_SCHEMA,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
)
from verinote.pipeline.query_intent import QueryIntent, parse_query_intent
from verinote.prompts import PromptError, render_prompt


class OpenAIAdapter:
    name = "openai"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _client(self):
        """Build a client that honours the configured request timeout.

        Every method routes through here so the timeout (and any future
        client-wide setting) is applied at exactly one site. The base_url
        also makes this work against any OpenAI-compatible endpoint.
        """
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("openai SDK not installed; `pip install verinote[openai]`") from exc
        return OpenAI(
            api_key=self.cfg.api_key,
            base_url=self.cfg.base_url,
            timeout=self.cfg.llm_timeout_seconds,
        )

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        client = self._client()
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {
                        "role": "system",
                        "content": _with_schema_hint(
                            _render_prompt(self.cfg.root, "extraction"), schema_hint
                        ),
                    },
                    {"role": "user", "content": source_text},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "facts", "schema": FACT_ARRAY_SCHEMA, "strict": True},
                },
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise LLMError(f"openai request failed: {exc}") from exc

        return parse_facts(resp.choices[0].message.content or "")

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        client = self._client()
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {
                        "role": "system",
                        "content": _with_schema_hint(
                            _render_prompt(
                                self.cfg.root, "query-translation", qid=qid
                            ),
                            schema_hint,
                        ),
                    },
                    {"role": "user", "content": question},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "query", "schema": QUERY_SCHEMA, "strict": True},
                },
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise LLMError(f"openai request failed: {exc}") from exc

        return parse_query(resp.choices[0].message.content or "")

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> QueryIntent:
        client = self._client()
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {
                        "role": "system",
                        "content": _with_schema_hint(
                            _render_prompt(self.cfg.root, "query-intent"),
                            schema_hint,
                        ),
                    },
                    {"role": "user", "content": question},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "query_intent",
                        "schema": QUERY_INTENT_SCHEMA,
                        "strict": True,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise LLMError(f"openai request failed: {exc}") from exc

        return parse_query_intent(resp.choices[0].message.content or "")

    def answer_question(self, *, question: str, context: str) -> str:
        client = self._client()
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {
                        "role": "system",
                        "content": _render_prompt(self.cfg.root, "ask-fallback"),
                    },
                    {
                        "role": "user",
                        "content": f"Question:\n{question}\n\nContext:\n{context}",
                    },
                ],
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise LLMError(f"openai request failed: {exc}") from exc

        return (resp.choices[0].message.content or "").strip()


def _with_schema_hint(prompt: str, schema_hint: str) -> str:
    return prompt + ("\n" + schema_hint if schema_hint else "")


def _render_prompt(root, prompt_id: str, **values: object) -> str:
    try:
        return render_prompt(root, prompt_id, **values)
    except PromptError as exc:
        raise LLMError(str(exc)) from exc
