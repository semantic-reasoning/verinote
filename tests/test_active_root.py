# SPDX-License-Identifier: MPL-2.0
"""save_active_root should only touch app.json when the selection actually changes."""

import json
import os

from verinote.config import (
    active_root,
    app_config_path,
    read_app_config,
    save_active_root,
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

    save_active_root(kb_a)
    path = app_config_path()
    os.utime(path, ns=(_SENTINEL_NS, _SENTINEL_NS))

    save_active_root(kb_b)

    assert read_app_config()["active_root"] == str(kb_b.resolve())
    assert path.stat().st_mtime_ns != _SENTINEL_NS


def test_save_active_root_creates_file_when_absent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb = _make_kb(tmp_path, "kb")

    assert not app_config_path().exists()

    save_active_root(kb)

    assert app_config_path().is_file()
    assert read_app_config()["active_root"] == str(kb.resolve())
