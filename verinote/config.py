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
truthy strings as true and everything else as false. The API key is never in
the settings file and never inside a KB — a KB gets synced and shared — but it
is not environment-only either: it resolves from the provider-scoped
`VERINOTE_<PROVIDER>_API_KEY`, then a key saved in the machine-wide
`credentials.json` beside `app.json`, then the legacy provider-agnostic
`VERINOTE_API_KEY`. That last tier sits *below* the saved key deliberately,
against this module's usual env-first rule: naming no provider, it would
otherwise replace a key saved for one provider with whatever single value
happens to be exported. Every tier shares the blank-value handling — a blank
value normalises to unset and a used value is trimmed.

The saved file is also type-checked per key, since it is hand-editable JSON.
A setting whose value has the wrong JSON type is never handed on to the code
that expects the right one. What happens next depends on the setting: a bad
`provider`, `model`, or `base_url` makes the whole KB refuse to run
(`settings_error`), because ignoring it would resolve to the built-in cloud
default the user never chose; a bad extraction setting only warns and falls back
to its default, which is what the numeric parsers already do for an unparsable
value. `null` means "unset" only where `save_settings` can write it —
`base_url` and the optional extraction settings — and counts as unusable in
`provider` and `model`.

CLI and non-UI runtime paths resolve their KB root consistently: an explicit
root (the CLI's `--root`) wins, then `VERINOTE_ROOT`, then a CWD-independent
platform user-data location. The web UI's separately selected active KB remains
stored in a platform-native app config file: Windows uses `%APPDATA%`, macOS
uses `~/Library/Application Support`, and Unix uses
`${XDG_CONFIG_HOME:-~/.config}`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager

try:  # POSIX only; the credentials lock degrades to atomic-replace without it
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
from dataclasses import dataclass, field
from pathlib import Path

from verinote.kb_location import resolve_kb_root
from verinote.prompts import render_prompt

SETTINGS_FILENAME = "config.json"
APP_CONFIG_FILENAME = "app.json"
CREDENTIALS_FILENAME = "credentials.json"
CREDENTIALS_VERSION = 1
APP_NAME = "verinote"
APP_THEMES = ("system", "light", "dark")

_MODEL_DEFAULTS = {
    "anthropic": "claude-opus-4-8",
    "claudecli": "",
    "openai": "gpt-4o",
    # A concrete free model that advertises the structured output verinote
    # forces, NOT the `openrouter/free` router. A router picks a different model
    # per request, so the settings banner's "answered ... from <model>" and a
    # captured fixture's recorded model would both name something that did not
    # answer — and one Test connection could not stand for the next request.
    #
    # Chosen because *every* endpoint serving it advertises structured output —
    # it has exactly one. More endpoints is the wrong criterion here: OpenRouter
    # routes across them per request, and `google/gemma-4-26b-a4b-it:free`, the
    # only free structured-output model with two, is served by one endpoint that
    # advertises it and one that does not. Being routed to the second would drop
    # the schema, so a Test connection that passed would say nothing about the
    # next extraction — the same failure the router above is rejected for.
    #
    # What is verified is that OpenRouter *advertises* structured output for this
    # model and its endpoint. Whether the endpoint honours `strict` is only
    # knowable by running it, which is what Test connection is for.
    "openrouter": "openai/gpt-oss-20b:free",
    "ollama": "llama3.1",
}
PROVIDERS = tuple(_MODEL_DEFAULTS)
PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "claudecli": "ClaudeCLI",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
}
# Providers whose configuration can be checked by actually running it. `claudecli`
# belongs here for the same reason the others do -- the check is one real
# extraction -- and it matters most there: its installed models cannot be
# enumerated, so running the chosen one is the only way to learn it works.
TESTABLE_PROVIDERS = frozenset({"anthropic", "claudecli", "openai", "openrouter", "ollama"})
# Providers whose models can be enumerated from the very endpoint the user
# configured, so the settings UI can offer a picker instead of free text.
# `anthropic` and `openai` are absent because a vendor's own catalogue is not a
# property of the endpoint being configured, and `claudecli` because it has no
# listing at all, so neither can be answered truthfully for them.
#
# `openrouter` is the exception that does not generalise: its catalogue IS
# served by the endpoint being configured (`GET {base_url}/models`, unauthenticated),
# so the "a cloud catalogue is never endpoint-served" reasoning does not cover it.
# What that listing enumerates is the published catalogue, not what this user's
# account may call — the request carries no key, which is also what keeps the
# listing seam keyless (`web.app._MODEL_LISTERS`).
MODEL_LISTING_PROVIDERS = frozenset({"ollama", "openrouter"})
# Providers that authenticate with an API key. The others never read
# `cfg.api_key` at all (`claudecli` shells out, `ollama` talks to a local
# endpoint), so resolving a key for them could only produce a value that cannot
# be used and can leak — resolution returns None for them unconditionally.
PROVIDERS_REQUIRING_KEY = frozenset({"anthropic", "openai", "openrouter"})
_KEYLESS_PROVIDERS = frozenset({"claudecli", "ollama"})


def _check_every_provider_is_classified(providers, requiring, keyless) -> None:
    """Every provider must be on exactly one side of "needs an API key".

    A *partition* check, not a subset one. A subset assertion passes when a new
    provider is added to `_MODEL_DEFAULTS` and classified nowhere — which is the
    one case that must not slip through, because resolution would silently
    return None for it and the provider would be called unauthenticated.

    A function so it can be tested against a hypothetical provider set; raised
    rather than asserted so `python -O` cannot strip the guard whose whole job
    is to fail loudly.
    """
    # Both directions, and the contradiction: a provider missing from both sets
    # would resolve to None silently, one in a set but not in PROVIDERS is a
    # stale entry, and one in *both* sets is classified ambiguously — which a
    # union-based check cannot see, because the union looks complete.
    wrong = (set(providers) ^ (set(requiring) | set(keyless))) | (
        set(requiring) & set(keyless)
    )
    if wrong:
        raise RuntimeError(
            "every provider must be classified as needing an API key or not, "
            f"and exactly once: {sorted(wrong)}"
        )


_check_every_provider_is_classified(PROVIDERS, PROVIDERS_REQUIRING_KEY, _KEYLESS_PROVIDERS)


def normalize_provider(provider: str | None) -> str:
    """Canonical provider id used in config and dispatch."""
    key = (provider or "").replace("-", "").replace("_", "").lower()
    if key == "claude":
        return "claudecli"
    return key


def _root() -> Path:
    return resolve_kb_root()


def local_root(explicit: Path | str | None = None) -> Path:
    """Backward-compatible name for the single CLI/config KB root resolver."""
    return resolve_kb_root(explicit)


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


def credentials_path() -> Path:
    """Where stored API keys live: beside `app.json`, never inside a KB.

    A KB is user data that gets synced, copied and shared; the app config
    directory is machine-local. Keeping the two apart is what lets
    `config.json` stay safe to hand to somebody else.
    """
    return app_config_dir() / CREDENTIALS_FILENAME


def provider_key_env_var(provider: str) -> str:
    """The provider-scoped environment variable name for `provider`'s key."""
    return f"VERINOTE_{provider.upper()}_API_KEY"


def _bad_config_reason(path: Path, error: Exception | None) -> str:
    """Classify why a *present* config file cannot be used, as one plain line.

    The single source of that classification: the CLI's stderr warning
    (`_warn_bad_config`) and the web/GUI halt text (`Config.settings_error`) both
    derive from it, so the two surfaces can never describe the same corrupt file
    in different words. `error` is the raised exception, or None for a well-formed
    JSON value that simply is not an object.
    """
    if isinstance(error, OSError):
        return f"could not read {path}: {error}"
    if isinstance(error, UnicodeDecodeError):
        return f"could not decode {path} as UTF-8"
    if isinstance(error, json.JSONDecodeError):
        return f"{path} is not valid JSON"
    return f"{path} is not a JSON object"


def _warn_config(reason: str, consequence: str) -> None:
    """Emit one config warning in the single shape every config warning has.

    Whole-file and per-key problems differ in how they are classified, not in
    how they are announced, so both go through here.
    """
    print(f"warning: {reason}; {consequence}", file=sys.stderr)


def _warn_bad_config(path: Path, error: Exception | None, consequence: str) -> None:
    """Warn on stderr that a config file is unreadable and is being ignored.

    Absence is silent (a missing file is normal); this is only for a file that
    exists but cannot be used, so a silent fallback to defaults does not hide a
    real corruption from the user. `error` is the raised exception, or None for
    a well-formed JSON value that simply is not an object.
    """
    _warn_config(_bad_config_reason(path, error), consequence)


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


def _write_json_atomic(path: Path, payload: dict, *, mode: int = 0o600) -> None:
    """Replace `path` with `payload`, or leave it exactly as it was.

    `write_text` truncates in place, so a crash, a full disk, or two verinote
    processes writing at once can leave a half-written file. The reader
    (`read_app_config`) turns unparsable JSON into `{}` after a stderr-only
    warning, which in the web UI is invisible: the app silently forgets the
    active KB and the chosen theme rather than reporting that it lost them.

    Writing a sibling temp file and `os.replace`-ing it makes the swap atomic on
    POSIX and Windows alike, so a reader sees either the whole old file or the
    whole new one. `fsync` before the rename is what makes that survive a power
    loss rather than only a process crash; the parent-directory fsync that would
    also durably record the rename itself is deliberately skipped — it is not
    portable, and the guarantee being bought here is "never torn", not
    "committed before we return".

    `mode` is explicit because `mkstemp` creates its file 0o600 and `os.replace`
    carries that over — so switching to this writer silently tightened `app.json`
    from the 0o644 `write_text` produced, on existing files too. That tightening
    is kept (this directory is per-user, and it is about to hold a secrets file
    beside it), but it is stated here rather than left as a side effect of the
    temp-file mechanism.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # Set on the fd, not the path: the mode then lands on *this* file rather
        # than on whatever might occupy `tmp`'s name, and `os.replace` carries it
        # over. Guarded because `os.fchmod` is absent on Windows, which this
        # module explicitly supports — and unlike a best-effort restore, a
        # secrets file must not proceed as though a failed chmod had succeeded.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    _write_json_atomic(app_config_path(), {**existing, "active_root": str(resolved)})


def app_theme() -> str:
    """Return the app-wide colour preference, defaulting safely to the system."""
    theme = read_app_config().get("theme")
    return theme if theme in APP_THEMES else "system"


def save_app_theme(theme: str) -> None:
    """Persist an app-wide colour preference without disturbing other app settings."""
    if theme not in APP_THEMES:
        raise ValueError(f"unknown app theme: {theme}")
    existing = read_app_config()
    if existing.get("theme") == theme:
        return
    _write_json_atomic(app_config_path(), {**existing, "theme": theme})


def active_root() -> Path | None:
    """Return the selected KB root, or None when the web UI should ask."""
    env_root = os.environ.get("VERINOTE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    saved = _saved_root(read_app_config())
    if saved is not None and (saved / "kb.sqlite").is_file():
        return saved
    return None


def _settings_path(root: Path) -> Path:
    return root / SETTINGS_FILENAME


# Every setting this reader knows how to type-check: the JSON type its value
# must have, and whether `null` is a legitimate way to write "unset".
#
# The nullability column is not decoration. `save_settings` writes
# `"base_url": null` itself for a KB with no custom endpoint, so null there is
# this app's own output and must stay a silent "unset". `provider` and `model`
# are always written as strings, so a null in either is a value that is present
# but unusable — reading it as "unset" would let it fall through to the
# built-in `anthropic` default, which is exactly the silent cloud fallback the
# halt below exists to prevent.
_SETTING_SPECS: dict[str, tuple[type, bool]] = {  # key -> (JSON type, null means unset)
    "provider": (str, False),
    "model": (str, False),
    "base_url": (str, True),
    "extraction_chunk_chars": (int, True),
    "extraction_chunk_overlap_chars": (int, True),
    "extraction_max_facts_per_chunk": (int, True),
    "auto_accept_recommendations": (bool, True),
}

# The settings that decide *where* a request goes and *what* answers it. An
# unusable value in one of these cannot be warned about and defaulted away: the
# default is the `anthropic` cloud, so "ignore it and carry on" would ship the
# user's notes to a provider they never chose (#269). These become a
# `settings_error`, i.e. the same halt a corrupt file already triggers. The
# remaining settings only tune local extraction, where falling back to the
# built-in default is the documented and harmless answer to a bad value.
# Ordered, so the same broken file always reports the same reason.
_ROUTING_SETTINGS = ("provider", "model", "base_url")

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


def _bad_setting_reason(path: Path, key: str, value: object) -> str | None:
    """Why one saved setting's value is unusable, or None when it is fine.

    The single per-key verdict: the stderr warning and the `settings_error`
    halt both ask *this*, so a value can never be dropped by one and accepted by
    the other. Unknown keys are never judged — a `config.json` written by a
    newer verinote may hold settings this version has no opinion about, and
    guessing about them is worse than passing them through.
    """
    spec = _SETTING_SPECS.get(key)
    if spec is None:
        return None
    expected, null_is_unset = spec
    if value is None:
        if null_is_unset:
            return None
        return f"{path} has {key} set to null, expected {_EXPECTED_NAMES[expected]}"
    if _has_type(value, expected):
        return None
    return (
        f"{path} has {key} as {_json_type_name(value)}, "
        f"expected {_EXPECTED_NAMES[expected]}"
    )


def _bad_routing_setting_reason(path: Path, data: dict) -> str | None:
    """The first unusable provider-routing setting in an otherwise valid file.

    Separate from `_checked_settings` because the two do different jobs with the
    same verdict: that one decides what a caller *sees*, this one decides
    whether the KB may be used at all. Only `_ROUTING_SETTINGS` are consulted —
    a bad chunk size is a warning, not a reason to refuse the KB.
    """
    for key in _ROUTING_SETTINGS:
        if key not in data:
            continue
        reason = _bad_setting_reason(path, key, data[key])
        if reason is not None:
            return reason
    return None


def _checked_settings(path: Path, data: dict) -> dict:
    """Drop settings whose value is unusable, warning about each.

    This file is the boundary where untrusted JSON enters: a hand-edited (or
    older-version-written) `config.json` can hold `"base_url": 123`, and without
    this the failure surfaces far from its cause, as an `AttributeError` inside
    whichever adapter finally calls a string method on it. Dropping is *not* the
    whole answer for a routing setting — being dropped is what makes it fall
    back to the cloud default — so those also raise a `settings_error` that
    halts the KB, and the warning says so rather than promising a harmless
    default. Unknown keys pass through untouched.
    """
    checked = {}
    for key, value in data.items():
        reason = _bad_setting_reason(path, key, value)
        if reason is None:
            checked[key] = value
        elif key in _ROUTING_SETTINGS:
            _warn_config(reason, f"this KB is unusable until {key} is fixed")
        else:
            _warn_config(reason, f"ignoring it, so {key} falls back to its default")
    return checked


def _read_settings_snapshot(root: Path) -> tuple[dict, str | None]:
    """Read one settings-file snapshot for both resolution and the halt verdict.

    A Config must never resolve its values from one version of config.json and
    decide whether it is safe from another. The checked settings and the
    settings_error therefore come from this single read and parse. `read_settings`
    remains the public dict-only view for existing callers.
    """
    path = _settings_path(root)
    if not path.is_file():
        return {}, None
    consequence = "saved runtime settings will fall back to defaults"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        _warn_bad_config(path, err, consequence)
        return {}, _bad_config_reason(path, err)
    except UnicodeDecodeError as err:
        _warn_bad_config(path, err, consequence)
        return {}, _bad_config_reason(path, err)
    except json.JSONDecodeError as err:
        _warn_bad_config(path, err, consequence)
        return {}, _bad_config_reason(path, err)
    if not isinstance(data, dict):
        _warn_bad_config(path, None, consequence)
        return {}, _bad_config_reason(path, None)
    return _checked_settings(path, data), _bad_routing_setting_reason(path, data)


def read_settings(root: Path) -> dict:
    """Read saved non-secret runtime settings, or {} if absent/bad.

    This preserves the public dict-only contract. `Config.for_root` uses
    `_read_settings_snapshot` directly so its resolved values and halt verdict
    come from the same observed file contents.
    """
    return _read_settings_snapshot(root)[0]


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
        # `read_settings` now type-checks the saved side, so a non-string should
        # no longer arrive from there; this guard stays because it is what makes
        # the blank-value rule apply to the string sources only, and because a
        # caller may pass a saved value this function never chose.
        if isinstance(candidate, str):
            candidate = candidate.strip()
        if candidate:
            return candidate
    return default



class CredentialsCorruptError(RuntimeError):
    """Raised when `credentials.json` is present but unreadable.

    Deliberately its own type rather than a `ConfigCorruptError`: that one names
    a *KB's* `config.json` everywhere it surfaces — the halt page, the settings
    banner, the KB-switch error, the CLI. Reusing it for a machine-wide secrets
    file would tell the user the wrong file, the wrong scope, and a recovery
    action ("re-save your provider") that does not touch this file at all.
    """


def _read_credentials() -> tuple[dict[str, str], str | None]:
    """Stored keys, plus why they could not be read.

    A missing file is not an error — that is the normal state before anyone
    saves a key — so it returns `({}, None)`. Anything else that stops the file
    being understood returns `({}, reason)`: an unreadable secrets file must
    never be indistinguishable from "no keys are stored", because the two lead
    to opposite conclusions about whether the user has to enter one again.
    """
    path = credentials_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as exc:
        return {}, _bad_config_reason(path, exc)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, _bad_config_reason(path, exc)
    if not isinstance(data, dict):
        return {}, _bad_config_reason(path, None)
    keys = data.get("keys")
    if keys is None:
        return {}, None
    if not isinstance(keys, dict):
        return {}, f"{path} has a 'keys' entry that is not an object"
    # A non-string value is refused, never coerced: `str()` on an object would
    # invent a credential out of a structure a future version meant as
    # something else (a keychain reference, say).
    for provider, value in keys.items():
        if not isinstance(value, str):
            return {}, f"{path} holds a non-string key for {provider!r}"
    return {str(k): v for k, v in keys.items()}, None


def api_key_source(
    provider: str, stored: dict[str, str], error: str | None = None
) -> tuple[str, str | None]:
    """Where `provider`'s key comes from, and what it is.

    One walk, two answers: the settings badge and the resolved value apply the
    same precedence rather than two hand-kept copies of it, so neither can rank
    the sources differently from the other.

    What this does NOT make identical is the *inputs*. The badge reads the file
    fresh on every render; a running app holds `Config` as a snapshot. If the
    file changes out of band — a second verinote process, a hand edit — the page
    leads the snapshot until something rebuilds it. Every credentials route
    rebuilds it, so the in-app flow stays consistent; the residue is a
    deliberately fresh page, not a shared-input guarantee.

    States: `not_needed`, `env_provider`, `stored`, `env_global`, `unknown`
    (the file is present but unreadable, so we cannot say), `unset`.
    """
    if provider not in PROVIDERS_REQUIRING_KEY:
        return "not_needed", None
    scoped = os.environ.get(provider_key_env_var(provider))
    if isinstance(scoped, str) and scoped.strip():
        return "env_provider", scoped.strip()
    saved = stored.get(provider)
    if isinstance(saved, str) and saved.strip():
        return "stored", saved.strip()
    legacy = os.environ.get("VERINOTE_API_KEY")
    if isinstance(legacy, str) and legacy.strip():
        return "env_global", legacy.strip()
    # Only now does an unreadable file matter: every tier above it came back
    # empty, so the file is what would have decided.
    if error is not None:
        return "unknown", None
    return "unset", None


def resolve_api_key(provider: str, stored: dict[str, str]) -> str | None:
    """The key to authenticate `provider` with, or None.

    Order: the provider-scoped environment variable, then the stored key, then
    the legacy provider-agnostic `VERINOTE_API_KEY`.

    This inverts the module's usual env-then-saved rule for that last tier, on
    purpose. `VERINOTE_API_KEY` names no provider, so ranking it above a stored
    key would take a key the user saved *for OpenAI* and replace it with
    whatever single value happens to be exported — commonly their Anthropic key,
    which would then be transmitted to `api.openai.com`. The scoped variable
    keeps env-first available for anyone who wants it, without that ambiguity;
    the legacy variable stays last so existing setups keep working unchanged for
    any provider that has no stored key.
    """
    return api_key_source(provider, stored)[1]

@contextmanager
def _credentials_lock():
    """Serialise read-modify-write on the credentials file across processes.

    A save merges one provider's key into the others, so two verinote processes
    saving at once can each write a payload built from the state before the
    other's — losing a key with no error and no UI signal. The atomic replace
    bounds that to a lost update rather than a corrupt file, but a silently
    vanished secret is still the worst failure this file has.

    POSIX only. `fcntl` is absent on Windows, where this degrades to the
    atomic-replace guarantee alone; that gap is real and is not papered over.
    """
    directory = app_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover - platform-dependent
        yield
        return
    lock_path = directory / (CREDENTIALS_FILENAME + ".lock")
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ensure_config_dir_gitignore() -> None:
    """Keep a secrets file out of a version-controlled config directory.

    `XDG_CONFIG_HOME` is routinely `~/.config` tracked as a dotfiles repo, and a
    subdirectory `.gitignore` is honoured by a repo rooted anywhere above it.
    Written only when absent, so a user's own file is never overwritten.

    Bounded value, stated so it is not mistaken for protection: it does nothing
    for a file git already tracks, for a dotfiles manager that copies rather
    than symlinks, or for a backup tool.
    """
    path = app_config_dir() / ".gitignore"
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("*\n", encoding="utf-8")
    except OSError:
        pass  # advisory only; never block saving a key on it


def save_credential(provider: str, key: str) -> None:
    """Store `provider`'s API key, leaving every other provider's untouched."""
    if provider not in PROVIDERS_REQUIRING_KEY:
        raise ValueError(f"{provider} does not use an API key")
    cleaned = key.strip()
    if not cleaned:
        raise ValueError("API key is empty")
    with _credentials_lock():
        stored, error = _read_credentials()
        if error is not None:
            # Refuse rather than clobber: a save is a *merge*, so writing a fresh
            # file over an unreadable one would silently discard every other
            # provider's key. (`save_settings` may rewrite a corrupt config.json
            # because it writes a complete payload from the form; this cannot.)
            raise CredentialsCorruptError(error)
        _ensure_config_dir_gitignore()
        _write_json_atomic(
            credentials_path(),
            {"version": CREDENTIALS_VERSION, "keys": {**stored, provider: cleaned}},
            mode=0o600,
        )


def delete_credential(provider: str) -> bool:
    """Remove `provider`'s stored key. True when one was actually removed."""
    if provider not in PROVIDERS_REQUIRING_KEY:
        # Same membership rule as `save_credential`: without it a bogus id gets a
        # cheerful "nothing to remove" while the same id is refused on save.
        raise ValueError(f"{provider} does not use an API key")
    with _credentials_lock():
        stored, error = _read_credentials()
        if error is not None:
            raise CredentialsCorruptError(error)
        if provider not in stored:
            return False
        remaining = {k: v for k, v in stored.items() if k != provider}
        _write_json_atomic(
            credentials_path(),
            {"version": CREDENTIALS_VERSION, "keys": remaining},
            mode=0o600,
        )
        return True


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
    provider: str  # one of PROVIDERS — enumerating it here only goes stale
    model: str
    # Kept out of the repr so a stray `logger.exception` with locals, or a
    # pytest assertion diff, cannot print the credential. Necessary but not
    # sufficient once keys are stored on disk: that unit must also handle file
    # mode, `asdict`, and the fact that a frozen dataclass compares on this field.
    api_key: str | None = field(repr=False)
    base_url: str | None
    llm_timeout_seconds: float = 600.0
    extraction_chunk_chars: int = 300
    extraction_chunk_overlap_chars: int = 40
    extraction_max_facts_per_chunk: int = 8
    auto_accept_recommendations: bool = False
    # Set only when the settings file is PRESENT but unusable — either as a whole
    # (unreadable/not a JSON object) or in one of the provider-routing keys, whose
    # wrong-typed value would otherwise be dropped and resolve to the cloud
    # default (#325). Never set for a missing file: absence is legitimate. A
    # trailing, defaulted field so no existing `Config(...)` construction site has
    # to change. `assert_settings_intact` turns it into the halt that keeps a
    # corrupt config from silently defaulting to a provider the user never chose
    # (#269).
    settings_error: str | None = None
    # Set only when the machine-wide credentials file is PRESENT but unreadable
    # AND the resolved provider needs a key AND no environment tier supplied one
    # — i.e. only when the unreadable file is what actually decides the outcome.
    # An Ollama user, or one with the scoped variable exported, is not halted by
    # a file that could not have changed their result. A trailing, defaulted
    # field so no existing `Config(...)` construction site has to change.
    credentials_error: str | None = None

    def extraction_schema_hint(self) -> str:
        return render_prompt(
            self.root,
            "extraction-limit-hint",
            max_facts=self.extraction_max_facts_per_chunk,
        )

    @classmethod
    def for_root(cls, root: Path) -> "Config":
        saved, settings_error = _read_settings_snapshot(root)
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
        stored_keys, credentials_read_error = _read_credentials()
        state, api_key = api_key_source(provider, stored_keys, credentials_read_error)
        # The halt is exactly the state that says we cannot tell — every other
        # state means some tier answered, so the unreadable file could not have
        # changed the outcome and must not stop this provider.
        credentials_error = credentials_read_error if state == "unknown" else None
        return cls(
            root=root,
            db_path=root / "kb.sqlite",
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            llm_timeout_seconds=_llm_timeout_seconds(),
            extraction_chunk_chars=chunk_chars,
            extraction_chunk_overlap_chars=chunk_overlap,
            extraction_max_facts_per_chunk=max_facts,
            auto_accept_recommendations=auto_accept,
            settings_error=settings_error,
            credentials_error=credentials_error,
        )

    @classmethod
    def load(cls) -> "Config":
        return cls.for_root(_root())

    @classmethod
    def load_for_ui(cls) -> "Config | None":
        root = active_root()
        return cls.for_root(root) if root is not None else None


class ConfigCorruptError(RuntimeError):
    """Raised when a KB's `config.json` is present but unreadable/corrupt.

    Deliberately NOT an `LLMError`: several `except LLMError` blocks wrap the very
    `get_client()` call sites this guards (the web `/ask`, `/settings/test`, and
    the CLI extraction path). Being an `LLMError` would let those blocks swallow
    the halt into a generic "provider failed" message instead of reaching the
    dedicated config-corrupt handler — and the whole point is that a corrupt
    config must NOT silently fall back to the cloud default provider the user
    never chose (#269).
    """


def assert_credentials_intact(cfg: Config) -> None:
    """Refuse to proceed when the stored-keys file is present but unreadable.

    Separate from `assert_settings_intact` because it is a different file with a
    different scope and a different fix. Only reached when the file is what
    decides the key — see `Config.credentials_error`.
    """
    if cfg.credentials_error is not None:
        raise CredentialsCorruptError(cfg.credentials_error)


def assert_settings_intact(cfg: Config) -> None:
    """Refuse to proceed when the KB's settings file is present but corrupt.

    The single confidentiality predicate: `llm.factory.get_client` and every
    explicit web/CLI checkpoint ask *this*, so none of them can disagree about
    when a corrupt config halts. A corrupt `config.json` makes the resolved
    provider untrustworthy — the user's saved `ollama` may have silently become
    the `anthropic` cloud default — so any path that could reach a provider halts
    instead of leaking to a cloud the user never consented to.

    Mirrors `pipeline.policy_state.assert_writable`: a pure predicate over already
    resolved state, raising the one dedicated error its callers catch. A missing
    file is never corrupt, so a fresh KB with no `config.json` never halts here.
    """
    if cfg.settings_error is not None:
        raise ConfigCorruptError(cfg.settings_error)
