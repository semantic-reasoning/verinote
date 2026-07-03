# SPDX-License-Identifier: MPL-2.0
"""Runtime configuration: where the KB lives and which LLM provider to use.

Resolved so nothing about the active provider is hard-coded — this is the
anti-lock-in seam. Precedence (highest first): environment variable, then the
saved non-secret settings file (`<root>/config.json`, written by the Settings
UI), then a built-in default. The API key is **only** ever read from the
environment — it is never persisted to or read from the settings file.

The active KB root is stored in a platform-native app config file when the web
UI selects one: Windows uses `%APPDATA%`, macOS uses `~/Library/Application
Support`, and Unix uses `${XDG_CONFIG_HOME:-~/.config}`. `VERINOTE_ROOT` still
overrides this for scripts and tests.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

SETTINGS_FILENAME = "config.json"
APP_CONFIG_FILENAME = "app.json"
APP_NAME = "verinote"

_MODEL_DEFAULTS = {
    "anthropic": "claude-opus-4-8",
    "claudecli": "",
    "openai": "gpt-4o",
    "ollama": "llama3.1",
}
PROVIDERS = tuple(_MODEL_DEFAULTS)
PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "claudecli": "ClaudeCLI",
    "openai": "OpenAI",
    "ollama": "Ollama",
}
TESTABLE_PROVIDERS = frozenset({"anthropic", "openai", "ollama"})


def normalize_provider(provider: str | None) -> str:
    """Canonical provider id used in config and dispatch."""
    key = (provider or "").replace("-", "").replace("_", "").lower()
    if key == "claude":
        return "claudecli"
    return key


def _default_root() -> Path:
    return Path("./data").expanduser().resolve()


def _root() -> Path:
    root = active_root()
    return root if root is not None else _default_root()


def app_config_dir() -> Path:
    """Return the platform-native directory for verinote's app-level config."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base).expanduser() / APP_NAME
        return Path.home() / "AppData" / "Roaming" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".config") / APP_NAME


def app_config_path() -> Path:
    return app_config_dir() / APP_CONFIG_FILENAME


def read_app_config() -> dict:
    path = app_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_active_root(root: Path) -> None:
    """Persist the active KB root outside any individual KB."""
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"active_root": str(Path(root).expanduser().resolve())}
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def active_root() -> Path | None:
    """Return the selected KB root, or None when the web UI should ask."""
    env_root = os.environ.get("VERINOTE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    saved = read_app_config().get("active_root")
    if saved:
        root = Path(str(saved)).expanduser().resolve()
        if (root / "kb.sqlite").is_file():
            return root

    default = _default_root()
    if (default / "kb.sqlite").is_file():
        return default
    return None


def _settings_path(root: Path) -> Path:
    return root / SETTINGS_FILENAME


def read_settings(root: Path) -> dict:
    """Read non-secret settings (provider/model/base_url), or {} if absent/bad."""
    path = _settings_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(
    root: Path, *, provider: str, model: str, base_url: str | None = None
) -> None:
    """Persist non-secret settings to `<root>/config.json` (never the API key)."""
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": normalize_provider(provider),
        "model": model,
        "base_url": base_url or None,
    }
    _settings_path(root).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _pick(env: str, saved: str | None, default: str | None) -> str | None:
    value = os.environ.get(env)
    if value is not None:
        return value
    if saved:
        return saved
    return default


def _llm_timeout_seconds() -> float:
    raw = os.environ.get("VERINOTE_LLM_TIMEOUT")
    if raw is None:
        return 600.0
    try:
        value = float(raw)
    except ValueError:
        return 600.0
    return value if value > 0 else 600.0


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    provider: str  # "anthropic" | "claudecli" | "openai" | "ollama"
    model: str
    api_key: str | None
    base_url: str | None
    llm_timeout_seconds: float = 600.0

    @classmethod
    def for_root(cls, root: Path) -> "Config":
        saved = read_settings(root)
        provider = normalize_provider(
            _pick("VERINOTE_PROVIDER", saved.get("provider"), "anthropic")
        )
        model = _pick("VERINOTE_MODEL", saved.get("model"), _MODEL_DEFAULTS.get(provider, "")) or ""
        base_url = _pick("VERINOTE_BASE_URL", saved.get("base_url"), None)
        return cls(
            root=root,
            db_path=root / "kb.sqlite",
            provider=provider,
            model=model,
            api_key=os.environ.get("VERINOTE_API_KEY"),  # secrets only from env
            base_url=base_url,
            llm_timeout_seconds=_llm_timeout_seconds(),
        )

    @classmethod
    def load(cls) -> "Config":
        return cls.for_root(_root())

    @classmethod
    def load_for_ui(cls) -> "Config | None":
        root = active_root()
        return cls.for_root(root) if root is not None else None
