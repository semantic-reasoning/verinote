# SPDX-License-Identifier: MPL-2.0
"""save_active_root should only touch app.json when the selection actually changes."""

import json
import os

from verinote.config import (
    active_root,
    app_theme,
    app_config_path,
    read_app_config,
    save_active_root,
    save_app_theme,
)

_SENTINEL_NS = 1_000_000_000_000_000_000  # a fixed, unmistakably-old mtime


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


def _make_kb(tmp_path, name):
    kb = tmp_path / name
    kb.mkdir()
    (kb / "kb.sqlite").write_text("", encoding="utf-8")
    return kb


def _seed_app_config(saved_root):
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"active_root": str(saved_root), "extra": "keep"}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, ns=(_SENTINEL_NS, _SENTINEL_NS))
    return path


def test_save_active_root_skips_rewrite_when_saved_value_is_a_symlink(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    kb = _make_kb(tmp_path, "real-kb")
    link = tmp_path / "link-kb"
    link.symlink_to(kb)
    path = _seed_app_config(link)

    # The saved symlink already resolves to the KB we are about to select.
    assert active_root() == kb.resolve()

    save_active_root(kb)

    assert path.stat().st_mtime_ns == _SENTINEL_NS


def test_save_active_root_skips_rewrite_when_saved_value_is_relative(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    kb = _make_kb(tmp_path, "kb")
    monkeypatch.chdir(tmp_path)
    path = _seed_app_config("kb")

    assert active_root() == kb.resolve()

    save_active_root(kb)

    assert path.stat().st_mtime_ns == _SENTINEL_NS


def test_save_active_root_skips_rewrite_when_unchanged(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb = _make_kb(tmp_path, "kb")

    save_active_root(kb)
    path = app_config_path()
    os.utime(path, ns=(_SENTINEL_NS, _SENTINEL_NS))

    save_active_root(kb)

    assert path.stat().st_mtime_ns == _SENTINEL_NS


def test_save_active_root_writes_when_target_differs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb_a = _make_kb(tmp_path, "kb_a")
    kb_b = _make_kb(tmp_path, "kb_b")
    path = _seed_app_config(kb_a)

    save_active_root(kb_b)

    assert read_app_config()["active_root"] == str(kb_b.resolve())
    assert path.stat().st_mtime_ns != _SENTINEL_NS


def test_save_active_root_keeps_unknown_keys_when_switching(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb_a = _make_kb(tmp_path, "kb_a")
    kb_b = _make_kb(tmp_path, "kb_b")
    _seed_app_config(kb_a)

    save_active_root(kb_b)

    # Switching KBs must not drop settings this version does not know about.
    assert read_app_config()["extra"] == "keep"


def test_save_app_theme_keeps_active_root_and_unknown_keys(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb = _make_kb(tmp_path, "kb")
    _seed_app_config(kb)

    save_app_theme("dark")

    assert read_app_config() == {
        "active_root": str(kb),
        "extra": "keep",
        "theme": "dark",
    }
    assert active_root() == kb.resolve()
    assert app_theme() == "dark"


def test_app_theme_defaults_to_system_for_absent_or_unknown_values(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    assert app_theme() == "system"
    path = app_config_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"theme": "synthetic-future-theme"}\n', encoding="utf-8")

    assert app_theme() == "system"


def test_save_active_root_writes_when_saved_value_is_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb = _make_kb(tmp_path, "kb")
    monkeypatch.chdir(kb)
    path = _seed_app_config("")

    # An empty saved value selects nothing, so it can never match a real KB --
    # even the cwd, which is what an empty path would normalize to.
    assert active_root() is None

    save_active_root(kb)

    assert read_app_config()["active_root"] == str(kb.resolve())
    assert path.stat().st_mtime_ns != _SENTINEL_NS


def test_save_active_root_creates_file_when_absent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb = _make_kb(tmp_path, "kb")

    assert not app_config_path().exists()

    save_active_root(kb)

    assert app_config_path().is_file()
    assert read_app_config()["active_root"] == str(kb.resolve())


def test_active_root_does_not_fall_back_to_cwd_data(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("VERINOTE_ROOT", raising=False)
    cwd_data = _make_kb(tmp_path, "data")
    monkeypatch.chdir(tmp_path)

    assert active_root() is None
    assert cwd_data.is_dir()
