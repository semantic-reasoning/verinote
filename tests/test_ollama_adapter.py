# SPDX-License-Identifier: MPL-2.0
import json
from types import SimpleNamespace

from verinote.config import Config
from verinote.llm.ollama_adapter import OllamaAdapter


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
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        return json.dumps(
            {
                "message": {
                    "content": json.dumps(
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
    assert payload["options"] == {"temperature": 0}
    assert facts[0].subject == "Ada"
