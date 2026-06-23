# SPDX-License-Identifier: MPL-2.0
from verinote.config import Config, read_settings, save_settings


def _clear_env(monkeypatch):
    for var in ("VERINOTE_PROVIDER", "VERINOTE_MODEL", "VERINOTE_BASE_URL", "VERINOTE_API_KEY"):
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


def test_api_key_only_from_env_never_persisted(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("VERINOTE_API_KEY", "supersecret")
    save_settings(tmp_path, provider="anthropic", model="m")
    cfg = Config.for_root(tmp_path)
    assert cfg.api_key == "supersecret"
    assert "supersecret" not in (tmp_path / "config.json").read_text(encoding="utf-8")
