# SPDX-License-Identifier: MPL-2.0
"""Ollama adapter — fully local, no cloud vendor. Uses Ollama's JSON format mode.

This adapter is the proof that anti-lock-in is real: with a local model the whole
pipeline runs offline, and the DuckDB-backed verifier still guarantees correctness.
"""

from __future__ import annotations

import json
import urllib.request

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError, ModelListing
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
    req = urllib.request.Request(f"{root}/api/tags")
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
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

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
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

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
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc

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
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - local trusted endpoint
                req, timeout=self.cfg.llm_timeout_seconds
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
            raise LLMError(f"ollama request failed: {exc}") from exc
        return str(body.get("message", {}).get("content", "")).strip()


def _with_schema_hint(prompt: str, schema_hint: str) -> str:
    return prompt + ("\n" + schema_hint if schema_hint else "")


def _render_prompt(root, prompt_id: str, **values: object) -> str:
    try:
        return render_prompt(root, prompt_id, **values)
    except PromptError as exc:
        raise LLMError(str(exc)) from exc
