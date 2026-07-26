# SPDX-License-Identifier: MPL-2.0
import json
import sys
from pathlib import Path

import pytest

import verinote.config as config_module
from verinote.config import (
    Config,
    ConfigCorruptError,
    PROVIDERS,
    TESTABLE_PROVIDERS,
    active_root,
    app_config_path,
    assert_settings_intact,
    read_app_config,
    read_settings,
    save_active_root,
    save_settings,
)
from verinote.llm.base import LLMError


def test_save_and_read_round_trip(tmp_path):
    save_settings(tmp_path, provider="ollama", model="llama3.1", base_url="http://x")
    assert read_settings(tmp_path) == {
        "provider": "ollama",
        "model": "llama3.1",
        "base_url": "http://x",
    }


def test_for_root_uses_saved_settings(tmp_path):
    save_settings(tmp_path, provider="openai", model="gpt-4o-mini")
    cfg = Config.for_root(tmp_path)
    assert (cfg.provider, cfg.model) == ("openai", "gpt-4o-mini")


def test_env_overrides_saved_settings(tmp_path, monkeypatch):
    save_settings(tmp_path, provider="openai", model="gpt-4o")
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    assert Config.for_root(tmp_path).provider == "ollama"


def test_empty_base_url_env_reads_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_BASE_URL", "")
    assert Config.for_root(tmp_path).base_url is None


def test_empty_base_url_env_falls_back_to_saved_settings(tmp_path, monkeypatch):
    # The point of the normalisation: an empty env var is *unset*, so the next
    # source in the precedence chain wins. Nulling it out would pass the test
    # above and still be wrong here.
    save_settings(tmp_path, provider="openai", model="gpt-4o", base_url="http://saved:1234")
    monkeypatch.setenv("VERINOTE_BASE_URL", "")
    assert Config.for_root(tmp_path).base_url == "http://saved:1234"


def test_whitespace_only_base_url_env_reads_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_BASE_URL", "   ")
    assert Config.for_root(tmp_path).base_url is None


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_empty_base_url_is_unset_for_every_provider(tmp_path, monkeypatch, provider):
    # The whole point of #293: one empty value, one meaning, whatever the
    # provider. claudecli never reads base_url at all, so this config-layer
    # assertion is the only meaningful guard for it.
    save_settings(tmp_path, provider=provider, model="m")
    monkeypatch.setenv("VERINOTE_BASE_URL", "")
    assert Config.for_root(tmp_path).base_url is None


def test_whitespace_only_saved_base_url_reads_as_unset(tmp_path, monkeypatch):
    # The Settings UI is the other door into the same bug: normalising only the
    # env source leaves a blank saved value reaching the SDK verbatim.
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider="openai", model="m", base_url="   ")
    assert Config.for_root(tmp_path).base_url is None


def test_padded_base_url_env_is_trimmed(tmp_path, monkeypatch):
    # Judging on the trimmed text but returning the raw one would yield a URL
    # with embedded spaces that no endpoint answers.
    monkeypatch.setenv("VERINOTE_BASE_URL", "  https://llm.internal/v1  ")
    assert Config.for_root(tmp_path).base_url == "https://llm.internal/v1"


def test_padded_saved_base_url_is_trimmed(tmp_path, monkeypatch):
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider="openai", model="m", base_url="  https://llm.internal/v1  ")
    assert Config.for_root(tmp_path).base_url == "https://llm.internal/v1"


def test_empty_provider_env_falls_back_instead_of_failing(tmp_path, monkeypatch):
    # Behaviour change: this used to reach the factory as "" and blow up with
    # `unknown VERINOTE_PROVIDER=''`.
    monkeypatch.setenv("VERINOTE_PROVIDER", "")
    assert Config.for_root(tmp_path).provider == "anthropic"

    save_settings(tmp_path, provider="ollama", model="llama3.1")
    assert Config.for_root(tmp_path).provider == "ollama"


def test_whitespace_only_provider_env_falls_back(tmp_path, monkeypatch):
    # The normalisation is not base_url-only: narrowing it to that one setting
    # would leave a blank provider reaching normalize_provider as "   ".
    monkeypatch.setenv("VERINOTE_PROVIDER", "   ")
    assert Config.for_root(tmp_path).provider == "anthropic"

    save_settings(tmp_path, provider="ollama", model="llama3.1")
    assert Config.for_root(tmp_path).provider == "ollama"


def test_padded_provider_env_is_trimmed(tmp_path, monkeypatch):
    # normalize_provider strips dashes and underscores but not whitespace, so
    # an untrimmed "  ollama  " reaches dispatch as an unknown provider.
    monkeypatch.setenv("VERINOTE_PROVIDER", "  ollama  ")
    assert Config.for_root(tmp_path).provider == "ollama"


def test_padded_model_env_is_trimmed(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    monkeypatch.setenv("VERINOTE_MODEL", "  gpt-4o  ")
    assert Config.for_root(tmp_path).model == "gpt-4o"


def test_empty_model_env_falls_back_to_provider_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_PROVIDER", "openai")
    monkeypatch.setenv("VERINOTE_MODEL", "")
    assert Config.for_root(tmp_path).model == "gpt-4o"


def test_custom_base_url_env_survives_normalisation(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/v1")
    assert Config.for_root(tmp_path).base_url == "https://llm.internal/v1"


def test_custom_base_url_from_settings_file_survives_normalisation(tmp_path):
    save_settings(tmp_path, provider="openai", model="gpt-4o", base_url="https://llm.internal/v1")
    assert Config.for_root(tmp_path).base_url == "https://llm.internal/v1"


def test_default_model_when_nothing_set(tmp_path):
    cfg = Config.for_root(tmp_path)  # no settings file, no env
    assert (cfg.provider, cfg.model) == ("anthropic", "claude-opus-4-8")
    assert cfg.llm_timeout_seconds == 600.0
    assert cfg.extraction_chunk_chars == 300
    assert cfg.extraction_chunk_overlap_chars == 40
    assert cfg.extraction_max_facts_per_chunk == 8
    assert cfg.auto_accept_recommendations is False


def test_llm_timeout_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_LLM_TIMEOUT", "900")
    assert Config.for_root(tmp_path).llm_timeout_seconds == 900.0


def test_extraction_settings_round_trip_and_env_override(tmp_path, monkeypatch):
    save_settings(
        tmp_path,
        provider="ollama",
        model="qwen3.5:9b",
        extraction_chunk_chars=450,
        extraction_chunk_overlap_chars=25,
        extraction_max_facts_per_chunk=6,
        auto_accept_recommendations=True,
    )

    cfg = Config.for_root(tmp_path)

    assert cfg.extraction_chunk_chars == 450
    assert cfg.extraction_chunk_overlap_chars == 25
    assert cfg.extraction_max_facts_per_chunk == 6
    assert cfg.auto_accept_recommendations is True

    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "200")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    monkeypatch.setenv("VERINOTE_EXTRACTION_MAX_FACTS_PER_CHUNK", "3")
    monkeypatch.setenv("VERINOTE_AUTO_ACCEPT_RECOMMENDATIONS", "false")
    cfg = Config.for_root(tmp_path)
    assert cfg.extraction_chunk_chars == 200
    assert cfg.extraction_chunk_overlap_chars == 0
    assert cfg.extraction_max_facts_per_chunk == 3
    assert cfg.auto_accept_recommendations is False


def test_claude_cli_provider_is_available():
    assert "claudecli" in PROVIDERS
    assert "claudecli" not in TESTABLE_PROVIDERS
    assert "ollama" in TESTABLE_PROVIDERS


def test_legacy_claude_provider_normalizes_to_claudecli(tmp_path):
    save_settings(tmp_path, provider="claude", model="")
    assert read_settings(tmp_path)["provider"] == "claudecli"
    assert Config.for_root(tmp_path).provider == "claudecli"


def test_api_key_only_from_env_never_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_API_KEY", "supersecret")
    save_settings(tmp_path, provider="anthropic", model="m")
    cfg = Config.for_root(tmp_path)
    assert cfg.api_key == "supersecret"
    assert "supersecret" not in (tmp_path / "config.json").read_text(encoding="utf-8")


def test_empty_api_key_env_reads_as_unset(tmp_path, monkeypatch):
    # #326: the key now shares the blank-value normalisation the other settings
    # get, so a blank VERINOTE_API_KEY is unset rather than an empty credential.
    monkeypatch.setenv("VERINOTE_API_KEY", "")
    assert Config.for_root(tmp_path).api_key is None


def test_whitespace_only_api_key_env_reads_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_API_KEY", "   ")
    assert Config.for_root(tmp_path).api_key is None


def test_padded_api_key_env_is_trimmed(tmp_path, monkeypatch):
    # Surrounding whitespace on a real key is always a copy-paste or .env-file
    # artifact, never part of the credential. Trimming makes an otherwise-valid
    # key authenticate instead of failing; this is a deliberate decision, not an
    # accidental side effect of routing through _pick.
    monkeypatch.setenv("VERINOTE_API_KEY", "  sk-secret  ")
    assert Config.for_root(tmp_path).api_key == "sk-secret"


def test_active_root_uses_env_first(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    assert active_root() == tmp_path.resolve()


def test_active_root_uses_app_config_when_kb_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "kb.sqlite").write_text("", encoding="utf-8")

    save_active_root(kb)

    if sys.platform == "darwin":
        expected = (
            tmp_path
            / "home"
            / "Library"
            / "Application Support"
            / "verinote"
            / "app.json"
        )
    elif sys.platform == "win32":
        expected = tmp_path / "appdata" / "verinote" / "app.json"
    else:
        expected = tmp_path / "xdg" / "verinote" / "app.json"
    assert app_config_path() == expected
    assert active_root() == kb.resolve()


def test_ui_config_is_none_without_selected_kb(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.chdir(tmp_path)

    assert Config.load_for_ui() is None


def _write_settings_raw(root, text):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(text, encoding="utf-8")


def test_read_settings_missing_file_is_silent(tmp_path, capsys):
    assert read_settings(tmp_path) == {}
    assert capsys.readouterr().err == ""


def test_read_settings_broken_json_warns_with_path(tmp_path, capsys):
    _write_settings_raw(tmp_path, "{bad")
    assert read_settings(tmp_path) == {}
    err = capsys.readouterr().err
    assert str(tmp_path / "config.json") in err
    assert "not valid JSON" in err
    assert "saved runtime settings" in err


def test_read_settings_invalid_utf8_warns(tmp_path, capsys):
    (tmp_path / "config.json").write_bytes(b"\xff\xfe\x00bad")
    assert read_settings(tmp_path) == {}
    err = capsys.readouterr().err
    assert str(tmp_path / "config.json") in err
    assert "could not decode" in err


def test_read_settings_non_dict_json_warns(tmp_path, capsys):
    _write_settings_raw(tmp_path, "[]")
    assert read_settings(tmp_path) == {}
    err = capsys.readouterr().err
    assert str(tmp_path / "config.json") in err
    assert "not a JSON object" in err


def test_read_settings_oserror_warns(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    def _boom(self, *args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert read_settings(tmp_path) == {}
    err = capsys.readouterr().err
    assert "could not read" in err
    assert str(tmp_path / "config.json") in err


def test_read_app_config_missing_file_is_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert read_app_config() == {}
    assert capsys.readouterr().err == ""


def test_read_app_config_broken_json_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad", encoding="utf-8")
    assert read_app_config() == {}
    err = capsys.readouterr().err
    assert str(path) in err
    assert "not valid JSON" in err
    assert "active KB" in err


def test_read_app_config_invalid_utf8_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00bad")
    assert read_app_config() == {}
    err = capsys.readouterr().err
    assert str(path) in err
    assert "could not decode" in err


def test_read_app_config_non_dict_json_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    assert read_app_config() == {}
    err = capsys.readouterr().err
    assert str(path) in err
    assert "not a JSON object" in err


def test_read_app_config_oserror_warns(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    def _boom(self, *args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert read_app_config() == {}
    err = capsys.readouterr().err
    assert "could not read" in err
    assert str(path) in err


# --- #269: a corrupt config.json must halt, not silently fall back to the cloud ---


def test_absent_config_sets_no_settings_error(tmp_path):
    # The critical discriminator: a fresh KB with no config.json is legitimate and
    # must never be flagged — a naive "flag whenever the dict is empty"
    # implementation would spuriously halt every brand-new KB.
    cfg = Config.for_root(tmp_path)
    assert cfg.settings_error is None
    assert_settings_intact(cfg)  # must not raise


def test_valid_config_sets_no_settings_error(tmp_path):
    save_settings(tmp_path, provider="ollama", model="llama3.1")
    cfg = Config.for_root(tmp_path)
    assert cfg.settings_error is None
    assert_settings_intact(cfg)  # must not raise


def test_broken_json_config_sets_settings_error_but_still_resolves(tmp_path, capsys):
    save_settings(tmp_path, provider="ollama", model="llama3.1")
    (tmp_path / "config.json").write_text("{bad", encoding="utf-8")
    capsys.readouterr()  # drop the loader's stderr warning

    cfg = Config.for_root(tmp_path)

    assert cfg.settings_error is not None
    assert str(tmp_path / "config.json") in cfg.settings_error
    assert "not valid JSON" in cfg.settings_error
    # The provider still resolves (to the built-in default) — the point is that a
    # halt guards that fallback, not that resolution itself fails.
    assert cfg.provider == "anthropic"


def test_invalid_utf8_config_sets_settings_error(tmp_path, capsys):
    (tmp_path / "config.json").write_bytes(b"\xff\xfe\x00bad")
    capsys.readouterr()
    cfg = Config.for_root(tmp_path)
    assert cfg.settings_error is not None
    assert "could not decode" in cfg.settings_error


def test_non_dict_config_sets_settings_error(tmp_path, capsys):
    (tmp_path / "config.json").write_text("[]", encoding="utf-8")
    capsys.readouterr()
    cfg = Config.for_root(tmp_path)
    assert cfg.settings_error is not None
    assert "not a JSON object" in cfg.settings_error


def test_assert_settings_intact_raises_only_when_error_set(tmp_path, capsys):
    (tmp_path / "config.json").write_text("{bad", encoding="utf-8")
    capsys.readouterr()
    corrupt = Config.for_root(tmp_path)

    with pytest.raises(ConfigCorruptError) as excinfo:
        assert_settings_intact(corrupt)
    # The raised message is the settings_error reason verbatim, so the CLI/web
    # surfaces cannot describe the same file in different words.
    assert str(excinfo.value) == corrupt.settings_error


def test_config_corrupt_error_is_not_an_llm_error():
    # Load-bearing: several `except LLMError` blocks wrap the very get_client()
    # sites this guards. If ConfigCorruptError were an LLMError it would be
    # swallowed there into a generic provider-failure message instead of reaching
    # the dedicated halt handler.
    assert not issubclass(ConfigCorruptError, LLMError)
    assert not isinstance(ConfigCorruptError("x"), LLMError)


@pytest.mark.parametrize(
    ("first", "later", "provider", "error_fragment"),
    [
        ("{bad", '{"provider":"ollama","model":"m"}', "anthropic", "not valid JSON"),
        ('{"provider":"ollama","model":"m"}', "{bad", "ollama", None),
        ('{"provider":123,"model":"m"}', '{"provider":"ollama","model":"m"}', "anthropic", "provider"),
    ],
)
def test_for_root_uses_one_config_snapshot_for_values_and_halt(
    tmp_path, monkeypatch, first, later, provider, error_fragment
):
    path = tmp_path / "config.json"
    path.write_text('{"provider":"ollama","model":"m"}', encoding="utf-8")
    original_read_text = Path.read_text
    original_loads = config_module.json.loads
    read_calls = 0
    loads_calls = 0

    def changing_read_text(self, *args, **kwargs):
        nonlocal read_calls
        if self == path:
            read_calls += 1
            return first if read_calls == 1 else later
        return original_read_text(self, *args, **kwargs)

    def counting_loads(value, *args, **kwargs):
        nonlocal loads_calls
        loads_calls += 1
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", changing_read_text)
    monkeypatch.setattr(config_module.json, "loads", counting_loads)

    cfg = Config.for_root(tmp_path)

    assert read_calls == 1
    assert loads_calls == 1
    assert cfg.provider == provider
    if error_fragment is None:
        assert cfg.settings_error is None
        assert_settings_intact(cfg)
    else:
        assert cfg.settings_error is not None
        assert error_fragment in cfg.settings_error
        with pytest.raises(ConfigCorruptError):
            assert_settings_intact(cfg)


# --- #325: a wrong-typed value in config.json is as unusable as a corrupt file ---


def _write_settings(root, payload):
    _write_settings_raw(root, json.dumps(payload))


# Wrong-typed values that are nonetheless truthy and plausible in a hand-edited
# file: the interesting cases are the ones a silent `if value:` guard would let
# through, not `null` and `0`.
_WRONG_FOR_STRING = [123, 0, 12.5, True, False, ["http://x"], {"url": "http://x"}]
_WRONG_FOR_INT = ["450", "", 4.5, True, False, [450], {"n": 450}]
_WRONG_FOR_BOOL = ["true", "false", "", 1, 0, ["true"], {"on": True}]

# The nullability split the policy turns on. `save_settings` writes
# `"base_url": null` and omits the extraction settings entirely, so null is a
# legitimate "unset" in those; it never writes a null provider or model, so one
# there is a present-but-unusable value.
_NULLABLE = [
    "base_url",
    "extraction_chunk_chars",
    "extraction_chunk_overlap_chars",
    "extraction_max_facts_per_chunk",
    "auto_accept_recommendations",
]
_NON_NULLABLE = ["provider", "model"]
# Settings that decide where a request goes: unusable here means halt, not
# default. The rest only tune local extraction.
_ROUTING = ["provider", "model", "base_url"]
_TUNING = [
    "extraction_chunk_chars",
    "extraction_chunk_overlap_chars",
    "extraction_max_facts_per_chunk",
    "auto_accept_recommendations",
]


def _valid_settings(**overrides):
    payload = {"provider": "ollama", "model": "llama3.1", "base_url": "http://x"}
    payload.update(overrides)
    return payload


# --- routing settings: unusable means the KB refuses to run ---


@pytest.mark.parametrize("value", _WRONG_FOR_STRING)
@pytest.mark.parametrize("key", _ROUTING)
def test_wrong_typed_routing_setting_halts_instead_of_defaulting(
    tmp_path, capsys, key, value
):
    # The whole point of the rework: dropping the key is what *causes* the
    # silent fallback, so dropping cannot be the remedy. `provider` resolving to
    # "anthropic" below is the leak this halt is the only thing standing in
    # front of.
    _write_settings(tmp_path, _valid_settings(**{key: value}))
    capsys.readouterr()

    cfg = Config.for_root(tmp_path)

    assert cfg.settings_error is not None
    assert key in cfg.settings_error
    assert str(tmp_path / "config.json") in cfg.settings_error
    with pytest.raises(ConfigCorruptError):
        assert_settings_intact(cfg)


@pytest.mark.parametrize("key", _NON_NULLABLE)
def test_null_routing_setting_halts_because_it_is_unusable_not_unset(
    tmp_path, capsys, key
):
    # `"provider": null` is present-but-unusable, not absent. Reading it as
    # "unset" is how a null lands on the anthropic cloud default.
    _write_settings(tmp_path, _valid_settings(**{key: None}))
    capsys.readouterr()

    cfg = Config.for_root(tmp_path)

    assert cfg.settings_error is not None
    assert key in cfg.settings_error
    assert "null" in cfg.settings_error
    with pytest.raises(ConfigCorruptError):
        assert_settings_intact(cfg)


@pytest.mark.parametrize("key", _ROUTING)
def test_wrong_typed_routing_setting_never_reaches_a_provider_client(
    tmp_path, capsys, key
):
    # The halt asserted through its real consumer, not through the flag: a
    # config that should refuse must not hand back a working cloud client.
    from verinote.llm.factory import get_client

    _write_settings(tmp_path, _valid_settings(**{key: 123}))
    capsys.readouterr()

    with pytest.raises(ConfigCorruptError):
        get_client(Config.for_root(tmp_path))


def test_wrong_typed_provider_would_otherwise_resolve_to_the_cloud_default(
    tmp_path, capsys
):
    # Pins the danger the halt exists for. If this ever stops holding, the
    # halt tests above could pass for the wrong reason.
    _write_settings(tmp_path, _valid_settings(provider=123))
    capsys.readouterr()

    assert Config.for_root(tmp_path).provider == "anthropic"


def test_env_override_does_not_excuse_an_unusable_saved_routing_setting(
    tmp_path, monkeypatch, capsys
):
    # An env var that happens to shadow the bad key does not make the file
    # usable — the same stance main already takes for a whole-file corruption.
    _write_settings(tmp_path, _valid_settings(base_url=123))
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/v1")
    capsys.readouterr()

    cfg = Config.for_root(tmp_path)

    assert cfg.base_url == "https://llm.internal/v1"
    assert cfg.settings_error is not None


def test_wrong_typed_base_url_does_not_reach_the_adapter(tmp_path, capsys):
    # The issue's reproduction: a number here used to survive `read_settings`
    # and blow up far away, in `OllamaAdapter.__init__`'s `rstrip`. The halt is
    # the remedy; this is the containment behind it.
    from verinote.llm.ollama_adapter import OllamaAdapter

    _write_settings(tmp_path, _valid_settings(base_url=123))
    capsys.readouterr()

    cfg = Config.for_root(tmp_path)
    assert cfg.base_url is None
    assert OllamaAdapter(cfg).base_url == "http://localhost:11434"


@pytest.mark.parametrize("key", _ROUTING)
def test_unusable_routing_setting_warns_that_the_kb_is_unusable(tmp_path, capsys, key):
    # The warning must not promise a harmless default, because there is none.
    _write_settings(tmp_path, _valid_settings(**{key: 123}))

    read_settings(tmp_path)

    err = capsys.readouterr().err
    assert key in err
    assert "falls back to its default" not in err
    assert "unusable" in err


def test_only_the_first_routing_reason_is_reported_and_it_is_stable(tmp_path, capsys):
    # Key order in the file must not change the diagnosis, or the same broken
    # config would describe itself differently on two machines.
    both = {"model": 1, "provider": 2, "base_url": 3}
    _write_settings(tmp_path, both)
    capsys.readouterr()
    first = Config.for_root(tmp_path).settings_error

    _write_settings(tmp_path, {"base_url": 3, "model": 1, "provider": 2})
    capsys.readouterr()

    assert Config.for_root(tmp_path).settings_error == first
    assert "provider" in first


# --- tuning settings: unusable means warn and use the default, not halt ---


@pytest.mark.parametrize("value", _WRONG_FOR_INT)
@pytest.mark.parametrize("key", _TUNING[:3])
def test_wrong_typed_int_setting_warns_and_defaults_without_halting(
    tmp_path, capsys, key, value
):
    # The discriminator that keeps the policy a *split* rather than "everything
    # halts": a bad chunk size changes how much local text a step reads, not
    # where the text goes, so it must not brick the KB.
    _write_settings(tmp_path, _valid_settings(**{key: value}))

    cfg = Config.for_root(tmp_path)

    assert key not in read_settings(tmp_path)
    assert cfg.settings_error is None
    assert_settings_intact(cfg)  # must not raise
    assert getattr(cfg, key) == {
        "extraction_chunk_chars": 300,
        "extraction_chunk_overlap_chars": 40,
        "extraction_max_facts_per_chunk": 8,
    }[key]
    assert key in capsys.readouterr().err


@pytest.mark.parametrize("value", _WRONG_FOR_BOOL)
def test_wrong_typed_bool_setting_warns_and_defaults_without_halting(
    tmp_path, capsys, value
):
    _write_settings(tmp_path, _valid_settings(auto_accept_recommendations=value))

    cfg = Config.for_root(tmp_path)

    assert "auto_accept_recommendations" not in read_settings(tmp_path)
    assert cfg.settings_error is None
    assert cfg.auto_accept_recommendations is False
    assert "auto_accept_recommendations" in capsys.readouterr().err


def test_json_true_is_not_a_chunk_size_and_json_one_is_not_a_flag(tmp_path, capsys):
    # `bool` is an `int` subclass in Python but a distinct JSON type. A plain
    # isinstance check would let each of these through as the other.
    _write_settings(
        tmp_path,
        _valid_settings(extraction_chunk_chars=True, auto_accept_recommendations=1),
    )

    cfg = Config.for_root(tmp_path)

    assert cfg.extraction_chunk_chars == 300
    assert cfg.auto_accept_recommendations is False


# --- what a valid file must keep doing ---


def test_correctly_typed_settings_survive_untouched(tmp_path, capsys):
    payload = _valid_settings(
        extraction_chunk_chars=450,
        extraction_chunk_overlap_chars=0,
        extraction_max_facts_per_chunk=6,
        auto_accept_recommendations=True,
    )
    _write_settings(tmp_path, payload)

    cfg = Config.for_root(tmp_path)

    assert read_settings(tmp_path) == payload
    assert cfg.settings_error is None
    assert (cfg.provider, cfg.model, cfg.base_url) == ("ollama", "llama3.1", "http://x")
    assert cfg.extraction_chunk_chars == 450
    assert cfg.extraction_chunk_overlap_chars == 0
    assert cfg.extraction_max_facts_per_chunk == 6
    assert cfg.auto_accept_recommendations is True
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("key", _NULLABLE)
def test_null_in_a_nullable_setting_is_unset_and_stays_silent(tmp_path, capsys, key):
    _write_settings(tmp_path, _valid_settings(**{key: None}))

    cfg = Config.for_root(tmp_path)

    assert read_settings(tmp_path)[key] is None
    assert cfg.settings_error is None
    assert_settings_intact(cfg)  # must not raise
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_nothing_save_settings_writes_can_halt_its_own_kb(tmp_path, capsys, provider):
    # The self-inflicted-halt guard: every shape the Settings UI can produce
    # must read back clean, including the `"base_url": null` it writes for a KB
    # with no custom endpoint and the extraction keys it omits entirely.
    save_settings(tmp_path, provider=provider, model="")
    bare = Config.for_root(tmp_path)

    save_settings(
        tmp_path,
        provider=provider,
        model="m",
        base_url="http://x",
        extraction_chunk_chars=450,
        extraction_chunk_overlap_chars=0,
        extraction_max_facts_per_chunk=6,
        auto_accept_recommendations=False,
    )
    full = Config.for_root(tmp_path)

    assert bare.settings_error is None
    assert full.settings_error is None
    assert_settings_intact(bare)
    assert_settings_intact(full)
    assert capsys.readouterr().err == ""


def test_unknown_setting_passes_through_and_never_halts(tmp_path, capsys):
    # A config written by a newer verinote must not be eaten or condemned by an
    # older one that has no opinion about its keys.
    _write_settings(tmp_path, _valid_settings(future_setting=7))

    cfg = Config.for_root(tmp_path)

    assert read_settings(tmp_path)["future_setting"] == 7
    assert cfg.settings_error is None
    assert capsys.readouterr().err == ""


def test_one_bad_tuning_setting_does_not_discard_the_good_ones(tmp_path, capsys):
    _write_settings(tmp_path, _valid_settings(extraction_chunk_chars="450"))

    saved = read_settings(tmp_path)

    assert saved["provider"] == "ollama"
    assert saved["base_url"] == "http://x"
    assert "extraction_chunk_chars" not in saved


@pytest.mark.parametrize(
    ("key", "attr", "expected"),
    [("provider", "provider", "ollama"), ("model", "model", "llama3.1")],
)
def test_saved_routing_values_still_go_through_the_trim_normalisation(
    tmp_path, monkeypatch, key, attr, expected
):
    # Type-checking the saved side must not become a bypass around `_pick`: a
    # padded saved value is a valid string, so it survives the type check and
    # must still be trimmed on the way out.
    monkeypatch.delenv("VERINOTE_PROVIDER", raising=False)
    monkeypatch.delenv("VERINOTE_MODEL", raising=False)
    _write_settings(tmp_path, _valid_settings(**{key: f"  {expected}  "}))

    assert getattr(Config.for_root(tmp_path), attr) == expected
