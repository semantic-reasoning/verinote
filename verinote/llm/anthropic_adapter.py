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
        """Failures from a `client.messages.create` call, redacted.

        Redaction is the property that matters, not the split. Two sites in this
        class put a *caught* exception into an `LLMError` -- this one and
        `_client_failed` -- and both redact, so a raise site somebody forgot
        cannot let the configured key survive into a message that is persisted
        to `source_chunks.error`. Those two are the whole of the safety
        argument; the class's other `LLMError`s are a different kind.

        Carriers outside the class are not covered by that, and there is more
        than one. Module-level `_render_prompt` builds `LLMError(str(exc))` and
        does NOT redact; nothing leaks through it today, because it catches only
        `PromptError` and those messages are fixed strings plus a prompt id,
        placeholder name, or title. The schema helpers each generation method
        calls just past its guarded region -- `parse_facts`, `parse_query`,
        `parse_query_intent` -- do not redact either, and none of the three is
        harmless. What they parse is the provider's response payload, which in
        this adapter is the already-decoded `tool_use` input rather than a
        string (`parse_facts(block.input)`), and `_require_key` below already
        establishes the mechanism that puts a credential in it: `base_url` is
        caller-supplied, so the endpoint dialled is one a user can point
        anywhere, and what it echoes is persisted. `_require_key` applies that
        to a key verinote never resolved, which `redact_secret` could not match
        anyway; the *configured* key arrives the same way and could be matched,
        but nothing on this path tries. Measured, an echoing response reaches
        `llm/schema.py`'s "malformed fact object {item!r}" verbatim, with no
        `***`. What `_request_failed` redacts is that echo arriving as an
        *error*; arriving as a parsable response it goes around the guard
        entirely. That predates this change and is tracked as #514, not
        something prose can fix. Neither of the two carrier lists in this
        paragraph is offered as exhaustive. The point is that "every site
        carrying a caught exception redacts" stops being true the moment you
        leave this class, and asserting it unqualified is the almost-true claim
        this docstring was rewritten to stop making.

        The remaining raises use fixed strings and carry nothing caught:
        `_require_key`, the `ImportError` clause in `_client`, and the three "no
        tool_use block" raises in the generation methods. `_require_key` is the
        instructive one -- it fails before anything is dialled and is
        deliberately NOT `_client_failed`, which is what the hoist comment in
        `_client` is protecting. "Before the request" is therefore not what
        separates these messages.

        Timing is not what this name promises, either. Prompt rendering is an
        ARGUMENT to `client.messages.create`, so it is evaluated inside the
        guarded region: an override missing a required placeholder is reported
        as "anthropic request failed" with nothing dialled (measured). Read this
        message as "the SDK call yielded no result", not "the provider
        answered"; `_client_failed` is the one entitled to speak about when.
        That imprecision predates this change and is #500's. Its stated fix is
        to hoist the render out of the `try` and leave only the SDK call inside
        -- the shape `key = self._require_key()` already has in `_client`. Done
        bare, that satisfies what #500 asks for: measured, the same broken
        override then reports "Datalog translation prompt must include required
        placeholder {qid}", with no "request failed" in front of it. Something
        it also does is not in the issue, and is measured here. `get_prompt`
        reads an override with `read_text`, so a hand-edited non-UTF-8 file
        raises `UnicodeDecodeError`, which `_render_prompt`'s
        `except PromptError` does not convert. Inside the guarded region that is
        normalised like every other failure; hoisted out, it leaves this adapter
        as itself -- an LLM failure that is not an `LLMError`, which is the
        §10.1 violation this change closes at the client-construction site,
        reopened at the render. So the hoist wants the render's other failures
        normalised along with it.

        Redaction covers only the key this process knows about, which is why
        `_require_key` refuses to let the SDK authenticate with one it never saw.
        """
        return LLMError(redact_secret(f"{self.name} request failed: {exc}", self.cfg.api_key))

    def _client_failed(self, exc: Exception) -> LLMError:
        """The client could not be built, so nothing was ever dialled.

        Deliberately does NOT name the Base URL setting. A malformed `base_url`
        is the reachable cause this exists for (#493), but measured against the
        installed `anthropic` SDK it is not the only one: with `base_url` unset
        entirely, `SSL_CERT_FILE` pointing at a missing file raises
        `FileNotFoundError`, and `HTTPS_PROXY='::::'` or
        `ANTHROPIC_BASE_URL='::::'` raise `httpx.InvalidURL`. Telling those users
        to check a field they left blank sends them to fix something that is not
        broken -- the misdirection #474 was reported as. The urllib adapters can
        be specific, and are, because `Request(url)` has no second cause; see
        `base_url_unusable`.

        Singular SDK, and its own variable, because each SDK reads only its own
        *base-URL* variable: measured, `OPENAI_BASE_URL` carrying that same
        `::::` leaves this constructor raising nothing at all. It is named here
        as a non-cause, which is what the guard in `tests/test_cloud_adapters.py`
        allows: it reads the paragraph above -- the one enumerating causes,
        found by a marker phrase that paragraph contains -- and rejects the
        other vendor's variable from *that paragraph*, leaving a mention like
        this one in a later paragraph legal. The other two entries above are not
        vendor-scoped -- both SDKs read `SSL_CERT_FILE` and `HTTPS_PROXY` --
        which is why they are listed unqualified. The
        otherwise identical paragraph in `openai_adapter` therefore has to name
        a different variable: two paragraphs documenting two SDKs, not one
        copied twice.
        """
        return LLMError(
            redact_secret(f"{self.name} client could not be created: {exc}", self.cfg.api_key)
        )

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
        # Hoisted out of the constructor call, not merely out of the `try`: as an
        # ARGUMENT it would be evaluated inside the guarded region, and the
        # `LLMError` it raises for a missing key would come back out relabelled
        # "client could not be created" -- a config error reported as an SDK
        # failure. The region below must contain no statement that raises
        # `LLMError`, and this line is how that stays true.
        key = self._require_key()
        try:
            return anthropic.Anthropic(
                api_key=key, base_url=self.cfg.base_url, timeout=self.cfg.llm_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - normalise SDK construction errors
            raise self._client_failed(exc) from exc

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
