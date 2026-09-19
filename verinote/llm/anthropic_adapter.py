# SPDX-License-Identifier: MPL-2.0
"""Anthropic adapter. Forces structured output via tool use."""

from __future__ import annotations

from verinote.config import Config
from verinote.llm.base import (
    ExtractedFact,
    LLMError,
    LLMOutputError,
    parsed_under_redaction,
    redact_secret,
    render_prompt_or_error,
)
from verinote.llm.schema import (
    FACT_ARRAY_SCHEMA,
    QUERY_INTENT_SCHEMA,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
)
from verinote.pipeline.query_intent import QueryIntent, parse_query_intent

# The endpoint an unset `base_url` resolves to. Named rather than inlined so the
# SDK never sees `base_url=None`: a `None` there is a delegation, and the
# anthropic SDK's delegation target is its own `ANTHROPIC_BASE_URL`, so a blank
# field would let an environment the settings screen never shows decide where
# documents go. `_require_key` makes the same refusal for the credential -- the
# endpoint and the key must both be values this process resolved.
# `OllamaAdapter.__init__` and `OpenRouterAdapter._base_url` already bind their
# endpoints this way.
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"


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

        Carriers outside the class were not covered by that, and there was more
        than one: the per-adapter `_render_prompt` copies built an `LLMError`
        around a caught exception and did NOT redact, and `ollama_adapter` and
        `claude_cli_adapter` called their copy bare, importing no
        `redact_secret` at all -- on `759eac0` the same unreadable override
        left them with a raw `PermissionError`, the key in it, spelled into
        `cfg.root`. #540 took that residue out: the render now goes through
        `render_prompt_or_error` in `llm/base.py`, which takes the `Config`
        and hands `cfg.api_key` to `redact_secret` at both of its exit
        clauses. Measured on this branch, the cells that carry a path --
        this adapter, `openai`, `openrouter`, four methods each, an unreadable
        override, plus the `ollama` and `claude_cli` paths -- all come out
        `.../kb-***/...` again, which is what `759eac0` did before the render
        was hoisted out of the `try` that used to redact them.

        The schema helpers three of the four generation methods call just past
        their guarded region -- `parse_facts`, `parse_query`,
        `parse_query_intent`; `answer_question` calls none, it joins the
        stripped text blocks -- do not redact either, and two of the
        three are not harmless. Measured, `parse_facts` and `parse_query_intent`
        put what they were handed into the message they raise; `parse_query`
        cannot -- its two raise sites carry a missing key name, a builtin
        `TypeError` phrase, or a JSON position, and nothing of the payload,
        which is a boundedness the render's catch-all no longer has. What those
        two parse is the provider's response payload, which in this adapter is
        the already-decoded `tool_use` input rather than a string
        (`parse_facts(block.input)`), and `_require_key` below already
        establishes the mechanism that puts a credential in it:
        `base_url` is caller-supplied, so the endpoint dialled is one a user can
        point anywhere, and what it echoes is persisted. `_require_key` applies
        that to a key verinote never resolved, which `redact_secret` could not
        match anyway; the *configured* key arrives the same way and could be
        matched, but nothing on this path tries. Measured, an echoing response
        reaches `llm/schema.py`'s "malformed fact object {item!r}" verbatim,
        with no `***`. What `_request_failed` redacts is that echo arriving as
        an *error*; arriving as a parsable response it goes around the guard
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

        Timing is not what this name promises, either, and it used to be worse.
        Prompt rendering was an ARGUMENT to `client.messages.create`, evaluated
        inside the guarded region, so an override missing a required placeholder
        came back as "anthropic request failed" with nothing dialled (measured).
        #500 lifted the render into a statement of its own -- below
        `client = self._client()`, above the `try`, the shape
        `key = self._require_key()` already has in `_client` -- leaving the
        guarded region holding the SDK call and nothing else. Measured, the same
        broken override now reports "Datalog translation prompt must include
        required placeholder {qid}", with no "request failed" in front of it.

        What that does not buy is a message that can speak about when. Measured
        against anthropic 0.116.0, `base_url` on a closed port, the client built
        the way `_client` builds it -- `timeout=` passed, which is the condition
        and not a detail: `messages.create(max_tokens="x")` and `messages="hi"`
        both come back as `APIConnectionError`, exactly as a well-formed call
        does. Leave the timeout off and that same `max_tokens="x"` fails locally
        with a `TypeError` instead, because the SDK derives a timeout from it;
        that is why the condition is stated rather than a claim that this SDK
        validates nothing locally. Under the condition this adapter is always in,
        the reading stays "the SDK call yielded no result" rather than "the
        provider answered", and `_client_failed` is still the one entitled to say
        when. What the hoist changed is that the prompt error -- the one measured
        case of this message being worn by something that never dialled -- no
        longer arrives here.

        The render sits outside this guard in every adapter, and the hole
        that position opens -- `get_prompt` reads an override with
        `read_text`, so a hand-edited non-UTF-8 file raises
        `UnicodeDecodeError` and a mode bit raises `PermissionError`, neither
        a `PromptError` -- is closed by the catch-all in
        `render_prompt_or_error` (`llm/base.py`), which normalises the
        render's failures wherever the render sits. That is what keeps §10.1
        true at the seam: the message here claims only the SDK call.

        Redaction covers only the key this process knows about, which is why
        `_require_key` refuses to let the SDK authenticate with one it never saw.
        """
        error = LLMError(redact_secret(f"{self.name} request failed: {exc}", self.cfg.api_key))
        error.population = "unreachable"
        return error

    def _client_failed(self, exc: Exception) -> LLMError:
        """The client could not be built, so nothing was ever dialled.

        Deliberately does NOT name the Base URL setting. A malformed `base_url`
        is the reachable cause this exists for (#493), but measured against the
        installed `anthropic` SDK it is not the only one: `SSL_CERT_FILE`
        pointing at a missing file raises `FileNotFoundError`, and
        `HTTPS_PROXY='::::'` raises `httpx.InvalidURL`. The vendor's own
        `ANTHROPIC_BASE_URL` is no longer among them: `_client` resolves a blank
        field to `ANTHROPIC_DEFAULT_BASE_URL`, so the SDK never sees
        `base_url=None` and cannot fall back to the environment -- measured, the
        `ANTHROPIC_BASE_URL='::::'` that raised `httpx.InvalidURL` with
        `base_url=None` raises nothing once the explicit default is supplied
        (#499). Telling those users to check a field they left blank sends them
        to fix something that is not broken -- the misdirection #474 was
        reported as. The urllib adapters can be specific, and are, because
        `Request(url)` has no second cause; see `base_url_unusable`.

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
        error = LLMError(
            redact_secret(f"{self.name} client could not be created: {exc}", self.cfg.api_key)
        )
        error.population = "unreachable"
        return error

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
            error = LLMError(
                f"{self.name} requires an API key; set "
                f"VERINOTE_{self.name.upper()}_API_KEY "
                f"(the {self.name} SDK's own environment variable is deliberately not used)"
            )
            error.population = "credentials"
            raise error
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
        # `or ANTHROPIC_DEFAULT_BASE_URL` is #499: a `None` here is a
        # delegation, and the anthropic SDK's delegation target is its own
        # `ANTHROPIC_BASE_URL` -- an environment the settings screen never
        # shows. The same refusal `_require_key` makes for the key.
        try:
            return anthropic.Anthropic(
                api_key=key,
                base_url=self.cfg.base_url or ANTHROPIC_DEFAULT_BASE_URL,
                timeout=self.cfg.llm_timeout_seconds,
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
        system = _with_schema_hint(render_prompt_or_error(self.cfg, "extraction"), schema_hint)
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=4096,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_facts"},
                messages=[{"role": "user", "content": source_text}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return parsed_under_redaction(
                    parse_facts, block.input, self.cfg.api_key
                )
        raise LLMOutputError("anthropic response contained no tool_use block")

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        client = self._client()
        tool = {
            "name": "emit_query",
            "description": "Return the Datalog query line.",
            "input_schema": QUERY_SCHEMA,
        }
        system = _with_schema_hint(
            render_prompt_or_error(self.cfg, "query-translation", qid=qid), schema_hint
        )
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=1024,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_query"},
                messages=[{"role": "user", "content": question}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return parsed_under_redaction(
                    parse_query, block.input, self.cfg.api_key
                )
        raise LLMOutputError("anthropic response contained no tool_use block")

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> QueryIntent:
        client = self._client()
        tool = {
            "name": "emit_query_intent",
            "description": "Return the structured query intent.",
            "input_schema": QUERY_INTENT_SCHEMA,
        }
        system = _with_schema_hint(render_prompt_or_error(self.cfg, "query-intent"), schema_hint)
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=1024,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_query_intent"},
                messages=[{"role": "user", "content": question}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return parsed_under_redaction(
                    parse_query_intent, block.input, self.cfg.api_key
                )
        raise LLMOutputError("anthropic response contained no tool_use block")

    def answer_question(self, *, question: str, context: str) -> str:
        client = self._client()
        system = render_prompt_or_error(self.cfg, "ask-fallback")
        try:
            msg = client.messages.create(
                model=self.cfg.model,
                max_tokens=1200,
                system=system,
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


