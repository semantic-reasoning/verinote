# SPDX-License-Identifier: MPL-2.0
import ast
import json
import inspect
import sys
from pathlib import Path
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


def _client_failed_docstring(adapter) -> str:
    """`_client_failed`'s docstring as the source spells it.

    Read out of the AST, not off `__doc__`: under `python -OO` the interpreter
    strips docstrings, so `adapter._client_failed.__doc__` is `None`. A
    `__doc__` version does not go quietly vacuous under that flag: measured
    against the body below, it dies on `None.split` with an `AttributeError` and
    both parameterisations fail loudly. (That exception type is a fact about the
    body, not about `-OO` -- it has already gone stale once here when the body
    changed. Re-measure it rather than reasoning about it.) Failing loudly is
    still failing, and a guard that cannot run under a supported interpreter
    flag is not a guard. And what
    is being asserted is a property of the source text that ships, so reading
    the source is the direct route as well as the flag-independent one; the
    source-shape guards elsewhere in `tests/` take it for the same reason.
    """
    tree = ast.parse(Path(inspect.getfile(adapter)).read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == adapter.__name__
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_client_failed"
    )
    doc = ast.get_docstring(method)
    assert doc is not None, f"{adapter.__name__}._client_failed has no docstring to check"
    return doc


@pytest.mark.parametrize(
    ("adapter", "own", "foreign"),
    [
        (AnthropicAdapter, "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"),
        (OpenAIAdapter, "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"),
    ],
    ids=["anthropic", "openai"],
)
def test_each_adapter_names_only_its_own_vendor_base_url_variable(adapter, own, foreign):
    """These two docstrings are near-identical prose about two different SDKs,
    and the anthropic one shipped naming `OPENAI_BASE_URL` — a variable measured
    to do nothing in `anthropic.Anthropic(...)`. That is the misdirection the
    docstring itself argues against, one indirection out: a user handed a name
    their SDK never reads goes looking for a setting that cannot be their cause.

    Measured with each variable set alone in an `env -i` environment, against
    anthropic 0.116.0 / openai 2.44.0 / httpx 0.28.1, with `base_url=None`:
    `ANTHROPIC_BASE_URL='::::'` raises `httpx.InvalidURL` for anthropic and
    nothing for openai, and `OPENAI_BASE_URL='::::'` the other way round.

    Scoped by role, not by spelling: only the paragraph carrying the marker
    "Deliberately does NOT name" is read -- matched anywhere in it, though today
    it is that paragraph's opening phrase -- because that is the paragraph
    enumerating causes, and the vendor variable named there has to be the file's
    own. An earlier version anchored on the `VAR='::::'` spelling every cause
    happens to use, which got the trade the wrong way round: it let "A malformed
    `OPENAI_BASE_URL` does the same, so check that too" into the anthropic cause
    paragraph, and failed a harmless reflow of that paragraph's own clause.

    Mentions in the other paragraphs stay legal, and the anthropic docstring
    makes one deliberately: that `OPENAI_BASE_URL` does nothing in that
    constructor is a measured fact worth stating, and it is not a misdirection
    because it is not offered as a cause. `len(causes) == 1` is what stops that
    permissiveness from swallowing the guard: reword the marker away and
    `causes` is empty, duplicate it into a second paragraph and `causes` has
    two, and either way this fails instead of quietly checking nothing or
    checking only one of them.
    """
    paragraphs = [" ".join(p.split()) for p in _client_failed_docstring(adapter).split("\n\n")]
    causes = [p for p in paragraphs if "Deliberately does NOT name" in p]
    assert len(causes) == 1, paragraphs

    assert own in causes[0]
    assert foreign not in causes[0]


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

    Skipped where the optional dependency is absent, which includes the `ci.yml`
    pytest job — it installs `.[test,wirelog]` and neither vendor SDK.
    (`provider-contract.yml` does install the openai extra, but it runs
    `tests/contract/run.sh` on a schedule, not this suite.) Local green therefore
    does not speak for that job on this one axis, which is why the stubs carry
    the contract.
    """
    pytest.importorskip("anthropic")

    with pytest.raises(LLMError, match="^anthropic client could not be created"):
        AnthropicAdapter(_unusable_url_cfg(tmp_path, "anthropic")).extract_facts(source_text="x")


def test_the_installed_openai_sdk_really_does_reject_an_unusable_base_url(tmp_path):
    """The openai half of the anchor above. #493 could only reason about this SDK
    by structural analogy because it was not installed when the issue was filed;
    measured on openai 2.54.0 it raises the same `httpx.InvalidURL` anthropic
    0.116.0 does.

    No openrouter twin: `OpenRouterAdapter` inherits `_client` unchanged, so a
    third anchor would dial the same constructor and add only a skip.
    """
    pytest.importorskip("openai")

    with pytest.raises(LLMError, match="^openai client could not be created"):
        OpenAIAdapter(_unusable_url_cfg(tmp_path, "openai")).extract_facts(source_text="x")


# --- a broken prompt template is not the provider's fault (#500) ---

# Which template each method renders. The three cloud adapters share it:
# `OpenRouterAdapter` inherits the four methods unchanged, and the anthropic
# copies name the same four ids.
_PROMPT_ID = {
    "extract_facts": "extraction",
    "translate_query": "query-translation",
    "extract_query_intent": "query-intent",
    "answer_question": "ask-fallback",
}


def _undecodable_override(tmp_path, prompt_id: str):
    """An override the render cannot decode, written as bytes.

    `save_prompt_override` validates and writes UTF-8, so it cannot produce this
    file. The break has to be one that works for every prompt id: only
    `query-translation` among these four declares a `required_placeholder`, so an
    override "missing a placeholder" is not a break at all for the other three --
    `_validate_prompt_text` passes it and the SDK is reached with a perfectly
    usable prompt. Nine of the twelve cells below would assert nothing. The
    missing-placeholder case gets its own test, on the one method where it bites.
    """
    path = tmp_path / "policy" / "prompts" / f"{prompt_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not utf-8\n")
    return path


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_prompt_that_cannot_be_read_is_not_a_request_failure(
    tmp_path, monkeypatch, method, provider
):
    """The render used to sit in argument position inside the `try`, so a
    template the user broke came back as "<provider> request failed" -- their own
    file, reported as the provider's outage (#500). All four methods, because the
    hoist is four separate edits per file and one left behind is one method still
    lying; `openrouter` explicitly, because it inherits the four from
    `OpenAIAdapter` and a reader should not have to know that to see it covered.

    The SDK raises "dialled" if it is reached at all, so the assertions also say
    nothing was sent.
    """
    _undecodable_override(tmp_path, _PROMPT_ID[method])
    _raising_sdk(monkeypatch, provider, RuntimeError("dialled"))
    adapter = _KEYED_ADAPTERS[provider](_cfg(tmp_path, provider=provider))

    with pytest.raises(
        LLMError, match=rf"^prompt {_PROMPT_ID[method]} could not be loaded"
    ) as exc:
        _INVOCATIONS[method](adapter)

    assert "request failed" not in str(exc.value)
    assert "dialled" not in str(exc.value)


@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_missing_placeholder_keeps_the_library_s_own_words(tmp_path, monkeypatch, provider):
    """`translate_query` is the one method here whose prompt declares a required
    placeholder, and this is the exact string #500 quotes.

    Anchored at both ends, which is the whole test. `_render_prompt` keeps
    `except PromptError` above its catch-all so a prompt-contract violation --
    something the user can read as an instruction and act on -- goes out as the
    library wrote it. Delete that clause and the catch-all absorbs the case with
    "prompt query-translation could not be loaded: " in front; put the render
    back inside the `try` and "openai request failed: " goes in front. The `$`
    kills the first, the `^` the second. The older
    `test_*_prompt_validation_error_is_llm_error` pair above matches on `{qid}`
    alone and survives both.
    """
    path = tmp_path / "policy" / "prompts" / "query-translation.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Missing qid placeholder.\n", encoding="utf-8")
    _raising_sdk(monkeypatch, provider, RuntimeError("dialled"))
    adapter = _KEYED_ADAPTERS[provider](_cfg(tmp_path, provider=provider))

    with pytest.raises(
        LLMError,
        match=r"^Datalog translation prompt must include required placeholder \{qid\}$",
    ):
        adapter.translate_query(question="Who?", qid=3)


@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_an_unreadable_prompt_override_is_still_an_llm_error(tmp_path, monkeypatch, provider):
    """The cheaper trigger, and a second way in through the same clause.

    A non-UTF-8 override takes a hand-edited file; this takes one mode bit, which
    a backup tool or an umask can set without anybody meaning to. `read_text`
    raises `PermissionError`, which is not a `PromptError` either, so it arrives
    at the same catch-all -- the reachability half of the argument that the
    render's failures cannot be enumerated by type.

    Carries T1's anchors rather than a bare `pytest.raises(LLMError)`. Bare, this
    passes before the change too: inside the `try` the `PermissionError` was
    still wrapped, just wrapped as the provider's fault.
    """
    path = tmp_path / "policy" / "prompts" / "extraction.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Custom extraction prompt.\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        try:
            path.read_text(encoding="utf-8")
        except PermissionError:
            pass
        else:
            pytest.skip("this user reads straight through mode 0o000")

        _raising_sdk(monkeypatch, provider, RuntimeError("dialled"))
        adapter = _KEYED_ADAPTERS[provider](_cfg(tmp_path, provider=provider))

        with pytest.raises(LLMError, match=r"^prompt extraction could not be loaded") as exc:
            adapter.extract_facts(source_text="x")

        assert "request failed" not in str(exc.value)
        assert "dialled" not in str(exc.value)
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize(
    ("provider", "adapter_cls", "module"),
    [
        ("anthropic", AnthropicAdapter, "verinote.llm.anthropic_adapter"),
        ("openai", OpenAIAdapter, "verinote.llm.openai_adapter"),
    ],
)
def test_a_render_failure_of_a_kind_nobody_enumerated_is_still_an_llm_error(
    tmp_path, monkeypatch, provider, adapter_cls, module
):
    """The guard that does not depend on a list of types.

    `UnicodeDecodeError` and `PermissionError` are what the two tests above can
    reach through a file, and #500's reviewer refused to treat that pair as
    complete: `render_prompt` reads the packaged default and the override, and
    the `OSError` family those two reads can raise is open. Narrow the clause
    back to `except (UnicodeDecodeError, PermissionError)` and both of those stay
    green while this one fails.
    """

    class _Unlisted(Exception):
        pass

    def boom(*args, **kwargs):
        raise _Unlisted("nobody enumerated this")

    monkeypatch.setattr(f"{module}.render_prompt", boom)
    _raising_sdk(monkeypatch, provider, RuntimeError("dialled"))

    with pytest.raises(LLMError, match=r"^prompt extraction could not be loaded") as exc:
        adapter_cls(_cfg(tmp_path, provider=provider)).extract_facts(source_text="x")

    assert "request failed" not in str(exc.value)


def test_a_programming_error_in_the_render_is_deliberately_an_llm_error(tmp_path, monkeypatch):
    """The cost of the catch-all, pinned so it cannot be quietly repaid.

    A `TypeError` from inside `render_prompt` is a bug in this repo, and
    "prompt extraction could not be loaded" points the reader at
    `policy/prompts/extraction.md`, which is fine. That is the relabelling
    `test_a_non_valueerror_from_the_request_constructor_is_not_blamed_on_the_base_url`
    in `tests/test_ollama_adapter.py` refuses for `Request()` -- and it is
    accepted here, deliberately, because unlike `Request()` the render has more
    than one reachable failure and no type separates a bug from a broken file.
    §10.1 wins the trade at the adapter seam.

    Without this test the trade looks like an oversight, and the next reader
    narrows the clause to let programming errors through -- reopening §10.1 for
    every `OSError` nobody listed. `_Unlisted` above cannot carry that: a test
    exception class says "not enumerated", not "a bug in this file too".
    """

    def boom(*args, **kwargs):
        raise TypeError("render_prompt() got an unexpected keyword argument")

    monkeypatch.setattr("verinote.llm.openai_adapter.render_prompt", boom)
    _raising_sdk(monkeypatch, "openai", RuntimeError("dialled"))

    with pytest.raises(LLMError, match=r"^prompt extraction could not be loaded"):
        OpenAIAdapter(_cfg(tmp_path, provider="openai")).extract_facts(source_text="x")


@pytest.mark.parametrize(
    ("provider", "adapter_cls", "module"),
    [
        ("anthropic", AnthropicAdapter, "verinote.llm.anthropic_adapter"),
        ("openai", OpenAIAdapter, "verinote.llm.openai_adapter"),
    ],
)
def test_a_render_failure_keeps_the_original_error_as_the_cause(
    tmp_path, monkeypatch, provider, adapter_cls, module
):
    """`from exc` on the catch-all, which is what pays for the test above.

    Relabelling a programming error as a load failure is only tolerable if the
    original survives for a log. `_client_failed` has this guard already
    (`test_a_client_that_cannot_be_built_keeps_the_sdk_error_as_the_cause`); the
    render path did not, and the docstring now argues from it.

    BOTH adapters, because each carries its own `_rendered` and each re-raises
    `from exc.__cause__` in its own copy of that line. Covering one left the
    other's unpinned while its docstring named this test as what pins it —
    change either copy to `from exc` and the matching case here fails.
    """
    boom = TypeError("render_prompt() got an unexpected keyword argument")

    def raiser(*args, **kwargs):
        raise boom

    monkeypatch.setattr(f"{module}.render_prompt", raiser)
    _raising_sdk(monkeypatch, provider, RuntimeError("dialled"))

    with pytest.raises(LLMError) as exc:
        adapter_cls(_cfg(tmp_path, provider=provider)).extract_facts(source_text="x")

    assert exc.value.__cause__ is boom


@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_broken_template_does_not_outrank_a_missing_key(tmp_path, monkeypatch, provider):
    """Order: `client = self._client()` first, then the render, then the `try`.

    Hoisting the render above `_client()` would also satisfy #500 and would
    change which of two simultaneous problems a user is told about. A KB with no
    key configured and a broken template has one blocking problem and one that
    only matters afterwards, and the shipped order reports the blocking one. This
    fails if the render moves up.

    The two halves are one test on purpose. The first `raises` passes with no
    override written at all, so on its own it would keep passing if the fixture
    ever stopped breaking the template -- for instance if `extract_facts` came to
    render a different id. The second half re-runs the identical setup with a key
    and requires the template to be broken, so the setup cannot go inert
    unnoticed.
    """
    _undecodable_override(tmp_path, "extraction")
    _raising_sdk(monkeypatch, provider, RuntimeError("dialled"))
    adapter_cls = _KEYED_ADAPTERS[provider]

    with pytest.raises(LLMError, match=rf"^{provider} requires an API key"):
        adapter_cls(_keyed_cfg(tmp_path, provider, None)).extract_facts(source_text="x")

    with pytest.raises(LLMError, match=r"^prompt extraction could not be loaded"):
        adapter_cls(_keyed_cfg(tmp_path, provider, "key")).extract_facts(source_text="x")


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_render_failure_never_carries_the_key(tmp_path, monkeypatch, method, provider):
    """The render path's half of `test_provider_error_never_carries_the_key`.

    That test says "each raise site is its own chance to bypass the redacting
    constructor", and #500 added a raise site to each of these eight methods:
    hoisting the render above the `try` took it out from under
    `_request_failed`. On `759eac0` this same setup produced
    `anthropic request failed: [Errno 13] Permission denied: '.../kb-***/...'`;
    between the hoist and this test it produced the key. `self._rendered` puts
    the render back under a redactor and this is what holds it there.

    Twelve cells, not twenty-four. The other break this section uses -- a
    non-UTF-8 override -- cannot exercise redaction at all, because
    `UnicodeDecodeError` names a byte offset and no path, so there is nothing
    for the key to ride in on. An `OSError` from the override read spells out
    `cfg.root`, which is why the unreadable file is the one that reaches this.
    `openrouter` is in the parametrization rather than assumed: it inherits the
    four methods and `_rendered` from `OpenAIAdapter` and redacts with its own
    key.

    The SDK is never dialled on this path -- the render raises just after
    `self._client()` and before the `try` -- so `_request_failed` is not what
    these cells exercise; `test_provider_error_never_carries_the_key` keeps that
    half. `_raising_sdk` is still armed so that a regression which let the call
    through would surface as a dialled request rather than a quiet pass.
    """
    root = tmp_path / f"kb-{_LONG_KEY}"
    path = root / "policy" / "prompts" / _PROMPT_ID[method]
    path = path.with_suffix(".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Custom prompt.\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        try:
            path.read_text(encoding="utf-8")
        except PermissionError:
            pass
        else:
            pytest.skip("this user reads straight through mode 0o000")

        _raising_sdk(monkeypatch, provider, RuntimeError(f"401 invalid key {_LONG_KEY}"))
        adapter = _KEYED_ADAPTERS[provider](_keyed_cfg(root, provider, _LONG_KEY))

        with pytest.raises(LLMError) as exc:
            _INVOCATIONS[method](adapter)

        assert _LONG_KEY not in str(exc.value)
        assert "***" in str(exc.value)
    finally:
        path.chmod(0o600)


# --- the parse path: a 200 body that echoes the key (#514) --------------------

#: The parsers that can put the payload they rejected into their own message,
#: with a body shape that makes each one do it. Measured, not assumed:
#: `schema.parse_facts` interpolates the offending object with `{item!r}`, and
#: `parse_query_intent` interpolates the caller's `kind` string. `parse_query`
#: is deliberately absent -- across a body that is not JSON, one missing
#: `datalog`, a non-string `datalog` and a top-level list, its raise sites carried
#: a JSON position, a missing key name and a builtin `TypeError` phrase and never
#: the payload, which is what `_request_failed`'s docstring already says of it. It
#: still goes through `parsed_under_redaction`, because that is a property of
#: today's raise sites rather than a guarantee about them.
_LEAKING_PARSE_INVOCATIONS = {
    "extract_facts": (
        lambda a: a.extract_facts(source_text="x"),
        lambda key: {"facts": [{"subject": key, "oops": 1}]},
    ),
    "extract_query_intent": (
        lambda a: a.extract_query_intent(question="What?"),
        lambda key: {"kind": key},
    ),
}


def _echoing_sdk(monkeypatch, provider: str, body: dict) -> None:
    """Stub a 200 response whose BODY carries the configured key.

    Not a 401 echo -- that path goes through `_request_failed`, which redacts and
    is pinned by `test_provider_error_never_carries_the_key`. This is the other
    one: a well-formed HTTP success whose payload is off-schema, which the parsers
    reject by interpolating what they were handed. `base_url` is caller-supplied,
    so a body under someone else's control reaches here (#514).
    """

    class _Responds:
        def create(self, **kwargs):
            if provider == "anthropic":
                return SimpleNamespace(
                    content=[SimpleNamespace(type="tool_use", name="emit", input=body)]
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))]
            )

    if provider == "anthropic":
        monkeypatch.setattr(
            AnthropicAdapter, "_client", lambda self: SimpleNamespace(messages=_Responds())
        )
    else:
        cls = OpenRouterAdapter if provider == "openrouter" else OpenAIAdapter
        monkeypatch.setattr(
            cls,
            "_client",
            lambda self: SimpleNamespace(chat=SimpleNamespace(completions=_Responds())),
        )


@pytest.mark.parametrize("method", sorted(_LEAKING_PARSE_INVOCATIONS))
@pytest.mark.parametrize("provider", sorted(_KEYED_ADAPTERS))
def test_a_parse_failure_never_carries_the_key(tmp_path, monkeypatch, method, provider):
    """The parse path's half of `test_provider_error_never_carries_the_key`.

    That test says "each raise site is its own chance to bypass the redacting
    constructor". The parsers are raise sites the adapters reach just PAST the
    `try`, so `_request_failed` never sees them, and two of the three interpolate
    the payload they rejected. Measured before `parsed_under_redaction`: every
    cell here printed `sk-test-DEADBEEFDEADBEEF` into the message that
    `pipeline/extract.py` stores as `source_chunks.error` and `sources.html`
    renders (#514).

    Both halves are asserted. Absence alone passes when the message stops
    carrying the payload for some unrelated reason, at which point it has stopped
    being about redaction.
    """
    invoke, body_for = _LEAKING_PARSE_INVOCATIONS[method]
    _echoing_sdk(monkeypatch, provider, body_for(_LONG_KEY))

    with pytest.raises(LLMError) as exc:
        invoke(_KEYED_ADAPTERS[provider](_keyed_cfg(tmp_path, provider, _LONG_KEY)))

    assert _LONG_KEY not in str(exc.value)
    assert "***" in str(exc.value)


def test_a_parse_failure_keeps_the_original_error_as_the_cause(tmp_path, monkeypatch):
    """Redacting must not cost the cause the parsers already chain.

    `schema.parse_facts` raises `... from exc` with the `KeyError` that named the
    missing field. `parsed_under_redaction` re-raises `from exc.__cause__`, so the
    original survives for a log rather than being buried behind the wrapper's own
    `LLMError`. Re-raising `from exc` instead is the way to make this fail.
    """
    _echoing_sdk(monkeypatch, "openai", {"facts": [{"subject": _LONG_KEY, "oops": 1}]})

    with pytest.raises(LLMError) as exc:
        OpenAIAdapter(_keyed_cfg(tmp_path, "openai", _LONG_KEY)).extract_facts(source_text="x")

    assert isinstance(exc.value.__cause__, KeyError)
