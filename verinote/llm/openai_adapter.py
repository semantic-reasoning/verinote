# SPDX-License-Identifier: MPL-2.0
"""OpenAI adapter. Forces structured output via response_format json_schema."""

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


class OpenAIAdapter:
    name = "openai"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _request_failed(self, exc: Exception) -> LLMError:
        """Failures from a `client.chat.completions.create` call, redacted.

        What has to hold is redaction, not the two-way split. Exactly two sites
        in this class hand a *caught* exception to `LLMError`: this one and
        `_client_failed`. Both redact, which is what keeps a forgotten raise
        site from persisting the configured key into `source_chunks.error`.
        `OpenRouterAdapter` overrides neither and adds no third, so both
        carriers reach it unchanged. What it does override is `name` and
        `_base_url` -- the latter being what the `_client_failed` note below
        turns on, so this is not a subclass that merely renames things.

        Redaction is not universal outside that class boundary, though.
        Module-level `_render_prompt` in this file builds an `LLMError` around
        a caught exception with no redaction, a third carrier of caught text; it
        is harmless, but not for the reason the sentence this replaces gave.
        #500 widened its second clause, so what it carries is whatever the
        render raised. What holds instead is the shape of the function:
        module-level, taking a root path and a prompt id, so no key is in scope
        for it to put in a message, and a render dials nothing, so no provider
        response can arrive in it either.
        `openrouter_adapter.list_models` is the same shape -- a bare
        `LLMError(f"openrouter request failed: {exc}")`, in a module that
        imports no `redact_secret` at all -- and rests on the first half of that
        same argument, which its own docstring makes at length. The
        schema helpers three of the four generation methods call just past their
        guarded region -- `parse_facts`, `parse_query`, `parse_query_intent`;
        `answer_question` calls none, it strips the message text and returns --
        are unredacted too, and two of them are worse than a third kind.
        Measured, `parse_facts` and `parse_query_intent` copy what they were
        handed into the message they raise. `parse_query` does not: its two
        raise sites can carry a missing key name, a builtin `TypeError` phrase,
        or a JSON position -- unredacted, and bounded in a way `_render_prompt`
        above no longer is. For the other two the input is provider response
        text, and the threat model is the one `_require_key` sets out below,
        transposed: there an attacker-influenced endpoint -- `base_url` is
        caller-supplied -- echoes back a credential verinote never resolved, so
        `redact_secret` has nothing to match. Here the same endpoint echoes the
        key verinote *did* resolve, which `redact_secret` would match, down a
        path that never calls it. Measured, it survives into `llm/schema.py`'s
        "malformed fact object {item!r}" with no `***`. `_request_failed` covers
        that echo when it arrives as an *error*; as a parsable response it never
        meets a redactor. Pre-existing, and #514 rather than a docstring's
        business. That enumeration is not offered as complete. What it is for is
        the shape of the claim: "both carriers redact" is true of this class and
        not of the call graph around it, and stating only the first half is how
        the sentence this paragraph replaces went wrong.

        Everything else raising `LLMError` here uses a fixed string and carries
        nothing caught: `_require_key` and the `ImportError` clause in
        `_client`. Both fail before anything is dialled and neither is
        `_client_failed` -- which is the point of the hoist comment in
        `_client`, and the reason "before the request" cannot be the line
        between these two messages.

        Nor does this name promise timing, and it used to promise less. The
        prompt render was an ARGUMENT to the SDK call, evaluated inside the
        guarded region, so an override missing a required placeholder surfaced
        as "openai request failed" without a request (measured). #500 lifted it
        into a statement of its own -- below `client = self._client()`, above
        the `try` -- in the four methods here, and in `OpenRouterAdapter` by
        inheritance, which leaves the guarded region holding the SDK call and
        nothing else. Measured, the same broken override now reports "Datalog
        translation prompt must include required placeholder {qid}", with no
        "request failed" in front of it.

        What is left still reads as "the SDK call yielded no result" rather than
        "the provider answered"; `_client_failed` is the message that can speak
        about when. That is asserted here from the structure and from stubs,
        which is all this file is entitled to: the openai SDK is an optional
        dependency, absent in CI and in some development environments, so the
        vendor-behaviour measurement behind this claim lives in the anthropic
        twin of this paragraph and is not restated as if it had been taken here.
        What the hoist did change is that the prompt error -- the one measured
        case of this message covering something that never dialled -- no longer
        arrives.

        Rendering outside the guard is the shape `ollama_adapter` and
        `claude_cli_adapter` were already in, and it carries a hole this `try`
        was covering. `get_prompt` reads an override with `read_text`, so a
        hand-edited non-UTF-8 file raises `UnicodeDecodeError` and a mode bit
        raises `PermissionError`, and `_render_prompt`'s `except PromptError`
        converted neither. Measured on those two adapters before this change,
        that is sixteen method-and-condition pairs leaving the adapter as
        something that is not an `LLMError` -- §10.1 broken in the tree already,
        not by the hoist. So the hoist ships with the second clause added to
        `_render_prompt` in the same PR, which normalises the render's failures
        wherever the render sits.

        Redaction covers only the key this process knows about, which is why
        `_require_key` refuses to let the SDK authenticate with one it never saw.
        """
        return LLMError(redact_secret(f"{self.name} request failed: {exc}", self.cfg.api_key))

    def _client_failed(self, exc: Exception) -> LLMError:
        """The client could not be built, so nothing was ever dialled.

        Deliberately does NOT name the Base URL setting. A malformed `base_url`
        is the reachable cause this exists for (#493), but measured against the
        installed `openai` SDK it is not the only one: with `base_url` unset
        entirely, `SSL_CERT_FILE` pointing at a missing file raises
        `FileNotFoundError`, and `HTTPS_PROXY='::::'` or `OPENAI_BASE_URL='::::'`
        raise `httpx.InvalidURL`. Telling those users to check a field they left
        blank sends them to fix something that is not broken -- the misdirection
        #474 was reported as. The urllib adapters can be specific, and are,
        because `Request(url)` has no second cause; see `base_url_unusable`.

        `OpenRouterAdapter` inherits this, and one clause above cannot reach it:
        its `_base_url()` substitutes `OPENROUTER_DEFAULT_BASE_URL` for a blank
        field, so `base_url` is never unset there and the SDK's
        `OPENAI_BASE_URL` fallback is never consulted -- measured, the `::::`
        that raises with `base_url=None` raises nothing once that default is
        supplied. `SSL_CERT_FILE` and `HTTPS_PROXY` still do reach it, so the
        conclusion -- do not name the Base URL setting -- holds there too.
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
        client-wide setting) is applied at exactly one site. The base_url
        also makes this work against any OpenAI-compatible endpoint.
        """
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LLMError("openai SDK not installed; `pip install verinote[openai]`") from exc
        # Hoisted out of the constructor call, not merely out of the `try`: as an
        # ARGUMENT it would be evaluated inside the guarded region, and the
        # `LLMError` it raises for a missing key would come back out relabelled
        # "client could not be created" -- a config error reported as an SDK
        # failure. The region below must contain no statement that raises
        # `LLMError`, and this line is how that stays true.
        key = self._require_key()
        # Hoisted for uniformity rather than for a fix: `_base_url` cannot raise
        # today, so no mutant can prove this line necessary. It keeps the region
        # below to the constructor call alone, which makes the rule above
        # "nothing else lives here" instead of a per-line judgement about which
        # helper happens to be safe this month.
        base = self._base_url()
        try:
            return OpenAI(api_key=key, base_url=base, timeout=self.cfg.llm_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - normalise SDK construction errors
            raise self._client_failed(exc) from exc

    def _base_url(self) -> str | None:
        """The endpoint to dial. `None` lets the SDK use its own default.

        A seam, not indirection for its own sake: a subclass that IS one
        specific service overrides this so an unset `base_url` cannot silently
        resolve to `api.openai.com` and ship documents to a vendor the user did
        not choose. Keeping it here means there is still exactly one `OpenAI(...)`
        construction site.
        """
        return self.cfg.base_url

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        client = self._client()
        system = _with_schema_hint(_render_prompt(self.cfg.root, "extraction"), schema_hint)
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": source_text},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "facts", "schema": FACT_ARRAY_SCHEMA, "strict": True},
                },
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        return parse_facts(resp.choices[0].message.content or "")

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        client = self._client()
        system = _with_schema_hint(
            _render_prompt(self.cfg.root, "query-translation", qid=qid), schema_hint
        )
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": question},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "query", "schema": QUERY_SCHEMA, "strict": True},
                },
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        return parse_query(resp.choices[0].message.content or "")

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> QueryIntent:
        client = self._client()
        system = _with_schema_hint(_render_prompt(self.cfg.root, "query-intent"), schema_hint)
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {"role": "system", "content": system},
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
            raise self._request_failed(exc) from exc

        return parse_query_intent(resp.choices[0].message.content or "")

    def answer_question(self, *, question: str, context: str) -> str:
        client = self._client()
        system = _render_prompt(self.cfg.root, "ask-fallback")
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": f"Question:\n{question}\n\nContext:\n{context}",
                    },
                ],
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 - normalise provider errors
            raise self._request_failed(exc) from exc

        return (resp.choices[0].message.content or "").strip()


def _with_schema_hint(prompt: str, schema_hint: str) -> str:
    return prompt + ("\n" + schema_hint if schema_hint else "")


def _render_prompt(root, prompt_id: str, **values: object) -> str:
    """Render `prompt_id` under `root`, or raise `LLMError`.

    Two clauses, and their order is the design. `PromptError` is the prompt
    contract stated back to the user -- a required placeholder their override
    left out -- so it goes out as the library wrote it, with nothing in front
    of it. The clause below names the operation first, because what that one
    catches is not always a sentence a user can act on.

    Being that wide relabels a genuine programming error as something that
    reads like a broken file: `prompt <id> could not be loaded` points at
    `policy/prompts/<id>.md`, and for a `TypeError` raised inside
    `render_prompt` that file is fine. It is the same widening
    `test_a_non_valueerror_from_the_request_constructor_is_not_blamed_on_the_base_url`
    in `tests/test_ollama_adapter.py` refuses for `Request()` -- refused there
    for a reason that does not hold here. `Request()` has one reachable
    failure, so a type tells a real cause and a bug apart. `render_prompt`
    reads two files, the packaged default and the override, and the `OSError`
    family those reads can raise is not closed by a list; #500's reviewer
    rejected enumerating it. §10.1 -- every LLM failure reaches its caller as
    an `LLMError` -- wins that trade at the adapter seam, because what the
    narrow clause alone lets past is a `UnicodeDecodeError` from a hand-edited
    override or a `PermissionError` from a mode bit, escaping as itself.
    `claude_cli_adapter._invoke`'s `except OSError` is the precedent for a
    clause this wide: "ONE clause, out here", justified by what the caught type
    is rather than by a list of the values it can carry. `from exc` pays for
    the trade -- the original exception stays on `__cause__` for a log.

    `except Exception` reaches neither `KeyboardInterrupt` nor `SystemExit`.
    """
    try:
        return render_prompt(root, prompt_id, **values)
    except PromptError as exc:
        raise LLMError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - normalise every render failure
        raise LLMError(f"prompt {prompt_id} could not be loaded: {exc}") from exc
