# SPDX-License-Identifier: MPL-2.0
import sys

from verinote.config import (
    Config,
    PROVIDERS,
    TESTABLE_PROVIDERS,
    active_root,
    app_config_path,
    read_settings,
    save_active_root,
    save_settings,
)


def _clear_env(monkeypatch):
    for var in (
        "VERINOTE_PROVIDER",
        "VERINOTE_MODEL",
        "VERINOTE_BASE_URL",
        "VERINOTE_API_KEY",
        "VERINOTE_LLM_TIMEOUT",
        "VERINOTE_EXTRACTION_CHUNK_CHARS",
        "VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS",
        "VERINOTE_EXTRACTION_MAX_FACTS_PER_CHUNK",
        "VERINOTE_AUTO_ACCEPT_RECOMMENDATIONS",
        "VERINOTE_ROOT",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_save_and_read_round_trip(tmp_path):
    save_settings(tmp_path, provider="ollama", model="llama3.1", base_url="http://x")
    assert read_settings(tmp_path) == {
        "provider": "ollama",
        "model": "llama3.1",
        "base_url": "http://x",
    }


def test_for_root_uses_saved_settings(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    save_settings(tmp_path, provider="openai", model="gpt-4o-mini")
    cfg = Config.for_root(tmp_path)
    assert (cfg.provider, cfg.model) == ("openai", "gpt-4o-mini")


def test_env_overrides_saved_settings(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    save_settings(tmp_path, provider="openai", model="gpt-4o")
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    assert Config.for_root(tmp_path).provider == "ollama"


def test_default_model_when_nothing_set(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    cfg = Config.for_root(tmp_path)  # no settings file, no env
    assert (cfg.provider, cfg.model) == ("anthropic", "claude-opus-4-8")
    assert cfg.llm_timeout_seconds == 600.0
    assert cfg.extraction_chunk_chars == 300
    assert cfg.extraction_chunk_overlap_chars == 40
    assert cfg.extraction_max_facts_per_chunk == 8
    assert cfg.auto_accept_recommendations is False


def test_llm_timeout_env_override(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERINOTE_LLM_TIMEOUT", "900")
    assert Config.for_root(tmp_path).llm_timeout_seconds == 900.0


def test_extraction_settings_round_trip_and_env_override(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
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


def test_legacy_claude_provider_normalizes_to_claudecli(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    save_settings(tmp_path, provider="claude", model="")
    assert read_settings(tmp_path)["provider"] == "claudecli"
    assert Config.for_root(tmp_path).provider == "claudecli"


def test_api_key_only_from_env_never_persisted(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERINOTE_API_KEY", "supersecret")
    save_settings(tmp_path, provider="anthropic", model="m")
    cfg = Config.for_root(tmp_path)
    assert cfg.api_key == "supersecret"
    assert "supersecret" not in (tmp_path / "config.json").read_text(encoding="utf-8")


def test_active_root_uses_env_first(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    assert active_root() == tmp_path.resolve()


def test_active_root_uses_app_config_when_kb_exists(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
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
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.chdir(tmp_path)

    assert Config.load_for_ui() is None
