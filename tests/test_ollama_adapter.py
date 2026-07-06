# SPDX-License-Identifier: MPL-2.0
import json
from types import SimpleNamespace

import pytest

from verinote.config import Config
from verinote.llm.base import LLMError
from verinote.llm.ollama_adapter import OllamaAdapter
from verinote.prompts import save_prompt_override


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
    assert payload["format"]["type"] == "array"
    assert payload["format"]["items"]["properties"]["subject"] == {"type": "string"}
    assert "document chunk" in payload["messages"][0]["content"]
    assert "up to 8 facts" in payload["messages"][0]["content"]
    assert "semantic subject-predicate-object statement" in payload["messages"][0]["content"]
    assert "instead of copying whole source phrases" in payload["messages"][0]["content"]
    assert "co-occurrence in the same chunk" in payload["messages"][0]["content"]
    assert "same local evidence record" in payload["messages"][0]["content"]
    assert "key-value or label-value text" in payload["messages"][0]["content"]
    assert "use relation `value`" in payload["messages"][0]["content"]
    assert "Do not use `is_a` unless" in payload["messages"][0]["content"]
    assert facts[0].subject == "Ada"


def test_ollama_extract_ignores_malformed_only_fact_payload(tmp_path, monkeypatch):
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

    assert OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada") == []


def test_ollama_extract_ignores_schema_mismatch_payload(tmp_path, monkeypatch):
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

    assert OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada") == []


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
