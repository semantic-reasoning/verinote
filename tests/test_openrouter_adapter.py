# SPDX-License-Identifier: MPL-2.0
"""OpenRouter reuses every OpenAI generation path, so the tests here cover only
what is NOT inherited: which endpoint gets dialled, and which provider a failure
names. Re-testing the four inherited methods against a stub would pass with the
endpoint binding deleted, which is the whole point of having this provider.
"""

import inspect
import json
import sys
from types import SimpleNamespace

import pytest

from verinote.config import PROVIDERS, Config, ConfigCorruptError
from verinote.llm import factory
from verinote.llm.base import LLMError
from verinote.llm.openai_adapter import OpenAIAdapter
from verinote.llm.openrouter_adapter import (
    OPENROUTER_DEFAULT_BASE_URL,
    OpenRouterAdapter,
    list_models,
)

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


# --- list_models: the settings picker's source of truth ---


class _CatalogueResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _catalogue(monkeypatch, payload):
    """Answer /models with `payload`; return the recorded requests."""
    calls = []

    def fake_urlopen(req, *, timeout):
        calls.append(SimpleNamespace(url=req.full_url, timeout=timeout, headers=req.header_items()))
        return _CatalogueResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _entry(model_id, *, structured=True, extra_parameters=("temperature",)):
    parameters = list(extra_parameters) + (["structured_outputs"] if structured else [])
    return {"id": model_id, "supported_parameters": parameters}


def test_list_models_groups_the_catalogue_by_what_each_entry_advertises(monkeypatch):
    """The picker's two groups are exactly this split, so both halves have to be
    reported: the ids, and which of them listed `structured_outputs`.

    Mutation: return every id in `structured_output_ids` (or none of them) and
    the settings picker files a model under a heading its catalogue entry
    contradicts.
    """
    calls = _catalogue(
        monkeypatch,
        {
            "data": [
                _entry("z/schema-model"),
                _entry("a/plain-model", structured=False),
                _entry("  z/schema-model  "),  # same id, padded
            ]
        },
    )

    listing = list_models(None, 5.0)

    assert listing.models == ("a/plain-model", "z/schema-model")
    assert listing.structured_output_ids == frozenset({"z/schema-model"})
    assert calls[0].url == "https://openrouter.ai/api/v1/models"


def test_list_models_sends_no_authorization_header(monkeypatch):
    """The catalogue answers unauthenticated, so there is nothing to trade for
    the omission -- and this lister is dialled at a URL the caller supplied in a
    query string, where a key must never go.

    The request is asserted to carry NO headers at all rather than to lack an
    `Authorization` one specifically: a credential can be spelled `X-Api-Key`, or
    ride in any header a later edit adds, and an empty list is the only assertion
    that does not have to enumerate the spellings. Mutation: add any header here
    and this fails, which is the intended review prompt.

    The end-to-end egress proof lives in `tests/test_web.py`'s
    `test_openrouter_model_field_puts_no_api_key_on_the_wire`; this one pins the
    adapter in isolation.
    """
    calls = _catalogue(monkeypatch, {"data": []})

    list_models(None, 5.0)

    assert calls[0].headers == []


def test_list_models_honours_the_supplied_base_url(monkeypatch):
    """A proxy or gateway is configured with Base URL, and the catalogue must be
    read from the endpoint that will actually serve the generation."""
    calls = _catalogue(monkeypatch, {"data": []})

    list_models("https://proxy.internal/v1/", 5.0)

    assert calls[0].url == "https://proxy.internal/v1/models"


def test_list_models_passes_the_supplied_timeout_through(monkeypatch):
    """The clamp against the generation budget belongs to the caller that still
    holds a Config; this function must not silently re-derive or override it."""
    calls = _catalogue(monkeypatch, {"data": []})

    list_models(None, 1.5)

    assert calls[0].timeout == 1.5


def test_list_models_takes_no_config_so_it_cannot_be_handed_a_key(monkeypatch):
    """`openrouter` IS a key-holding provider, so unlike Ollama's lister there is
    a real key to leak here. Taking no `Config` means none is handed in; it is not
    a guarantee that none is reachable, which is why the web layer applies the
    same shape rule at import to every lister in its shipped table."""
    assert tuple(inspect.signature(list_models).parameters) == ("base_url", "timeout")
    assert not hasattr(OpenRouterAdapter, "list_models")


def test_list_models_returns_empty_for_a_catalogue_that_lists_nothing(monkeypatch):
    """Reachable-but-empty is data, not an error: the caller must be able to say
    'this endpoint listed nothing' rather than 'it could not be reached'."""
    _catalogue(monkeypatch, {"data": []})

    listing = list_models(None, 5.0)

    assert listing.models == ()
    # Reported and empty -- NOT `None`, which would mean "this listing does not
    # report the property at all". The picker renders the two differently.
    assert listing.structured_output_ids == frozenset()


def test_list_models_raises_on_transport_failure(monkeypatch):
    """Never `[]` on error: 'reachable but empty' and 'could not be reached' are
    different facts, and the settings page renders each differently."""

    def boom(req, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(LLMError, match="openrouter request failed"):
        list_models(None, 5.0)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "gpt"},
        {},
        ["gpt"],
        {"data": [{"supported_parameters": []}]},
        {"data": [{"id": "  ", "supported_parameters": []}]},
        {"data": ["not-an-object"]},
        {"data": [{"id": "a/b"}]},
        {"data": [{"id": "a/b", "supported_parameters": None}]},
    ],
)
def test_list_models_raises_on_schema_mismatch(monkeypatch, payload):
    """A shape that is not `{'data': [{id, supported_parameters}, ...]}` is a
    failure, never a silent empty or half-read list.

    The per-entry cases are the load-bearing ones: an entry that cannot be
    classified must not be swept into 'does not advertise structured output',
    which would report a claim the catalogue never made. Mutation: skip such
    entries (or default them to non-advertising) and these stop raising.
    """
    _catalogue(monkeypatch, payload)

    with pytest.raises(LLMError, match="did not match schema"):
        list_models(None, 5.0)


# The settings-UI typo #493 was reported for. Passed explicitly and never as
# `None`: an unset base_url resolves to OPENROUTER_DEFAULT_BASE_URL, which is a
# perfectly good URL, so a `None` here would assert nothing at all.
_UNUSABLE_BASE_URL = "::::"


def test_list_models_normalises_a_base_url_no_request_can_be_built_from(monkeypatch):
    """`GET /settings/model-field?provider=openrouter&base_url=::::` reaches this
    with a URL the user has only typed, not saved. Unnormalised the `ValueError`
    escapes as a 500 from the settings page; `LLMError` is what that endpoint
    already renders as a banner.
    """
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(LLMError, match="^openrouter base URL is unusable"):
        list_models(_UNUSABLE_BASE_URL, 5.0)


def test_list_models_names_openrouter_not_ollama_for_an_unusable_base_url(monkeypatch):
    """Two listers now share one helper, and the provider name is the only thing
    distinguishing their messages. It is what the banner shows, so a copy-paste
    that left "ollama" here would point at a provider the user is not using."""
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(LLMError) as exc:
        list_models(_UNUSABLE_BASE_URL, 5.0)

    assert "ollama" not in str(exc.value)


def test_list_models_does_not_blame_the_base_url_for_a_non_valueerror(monkeypatch):
    """The clause catches `ValueError` and nothing wider, so a genuine bug in
    this statement is not relabelled as the user's configuration mistake."""

    def boom(*args, **kwargs):
        raise TypeError("Request() got an unexpected keyword argument")

    monkeypatch.setattr("urllib.request.Request", boom)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(TypeError):
        list_models(None, 5.0)
