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

These tests assert both directions with `git check-ignore`'s exit status
(0 = ignored, 1 = not ignored). It is pattern-based, so it works for paths that
do not exist on disk. Asserting only one direction would be vacuous: emptying
.gitignore passes the "sources are tracked" half, and ignoring everything passes
the "artifacts are ignored" half. Both halves together pin the actual split.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

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


def _check_ignore(path: str) -> int:
    """Exit status of `git check-ignore -q <path>`: 0 ignored, 1 not ignored."""
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert proc.returncode in (0, 1), (
        f"git check-ignore errored on {path!r}: {proc.stderr.decode()}"
    )
    return proc.returncode


@pytest.mark.parametrize("path", SOURCE_PATHS)
def test_hand_written_sources_are_not_ignored(path: str) -> None:
    assert _check_ignore(path) == 1, (
        f"{path} is ignored by .gitignore; hand-written policy/fixtures must be "
        "committable. An ignored policy never reaches git, and nothing in this "
        "repo can regenerate it."
    )


@pytest.mark.parametrize("path", ARTIFACT_PATHS)
def test_generated_kb_artifacts_stay_ignored(path: str) -> None:
    assert _check_ignore(path) == 0, f"{path} is a generated artifact and must stay ignored"


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
