# SPDX-License-Identifier: MPL-2.0
import sys
from types import SimpleNamespace

import pytest

from verinote.config import Config, save_settings
from verinote.llm.anthropic_adapter import AnthropicAdapter
from verinote.llm.base import LLMError
from verinote.llm.openai_adapter import OpenAIAdapter
from verinote.prompts import save_prompt_override


def _cfg(tmp_path, *, provider: str, llm_timeout_seconds: float = 600.0) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider=provider,
        model="model",
        api_key="key",
        base_url=None,
        llm_timeout_seconds=llm_timeout_seconds,
    )


# A distinctive value that is neither the 600.0 default nor the 180 the CLI
# adapter used to hardcode, so a passing assertion can only mean the configured
# timeout actually reached the client.
_TIMEOUT = 1234.0

_INTENT = {
    "kind": "unknown_or_unsupported",
    "subject": None,
    "relation": None,
    "object": None,
    "relation_candidates": None,
    "operator": None,
    "value_type": None,
    "value": None,
    "reason": "unsupported",
}

_DATALOG = 'answer_q1(V) :- relation(V, "is_a", "x").'

# One invocation per LLM method; each site constructs its own client, so a
# single-method test would leave the other three sites unguarded.
_INVOCATIONS = {
    "extract_facts": lambda a: a.extract_facts(source_text="x"),
    "translate_query": lambda a: a.translate_query(question="Who?", qid=1),
    "extract_query_intent": lambda a: a.extract_query_intent(question="What?"),
    "answer_question": lambda a: a.answer_question(question="q", context="c"),
}


def _openai_content(method: str) -> str:
    import json

    return {
        "extract_facts": '{"facts":[]}',
        "translate_query": json.dumps({"datalog": _DATALOG}),
        "extract_query_intent": json.dumps(_INTENT),
        "answer_question": "ok",
    }[method]


def _anthropic_content(method: str):
    tool_input = {
        "extract_facts": {"facts": []},
        "translate_query": {"datalog": _DATALOG},
        "extract_query_intent": _INTENT,
    }
    if method in tool_input:
        return [SimpleNamespace(type="tool_use", input=tool_input[method])]
    return [SimpleNamespace(type="text", text="ok")]


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_openai_adapter_applies_configured_timeout(tmp_path, monkeypatch, method):
    recorded: dict = {}
    content = _openai_content(method)

    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    def _factory(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_factory))

    adapter = OpenAIAdapter(_cfg(tmp_path, provider="openai", llm_timeout_seconds=_TIMEOUT))
    _INVOCATIONS[method](adapter)

    assert recorded["timeout"] == _TIMEOUT


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_anthropic_adapter_applies_configured_timeout(tmp_path, monkeypatch, method):
    recorded: dict = {}
    content = _anthropic_content(method)

    class _Messages:
        def create(self, **kwargs):
            return SimpleNamespace(content=content)

    def _factory(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(messages=_Messages())

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=_factory))

    adapter = AnthropicAdapter(_cfg(tmp_path, provider="anthropic", llm_timeout_seconds=_TIMEOUT))
    _INVOCATIONS[method](adapter)

    assert recorded["timeout"] == _TIMEOUT


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


def _record_openai_client(monkeypatch) -> dict:
    recorded: dict = {}

    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"facts":[]}'))]
            )

    def _factory(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_factory))
    return recorded


def _record_anthropic_client(monkeypatch) -> dict:
    recorded: dict = {}

    class _Messages:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"facts": []})])

    def _factory(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(messages=_Messages())

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=_factory))
    return recorded


_CLOUD_ADAPTERS = {
    "openai": (_record_openai_client, OpenAIAdapter),
    "anthropic": (_record_anthropic_client, AnthropicAdapter),
}


@pytest.mark.parametrize("provider", sorted(_CLOUD_ADAPTERS))
def test_empty_base_url_env_reaches_cloud_client_as_none(tmp_path, monkeypatch, provider):
    # `is None`, not falsy: an empty string is falsy too, so a truthiness check
    # here would pass against the very bug this guards.
    record, adapter_cls = _CLOUD_ADAPTERS[provider]
    recorded = record(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", provider)
    monkeypatch.setenv("VERINOTE_BASE_URL", "")

    adapter_cls(Config.for_root(tmp_path)).extract_facts(source_text="x")

    assert recorded["base_url"] is None


@pytest.mark.parametrize("provider", sorted(_CLOUD_ADAPTERS))
def test_custom_base_url_reaches_cloud_client_verbatim(tmp_path, monkeypatch, provider):
    record, adapter_cls = _CLOUD_ADAPTERS[provider]
    recorded = record(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", provider)
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/v1")

    adapter_cls(Config.for_root(tmp_path)).extract_facts(source_text="x")

    assert recorded["base_url"] == "https://llm.internal/v1"


@pytest.mark.parametrize("provider", sorted(_CLOUD_ADAPTERS))
def test_settings_file_base_url_reaches_cloud_client(tmp_path, monkeypatch, provider):
    # No env at all: a self-hosted endpoint configured through the Settings UI
    # must still reach the SDK.
    record, adapter_cls = _CLOUD_ADAPTERS[provider]
    recorded = record(monkeypatch)
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider=provider, model="m", base_url="https://llm.internal/v1")

    adapter_cls(Config.for_root(tmp_path)).extract_facts(source_text="x")

    assert recorded["base_url"] == "https://llm.internal/v1"
