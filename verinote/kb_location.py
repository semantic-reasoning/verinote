# SPDX-License-Identifier: MPL-2.0
"""KB location resolution and safeguards for commands that create KB files."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "verinote"


class KBLocationError(ValueError):
    """Raised when a requested KB location is not an unambiguous absolute path."""


class KBRootSafetyError(KBLocationError):
    """Raised when creating a KB at a target could modify a Git worktree."""


def _canonical_absolute_path(value: Path | str, *, source: str) -> Path:
    raw = os.fspath(value)
    if not raw.strip():
        raise KBLocationError(f"{source} must be a path, not an empty string")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise KBLocationError(
            f"{source} must be an absolute path; relative KB roots are not supported"
        )
    try:
        return path.resolve()
    except OSError as exc:
        raise KBLocationError(f"could not resolve {source} {path}: {exc}") from exc
    except RuntimeError as exc:
        raise KBLocationError(f"could not resolve {source} {path}: symlink loop") from exc


def user_data_kb_root() -> Path:
    """Return the CWD-independent default KB root for the current platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            candidate = Path(base).expanduser()
            if candidate.is_absolute():
                return (candidate / APP_NAME / "kb").resolve()
        return (Path.home() / "AppData" / "Local" / APP_NAME / "kb").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / APP_NAME / "kb").resolve()

    base = os.environ.get("XDG_DATA_HOME")
    if base:
        candidate = Path(base).expanduser()
        if candidate.is_absolute():
            return (candidate / APP_NAME / "kb").resolve()
    return (Path.home() / ".local" / "share" / APP_NAME / "kb").resolve()


def resolve_kb_root(explicit: Path | str | None = None) -> Path:
    """Resolve CLI/config KB location: explicit root, environment, then user data."""
    if explicit is not None:
        return _canonical_absolute_path(explicit, source="KB root")

    env_root = os.environ.get("VERINOTE_ROOT")
    if env_root is not None:
        return _canonical_absolute_path(env_root, source="VERINOTE_ROOT")
    return user_data_kb_root()


def assert_kb_root_is_safe_to_create(root: Path | str) -> None:
    """Refuse a root inside a normal or linked Git worktree before writes begin.

    `resolve()` follows existing symlink components even when the final target
    does not exist. Walking its ancestors therefore covers direct, nested, and
    symlinked descendants of both normal worktrees (`.git` directory) and linked
    worktrees (`.git` file).
    """
    target = _canonical_absolute_path(root, source="KB root")
    if target.exists() and not target.is_dir():
        # Let `cmd_init` issue its more specific existing-file diagnostic.
        return
    for directory in (target, *target.parents):
        marker = directory / ".git"
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise KBRootSafetyError(
                f"cannot inspect {marker} before creating a KB: {exc}"
            ) from exc
        raise KBRootSafetyError(
            f"refusing to create a KB inside Git worktree {directory} (found {marker})"
        )
