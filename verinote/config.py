# SPDX-License-Identifier: MPL-2.0
"""Runtime configuration: where the KB lives and which LLM provider to use.

Resolved so nothing about the active provider is hard-coded — this is the
anti-lock-in seam. Precedence (highest first): environment variable, then the
saved non-secret settings file (`<root>/config.json`, written by the Settings
UI), then a built-in default. The API key is **only** ever read from the
environment — it is never persisted to or read from the settings file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

SETTINGS_FILENAME = "config.json"

_MODEL_DEFAULTS = {
    "anthropic": "claude-opus-4-8",
    "claude": "",
    "openai": "gpt-4o",
    "ollama": "llama3.1",
}
PROVIDERS = tuple(_MODEL_DEFAULTS)


def _root() -> Path:
    return Path(os.environ.get("VERINOTE_ROOT", "./data")).expanduser().resolve()


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
    payload = {"provider": provider, "model": model, "base_url": base_url or None}
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


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    provider: str  # "anthropic" | "claude" | "openai" | "ollama"
    model: str
    api_key: str | None
    base_url: str | None

    @classmethod
    def for_root(cls, root: Path) -> "Config":
        saved = read_settings(root)
        provider = (_pick("VERINOTE_PROVIDER", saved.get("provider"), "anthropic") or "").lower()
        model = _pick("VERINOTE_MODEL", saved.get("model"), _MODEL_DEFAULTS.get(provider, "")) or ""
        base_url = _pick("VERINOTE_BASE_URL", saved.get("base_url"), None)
        return cls(
            root=root,
            db_path=root / "kb.sqlite",
            provider=provider,
            model=model,
            api_key=os.environ.get("VERINOTE_API_KEY"),  # secrets only from env
            base_url=base_url,
        )

    @classmethod
    def load(cls) -> "Config":
        return cls.for_root(_root())
