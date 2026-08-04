# SPDX-License-Identifier: MPL-2.0
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from verinote.config import Config, save_settings
from verinote.engine.terms import Compound, NumberLit
from verinote.llm.base import LLMError
from verinote.llm.ollama_adapter import OllamaAdapter
from verinote.llm.schema import FACT_ARRAY_SCHEMA
from verinote.pipeline import extract_source
from verinote.prompts import save_prompt_override
from verinote.store import Store


def _cfg(tmp_path, *, timeout: float = 900.0) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="ollama",
        model="qwen3:8b",
        api_key=None,
        base_url="http://localhost:11434",
        llm_timeout_seconds=timeout,
    )


class _Response:
    def __init__(self, content=None):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        content = self.content
        if content is None:
            content = json.dumps(
                {
                    "facts": [
                        {
                            "subject": {"kind": "string", "value": "Ada"},
                            "relation": {"kind": "string", "value": "is_a"},
                            "object": {"kind": "string", "value": "person"},
                            "confidence": 0.9,
                            "note": "",
                        }
                    ]
                }
            )
        return json.dumps(
            {
                "message": {
                    "content": content
                }
            }
        ).encode("utf-8")


def test_ollama_extract_uses_configured_timeout(tmp_path, monkeypatch):
    calls = []

    def fake_urlopen(req, *, timeout):
        calls.append(SimpleNamespace(req=req, timeout=timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    facts = OllamaAdapter(_cfg(tmp_path, timeout=900.0)).extract_facts(source_text="Ada")

    assert calls[0].timeout == 900.0
    payload = json.loads(calls[0].req.data.decode("utf-8"))
    assert payload["think"] is False
    assert payload["options"] == {"temperature": 0, "num_predict": 1800}
    assert payload["format"] == FACT_ARRAY_SCHEMA
    assert "document chunk" in payload["messages"][0]["content"]
    assert "up to 8 facts" in payload["messages"][0]["content"]
    assert "Do not summarize" in payload["messages"][0]["content"]
    assert "Traverse every visible section, table, list" in payload["messages"][0]["content"]
    assert "Do not sample representative rows" in payload["messages"][0]["content"]
    assert "Extract them row by row" in payload["messages"][0]["content"]
    assert "Use the row-identifying key" in payload["messages"][0]["content"]
    assert "semantic subject-predicate-object statement" in payload["messages"][0]["content"]
    assert "instead of copying whole source phrases" in payload["messages"][0]["content"]
    assert "merely because two entities appear in the same chunk" in payload["messages"][0]["content"]
    assert "same local evidence record" in payload["messages"][0]["content"]
    assert "`date(YYYY)`" in payload["messages"][0]["content"]
    assert "`amount(N,\"unit\")`" in payload["messages"][0]["content"]
    assert "Typed literals are object values, never subjects or relations" in payload["messages"][0]["content"]
    assert '`{"kind":"term","value":"..."}`' in payload["messages"][0]["content"]
    assert "relation `number(8)` and object `명`" in payload["messages"][0]["content"]
    assert "key-value or label-value text" in payload["messages"][0]["content"]
    assert "use relation `value`" in payload["messages"][0]["content"]
    assert "Do not use `is_a` unless" in payload["messages"][0]["content"]
    assert "Do not include source, status, CSV headers" in payload["messages"][0]["content"]
    assert facts[0].subject == "Ada"


def test_ollama_extract_preserves_explicit_compound_term_slots(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                {
                    "facts": [
                        {
                            "subject": {"kind": "string", "value": "Example Corp"},
                            "relation": {"kind": "string", "value": "founded_on"},
                            "object": {"kind": "term", "value": "date(2024,1,2)"},
                            "confidence": 0.9,
                            "note": "",
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    fact = OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Example Corp was founded on 2024-01-02.")[0]

    assert fact.object == "date(2024,1,2)"
    assert fact.object_kind == "term"

    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    assert (
        extract_source(
            store,
            OllamaAdapter(_cfg(tmp_path)),
            source_path="sources/example.txt",
            source_text="Example Corp was founded on 2024-01-02.",
        )
        == 1
    )
    stored = store.facts()[0]
    assert store.get_fact_terms(stored["id"])[2] == Compound(
        "date", (NumberLit(2024), NumberLit(1), NumberLit(2))
    )


def test_ollama_extract_raises_on_malformed_only_fact_payload(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                [
                    {
                        "subject": "Ada",
                        "relation": "is_a",
                        "object": None,
                        "confidence": 0.9,
                        "note": "",
                    }
                ]
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(LLMError, match="malformed fact object"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def test_ollama_extract_raises_on_schema_mismatch_payload(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                {
                    "subject": "Ada",
                    "relation": "is_a",
                    "object": "mathematician",
                    "confidence": 0.9,
                    "note": "",
                }
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(LLMError, match="extractor output did not match schema"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def test_ollama_extract_raises_on_mixed_valid_and_malformed_payload(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                [
                    {
                        "subject": "Ada",
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                    {
                        "subject": [],
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                ]
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(LLMError, match="malformed fact object"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def test_ollama_extract_uses_kb_prompt_override(tmp_path, monkeypatch):
    calls = []
    save_prompt_override(
        tmp_path,
        "ollama-extraction",
        "Custom local extraction prompt capped at {max_facts} facts.",
    )

    def fake_urlopen(req, *, timeout):
        calls.append(req)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")

    payload = json.loads(calls[0].data.decode("utf-8"))
    assert payload["messages"][0]["content"].startswith(
        "Custom local extraction prompt capped at 8 facts."
    )


def test_ollama_prompt_validation_error_is_llm_error(tmp_path):
    path = tmp_path / "policy" / "prompts" / "ollama-extraction.md"
    path.parent.mkdir(parents=True)
    path.write_text("Missing required placeholder.\n", encoding="utf-8")

    with pytest.raises(LLMError, match="\\{max_facts\\}"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def _record_request_url(monkeypatch) -> list:
    """Capture the URL actually requested, not the adapter's attribute."""
    urls = []

    def fake_urlopen(req, *, timeout):
        urls.append(req.full_url)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return urls


def test_empty_base_url_env_still_requests_localhost(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["http://localhost:11434/api/chat"]


def test_custom_base_url_env_reaches_the_request_url(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/v1")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/v1/api/chat"]


def test_trailing_slash_is_still_stripped(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/api/chat"]


def test_settings_file_base_url_reaches_the_request_url(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider="ollama", model="qwen3:8b", base_url="https://llm.internal/v1")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/v1/api/chat"]


def test_whitespace_only_saved_base_url_still_requests_localhost(tmp_path, monkeypatch):
    # Without normalising the saved source too, this requests '   /api/chat'.
    urls = _record_request_url(monkeypatch)
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider="ollama", model="qwen3:8b", base_url="   ")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["http://localhost:11434/api/chat"]


def test_padded_base_url_env_does_not_break_the_request_url(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "  https://llm.internal/v1  ")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/v1/api/chat"]


# --- list_models: the settings picker's source of truth ---


class _TagsResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _tags(monkeypatch, payload):
    """Answer /api/tags with `payload`; return the recorded (url, timeout) calls."""
    calls = []

    def fake_urlopen(req, *, timeout):
        calls.append(SimpleNamespace(url=req.full_url, timeout=timeout))
        return _TagsResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def test_list_models_returns_sorted_unique_names_from_tags(tmp_path, monkeypatch):
    calls = _tags(
        monkeypatch,
        {
            "models": [
                {"name": "qwen3:8b"},
                {"name": "llava:7b"},
                {"name": "  qwen3:8b  "},  # same model, padded
                {"name": "   "},  # no usable id
                {"no_name": 1},
                "not-an-object",
            ]
        },
    )

    models = OllamaAdapter(_cfg(tmp_path)).list_models()

    assert models == ["llava:7b", "qwen3:8b"]
    assert calls[0].url == "http://localhost:11434/api/tags"


def test_list_models_honours_the_configured_base_url(tmp_path, monkeypatch):
    calls = _tags(monkeypatch, {"models": []})

    OllamaAdapter(replace(_cfg(tmp_path), base_url="http://llm.internal:9999/")).list_models()

    assert calls[0].url == "http://llm.internal:9999/api/tags"


def test_list_models_is_bounded_far_below_the_generation_timeout(tmp_path, monkeypatch):
    """A page-load call must not inherit the minutes-long completion budget."""
    calls = _tags(monkeypatch, {"models": []})

    OllamaAdapter(_cfg(tmp_path, timeout=900.0)).list_models()

    assert calls[0].timeout == 5.0


def test_list_models_keeps_an_even_shorter_configured_timeout(tmp_path, monkeypatch):
    """The bound is the *smaller* of the two, so a tighter user setting still wins."""
    calls = _tags(monkeypatch, {"models": []})

    OllamaAdapter(_cfg(tmp_path, timeout=1.5)).list_models()

    assert calls[0].timeout == 1.5


def test_list_models_returns_empty_for_a_server_with_nothing_pulled(tmp_path, monkeypatch):
    """Reachable-but-empty is data, not an error: the caller must be able to say
    'this server has no models' rather than 'this server could not be reached'."""
    _tags(monkeypatch, {"models": []})

    assert OllamaAdapter(_cfg(tmp_path)).list_models() == []


def test_list_models_raises_on_transport_failure(tmp_path, monkeypatch):
    def boom(req, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(LLMError, match="ollama request failed"):
        OllamaAdapter(_cfg(tmp_path)).list_models()


@pytest.mark.parametrize("payload", [{"models": "qwen3:8b"}, {}, ["qwen3:8b"]])
def test_list_models_raises_on_schema_mismatch(tmp_path, monkeypatch, payload):
    """A shape that is not {'models': [...]} is a failure, never a silent empty
    list -- that would render as 'no models installed' and blame the user."""
    _tags(monkeypatch, payload)

    with pytest.raises(LLMError, match="did not match schema"):
        OllamaAdapter(_cfg(tmp_path)).list_models()
