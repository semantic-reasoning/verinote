# SPDX-License-Identifier: MPL-2.0
"""Stored API keys: where they live, which one wins, and what a broken file does.

The precedence tests are written in pairs on purpose. "A stored key resolves"
passes for an implementation that only ever reads the store, and "the env var
resolves" passes for one that only ever reads the environment — neither says
anything about the ordering, which is the whole point of the design.
"""

import json
import os

import pytest

from verinote.config import (
    PROVIDERS,
    PROVIDERS_REQUIRING_KEY,
    Config,
    CredentialsCorruptError,
    assert_credentials_intact,
    credentials_path,
    delete_credential,
    provider_key_env_var,
    resolve_api_key,
    save_credential,
    save_settings,
)

_STORED = "sk-stored-openai-DEADBEEF"
_SCOPED = "sk-scoped-openai-DEADBEEF"
_GLOBAL = "sk-global-anthropic-DEADBEEF"


@pytest.fixture
def isolated_app_config(isolate_app_environment):
    """These tests write to a real filesystem path. The session-wide conftest
    fixture already gives every test its own home — and therefore its own
    `app_config_dir()` — so this only names the dependency the tests rely on.
    """
    return isolate_app_environment


@pytest.fixture(autouse=True)
def _clean_key_env(monkeypatch):
    monkeypatch.delenv("VERINOTE_API_KEY", raising=False)
    for provider in PROVIDERS:
        monkeypatch.delenv(provider_key_env_var(provider), raising=False)


# --- precedence: a key must never reach a provider it was not meant for ---


def test_a_stored_key_beats_the_provider_agnostic_env_var(tmp_path, monkeypatch):
    """The C1 case. `VERINOTE_API_KEY` names no provider, so ranking it above a
    key saved *for OpenAI* would send whatever is exported — commonly an
    Anthropic key — to api.openai.com."""
    monkeypatch.setenv("VERINOTE_API_KEY", _GLOBAL)

    assert resolve_api_key("openai", {"openai": _STORED}) == _STORED


def test_the_env_var_still_resolves_when_nothing_is_stored(tmp_path, monkeypatch):
    """Pairs with the test above: demoting the legacy variable must not break the
    only way keys worked before this existed."""
    monkeypatch.setenv("VERINOTE_API_KEY", _GLOBAL)

    assert resolve_api_key("anthropic", {}) == _GLOBAL


def test_the_provider_scoped_env_var_beats_a_stored_key(monkeypatch):
    """Env-first stays available — it just has to name the provider to get it."""
    monkeypatch.setenv(provider_key_env_var("openai"), _SCOPED)

    assert resolve_api_key("openai", {"openai": _STORED}) == _SCOPED


def test_a_scoped_var_does_not_leak_across_providers(monkeypatch):
    monkeypatch.setenv(provider_key_env_var("openai"), _SCOPED)

    assert resolve_api_key("anthropic", {}) is None


@pytest.mark.parametrize(
    "provider", sorted(set(PROVIDERS) - PROVIDERS_REQUIRING_KEY)
)
def test_a_keyless_provider_resolves_nothing_from_any_source(provider, monkeypatch):
    """These adapters never read `cfg.api_key`, so a resolved value could only be
    something to leak. All three sources are populated so the test fails if any
    tier is consulted."""
    monkeypatch.setenv("VERINOTE_API_KEY", _GLOBAL)
    monkeypatch.setenv(provider_key_env_var(provider), _SCOPED)

    assert resolve_api_key(provider, {provider: _STORED}) is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_stored_key_falls_through(blank, monkeypatch):
    monkeypatch.setenv("VERINOTE_API_KEY", _GLOBAL)

    assert resolve_api_key("openai", {"openai": blank}) == _GLOBAL


def test_a_padded_stored_key_is_trimmed():
    assert resolve_api_key("openai", {"openai": f"  {_STORED}  "}) == _STORED


# --- the file: where it is, what it is not, and how it is written ---


def test_saving_a_key_keeps_it_out_of_the_kb(tmp_path, isolated_app_config):
    """A KB is user data that gets synced and shared; the app config directory is
    machine-local. This is the invariant the whole storage choice exists for, so
    it is asserted over every file the KB contains — and `save_settings` is
    called first, because a KB with no config.json satisfies a config.json-only
    check without proving anything.
    """
    save_settings(tmp_path, provider="openai", model="m")
    save_credential("openai", _STORED)
    Config.for_root(tmp_path)

    assert _STORED in credentials_path().read_text(encoding="utf-8")
    assert (tmp_path / "config.json").exists()
    leaked = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and _STORED in path.read_bytes().decode("utf-8", "replace")
    ]
    assert leaked == []


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes only")
def test_the_credentials_file_is_owner_only(isolated_app_config):
    save_credential("openai", _STORED)

    assert credentials_path().stat().st_mode & 0o777 == 0o600


def test_saving_one_provider_leaves_the_others_untouched(isolated_app_config):
    """A save is a merge. Rewriting the file from scratch would silently discard
    every other provider's key."""
    save_credential("openai", _STORED)
    save_credential("anthropic", _GLOBAL)

    keys = json.loads(credentials_path().read_text(encoding="utf-8"))["keys"]
    assert keys == {"openai": _STORED, "anthropic": _GLOBAL}


def test_deleting_reports_whether_anything_was_removed(isolated_app_config):
    """A Remove that says "removed" when nothing was stored would tell the user
    their key is gone while it never existed."""
    save_credential("openai", _STORED)

    assert delete_credential("openai") is True
    assert delete_credential("openai") is False
    assert resolve_api_key("openai", _stored_keys()) is None


def test_a_keyless_provider_cannot_acquire_a_stored_key(isolated_app_config):
    with pytest.raises(ValueError):
        save_credential("ollama", _STORED)


def test_the_config_directory_gets_a_gitignore(isolated_app_config):
    """`XDG_CONFIG_HOME` is routinely a dotfiles repo, and a subdirectory
    .gitignore is honoured by a repo rooted anywhere above it."""
    save_credential("openai", _STORED)

    assert (credentials_path().parent / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_an_existing_gitignore_is_not_overwritten(isolated_app_config):
    path = credentials_path().parent
    path.mkdir(parents=True, exist_ok=True)
    (path / ".gitignore").write_text("mine\n", encoding="utf-8")

    save_credential("openai", _STORED)

    assert (path / ".gitignore").read_text(encoding="utf-8") == "mine\n"


# --- an unreadable file must not read as "no key" ---


def _stored_keys() -> dict:
    from verinote.config import _read_credentials

    return _read_credentials()[0]


def _corrupt(text: str = "{not json") -> None:
    credentials_path().parent.mkdir(parents=True, exist_ok=True)
    credentials_path().write_text(text, encoding="utf-8")


def test_a_missing_file_is_not_an_error(tmp_path, isolated_app_config, monkeypatch):
    """The normal state before anyone saves a key."""
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")

    assert Config.for_root(tmp_path).credentials_error is None


def test_an_unreadable_file_halts_instead_of_resolving_to_no_key(
    tmp_path, isolated_app_config, monkeypatch
):
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    _corrupt()

    cfg = Config.for_root(tmp_path)

    assert cfg.credentials_error is not None
    assert cfg.api_key is None
    with pytest.raises(CredentialsCorruptError):
        assert_credentials_intact(cfg)


@pytest.mark.parametrize(
    ("provider", "env"),
    [("ollama", None), ("openai", "scoped"), ("openai", "global")],
)
def test_an_unreadable_file_does_not_halt_when_it_could_not_decide_the_key(
    tmp_path, isolated_app_config, monkeypatch, provider, env
):
    """Scoping matters as much as the halt: bricking an Ollama user, or one whose
    key comes from the environment, over a file that changes nothing for them
    would be its own misreport."""
    monkeypatch.setenv("VERINOTE_PROVIDER", provider)
    if env == "scoped":
        monkeypatch.setenv(provider_key_env_var(provider), _SCOPED)
    elif env == "global":
        monkeypatch.setenv("VERINOTE_API_KEY", _GLOBAL)
    _corrupt()

    assert Config.for_root(tmp_path).credentials_error is None


@pytest.mark.parametrize("payload", ['{"version": 1, "keys": []}', '{"keys": {"openai": 5}}'])
def test_a_non_string_key_is_refused_not_coerced(
    tmp_path, isolated_app_config, monkeypatch, payload
):
    """`str()` on an object would invent a credential out of a structure a future
    version meant as something else."""
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    _corrupt(payload)

    assert Config.for_root(tmp_path).credentials_error is not None


def test_a_write_into_an_unreadable_file_refuses_and_changes_nothing(isolated_app_config):
    """A save merges into the other providers' entries, so writing a fresh file
    over an unreadable one would silently discard them."""
    _corrupt()
    before = credentials_path().read_bytes()

    with pytest.raises(CredentialsCorruptError):
        save_credential("openai", _STORED)

    assert credentials_path().read_bytes() == before


def test_an_undecodable_file_is_an_error_not_an_empty_store(
    tmp_path, isolated_app_config, monkeypatch
):
    """Bad JSON and bad *bytes* reach the reader through different branches. Only
    the first was covered, so a regression in the decode branch would have made an
    unreadable file resolve to "no keys stored" — the misreport this design
    exists to prevent — with a green suite."""
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    credentials_path().parent.mkdir(parents=True, exist_ok=True)
    credentials_path().write_bytes(b'{"version": 1, "keys": {"openai": "\xff\xfe"}}')

    cfg = Config.for_root(tmp_path)

    assert cfg.credentials_error is not None
    assert cfg.api_key is None


# --- the web halt surface ---


def _web_client(tmp_path):
    from fastapi.testclient import TestClient

    from verinote.web import create_app

    cfg = Config.for_root(tmp_path)
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def test_the_halt_page_names_the_credentials_file_not_config_json(
    tmp_path, isolated_app_config, monkeypatch
):
    """A separate page from the config.json halt is the whole reason this error is
    its own type: that one names a KB's config.json and tells the user to re-save
    a provider, which does not touch this file."""
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    _corrupt()

    r = _web_client(tmp_path).get("/credentials-unavailable")

    assert r.status_code == 409
    assert str(credentials_path()) in r.text
    assert "config.json" not in r.text
    assert _STORED not in r.text


def test_an_htmx_request_is_redirected_rather_than_swapped(
    tmp_path, isolated_app_config, monkeypatch
):
    """htmx never swaps a 4xx into the DOM (#173), so answering one with an inline
    body would be a silent no-op instead of a halt."""
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    _corrupt()
    client = _web_client(tmp_path)

    r = client.post("/settings/test", headers={"HX-Request": "true"})

    assert r.status_code == 409
    assert r.headers.get("HX-Redirect") == "/credentials-unavailable"


def test_settings_says_unknown_rather_than_not_set(tmp_path, isolated_app_config, monkeypatch):
    """"not set" asserts the user has no key stored. When the file cannot be read
    the app does not know that — it knows only that it cannot tell."""
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    _corrupt()

    r = _web_client(tmp_path).get("/settings")

    assert r.status_code == 200  # the recovery page must stay reachable
    assert "API key: not set" not in r.text
    assert "unknown" in r.text


def test_a_provider_classified_nowhere_is_rejected():
    """The guard a subset check could not give: adding a provider without
    deciding whether it needs a key would otherwise resolve to None for it
    silently, and the provider would be called unauthenticated."""
    from verinote.config import _check_every_provider_is_classified

    with pytest.raises(RuntimeError, match="gemini"):
        _check_every_provider_is_classified(
            ("openai", "ollama", "gemini"), {"openai"}, {"ollama"}
        )


def test_a_provider_classified_on_both_sides_is_rejected():
    from verinote.config import _check_every_provider_is_classified

    with pytest.raises(RuntimeError, match="openai"):
        _check_every_provider_is_classified(("openai",), {"openai"}, {"openai"})


def test_a_corrupt_credentials_file_does_not_block_opening_a_kb(
    tmp_path, isolated_app_config, monkeypatch
):
    """The per-KB reasoning behind refusing a switch on a corrupt `config.json`
    does not transfer: this file is machine-wide and identical before and after
    the switch, so refusing prevents no provider call that is not already gated —
    while making every KB unopenable, including one whose provider needs no key.
    """
    from fastapi.testclient import TestClient

    from verinote.web import create_app

    _corrupt()
    kb = tmp_path / "kb"
    kb.mkdir()
    client = TestClient(create_app(None), raise_server_exceptions=False)

    r = client.post("/kb/select", data={"root": str(kb)}, follow_redirects=False)

    assert r.status_code == 303


def test_no_surface_blames_config_json_for_a_credentials_failure(
    tmp_path, isolated_app_config, monkeypatch
):
    """"refused to open KB — its config.json is corrupt: .../credentials.json is
    not valid JSON" contradicted itself in one line and pointed at a fix that
    does not touch the failing file."""
    from fastapi.testclient import TestClient

    from verinote.web import create_app

    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    _corrupt()
    kb = tmp_path / "kb"
    kb.mkdir()
    client = TestClient(create_app(None), raise_server_exceptions=False)

    body = client.post("/kb/select", data={"root": str(kb)}, follow_redirects=True).text

    assert "config.json is corrupt" not in body


def test_settings_links_the_recovery_page(tmp_path, isolated_app_config, monkeypatch):
    """The halt page carries the only correct recovery text, so the page a halted
    user actually lands on has to reach it."""
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    _corrupt()

    r = _web_client(tmp_path).get("/settings")

    assert "/credentials-unavailable" in r.text
