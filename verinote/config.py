# SPDX-License-Identifier: MPL-2.0
"""Runtime configuration: where the KB lives and which LLM provider to use.

Resolved so nothing about the active provider is hard-coded — this is the
anti-lock-in seam. Precedence (highest first): environment variable, then the
saved non-secret settings file (`<root>/config.json`, written by the Settings
UI), then a built-in default. A blank (empty or whitespace-only) provider,
model, or base URL value counts as unset and falls through the chain, so
`VERINOTE_BASE_URL=` means the same thing to every provider. Numeric and boolean
settings use the environment value when present, otherwise the saved file, then
the default; if the selected value is blank or an invalid number, numeric
parsers fall back to the default, while the boolean parser treats recognised
truthy strings as true and everything else as false. The API key is **only**
ever read from the environment — it is never persisted to or read from the
settings file — but it shares the same blank-value handling: a blank (empty or
whitespace-only) `VERINOTE_API_KEY` normalises to unset (`None`) and a used
value is trimmed. With no saved or default source to fall back to, a blank key
simply becomes `None` rather than falling through a chain.

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

from verinote.prompts import render_prompt

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


def local_root(explicit: Path | str | None = None) -> Path:
    """Resolve the KB root for *local* commands that must never target a saved KB.

    Commands like `init` and `seed` act on a location the caller names, not on
    whatever KB the web UI last selected. Precedence (highest first): the
    explicit argument, `VERINOTE_ROOT`, then `./data` relative to the current
    working directory. The saved app config (`active_root()`) is deliberately
    **not** consulted — otherwise `verinote init` in an empty directory would
    write into somebody else's KB.

    A blank explicit root raises `ValueError`: falling back would write the KB
    somewhere the caller never named, which is exactly what these commands
    promise not to do.
    """
    if explicit is not None:
        if isinstance(explicit, str) and not explicit.strip():
            raise ValueError("KB root must be a path, not an empty string")
        return Path(explicit).expanduser().resolve()
    env_root = os.environ.get("VERINOTE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return _default_root()


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


def _warn_bad_config(path: Path, error: Exception | None, consequence: str) -> None:
    """Warn on stderr that a config file is unreadable and is being ignored.

    Absence is silent (a missing file is normal); this is only for a file that
    exists but cannot be used, so a silent fallback to defaults does not hide a
    real corruption from the user. `error` is the raised exception, or None for
    a well-formed JSON value that simply is not an object.
    """
    if isinstance(error, OSError):
        reason = f"could not read {path}: {error}"
    elif isinstance(error, UnicodeDecodeError):
        reason = f"could not decode {path} as UTF-8; ignoring it"
    elif isinstance(error, json.JSONDecodeError):
        reason = f"{path} is not valid JSON; ignoring it"
    else:
        reason = f"{path} is not a JSON object; ignoring it"
    print(f"warning: {reason}; {consequence}", file=sys.stderr)


def read_app_config() -> dict:
    path = app_config_path()
    if not path.is_file():
        return {}
    consequence = "the saved active KB will be ignored"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        _warn_bad_config(path, err, consequence)
        return {}
    except UnicodeDecodeError as err:
        _warn_bad_config(path, err, consequence)
        return {}
    except json.JSONDecodeError as err:
        _warn_bad_config(path, err, consequence)
        return {}
    if not isinstance(data, dict):
        _warn_bad_config(path, None, consequence)
        return {}
    return data


def _normalize_root(value: Path | str) -> Path:
    """Resolve a root the one way the app interprets it.

    Both the reader and the writer must normalize identically; if they drift,
    a saved symlink or relative path stops matching the root it resolves to.
    """
    return Path(str(value)).expanduser().resolve()


def _saved_root(config: dict) -> Path | None:
    """Read the saved root the one way the app interprets it, or None.

    An absent or empty value selects nothing. Reading it here keeps the reader
    and the writer from disagreeing about what a stored value means.
    """
    saved = config.get("active_root")
    if not saved:
        return None
    return _normalize_root(str(saved))


def save_active_root(root: Path) -> None:
    """Persist the active KB root outside any individual KB.

    Reselecting the KB that is already active is a no-op: rewriting the same
    value would churn the machine-wide `app.json` (mtime, and any other
    process watching it) for no change. Only an actual switch touches the file.
    The saved value is compared as `active_root()` resolves it, so a config
    holding a symlink or a relative path to the target counts as unchanged.
    """
    resolved = _normalize_root(root)
    existing = read_app_config()
    if _saved_root(existing) == resolved:
        return
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**existing, "active_root": str(resolved)}
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def active_root() -> Path | None:
    """Return the selected KB root, or None when the web UI should ask."""
    env_root = os.environ.get("VERINOTE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    saved = _saved_root(read_app_config())
    if saved is not None and (saved / "kb.sqlite").is_file():
        return saved

    default = _default_root()
    if (default / "kb.sqlite").is_file():
        return default
    return None


def _settings_path(root: Path) -> Path:
    return root / SETTINGS_FILENAME


_SETTINGS_TYPES: dict[str, type] = {
    "provider": str,
    "model": str,
    "base_url": str,
    "extraction_chunk_chars": int,
    "extraction_chunk_overlap_chars": int,
    "extraction_max_facts_per_chunk": int,
    "auto_accept_recommendations": bool,
}

_EXPECTED_NAMES = {str: "a string", int: "a whole number", bool: "true or false"}


def _json_type_name(value: object) -> str:
    """Name the JSON type a Python value came from, for the warning text."""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return "null"


def _has_type(value: object, expected: type) -> bool:
    """Check a value against the JSON type a setting is declared to hold.

    `bool` is a subclass of `int` in Python but a distinct type in JSON, so a
    plain `isinstance` would let `true` pass as a chunk size and `1` pass as a
    flag. Both directions are rejected here.
    """
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def _warn_bad_setting(path: Path, key: str, value: object, expected: type) -> None:
    print(
        f"warning: {path} has {key} as {_json_type_name(value)}, expected "
        f"{_EXPECTED_NAMES[expected]}; ignoring it, so {key} falls back to its default",
        file=sys.stderr,
    )


def _checked_settings(path: Path, data: dict) -> dict:
    """Drop settings whose value is the wrong type, warning about each.

    This file is the boundary where untrusted JSON enters: a hand-edited (or
    older-version-written) `config.json` can hold `"base_url": 123`, and
    without this the failure surfaces far from its cause, as an `AttributeError`
    inside whichever adapter finally calls a string method on it. Rejecting per
    key here is the same warn-and-ignore policy the whole-file checks already
    use, just finer grained. `null` means *unset* rather than a type error —
    `save_settings` itself writes `"base_url": null` — and unknown keys pass
    through untouched, since this reader should not silently eat a key a newer
    version wrote.
    """
    checked = {}
    for key, value in data.items():
        expected = _SETTINGS_TYPES.get(key)
        if expected is None or value is None or _has_type(value, expected):
            checked[key] = value
        else:
            _warn_bad_setting(path, key, value, expected)
    return checked


def read_settings(root: Path) -> dict:
    """Read saved non-secret runtime settings, or {} if absent/bad.

    Individual settings whose value has the wrong type are warned about and
    dropped, so the caller sees them as unset rather than passing a number on
    to code that expects a string. See `_checked_settings`.
    """
    path = _settings_path(root)
    if not path.is_file():
        return {}
    consequence = "saved runtime settings will fall back to defaults"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        _warn_bad_config(path, err, consequence)
        return {}
    except UnicodeDecodeError as err:
        _warn_bad_config(path, err, consequence)
        return {}
    except json.JSONDecodeError as err:
        _warn_bad_config(path, err, consequence)
        return {}
    if not isinstance(data, dict):
        _warn_bad_config(path, None, consequence)
        return {}
    return _checked_settings(path, data)


def save_settings(
    root: Path,
    *,
    provider: str,
    model: str,
    base_url: str | None = None,
    extraction_chunk_chars: int | None = None,
    extraction_chunk_overlap_chars: int | None = None,
    extraction_max_facts_per_chunk: int | None = None,
    auto_accept_recommendations: bool | None = None,
) -> None:
    """Persist non-secret settings to `<root>/config.json` (never the API key)."""
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": normalize_provider(provider),
        "model": model,
        "base_url": base_url or None,
    }
    if extraction_chunk_chars is not None:
        payload["extraction_chunk_chars"] = extraction_chunk_chars
    if extraction_chunk_overlap_chars is not None:
        payload["extraction_chunk_overlap_chars"] = extraction_chunk_overlap_chars
    if extraction_max_facts_per_chunk is not None:
        payload["extraction_max_facts_per_chunk"] = extraction_max_facts_per_chunk
    if auto_accept_recommendations is not None:
        payload["auto_accept_recommendations"] = bool(auto_accept_recommendations)
    _settings_path(root).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _pick(env: str, saved: str | None, default: str | None) -> str | None:
    """Resolve one setting: environment, then the saved file, then the default.

    A blank (empty or whitespace-only) value means *unset* from either source,
    so it falls through rather than being passed on as `""`. That is what the
    rest of this module already does, and it is what `export VAR=` in a CI or
    Docker env file means. A value that is used is trimmed: judging on the
    trimmed text but returning the raw one would let `" https://x "` through as
    a URL that no endpoint answers.
    """
    for candidate in (os.environ.get(env), saved):
        # A hand-edited config.json can hold a non-string here. Passing those
        # through untouched is what this function has always done; normalising
        # them is a separate question from the blank-value one.
        if isinstance(candidate, str):
            candidate = candidate.strip()
        if candidate:
            return candidate
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


def _pick_int(env: str, saved: object, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(env)
    if raw is None:
        raw = saved
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _pick_bool(env: str, saved: object, default: bool) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        raw = saved
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    provider: str  # "anthropic" | "claudecli" | "openai" | "ollama"
    model: str
    api_key: str | None
    base_url: str | None
    llm_timeout_seconds: float = 600.0
    extraction_chunk_chars: int = 300
    extraction_chunk_overlap_chars: int = 40
    extraction_max_facts_per_chunk: int = 8
    auto_accept_recommendations: bool = False

    def extraction_schema_hint(self) -> str:
        return render_prompt(
            self.root,
            "extraction-limit-hint",
            max_facts=self.extraction_max_facts_per_chunk,
        )

    @classmethod
    def for_root(cls, root: Path) -> "Config":
        saved = read_settings(root)
        provider = normalize_provider(
            _pick("VERINOTE_PROVIDER", saved.get("provider"), "anthropic")
        )
        model = _pick("VERINOTE_MODEL", saved.get("model"), _MODEL_DEFAULTS.get(provider, "")) or ""
        base_url = _pick("VERINOTE_BASE_URL", saved.get("base_url"), None)
        chunk_chars = _pick_int(
            "VERINOTE_EXTRACTION_CHUNK_CHARS",
            saved.get("extraction_chunk_chars"),
            300,
        )
        chunk_overlap = _pick_int(
            "VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS",
            saved.get("extraction_chunk_overlap_chars"),
            40,
            minimum=0,
        )
        max_facts = _pick_int(
            "VERINOTE_EXTRACTION_MAX_FACTS_PER_CHUNK",
            saved.get("extraction_max_facts_per_chunk"),
            8,
        )
        auto_accept = _pick_bool(
            "VERINOTE_AUTO_ACCEPT_RECOMMENDATIONS",
            saved.get("auto_accept_recommendations"),
            False,
        )
        return cls(
            root=root,
            db_path=root / "kb.sqlite",
            provider=provider,
            model=model,
            api_key=_pick("VERINOTE_API_KEY", None, None),  # no saved/default source — secrets only from env
            base_url=base_url,
            llm_timeout_seconds=_llm_timeout_seconds(),
            extraction_chunk_chars=chunk_chars,
            extraction_chunk_overlap_chars=chunk_overlap,
            extraction_max_facts_per_chunk=max_facts,
            auto_accept_recommendations=auto_accept,
        )

    @classmethod
    def load(cls) -> "Config":
        return cls.for_root(_root())

    @classmethod
    def load_for_ui(cls) -> "Config | None":
        root = active_root()
        return cls.for_root(root) if root is not None else None
