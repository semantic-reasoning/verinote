# SPDX-License-Identifier: Apache-2.0
"""Compile confirmed facts and run the wirelog check against the KB policy."""

from __future__ import annotations

from pathlib import Path

from verinote.engine import CheckReport, compile_dl, run_check
from verinote.store import ENGINE_STATUSES, Store

# Per-KB policy location, relative to the KB root (the db file's directory).
POLICY_RELPATH = Path("policy") / "logic-policy.dl"


def policy_path(store: Store) -> Path:
    return store.db_path.parent / POLICY_RELPATH


def load_policy(store: Store) -> str | None:
    """Read the KB's policy file, or None to fall back to the shipped default."""
    path = policy_path(store)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def verify(store: Store) -> CheckReport:
    """Project confirmed/accepted rows to `.dl` and run the deterministic check."""
    rows = store.facts(statuses=ENGINE_STATUSES)
    dl_text = compile_dl(rows)
    return run_check(dl_text, policy_dl=load_policy(store))
