# SPDX-License-Identifier: MPL-2.0
import sys
from types import SimpleNamespace

import pytest

from verinote.config import Config
from verinote.llm.anthropic_adapter import AnthropicAdapter
from verinote.llm.base import LLMError
from verinote.llm.openai_adapter import OpenAIAdapter
from verinote.prompts import save_prompt_override


def _cfg(tmp_path, *, provider: str) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider=provider,
        model="model",
        api_key="key",
        base_url=None,
    )


def test_openai_adapter_uses_kb_prompt_override(tmp_path, monkeypatch):
    calls = []
    save_prompt_override(tmp_path, "extraction", "Custom cloud extraction prompt.")

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"facts":[]}'))]
            )

    fake_module = SimpleNamespace(
        OpenAI=lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions())
        )
    )
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    OpenAIAdapter(_cfg(tmp_path, provider="openai")).extract_facts(source_text="x")

    assert calls[0]["messages"][0]["content"].startswith(
        "Custom cloud extraction prompt."
    )


def test_openai_adapter_prompt_validation_error_is_llm_error(tmp_path, monkeypatch):
    path = tmp_path / "policy" / "prompts" / "query-translation.md"
    path.parent.mkdir(parents=True)
    path.write_text("Missing qid placeholder.\n", encoding="utf-8")
    fake_module = SimpleNamespace(
        OpenAI=lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: None))
        )
    )
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    with pytest.raises(LLMError, match="\\{qid\\}"):
        OpenAIAdapter(_cfg(tmp_path, provider="openai")).translate_query(
            question="Who?", qid=3
        )


def test_anthropic_adapter_uses_kb_prompt_override(tmp_path, monkeypatch):
    calls = []
    save_prompt_override(tmp_path, "query-intent", "Custom intent prompt.")

    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        input={
                            "kind": "unknown_or_unsupported",
                            "subject": None,
                            "relation": None,
                            "object": None,
                            "relation_candidates": None,
                            "operator": None,
                            "value_type": None,
                            "value": None,
                            "reason": "unsupported",
                        },
                    )
                ]
            )

    fake_module = SimpleNamespace(
        Anthropic=lambda **kwargs: SimpleNamespace(messages=_Messages())
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    AnthropicAdapter(_cfg(tmp_path, provider="anthropic")).extract_query_intent(
        question="What?"
    )

    assert calls[0]["system"].startswith("Custom intent prompt.")


def test_anthropic_adapter_prompt_validation_error_is_llm_error(tmp_path, monkeypatch):
    path = tmp_path / "policy" / "prompts" / "query-translation.md"
    path.parent.mkdir(parents=True)
    path.write_text("Missing qid placeholder.\n", encoding="utf-8")
    fake_module = SimpleNamespace(
        Anthropic=lambda **kwargs: SimpleNamespace(
            messages=SimpleNamespace(create=lambda **_: None)
        )
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    with pytest.raises(LLMError, match="\\{qid\\}"):
        AnthropicAdapter(_cfg(tmp_path, provider="anthropic")).translate_query(
            question="Who?", qid=3
        )
