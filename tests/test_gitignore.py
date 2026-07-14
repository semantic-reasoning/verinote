# SPDX-License-Identifier: MPL-2.0
"""Regression lock on .gitignore's source/artifact split.

`.gitignore` used to carry bare, unanchored globs (`*.dl`, `*.sqlite`,
`*.duckdb`). Those match at *any* depth, so hand-written policy, test fixtures
and doc examples were silently ignored. An ignored file cannot be `git add`-ed
without `-f`, so a KB owner's policy never entered history — and no part of this
repo can regenerate a hand-written policy. The loss is permanent whether verinote
then falls back to the shipped default policy or refuses to run outright (#155),
which is why these tests pin the ignore rules rather than the engine's reaction
to a policy that is already gone.

The same split now covers agent tooling state (#214). This repo is developed from
linked worktrees under `.claude/worktrees/`, which `git add -A` stages as gitlinks
no clone can resolve — so they must be ignored, while `.claude/agents/`,
`.claude/skills/` and a shared `.claude/settings.json` must not be. A blanket
`.claude/` would take both, which is the `*.dl` mistake one directory over.

These tests assert both directions with `git check-ignore`'s exit status
(0 = ignored, 1 = not ignored). It is pattern-based, so it works for paths that
do not exist on disk. Asserting only one direction would be vacuous: emptying
.gitignore passes the "sources are tracked" half, and ignoring everything passes
the "artifacts are ignored" half. Both halves together pin the actual split.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT_ENV = os.environ | {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# A real, tracked, hand-written policy. Under the old `*.dl` rule this file was
# ignored and could not even be `git add`-ed without `-f`.
TRACKED_POLICY_FIXTURE = "tests/fixtures/policy/sample-policy.dl"

# Source inputs: hand-edited, irreplaceable, must stay committable.
#
# Only extensions that a plausible ignore rule could actually swallow are listed.
# `.md` policy inputs are deliberately absent: no pattern in .gitignore has ever
# threatened them, so asserting they stay un-ignored would pass even against the
# pre-fix .gitignore — a decoration, not a regression lock.
SOURCE_PATHS = [
    TRACKED_POLICY_FIXTURE,
    "docs/examples/logic-policy.dl",
    "verinote/policy/logic-policy.dl",
    "some/other/kb/policy/logic-policy.dl",
    "tests/fixtures/kb/facts.sqlite",
    "tests/fixtures/kb/sample.duckdb",
]

# Generated engine artifacts: rebuilt from the KB, must stay ignored.
# Both stores' sidecars are pinned: DuckDB leaves `.wal` behind on an unclean
# shutdown and spills to `.tmp/`, mirroring SQLite's `-wal`/`-shm`.
ARTIFACT_PATHS = [
    "data/facts/query.dl",
    "data/kb.sqlite",
    "data/facts.duckdb",
    "some/other/kb/facts/query.dl",
    "some/other/kb/facts.duckdb",
    "some/other/kb/facts.duckdb.wal",
    "some/other/kb/facts.duckdb.tmp/spill-0.tmp",
    "some/other/kb/kb.sqlite",
    "some/other/kb/kb.sqlite-wal",
    "some/other/kb/kb.sqlite-shm",
]

# Agent tooling state: local, per-session, must stay ignored.
#
# The worktree entry is the sharp one. `.claude/worktrees/<issue>` is a linked git
# worktree, so `git add -A` stages it as a gitlink rather than as files — a commit
# that no clone can resolve. git only *warns* about that; the add succeeds.
AGENT_STATE_PATHS = [
    ".claude/worktrees/issue-1/verinote/cli.py",
    ".claude/worktrees/issue-1/README.md",
    ".claude/settings.local.json",
    ".omc/project-memory.json",
    ".omc/state/sessions/abc/mission-state.json",
    ".omc/sessions/abc.json",
]

# Shared agent config: committable, and threatened by the obvious over-broad fix.
#
# These are the reason the rules above are anchored. A bare `.claude/` would ignore
# every one of them — the same failure as the old `*.dl` glob, one directory over.
# This half is not decoration: it fails the moment someone "simplifies" the three
# anchored rules into one.
AGENT_SHARED_PATHS = [
    ".claude/agents/dev-reviewer.md",
    ".claude/skills/run/SKILL.md",
    ".claude/settings.json",
]


@pytest.fixture(scope="module")
def gitignore_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway repo whose only ignore input is this repository's .gitignore."""
    repo = tmp_path_factory.mktemp("gitignore-repo")
    subprocess.run(["git", "init", "-q"], cwd=repo, env=GIT_ENV, check=True)
    (repo / ".gitignore").write_text((REPO_ROOT / ".gitignore").read_text(encoding="utf-8"))
    return repo


def _check_ignore(repo: Path, path: str) -> int:
    """Exit status of `git check-ignore -q <path>`: 0 ignored, 1 not ignored."""
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
    )
    assert proc.returncode in (0, 1), (
        f"git check-ignore errored on {path!r}: {proc.stderr.decode()}"
    )
    return proc.returncode


@pytest.mark.parametrize("path", SOURCE_PATHS)
def test_hand_written_sources_are_not_ignored(gitignore_repo: Path, path: str) -> None:
    assert _check_ignore(gitignore_repo, path) == 1, (
        f"{path} is ignored by .gitignore; hand-written policy/fixtures must be "
        "committable. An ignored policy never reaches git, and nothing in this "
        "repo can regenerate it."
    )


@pytest.mark.parametrize("path", ARTIFACT_PATHS)
def test_generated_kb_artifacts_stay_ignored(gitignore_repo: Path, path: str) -> None:
    assert _check_ignore(gitignore_repo, path) == 0, (
        f"{path} is a generated artifact and must stay ignored"
    )


@pytest.mark.parametrize("path", AGENT_STATE_PATHS)
def test_agent_tooling_state_stays_ignored(gitignore_repo: Path, path: str) -> None:
    assert _check_ignore(gitignore_repo, path) == 0, (
        f"{path} is agent tooling state and must stay ignored. A linked worktree "
        "under .claude/worktrees/ is staged by `git add -A` as an embedded git "
        "repository — a gitlink no clone can resolve."
    )


@pytest.mark.parametrize("path", AGENT_SHARED_PATHS)
def test_shared_agent_config_is_not_ignored(gitignore_repo: Path, path: str) -> None:
    assert _check_ignore(gitignore_repo, path) == 1, (
        f"{path} is shared, hand-written project config and must stay committable. "
        "Ignoring `.claude/` wholesale swallows it — the same mistake the bare "
        "`*.dl` glob made with hand-written policy."
    )


def test_no_agent_state_is_tracked() -> None:
    """Non-vacuity guard: the ignore rules match what is actually in the tree."""
    proc = subprocess.run(
        ["git", "ls-files", ".omc/", ".claude/worktrees/", ".claude/settings.local.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    assert proc.stdout.decode().strip() == "", (
        "agent tooling state is tracked by git; it must never enter history"
    )


def test_sample_policy_fixture_is_actually_tracked() -> None:
    """Non-vacuity guard: the fixture is committed, not just un-ignored."""
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", TRACKED_POLICY_FIXTURE],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert proc.returncode == 0, (
        f"{TRACKED_POLICY_FIXTURE} is not tracked by git: {proc.stderr.decode()}"
    )
    assert (REPO_ROOT / TRACKED_POLICY_FIXTURE).is_file()


def test_user_kb_under_data_stays_untracked() -> None:
    """The default KB holds user data, not repo artifacts: never commit it."""
    proc = subprocess.run(
        ["git", "ls-files", "data/"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    assert proc.stdout.decode().strip() == "", "no file under data/ may be tracked"
