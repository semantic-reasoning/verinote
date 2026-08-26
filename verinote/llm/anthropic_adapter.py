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
)
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
        than one. Module-level `_render_prompt` builds an `LLMError` around a
        caught exception and does NOT redact. The sentence this replaces called
        it harmless *today*, on the ground that it caught only `PromptError`;
        #500 widened its second clause, so the ground is gone and the claim has
        to be narrowed rather than un-hedged. What it carries is now whatever
        the render raised, `str(exc)` and no filter, and that set is open: an
        `OSError` from the override read puts the KB's absolute path in the
        message in full, `cfg.root` and all (measured), and a key a user spelled
        into a KB directory name is part of that path.

        Which is why nothing in this class calls it directly. `_rendered` above
        wraps it, and that wrapper is a member of the class precisely so it can
        hand `self.cfg.api_key` to `redact_secret` -- the four generation
        methods reach the render only through it. Measured on this branch, the
        twelve cells that carry a path (this adapter, `openai`, `openrouter`,
        four methods each, an unreadable override) come out `.../kb-***/...`
        again, which is what `759eac0` did before the hoist and what the hoist
        alone stopped doing. Whether `_render_prompt` itself redacts is
        therefore not the question here; it does not, and it is not called from
        anywhere in this file that a key can reach.

        `ollama_adapter` and `claude_cli_adapter` are the other story. They call
        their copies of `_render_prompt` bare, they import no `redact_secret`,
        and they never masked this -- on `759eac0` the same override left them
        as a raw `PermissionError` with the key in it, and they leak it as an
        `LLMError` now. That is untouched residue rather than anything this
        change moved, and it is what the consolidation follow-up is for.

        The schema helpers three of the four generation methods call just past
        their guarded region -- `parse_facts`, `parse_query`,
        `parse_query_intent`; `answer_question` calls none, it joins the
        stripped text blocks -- do not redact either, and two of the
        three are not harmless. Measured, `parse_facts` and `parse_query_intent`
        put what they were handed into the message they raise; `parse_query`
        cannot -- its two raise sites carry a missing key name, a builtin
        `TypeError` phrase, or a JSON position, and nothing of the payload,
        which is a boundedness `_render_prompt` above no longer has. What those
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

        Rendering outside the guard is the shape `ollama_adapter` and
        `claude_cli_adapter` were already in, and it carries a hole this `try`
        was covering. `get_prompt` reads an override with `read_text`, so a
        hand-edited non-UTF-8 file raises `UnicodeDecodeError` and a mode bit
        raises `PermissionError`, and `_render_prompt`'s `except PromptError`
        converted neither. Measured on those two adapters before this change,
        that is eight method-and-condition pairs on each of them -- sixteen
        cells -- leaving the adapter as something that is not an `LLMError`,
        which is §10.1 broken in the tree already and not by the hoist. So the
        hoist ships with the second clause added to
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

    def _rendered(self, prompt_id: str, **values: object) -> str:
        """Render a prompt, and put the result under the redacting constructor.

        Two layers, and they are not the same job. Module-level `_render_prompt`
        normalises: every render failure leaves it as an `LLMError`, which is
        §10.1, and all four adapters need it. This wraps that in the one thing a
        module-level function cannot do -- reach `self.cfg.api_key` -- and it
        exists only here and in `openai_adapter`, because those are the two
        classes that redact at all. Nothing else changes: no new exception type,
        no new message shape, only whether a configured key survives in the text.

        It is here because #500 took it away. Before the hoist the render was an
        ARGUMENT to the SDK call, so a `PermissionError` on the override went
        through `_request_failed`, which redacts (measured on `759eac0`: the KB
        path came out as `.../kb-***/policy/prompts/extraction.md`). Lifting the
        render above the `try` moved that raise site out from under the redactor
        while `_render_prompt` gained a message that carries `str(exc)` whole --
        and an `OSError` from the override read spells out `cfg.root`. A key a
        user put in their KB directory name then rode into
        `source_chunks.error`. Twelve cells: three adapters, four methods, and
        the conditions whose exception text carries a path. A non-UTF-8 override
        is not one of them -- `UnicodeDecodeError` names a byte offset and no
        file -- so the fix is not "the render leaks", it is that these twelve
        stopped being redacted and are redacted again.

        NO `from` CLAUSE, because there is nothing new to chain to.
        `_render_prompt` already hung the original failure on `__cause__` and
        `test_a_render_failure_keeps_the_original_error_as_the_cause` asserts on
        it; re-raising the same object carries that cause forward untouched.
        Replacing this with `raise LLMError(...) from exc` is the way to make
        that test fail -- it would bury the original behind a new wrapper.
        """
        try:
            return _render_prompt(self.cfg.root, prompt_id, **values)
        except LLMError as exc:
            # #592. Relabelled in place, not reconstructed, which is the shape
            # `parsed_under_redaction` settled on and the reason is the same:
            # rebuilding an exception from its message alone destroys the class
            # and any state it carries. Preserving the class here is a NO-OP
            # today -- measured: this `try` calls only `_render_prompt`, which
            # raises bare `LLMError` at both of its prompt-loading exits, so
            # `LLMError` is the only class that can arrive. It is written this
            # way anyway, because the day `_render_prompt` grows a subclass exit
            # the alternative swallows it with no test red anywhere.
            exc.args = (redact_secret(str(exc), self.cfg.api_key),)
            raise

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        client = self._client()
        tool = {
            "name": "emit_facts",
            "description": "Return the extracted facts.",
            "input_schema": FACT_ARRAY_SCHEMA,
        }
        system = _with_schema_hint(self._rendered("extraction"), schema_hint)
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
            self._rendered("query-translation", qid=qid), schema_hint
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
        system = _with_schema_hint(self._rendered("query-intent"), schema_hint)
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
        system = self._rendered("ask-fallback")
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


def _render_prompt(root, prompt_id: str, **values: object) -> str:
    """Render `prompt_id` under `root`, or raise `LLMError`.

    Two clauses, and their order is the design. `PromptError` is whatever the
    prompt library states in its own words -- a required placeholder the
    override left out, an id nothing defines (`unknown prompt: extractoin`), a
    value the caller never passed (`missing prompt value: qid`) -- so it goes
    out as written, with nothing in front of it. Only the first of those three
    is the user's doing; the other two are measured, and they reach the user
    through the narrow clause with no operation named at all. The clause below
    names the operation because what *it* catches is further still from a
    sentence anybody can act on.

    Being that wide relabels a genuine programming error as something that
    reads like a broken file: `prompt <id> could not be loaded` points at
    `policy/prompts/<id>.md`, and for a `TypeError` raised inside
    `render_prompt` that file is fine. It is the same widening
    `test_a_non_valueerror_from_the_request_constructor_is_not_blamed_on_the_base_url`
    in `tests/test_ollama_adapter.py` refuses for `Request()` -- refused there
    for a reason that does not hold here. `Request()` has one reachable
    failure, so a type tells a real cause and a bug apart. `render_prompt`
    reads two files, the packaged default and the override, and the `OSError`
    family those reads can raise is not closed by a list; #500's reviewer said
    normalising the region is safer than enumerating it, and offered no list as
    complete. §10.1 -- every LLM failure reaches its caller as an `LLMError` --
    wins that trade at the adapter seam, because what the narrow clause alone
    lets past is a `UnicodeDecodeError` from a hand-edited override or a
    `PermissionError` from a mode bit, escaping as itself.
    `claude_cli_adapter._invoke` is the nearest precedent, and only for the
    *form*: one `except OSError` "out here" rather than a copy per call site,
    broad "where the `ValueError` above may not". Its reason does not carry
    over -- `OSError` is not a domain type in this repo, and `except Exception`
    catches every domain type there is. `from exc` pays for the trade -- the
    original exception stays on `__cause__` for a log.

    `except Exception` reaches neither `KeyboardInterrupt` nor `SystemExit`.
    """
    try:
        return render_prompt(root, prompt_id, **values)
    except PromptError as exc:
        raise LLMError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - normalise every render failure
        raise LLMError(f"prompt {prompt_id} could not be loaded: {exc}") from exc
