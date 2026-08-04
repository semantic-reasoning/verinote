# SPDX-License-Identifier: MPL-2.0
"""Anthropic adapter. Forces structured output via tool use."""

from __future__ import annotations

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError, redact_secret
from verinote.llm.schema import (
    FACT_ARRAY_SCHEMA,
    QUERY_INTENT_SCHEMA,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
)
from verinote.pipeline.query_intent import QueryIntent, parse_query_intent
from verinote.prompts import PromptError, render_prompt


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _request_failed(self, exc: Exception) -> LLMError:
        """One construction site for provider failures, so a raise site somebody
        forgot cannot let the configured key survive into an error message.

        It can only redact the key it knows about, which is why `_require_key`
        refuses to let the SDK authenticate with one this process never saw.
        """
        return LLMError(redact_secret(f"{self.name} request failed: {exc}", self.cfg.api_key))

    def _require_key(self) -> str:
        """The configured key, or a clear failure instead of a silent fallback.

        Handing `api_key=None` to either vendor SDK makes it read its own
        `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` instead — verified against the
        installed SDKs. The request then authenticates with a credential verinote
        never resolved, so `redact_secret` cannot match it and a 4xx body echoing
        it is persisted verbatim into `source_chunks.error`. Since `base_url` is
        caller-supplied, that echo is attacker-influenced.

        Raising `LLMError` rather than passing `""` (which the SDK rejects with
        its own error type, outside the `except LLMError` every caller uses) keeps
        the failure inside the contract callers already handle.
        """
        if not self.cfg.api_key:
            raise LLMError(
                f"{self.name} requires an API key; set "
                f"VERINOTE_{self.name.upper()}_API_KEY "
                f"(the {self.name} SDK's own environment variable is deliberately not used)"
            )
        return self.cfg.api_key

    def _client(self):
        """Build a client that honours the configured request timeout.

        Every method routes through here so the timeout (and any future
        client-wide setting) is applied at exactly one site.
        """
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("anthropic SDK not installed; `pip install verinote[anthropic]`") from exc
        return anthropic.Anthropic(
            api_key=self._require_key(),
            base_url=self.cfg.base_url,
            timeout=self.cfg.llm_timeout_seconds,
        )

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        client = self._client()
        tool = {
            "name": "emit_facts",
            "description": "Return the extracted facts.",
            "input_schema": FACT_ARRAY_SCHEMA,
        }
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=4096,
                system=_with_schema_hint(
                    _render_prompt(self.cfg.root, "extraction"), schema_hint
                ),
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_facts"},
                messages=[{"role": "user", "content": source_text}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return parse_facts(block.input)
        raise LLMError("anthropic response contained no tool_use block")

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        client = self._client()
        tool = {
            "name": "emit_query",
            "description": "Return the Datalog query line.",
            "input_schema": QUERY_SCHEMA,
        }
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=1024,
                system=_with_schema_hint(
                    _render_prompt(self.cfg.root, "query-translation", qid=qid),
                    schema_hint,
                ),
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_query"},
                messages=[{"role": "user", "content": question}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return parse_query(block.input)
        raise LLMError("anthropic response contained no tool_use block")

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> QueryIntent:
        client = self._client()
        tool = {
            "name": "emit_query_intent",
            "description": "Return the structured query intent.",
            "input_schema": QUERY_INTENT_SCHEMA,
        }
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=1024,
                system=_with_schema_hint(
                    _render_prompt(self.cfg.root, "query-intent"), schema_hint
                ),
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_query_intent"},
                messages=[{"role": "user", "content": question}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return parse_query_intent(block.input)
        raise LLMError("anthropic response contained no tool_use block")

    def answer_question(self, *, question: str, context: str) -> str:
        client = self._client()
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=1200,
                system=_render_prompt(self.cfg.root, "ask-fallback"),
                messages=[
                    {
                        "role": "user",
                        "content": f"Question:\n{question}\n\nContext:\n{context}",
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        parts = [
            str(getattr(block, "text", "")).strip()
            for block in msg.content
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(part for part in parts if part).strip()


def _with_schema_hint(prompt: str, schema_hint: str) -> str:
    return prompt + ("\n" + schema_hint if schema_hint else "")


def _render_prompt(root, prompt_id: str, **values: object) -> str:
    try:
        return render_prompt(root, prompt_id, **values)
    except PromptError as exc:
        raise LLMError(str(exc)) from exc
