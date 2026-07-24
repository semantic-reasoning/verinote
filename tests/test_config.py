# SPDX-License-Identifier: MPL-2.0
import json
import sys

import pytest

from verinote.config import (
    Config,
    PROVIDERS,
    TESTABLE_PROVIDERS,
    active_root,
    app_config_path,
    read_app_config,
    read_settings,
    save_active_root,
    save_settings,
)


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


def _write_settings(root, payload):
    _write_settings_raw(root, json.dumps(payload))


# Wrong-typed values that are nonetheless truthy and plausible in a hand-edited
# file: the interesting cases are the ones a silent `if value:` guard would let
# through, not `null` and `0`.
_WRONG_FOR_STRING = [123, 0, 12.5, True, False, ["http://x"], {"url": "http://x"}]
_WRONG_FOR_INT = ["450", "", 4.5, True, False, [450], {"n": 450}]
_WRONG_FOR_BOOL = ["true", "false", "", 1, 0, ["true"], {"on": True}]


@pytest.mark.parametrize("value", _WRONG_FOR_STRING)
@pytest.mark.parametrize("key", ["provider", "model", "base_url"])
def test_wrong_typed_string_setting_is_dropped(tmp_path, capsys, key, value):
    _write_settings(tmp_path, {key: value})

    assert key not in read_settings(tmp_path)

    err = capsys.readouterr().err
    assert str(tmp_path / "config.json") in err
    assert key in err


@pytest.mark.parametrize("value", _WRONG_FOR_INT)
@pytest.mark.parametrize(
    "key",
    [
        "extraction_chunk_chars",
        "extraction_chunk_overlap_chars",
        "extraction_max_facts_per_chunk",
    ],
)
def test_wrong_typed_int_setting_is_dropped(tmp_path, capsys, key, value):
    _write_settings(tmp_path, {key: value})

    assert key not in read_settings(tmp_path)
    assert key in capsys.readouterr().err


@pytest.mark.parametrize("value", _WRONG_FOR_BOOL)
def test_wrong_typed_bool_setting_is_dropped(tmp_path, capsys, value):
    _write_settings(tmp_path, {"auto_accept_recommendations": value})

    assert "auto_accept_recommendations" not in read_settings(tmp_path)
    assert "auto_accept_recommendations" in capsys.readouterr().err


def test_correctly_typed_settings_survive(tmp_path, capsys):
    payload = {
        "provider": "ollama",
        "model": "llama3.1",
        "base_url": "http://x",
        "extraction_chunk_chars": 450,
        "extraction_chunk_overlap_chars": 0,
        "extraction_max_facts_per_chunk": 6,
        "auto_accept_recommendations": True,
    }
    _write_settings(tmp_path, payload)

    assert read_settings(tmp_path) == payload
    assert capsys.readouterr().err == ""


def test_null_setting_is_unset_not_a_type_error(tmp_path, capsys):
    # `save_settings` writes `"base_url": null` itself, so null must stay a
    # silent "unset" rather than becoming a warning about its own output.
    _write_settings(tmp_path, {"provider": "ollama", "model": "m", "base_url": None})

    assert read_settings(tmp_path)["base_url"] is None
    assert capsys.readouterr().err == ""
    assert Config.for_root(tmp_path).base_url is None


def test_unknown_setting_passes_through_untouched(tmp_path, capsys):
    _write_settings(tmp_path, {"provider": "ollama", "future_setting": 7})

    assert read_settings(tmp_path)["future_setting"] == 7
    assert capsys.readouterr().err == ""


def test_one_bad_setting_does_not_discard_the_good_ones(tmp_path):
    _write_settings(tmp_path, {"provider": "ollama", "base_url": 123})

    saved = read_settings(tmp_path)
    assert saved["provider"] == "ollama"
    assert "base_url" not in saved


def test_numeric_provider_falls_back_instead_of_reaching_normalisation(tmp_path):
    _write_settings(tmp_path, {"provider": 123, "model": "m"})

    assert Config.for_root(tmp_path).provider == "anthropic"


def test_numeric_base_url_does_not_reach_the_adapter(tmp_path):
    # The issue's reproduction: a number here used to survive `read_settings`
    # and blow up far away, in `OllamaAdapter.__init__`'s `rstrip`.
    from verinote.llm.ollama_adapter import OllamaAdapter

    _write_settings(tmp_path, {"provider": "ollama", "model": "llama3.1", "base_url": 123})

    cfg = Config.for_root(tmp_path)
    assert cfg.base_url is None
    assert OllamaAdapter(cfg).base_url == "http://localhost:11434"


def test_wrong_typed_numeric_settings_fall_back_to_defaults(tmp_path):
    _write_settings(
        tmp_path,
        {
            "extraction_chunk_chars": "450",
            "extraction_chunk_overlap_chars": [1],
            "extraction_max_facts_per_chunk": True,
            "auto_accept_recommendations": "true",
        },
    )

    cfg = Config.for_root(tmp_path)

    assert cfg.extraction_chunk_chars == 300
    assert cfg.extraction_chunk_overlap_chars == 40
    assert cfg.extraction_max_facts_per_chunk == 8
    assert cfg.auto_accept_recommendations is False


def test_env_still_wins_over_a_dropped_setting(tmp_path, monkeypatch):
    _write_settings(tmp_path, {"provider": "ollama", "base_url": 123})
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/v1")

    assert Config.for_root(tmp_path).base_url == "https://llm.internal/v1"
