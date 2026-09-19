# SPDX-License-Identifier: MPL-2.0
"""Repo-hygiene lock: a comment or docstring may not cite an `.md` file the
repository does not have (#581).

The failure mode this locks: plan/critique documents are written inside a
linked worktree under `.claude/worktrees/`, which `.gitignore` excludes; when
the worktree is cleaned up the document vanishes and the citation survives. A
citation whose target no reader can open makes a "measured" claim
unverifiable — and the reader cannot even know that verification is
impossible. #581 found 13 such citations (two worktree drafts plus a plan
section); the drafts had never been committed — no `.md` file has ever been
deleted from this history, which is how you tell "committed then deleted"
from "never committed". As long as the same workflow runs, the next dead
citation is generated the same way, so the guard lives in the repo.

RULE: every `*.md` filename cited in a comment or docstring of a tracked
`.py` file must be (a) a tracked path, or (b) on the runtime/user-data
whitelist below. Names in (b) refer to files that live in the user's KB
(policy data, prompt overrides, source documents) or that a test creates as
fixtures — anyone can make a KB or run the test and open one — unlike a
worktree draft. The whitelist is a literal set on purpose: adding an entry
is a reviewed act.

SCOPE, both halves deliberate:
- Tracked `.py` files only. Citations in this repo live in test
  docstrings and comments (every case found so far), and "comment/docstring"
  has a precise meaning only there.
- A candidate immediately preceded by a quote is a string literal —
  `Path(...) / "docs" / "configuration.md"` or
  `PromptDefinition(..., "extraction.md")` is path construction the code
  reads at runtime, not a citation. Dead citations sit in prose, unquoted.

NON-VACUITY: this test was written against the pre-fix tree and went RED,
reporting exactly the 5 surviving draft citations in
`tests/test_relation_aliases_web_guard.py` (the issue counted 7 at its
commit; the other 2 had already gone with the #570/#585 rewrites). The fix
deleted those citations and this test turned green. A guard that only ever
saw green proves nothing (#581 AC 5).
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (b) Runtime/user-data names: they live in the user's KB or in a test's
# temporary KB, so any reader can create and open them. Literal on purpose —
# an added entry must be a reviewed act (#581 AC 4).
WHITELIST_BARE_NAMES = frozenset(
    {
        # KB policy data files (user KB; the shipped defaults are separate).
        "relation-aliases.md",
        "typed-relations.md",
        # KB source documents / test fixtures.
        "notes.md",
        # Built-in prompt file names (PromptDefinition, verinote/prompts/library.py).
        "ask-fallback.md",
        "claude-json-wrapper.md",
        "extraction-limit-hint.md",
        "extraction.md",
        "focused-role-extraction.md",
        "ollama-extraction.md",
        "query-intent.md",
        "query-translation.md",
    }
)

# (b) Runtime/user-data path shapes, matched with fnmatch (`*` crosses `/`).
WHITELIST_PATH_GLOBS = (
    "policy/prompts/*.md",  # prompt overrides in a user KB
    "sources/*.md",  # KB source documents (also the test-fixture shape)
    "defaults/*.md",  # packaged default prompts
)

_MD_REFERENCE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.md)\b")


def _tracked_paths() -> frozenset[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return frozenset(p for p in proc.stdout.decode("utf-8").split("\0") if p)


def _is_allowed(name: str, tracked: frozenset[str]) -> bool:
    if name in tracked:  # (a) a tracked path: openable by any clone
        return True
    if name.rsplit("/", 1)[-1] in WHITELIST_BARE_NAMES:  # (b)
        return True
    return any(fnmatch.fnmatch(name, glob) for glob in WHITELIST_PATH_GLOBS)  # (b)


def _cited_md_references() -> list[tuple[str, int, str]]:
    """(file, line, name) for every cited `.md` reference in tracked `.py` files."""
    tracked = _tracked_paths()
    found: list[tuple[str, int, str]] = []
    for path in sorted(p for p in tracked if p.endswith(".py")):
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _MD_REFERENCE.finditer(line):
                name = match.group(1)
                if _is_allowed(name, tracked):
                    continue
                # A quoted candidate is path construction, not a citation.
                if match.start() > 0 and line[match.start() - 1] in "\"'":
                    continue
                found.append((path, lineno, name))
    return found


def test_comments_do_not_cite_md_files_the_repository_does_not_have() -> None:
    bad = _cited_md_references()
    assert bad == [], (
        "citations to .md files the repository does not have. Each is a "
        "document no reader can open — usually a worktree plan/critique draft "
        "that vanished with the worktree (#581). Make the claim reproducible "
        "(a procedure the reader can run) or delete the citation; adding a "
        "name to the whitelist is a reviewed act.\n"
        + "\n".join(f"  {file}:{line_no}: {name}" for file, line_no, name in bad)
    )


def test_the_scanner_still_sees_a_dead_citation_shape() -> None:
    """Non-vacuity: the extractor must still match a dead-citation line.

    A regex "simplification" that stops matching a cited `*.md` name in prose would let
    the test above pass while protecting nothing; this pin keeps the extractor
    honest independent of the tree. The name is assembled at runtime: a
    literal dead name in this file would be a citation of its own, and this
    test scans every tracked `.py` file -- itself included.
    """
    name = "draft" + "42.md"
    line = f"every route still renders 200 (measured — see {name} §2.1)."
    assert _MD_REFERENCE.findall(line) == [name]
