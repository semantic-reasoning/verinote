# SPDX-License-Identifier: MPL-2.0
"""Ollama adapter — fully local, no cloud vendor. Uses Ollama's JSON format mode.

This adapter is the proof that anti-lock-in is real: with a local model the whole
pipeline runs offline, and the DuckDB-backed verifier still guarantees correctness.
"""

from __future__ import annotations

import json
import urllib.request

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError, ModelListing, base_url_unusable
from verinote.llm.schema import (
    FACT_ARRAY_SCHEMA,
    QUERY_INTENT_SCHEMA,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
)
from verinote.pipeline.query_intent import QueryIntent, parse_query_intent
from verinote.prompts import PromptError, render_prompt

# The endpoint an unset `base_url` resolves to. Named so the settings UI can
# report the *same* URL it will actually talk to instead of printing "(default)".
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"


def list_models(base_url: str | None, timeout: float) -> ModelListing:
    """Model ids this Ollama server has installed, sorted, deduplicated.

    A module-level function taking a URL and a timeout, deliberately NOT a
    method on `OllamaAdapter`. The settings picker dials an endpoint the caller
    supplied in a query string, and a method would have `self.cfg.api_key` in
    reach — so "this listing sends no key" would be a property of what the body
    happens to read today, which an edit can change without review. Taking no
    `Config` at all moves it into the signature, where a reviewer sees it. The web
    layer checks that shape at import for every lister in its shipped table — not
    at dispatch, and it constrains only what a lister is handed, never what a body
    can reach for on its own; see `_MODEL_LISTERS` for what that does and does not
    buy. The timeout ceiling this parameter is clamped against lives there too,
    because that dispatch is the last caller still holding a `Config`.

    `structured_output_ids` is left `None`: `/api/tags` reports a name, a size
    and a digest per model and nothing about capabilities, so this listing has
    no answer to give. `None` says exactly that, and is not the same as the
    empty set a listing that *does* report the property and found none would
    return — see `ModelListing`.

    Raises `LLMError` on any transport or shape failure rather than
    returning an empty listing — no models means "this server has no models
    pulled", and a caller must be able to tell that apart from "the server
    could not be reached" (which the settings UI reports verbatim instead of
    showing an empty picker).
    """
    root = (base_url or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
    # The settings picker calls this with whatever is typed in the Base URL box,
    # unsaved, so a half-typed URL reaches `Request` on every keystroke that
    # triggers a refresh. Left to escape, that is a 500 from the model-field
    # endpoint instead of the banner the page already knows how to render.
    url = f"{root}/api/tags"
    try:
        req = urllib.request.Request(url)
    except ValueError as exc:
        raise base_url_unusable("ollama", url, exc) from exc
    try:
        with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
            req, timeout=timeout
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
        raise LLMError(f"ollama request failed: {exc}") from exc

    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        raise LLMError("ollama model list did not match schema: expected {'models': [...]}")
    names = {
        entry["name"].strip()
        for entry in body["models"]
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"].strip()
    }
    return ModelListing(models=tuple(sorted(names)))


class OllamaAdapter:
    name = "ollama"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or OLLAMA_DEFAULT_BASE_URL).rstrip("/")

    def _post_chat(self, payload: dict) -> dict:
        """POST one chat payload to `/api/chat` and return the decoded body.

        The single request site for all four methods, so which statements count
        as "the request" -- and which are the caller's own parse, reported as the
        caller's own failure -- is written once instead of four times in
        parallel.

        `Request(...)` gets its own narrow clause rather than joining the one
        below: a URL that cannot be built is not a request that failed, and
        telling a user their server did not answer when nothing was ever dialled
        sends them to the wrong place (#493).
        """
        # Hoisted out of the `Request(...)` call, not merely out of the `try`:
        # as an ARGUMENT it would be evaluated inside the guarded region, so a
        # `ValueError` from serialising the payload would come back out labelled
        # "base URL is unusable" — a bug in this file blamed on the user's
        # setting. The region below must contain nothing but the URL parse.
        data = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}/api/chat"
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
        except ValueError as exc:
            raise base_url_unusable(self.name, url, exc) from exc
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        system = _with_schema_hint(
            _render_prompt(
                self.cfg.root,
                "ollama-extraction",
                max_facts=self.cfg.extraction_max_facts_per_chunk,
            ),
            schema_hint,
        )
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            # Terms are structural only when their slot explicitly says so.
            # The common schema preserves that distinction for local extraction
            # instead of storing compound-looking values as string literals.
            "format": FACT_ARRAY_SCHEMA,
            "options": {"temperature": 0, "num_predict": 1800},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": source_text},
            ],
        }
        body = self._post_chat(payload)

        return parse_facts(body.get("message", {}).get("content", ""))

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        system = _with_schema_hint(
            _render_prompt(self.cfg.root, "query-translation", qid=qid), schema_hint
        )
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            "format": QUERY_SCHEMA,
            "options": {"temperature": 0, "num_predict": 512},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }
        body = self._post_chat(payload)

        return parse_query(body.get("message", {}).get("content", ""))

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> QueryIntent:
        system = _with_schema_hint(
            _render_prompt(self.cfg.root, "query-intent"), schema_hint
        )
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            "format": QUERY_INTENT_SCHEMA,
            "options": {"temperature": 0, "num_predict": 512},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        }
        body = self._post_chat(payload)

        return parse_query_intent(body.get("message", {}).get("content", ""))

    def answer_question(self, *, question: str, context: str) -> str:
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 1200},
            "messages": [
                {"role": "system", "content": _render_prompt(self.cfg.root, "ask-fallback")},
                {
                    "role": "user",
                    "content": f"Question:\n{question}\n\nContext:\n{context}",
                },
            ],
        }
        body = self._post_chat(payload)
        return str(body.get("message", {}).get("content", "")).strip()


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
