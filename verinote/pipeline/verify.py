# SPDX-License-Identifier: Apache-2.0
"""Compile confirmed facts and run the wirelog check."""

from __future__ import annotations

from verinote.engine import CheckReport, compile_dl, run_check
from verinote.store import ENGINE_STATUSES, Store


def verify(store: Store) -> CheckReport:
    """Project confirmed/accepted rows to `.dl` and run the deterministic check."""
    rows = store.facts(statuses=ENGINE_STATUSES)
    dl_text = compile_dl(rows)
    return run_check(dl_text)
