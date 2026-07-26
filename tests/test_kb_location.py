# SPDX-License-Identifier: MPL-2.0
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import verinote.cli as cli
import verinote.kb_location as kb_location
from verinote.store import Store


def test_resolve_kb_root_precedence_and_cwd_invariance(tmp_path, monkeypatch):
    data_home = tmp_path / "data-home"
    env_root = tmp_path / "from-env"
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("VERINOTE_ROOT", str(env_root))
    monkeypatch.setattr(kb_location.sys, "platform", "linux")

    assert kb_location.resolve_kb_root() == env_root.resolve()
    assert kb_location.resolve_kb_root(explicit_root) == explicit_root.resolve()

    monkeypatch.delenv("VERINOTE_ROOT")
    first = kb_location.resolve_kb_root()
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    assert first == data_home / "verinote" / "kb"
    assert kb_location.resolve_kb_root() == first


def test_user_data_kb_root_uses_each_platform_convention(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_location.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert kb_location.user_data_kb_root() == (tmp_path / "xdg-data" / "verinote" / "kb")

    monkeypatch.setattr(kb_location.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "mac-home"))
    assert kb_location.user_data_kb_root() == (
        tmp_path / "mac-home" / "Library" / "Application Support" / "verinote" / "kb"
    )

    monkeypatch.setattr(kb_location.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    assert kb_location.user_data_kb_root() == (
        tmp_path / "local-app-data" / "verinote" / "kb"
    )


@pytest.mark.parametrize("root", ["relative-kb", "", "   "])
def test_explicit_roots_must_be_nonempty_and_absolute(root):
    with pytest.raises(kb_location.KBLocationError):
        kb_location.resolve_kb_root(root)


def test_explicit_root_expands_tilde_before_absolute_validation(tmp_path, monkeypatch):
    home = tmp_path / "synthetic-home"
    monkeypatch.setenv("HOME", str(home))

    assert kb_location.resolve_kb_root("~/kb") == home / "kb"


def test_relative_verinote_root_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VERINOTE_ROOT", "relative-kb")

    with pytest.raises(kb_location.KBLocationError, match="VERINOTE_ROOT must be an absolute path"):
        kb_location.resolve_kb_root()


def test_cli_root_overrides_environment_for_normal_commands(tmp_path, monkeypatch, capsys):
    env_root = tmp_path / "from-env"
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv("VERINOTE_ROOT", str(env_root))
    store = Store(explicit_root / "kb.sqlite")
    store.init_schema()
    store.close()

    assert cli.main(["status", "--root", str(explicit_root)]) == 0
    assert f"KB: {explicit_root}" in capsys.readouterr().out
    assert not env_root.exists()


def test_ui_root_is_resolved_and_passed_to_the_app_factory(tmp_path, monkeypatch):
    root = tmp_path / "ui-root"
    calls: list[tuple[object, ...]] = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: calls.append(args)))
    args = cli.build_parser().parse_args(["ui", "--root", str(root), "--no-browser"])
    cfg = cli._config_for(args)

    assert cfg is not None
    assert cfg.root == root.resolve()
    assert cli.cmd_ui(cfg, args) == 0
    assert calls == [("verinote.web.app:_default",)]
    assert os.environ["VERINOTE_ROOT"] == str(root.resolve())


@pytest.mark.parametrize("argv", [["--root", "relative-kb", "init"], ["init", "relative-kb"]])
def test_cli_rejects_relative_roots(argv, capsys):
    assert cli.main(argv) == 1
    assert "must be an absolute path" in capsys.readouterr().err


def test_init_accepts_absolute_positional_alias_and_rejects_root_conflict(
    tmp_path, capsys
):
    positional_root = tmp_path / "positional"
    option_root = tmp_path / "option"

    assert cli.main(["init", str(positional_root)]) == 0
    assert (positional_root / "kb.sqlite").is_file()

    assert cli.main(["init", str(positional_root), "--root", str(option_root)]) == 1
    assert "cannot use ROOT and --root together" in capsys.readouterr().err
    assert not option_root.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for worktree safety tests")
@pytest.mark.parametrize("command", ["init", "seed"])
@pytest.mark.parametrize("target_kind", ["nested", "symlink", "linked-worktree"])
def test_init_and_seed_refuse_git_worktree_descendants_before_writing(
    tmp_path, capsys, command, target_kind
):
    repo = _git_repository(tmp_path)
    if target_kind == "nested":
        target = repo / "synthetic-nested" / "kb"
        expected = target
    elif target_kind == "symlink":
        alias = tmp_path / "repo-alias"
        alias.symlink_to(repo, target_is_directory=True)
        target = alias / "synthetic-nested" / "kb"
        expected = repo / "synthetic-nested" / "kb"
    else:
        linked = tmp_path / "linked-worktree"
        _git("-C", repo, "worktree", "add", "-b", "synthetic-linked", linked)
        target = linked / "synthetic-nested" / "kb"
        expected = target

    assert cli.main(["--root", str(target), command]) == 1
    assert "inside Git worktree" in capsys.readouterr().err
    assert not expected.exists()


def _git_repository(tmp_path: Path) -> Path:
    repo = tmp_path / "synthetic-repository"
    _git("init", repo)
    _git("-C", repo, "config", "user.email", "synthetic@example.invalid")
    _git("-C", repo, "config", "user.name", "Synthetic Fixture")
    (repo / "fixture.txt").write_text("synthetic fixture\n", encoding="utf-8")
    _git("-C", repo, "add", "fixture.txt")
    _git("-C", repo, "commit", "-m", "synthetic fixture")
    return repo


def _git(*args: Path | str) -> None:
    subprocess.run(
        ["git", *(str(arg) for arg in args)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
