# SPDX-License-Identifier: MPL-2.0
"""The single place that decides what a KB's logic policy *is*.

A missing `policy/logic-policy.dl` has two very different meanings:

* the KB never used rules (benign — run the shipped default), and
* the KB's rules were lost (an error — a human wrote them and they are gone).

Code cannot infer which one happened, and guessing produces the worst possible
outcome: a green "no findings — knowledge base is consistent." report from a KB
whose rules evaporated. So the KB *declares* it instead: when a policy file is
written (or adopted), a `policy.logic` marker is recorded in `kb_meta`. The
marker — never mtime, git state, or a leftover directory — is the only input to
the judgement here.

The marker stores a sha256 as *evidence* (so a report can say what was there),
not as a verdict: editing a policy is normal and a hash mismatch is never an
error. The `.dl` file remains the owner of the policy text; the DB never stores
its body.

Four states, one resolution point:

| file            | marker  | status               | behaviour                      |
|-----------------|---------|----------------------|--------------------------------|
| present (rules) | either  | `PRESENT`            | use the file                   |
| present (empty) | either  | `PRESENT_EMPTY`      | loud error, no engine run      |
| absent          | present | `MISSING_RECORDED`   | loud error, no engine run      |
| absent          | absent  | `UNRECORDED_DEFAULT` | shipped default + loud warning |

An empty or whitespace-only file (`PRESENT_EMPTY`, #171) is halted, not run: it
declares no `relation/3`, so the engine would otherwise reject it with a cryptic
"program must declare relation/3" that never names the real cause. Like the other
halted states it must not reintroduce a silent fallback to DEFAULT_POLICY, and —
unlike a non-empty present file — opening the KB records no marker for it, since
`sha256("")` would overwrite the evidence of a policy that once existed. Recovery
is `verinote policy reset --force`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from verinote.store import Store

# Per-KB policy location, relative to the KB root (the db file's directory).
POLICY_RELPATH = Path("policy") / "logic-policy.dl"

# Marker origins. `scaffold`: written by init/first KB open. `adopted`: a policy
# file already existed before markers were recorded. `reset`: an explicit human
# `verinote policy reset --force`.
POLICY_ORIGINS = frozenset({"scaffold", "adopted", "reset"})

POLICY_UNRECORDED_FINDING = (
    "WARNING policy_unrecorded: no KB policy file; running the shipped default"
)
# Replaces the engine's own clean-bill sentence (`engine.NO_FINDINGS_TEXT`) when
# the policy that ran was not this KB's. The engine owns that sentence; this
# module only owns the substitute, so the two cannot drift apart.
POLICY_UNRECORDED_NO_FINDINGS_TEXT = (
    "no findings from the shipped default policy — this KB has no policy file of "
    "its own, so this result says nothing about the KB's own rules."
)
POLICY_UNRECORDED_BANNER = (
    "WARNING policy_unrecorded: this KB has no policy file and never recorded one, "
    "so the shipped default policy was used. Findings below are the default's, not "
    "this KB's rules. Run `verinote init` (or `verinote policy reset --force`) to "
    "record a policy file for this KB."
)


# --- the CLI's diagnostic surface ------------------------------------------
#
# `status` and `coverage` are the only diagnosis a non-web user has, and a halt
# they never mention is a halt discovered only by trying to write (#194). These
# lines go to *stdout*, not stderr: `verinote status > out.txt` and CI health
# checks read stdout, and a halt marker they cannot see is not a marker.
#
# Deliberately one short line per state, not a banner. The UNRECORDED_DEFAULT
# case is the *normal* state of a brand-new KB; shouting at every `status` teaches
# people to ignore the policy line, and then the HALTED line gets ignored with it.
# The loud, actionable text for a real halt is `policy_missing_message`, which the
# CLI prints to stderr *in addition* to the stdout marker.
POLICY_CLI_LINE_PRESENT = "policy: ok (policy file present)"
POLICY_CLI_LINE_PRESENT_EMPTY = "policy: HALTED (policy file empty)"
POLICY_CLI_LINE_MISSING_RECORDED = "policy: HALTED (rules missing)"
POLICY_CLI_LINE_UNRECORDED_DEFAULT = "policy: default (this KB records no rules of its own)"


class PolicyMissingError(RuntimeError):
    """Raised when a KB's recorded logic policy cannot be run.

    Covers both halted-present states: the file is *gone* (a KB recorded one and
    it is missing), and the file *exists but is empty* (`PolicyEmptyError`). Both
    halt for the same reason — the KB's rules are not being applied — so every
    `except PolicyMissingError` write gate catches both without change.
    """


class PolicyEmptyError(PolicyMissingError):
    """Raised when a KB's policy file exists but is empty or whitespace-only.

    A subclass of `PolicyMissingError` on purpose: an empty file declares no
    rules, so it must halt writes exactly as a missing one does, and subclassing
    means every existing `except PolicyMissingError` site (CLI dispatch, the web
    write guard and its exception handler, the acceptance/corroboration gates)
    catches it with no new catch clause (#171).
    """


class PolicyStatus(str, Enum):
    """How the KB's policy file relates to what the KB declared about it."""

    PRESENT = "present"
    PRESENT_EMPTY = "present_empty"
    MISSING_RECORDED = "missing_recorded"
    UNRECORDED_DEFAULT = "unrecorded_default"


@dataclass(frozen=True)
class PolicyState:
    """The resolved policy situation for one KB — pure data, no side effects."""

    status: PolicyStatus
    path: Path
    text: str | None = None
    marker: dict[str, object] | None = None

    @property
    def is_missing(self) -> bool:
        return self.status is PolicyStatus.MISSING_RECORDED


def policy_sha256(text: str) -> str:
    """Evidence hash for a policy body (never a verdict — edits are normal)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _policy_text_is_empty(text: str) -> bool:
    """Whether a present policy file declares nothing at all (#171).

    A 0-byte file and a whitespace-only one are byte-different but behave
    identically: both parse to an empty program and fail the engine's later
    `relation/3` check. `text.strip() == ""` is the whole definition on purpose —
    a comment-only file or one that declares `functional` but not literally
    `relation` is *not* empty and is left to the engine, not caught here. The CLI's
    read-only resolver shares this helper so the two cannot drift apart.
    """
    return text.strip() == ""


def policy_path(store: "Store") -> Path:
    return store.db_path.parent / POLICY_RELPATH


def resolve_policy(store: "Store") -> PolicyState:
    """Resolve the KB's policy into exactly one of the four states.

    The only judgement inputs are: does the file exist, is it empty, and did the
    KB record a marker. Nothing else is inferred.
    """
    path = policy_path(store)
    marker = store.policy_marker()
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        status = (
            PolicyStatus.PRESENT_EMPTY if _policy_text_is_empty(text) else PolicyStatus.PRESENT
        )
        return PolicyState(status=status, path=path, text=text, marker=marker)
    if marker is not None:
        return PolicyState(status=PolicyStatus.MISSING_RECORDED, path=path, marker=marker)
    return PolicyState(status=PolicyStatus.UNRECORDED_DEFAULT, path=path)


def policy_missing_message(state: PolicyState) -> str:
    """The loud, actionable message for a recorded-but-missing policy."""
    marker = state.marker or {}
    origin = str(marker.get("origin") or "unknown")
    recorded_at = str(marker.get("recorded_at") or "unknown")
    sha = str(marker.get("sha256") or "")
    sha_text = sha[:12] if sha else "unknown"
    return (
        f"policy file {state.path} is missing, but this KB recorded one "
        f"(origin={origin}, recorded_at={recorded_at}, sha256={sha_text}). "
        "Verification is halted instead of silently falling back to the shipped "
        "default policy. Recover by either (1) restoring the policy file from a "
        "backup or version control, or (2) running `verinote policy reset --force` "
        "to explicitly re-create the default policy for this KB."
    )


def policy_empty_message(state: PolicyState) -> str:
    """The loud, actionable message for a present-but-empty policy file.

    Deliberately cites no marker hash: the marker is preserved as-is (#171), but a
    hash in a "your file is empty" message only adds confusion. The point is the
    file, not the evidence of what used to be in it.
    """
    return (
        f"policy file {state.path} exists but is empty or whitespace-only: "
        "it declares no rules at all — not even the `relation/3` declaration the "
        "engine requires. Verification is halted instead of running an empty policy "
        "or silently falling back to the shipped default policy. Recover by either "
        "(1) restoring the policy file from a backup or version control, or (2) "
        "running `verinote policy reset --force` to re-create the default policy "
        "for this KB."
    )


def policy_cli_line(state: PolicyState) -> str:
    """The one-line stdout marker for a resolved policy state.

    A new `PolicyStatus` must add its line to `_POLICY_CLI_LINES`; the KeyError
    that follows otherwise is deliberate. A blank line silently standing in for an
    unknown policy state is exactly the class of bug this module exists to kill.
    """
    return _POLICY_CLI_LINES[state.status]


_POLICY_CLI_LINES = {
    PolicyStatus.PRESENT: POLICY_CLI_LINE_PRESENT,
    PolicyStatus.PRESENT_EMPTY: POLICY_CLI_LINE_PRESENT_EMPTY,
    PolicyStatus.MISSING_RECORDED: POLICY_CLI_LINE_MISSING_RECORDED,
    PolicyStatus.UNRECORDED_DEFAULT: POLICY_CLI_LINE_UNRECORDED_DEFAULT,
}


def assert_writable(store: "Store") -> PolicyState:
    """Refuse to let a KB whose recorded policy file is gone be written to.

    The single enforcement predicate: every write entrypoint (CLI dispatch, the
    extraction worker's write boundary, the web guard) asks *this*, so the three
    of them cannot disagree about what "halted" means. Enforcement points must
    call it instead of re-deriving the state — `resolve_policy` is the only place
    allowed to look at the file and the marker.

    A halted KB still has to be *recoverable*, so this is deliberately not a
    blanket lock on the DB: read-only diagnosis (`status`, `coverage`, the web
    `/report` page) and `policy reset --force` do not go through here.
    """
    state = resolve_policy(store)
    if state.status is PolicyStatus.MISSING_RECORDED:
        raise PolicyMissingError(policy_missing_message(state))
    if state.status is PolicyStatus.PRESENT_EMPTY:
        raise PolicyEmptyError(policy_empty_message(state))
    return state


def ensure_policy_marker(store: "Store", root: Path | None = None) -> PolicyState:
    """Record/refresh the policy marker when a KB is opened.

    * file present, no marker  -> adopt it (`origin="adopted"`); pre-marker KBs
      keep working, and any *later* loss of the file is loud.
    * file present, marker     -> refresh the evidence hash (never an error).
    * file present but empty    -> record NOTHING and return `PRESENT_EMPTY` with
      the marker as read: recording `sha256("")` here would silently overwrite a
      real policy's marker, so merely opening a KB whose file was truncated — which
      the web UI does on launch and on a KB switch, without any write — would
      destroy the evidence a policy ever existed (#171).
    * file absent              -> do nothing; the two absent states are already
      distinguishable and must stay that way.
    """
    path = (root / POLICY_RELPATH) if root is not None else policy_path(store)
    marker = store.policy_marker()
    if not path.is_file():
        status = (
            PolicyStatus.MISSING_RECORDED if marker is not None else PolicyStatus.UNRECORDED_DEFAULT
        )
        return PolicyState(status=status, path=path, marker=marker)
    text = path.read_text(encoding="utf-8")
    if _policy_text_is_empty(text):
        return PolicyState(
            status=PolicyStatus.PRESENT_EMPTY, path=path, text=text, marker=marker
        )
    origin = str(marker.get("origin")) if marker else "adopted"
    if origin not in POLICY_ORIGINS:
        origin = "adopted"
    refreshed = store.record_policy_marker(policy_sha256(text), origin=origin)
    return PolicyState(
        status=PolicyStatus.PRESENT, path=path, text=text, marker=refreshed
    )


def write_default_policy(store: "Store", root: Path | None = None, *, origin: str) -> Path:
    """Write DEFAULT_POLICY and record the marker. Callers own the human gate."""
    from verinote.engine import DEFAULT_POLICY

    if origin not in POLICY_ORIGINS:
        raise ValueError(f"unknown policy marker origin: {origin}")
    path = (root / POLICY_RELPATH) if root is not None else policy_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ORDER IS LOAD-BEARING — file first, marker second. This is the function that
    # un-halts a KB (`policy reset --force`), and it runs *on* a halted KB. Writing
    # the file first flips `resolve_policy` to PRESENT before the marker write
    # happens, so the DB write lands on a KB that is no longer halted. Reverse
    # these two lines and the only recovery path starts tripping over the very
    # halt it exists to clear.
    path.write_text(DEFAULT_POLICY, encoding="utf-8")
    store.record_policy_marker(policy_sha256(DEFAULT_POLICY), origin=origin)
    return path
