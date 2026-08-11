# SPDX-License-Identifier: MPL-2.0
import sys
from types import SimpleNamespace

import pytest

from verinote.config import Config, save_settings
from verinote.llm.anthropic_adapter import AnthropicAdapter
from verinote.llm.base import LLMError
from verinote.llm.openai_adapter import OpenAIAdapter
from verinote.llm.openrouter_adapter import OpenRouterAdapter
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


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    """The base_url tests below resolve their Config from the environment, and a
    cloud adapter now refuses to run without a key rather than letting the vendor
    SDK fall back to its own env var. They are about base_url plumbing, so give
    them a key. Tests that exercise key handling build their Config directly and
    are unaffected by this.
    """
    monkeypatch.setenv("VERINOTE_API_KEY", "configured-test-key")


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


@pytest.mark.parametrize("provider", sorted(_CLOUD_ADAPTERS))
def test_whitespace_only_saved_base_url_reaches_cloud_client_as_none(tmp_path, monkeypatch, provider):
    record, adapter_cls = _CLOUD_ADAPTERS[provider]
    recorded = record(monkeypatch)
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider=provider, model="m", base_url="   ")

    adapter_cls(Config.for_root(tmp_path)).extract_facts(source_text="x")

    assert recorded["base_url"] is None


@pytest.mark.parametrize("provider", sorted(_CLOUD_ADAPTERS))
def test_padded_base_url_reaches_cloud_client_trimmed(tmp_path, monkeypatch, provider):
    record, adapter_cls = _CLOUD_ADAPTERS[provider]
    recorded = record(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", provider)
    monkeypatch.setenv("VERINOTE_BASE_URL", "  https://llm.internal/v1  ")

    adapter_cls(Config.for_root(tmp_path)).extract_facts(source_text="x")

    assert recorded["base_url"] == "https://llm.internal/v1"


# --- a provider's 401 body can echo the key it rejected ---

_LONG_KEY = "sk-test-DEADBEEFDEADBEEF"


def _raising_sdk(monkeypatch, provider: str, exc: Exception) -> None:
    """Stub the vendor SDK so the request path raises inside the adapter's try."""

    class _Raises:
        def create(self, **kwargs):
            raise exc

    if provider in ("openai", "openrouter"):
        client = SimpleNamespace(chat=SimpleNamespace(completions=_Raises()))
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **k: client))
    else:
        client = SimpleNamespace(messages=_Raises())
        monkeypatch.setitem(
            sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda **k: client)
        )


def _keyed_cfg(tmp_path, provider: str, key: str | None) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider=provider,
        model="model",
        api_key=key,
        base_url=None,
    )


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
@pytest.mark.parametrize("provider", sorted(_CLOUD_ADAPTERS))
def test_provider_error_never_carries_the_key(tmp_path, monkeypatch, method, provider):
    """This string is persisted into `source_chunks.error` and rendered on three
    pages, so a key echoed back by a 401 would come to rest inside the KB — the
    one place the key is supposed never to reach. Every method is covered because
    each raise site is its own chance to bypass the redacting constructor."""
    adapter_cls = _CLOUD_ADAPTERS[provider][1]
    _raising_sdk(monkeypatch, provider, RuntimeError(f"401 invalid key {_LONG_KEY}"))

    with pytest.raises(LLMError) as exc:
        _INVOCATIONS[method](adapter_cls(_keyed_cfg(tmp_path, provider, _LONG_KEY)))

    assert _LONG_KEY not in str(exc.value)
    assert "***" in str(exc.value)


def test_a_short_key_leaves_the_message_intact(tmp_path, monkeypatch):
    """Redacting a short string mangles ordinary diagnostics — `api_key="key"` is
    live in this file and would turn "invalid api key" into "invalid api ***".
    Message text that varies with the key's content is its own small oracle."""
    _raising_sdk(monkeypatch, "openai", RuntimeError("invalid api key here"))

    with pytest.raises(LLMError, match="invalid api key here"):
        OpenAIAdapter(_keyed_cfg(tmp_path, "openai", "key")).extract_facts(source_text="x")


# OpenRouter inherits both guards from OpenAIAdapter, so it is listed explicitly
# rather than left to transitive coverage: it is the provider most likely to be
# pointed at a caller-supplied endpoint, which is where an unredactable key hurts.
_KEYED_ADAPTERS = {"openai": OpenAIAdapter, "anthropic": AnthropicAdapter,
                   "openrouter": OpenRouterAdapter}


@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_no_key_refuses_instead_of_falling_back_to_the_vendor_env_var(
    tmp_path, monkeypatch, provider
):
    """Both SDKs read their own OPENAI_API_KEY/ANTHROPIC_API_KEY when handed
    `api_key=None`, so the request would authenticate with a credential this
    process never resolved — one `redact_secret` cannot match, whose echo in a
    4xx body is persisted verbatim into `source_chunks.error`. The env vars are
    set here so the test fails if the fallback is ever restored."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-FALLBACK-SECRET-0001")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-FALLBACK-SECRET-0001")
    adapter_cls = _KEYED_ADAPTERS[provider]
    _raising_sdk(monkeypatch, provider, RuntimeError("unreachable"))

    # Anchored, and interpolating the provider, because `match` is `re.search`:
    # an unanchored "requires an API key" also matches
    # "openai client could not be created: openai requires an API key", which is
    # exactly what re-inlining `_require_key()` as a constructor argument would
    # produce. Without the `^` this test passes against that regression.
    with pytest.raises(LLMError, match=rf"^{provider} requires an API key"):
        adapter_cls(_keyed_cfg(tmp_path, provider, None)).extract_facts(source_text="x")


# --- the client could not be built, so nothing was ever dialled (#493) ---

# Long enough for `redact_secret` to act on, unlike the `"key"` most fixtures in
# this file use. Its role is the opposite of `_LONG_KEY`'s: a key that is present
# and unremarkable, so the failure under test is the construction and never the
# key check standing in front of it.
_CONFIGURED_KEY = "sk-test-CONFIGURED-0001"

# The value #493 was filed for. Measured against the installed SDKs: both
# `anthropic.Anthropic(base_url="::::")` and `openai.OpenAI(base_url="::::")`
# raise `httpx.InvalidURL: Relative URLs cannot have a path starting with ':'`.
_UNUSABLE_BASE_URL = "::::"


def _unusable_url_cfg(tmp_path, provider: str) -> Config:
    """A keyed Config whose Base URL the SDK constructor cannot accept.

    Spelled out rather than left at `base_url=None`, which matters for
    `openrouter`: its `_base_url()` substitutes a perfectly good default, so a
    `None` here would hand the SDK a working endpoint and assert nothing.
    """
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider=provider,
        model="model",
        api_key=_CONFIGURED_KEY,
        base_url=_UNUSABLE_BASE_URL,
    )


def _sdk_failing_to_construct(monkeypatch, provider: str, exc: Exception) -> None:
    """Stub the vendor SDK so the CONSTRUCTOR raises, before any client exists.

    `_raising_sdk` cannot express this. It stubs the constructor as
    `lambda **k: client` — a function that always succeeds — and puts the failure
    on `.create`, so every failure it can produce happens after a client was
    built. That is precisely the case `_client_failed` is *not* about, which is
    why this is a second fake rather than a parameter on the first.
    """

    def _boom(**kwargs):
        raise exc

    if provider in ("openai", "openrouter"):
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_boom))
    else:
        monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=_boom))


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_client_that_cannot_be_built_is_a_normalised_failure(
    tmp_path, monkeypatch, method, provider
):
    """A `base_url` typo is settings-UI input, so this is reachable by typing.
    Left unnormalised the SDK's `httpx.InvalidURL` escapes the adapter, lands in
    the web worker's generic handler as "analysis failed" and in the CLI as a
    traceback — the §10.1 violation #474 fixed from the other direction.

    Anchored at the start of the message. Unanchored it would also pass with
    `_client()` moved inside each method's own `try`, which double-wraps into
    "openai request failed: openai client could not be created: ..." — a message
    claiming a request was made when none was.
    """
    _sdk_failing_to_construct(
        monkeypatch, provider, RuntimeError("Relative URLs cannot have a path starting with ':'")
    )
    adapter = _KEYED_ADAPTERS[provider](_unusable_url_cfg(tmp_path, provider))

    with pytest.raises(LLMError, match=rf"^{provider} client could not be created"):
        _INVOCATIONS[method](adapter)


def test_a_client_that_cannot_be_built_keeps_the_sdk_error_as_the_cause(tmp_path, monkeypatch):
    """`from exc`, so the SDK's own words survive for a log even though the
    user-facing message is the adapter's."""
    boom = RuntimeError("Relative URLs cannot have a path starting with ':'")
    _sdk_failing_to_construct(monkeypatch, "openai", boom)

    with pytest.raises(LLMError) as exc:
        OpenAIAdapter(_unusable_url_cfg(tmp_path, "openai")).extract_facts(source_text="x")

    assert exc.value.__cause__ is boom


@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_client_construction_error_never_carries_the_key(tmp_path, monkeypatch, provider):
    """The second construction site has to redact too, and only a fake can show
    it: the real `httpx.InvalidURL` this path exists for carries the URL and not
    the key, so the real-SDK anchors below would stay green with `redact_secret`
    deleted from `_client_failed`. That asymmetry is why this is written against
    a stub — a construction error that *does* echo the key (a gateway rejecting
    it during setup) is possible, and this message reaches `source_chunks.error`
    like any other.
    """
    _sdk_failing_to_construct(monkeypatch, provider, RuntimeError(f"401 {_LONG_KEY}"))

    with pytest.raises(LLMError) as exc:
        _KEYED_ADAPTERS[provider](_keyed_cfg(tmp_path, provider, _LONG_KEY)).extract_facts(
            source_text="x"
        )

    assert _LONG_KEY not in str(exc.value)
    assert "***" in str(exc.value)


@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_client_failure_does_not_send_a_user_to_a_field_they_left_blank(
    tmp_path, monkeypatch, provider
):
    """`base_url` unset, and the SDK still fails to construct: measured against
    both installed SDKs, `SSL_CERT_FILE` naming a missing file does exactly this,
    and `HTTPS_PROXY='::::'` raises `httpx.InvalidURL` with no `base_url` in
    sight. Naming the Base URL setting in the message would send those users to
    edit an empty field — the misdirection #474 was reported as. The urllib
    adapters DO name it, and correctly; see `base_url_unusable`.
    """
    _sdk_failing_to_construct(
        monkeypatch, provider, FileNotFoundError(2, "No such file or directory")
    )

    with pytest.raises(LLMError) as exc:
        _KEYED_ADAPTERS[provider](_keyed_cfg(tmp_path, provider, _CONFIGURED_KEY)).extract_facts(
            source_text="x"
        )

    assert "Base URL" not in str(exc.value)
    assert "base_url" not in str(exc.value)


@pytest.mark.parametrize(("provider", "module"), [("anthropic", "anthropic"), ("openai", "openai")])
def test_a_missing_sdk_still_says_the_sdk_is_missing(tmp_path, monkeypatch, provider, module):
    """The `ImportError` clause has to stay in front of the construction guard.
    Folded into it, "install the optional dependency" — an instruction the user
    can act on — becomes "client could not be created", which is true and useless.
    """
    monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(LLMError, match=rf"^{provider} SDK not installed"):
        _KEYED_ADAPTERS[provider](_keyed_cfg(tmp_path, provider, _CONFIGURED_KEY)).extract_facts(
            source_text="x"
        )


def test_the_installed_anthropic_sdk_really_does_reject_an_unusable_base_url(tmp_path):
    """The stubs above show the adapter normalises whatever the constructor
    raises; this shows the constructor raises at all for the value #493 was filed
    with. Without it every test in this section could be green against an SDK
    that quietly accepted `::::`.

    Skipped where the optional dependency is absent, which includes CI — it
    installs `.[test,wirelog]` and neither vendor SDK. Local green therefore does
    not speak for CI on this one axis, which is why the stubs carry the contract.
    """
    pytest.importorskip("anthropic")

    with pytest.raises(LLMError, match="^anthropic client could not be created"):
        AnthropicAdapter(_unusable_url_cfg(tmp_path, "anthropic")).extract_facts(source_text="x")


def test_the_installed_openai_sdk_really_does_reject_an_unusable_base_url(tmp_path):
    """The openai half of the anchor above. #493 could only reason about this SDK
    by structural analogy because it was not installed when the issue was filed;
    measured on openai 2.53.0 it raises the same `httpx.InvalidURL` anthropic
    0.116.0 does.

    No openrouter twin: `OpenRouterAdapter` inherits `_client` unchanged, so a
    third anchor would dial the same constructor and add only a skip.
    """
    pytest.importorskip("openai")

    with pytest.raises(LLMError, match="^openai client could not be created"):
        OpenAIAdapter(_unusable_url_cfg(tmp_path, "openai")).extract_facts(source_text="x")
