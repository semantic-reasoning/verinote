# SPDX-License-Identifier: MPL-2.0
"""OpenRouter reuses every OpenAI generation path, so the tests here cover only
what is NOT inherited: which endpoint gets dialled, and which provider a failure
names. Re-testing the four inherited methods against a stub would pass with the
endpoint binding deleted, which is the whole point of having this provider.
"""

import sys
from types import SimpleNamespace

import pytest

from verinote.config import PROVIDERS, Config, ConfigCorruptError
from verinote.llm import factory
from verinote.llm.base import LLMError
from verinote.llm.openai_adapter import OpenAIAdapter
from verinote.llm.openrouter_adapter import OPENROUTER_DEFAULT_BASE_URL, OpenRouterAdapter

_DATALOG = 'answer_q1(V) :- relation(V, "is_a", "x").'
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

# Each method builds its own client, so a single-method test would leave three
# construction sites unguarded.
_INVOCATIONS = {
    "extract_facts": lambda a: a.extract_facts(source_text="x"),
    "translate_query": lambda a: a.translate_query(question="Who?", qid=1),
    "extract_query_intent": lambda a: a.extract_query_intent(question="What?"),
    "answer_question": lambda a: a.answer_question(question="Who?", context="c"),
}


def _cfg(tmp_path, *, provider="openrouter", base_url=None, model="m") -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider=provider,
        model=model,
        api_key="key",
        base_url=base_url,
    )


def _content(method: str):
    import json

    return {
        "extract_facts": json.dumps({"facts": []}),
        "translate_query": json.dumps({"datalog": _DATALOG}),
        "extract_query_intent": json.dumps(_INTENT),
        "answer_question": "ok",
    }[method]


def _record_client(monkeypatch, method: str, *, raises: Exception | None = None) -> dict:
    """Stub the openai SDK and return the kwargs the client was built with."""
    recorded: dict = {}
    content = _content(method)

    class _Completions:
        def create(self, **kwargs):
            if raises is not None:
                raise raises
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    def _factory(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_factory))
    return recorded


# --- the endpoint is bound to the provider, not to a blank-able field ---


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_unset_base_url_dials_openrouter_not_openai(tmp_path, monkeypatch, method):
    """The reason this provider exists. Under `openai` + base_url, clearing the
    field sends documents to api.openai.com — a vendor the user did not choose.
    Asserted on the constructor argument, because a request-level assertion
    would pass against a stub that never looks at the endpoint.
    """
    recorded = _record_client(monkeypatch, method)

    _INVOCATIONS[method](OpenRouterAdapter(_cfg(tmp_path)))

    assert recorded["base_url"] == OPENROUTER_DEFAULT_BASE_URL
    # `LLMClient` makes applying the configured timeout a MUST for every adapter.
    # Inheritance satisfies it today; a future `_client` override here would
    # break that contract silently, and this adapter is in no other suite that
    # enumerates cloud providers.
    assert recorded["timeout"] == 600.0


def test_explicit_base_url_still_wins(tmp_path, monkeypatch):
    """A default, not a forced override — an OpenRouter-compatible proxy or a
    regional endpoint must remain reachable."""
    recorded = _record_client(monkeypatch, "extract_facts")

    OpenRouterAdapter(_cfg(tmp_path, base_url="https://proxy.internal/v1")).extract_facts(
        source_text="x"
    )

    assert recorded["base_url"] == "https://proxy.internal/v1"


def test_openai_provider_keeps_the_sdk_default_endpoint(tmp_path, monkeypatch):
    """The hook must not leak the OpenRouter endpoint into the openai provider."""
    recorded = _record_client(monkeypatch, "extract_facts")

    OpenAIAdapter(_cfg(tmp_path, provider="openai")).extract_facts(source_text="x")

    assert recorded["base_url"] is None


# --- a failure names the provider that failed ---


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_failure_names_openrouter_not_openai(tmp_path, monkeypatch, method):
    """This string is the settings banner and the persisted chunk error. Naming
    `openai` there would point a user at the wrong provider."""
    _record_client(monkeypatch, method, raises=RuntimeError("boom"))

    with pytest.raises(LLMError, match=r"^openrouter request failed"):
        _INVOCATIONS[method](OpenRouterAdapter(_cfg(tmp_path)))


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_openai_failure_still_names_openai(tmp_path, monkeypatch, method):
    """Deriving the prefix from `name` must not have renamed the existing one."""
    _record_client(monkeypatch, method, raises=RuntimeError("boom"))

    with pytest.raises(LLMError, match=r"^openai request failed"):
        _INVOCATIONS[method](OpenAIAdapter(_cfg(tmp_path, provider="openai")))


# --- dispatch and the corrupt-config halt ---


def test_factory_selects_the_openrouter_adapter(tmp_path):
    assert isinstance(factory.get_client(_cfg(tmp_path)), OpenRouterAdapter)


def test_corrupt_config_halts_before_the_adapter_is_constructed(tmp_path, monkeypatch):
    """A corrupt config.json makes the resolved provider untrustworthy. Asserting
    only that an error was raised would pass even if the adapter had already been
    built and dialled — so count constructions instead (#269)."""
    built = []
    monkeypatch.setattr(
        OpenRouterAdapter, "__init__", lambda self, cfg: built.append(cfg) or None
    )
    corrupt = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="openrouter",
        model="m",
        api_key="key",
        base_url=None,
        settings_error="config.json is not valid JSON",
    )

    with pytest.raises(ConfigCorruptError):
        factory.get_client(corrupt)

    assert built == []


def test_unknown_provider_message_lists_every_provider(tmp_path):
    """Spelled out by hand this went stale the moment a provider was added, and
    it is what tells a user with a typo what they were allowed to write."""
    with pytest.raises(LLMError) as exc:
        factory.get_client(_cfg(tmp_path, provider="madeup"))

    for provider in PROVIDERS:
        assert provider in str(exc.value)


# --- the default model is a model, not a router ---


def test_default_model_is_a_concrete_model_not_a_router(tmp_path, monkeypatch):
    """`openrouter/free` picks a different model per request, so the settings
    banner's "answered ... from <model>" and a captured fixture's recorded model
    would both name something that did not answer."""
    monkeypatch.delenv("VERINOTE_MODEL", raising=False)
    monkeypatch.setenv("VERINOTE_PROVIDER", "openrouter")

    # The equality is the whole assertion; a separate `not startswith("openrouter/")`
    # check would be implied by it and could never fail on its own.
    assert Config.for_root(tmp_path).model == "openai/gpt-oss-20b:free"
