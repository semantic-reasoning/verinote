# SPDX-License-Identifier: Apache-2.0
"""Compile confirmed facts into wirelog `.dl` and run the deterministic check.

The database is the source of truth; the `.dl` text here is DERIVED from
confirmed/accepted rows each time the check runs. `compile_dl` is pure and fully
tested without the engine; `run_check` shells into the wirelog/pyrewire engine
and degrades gracefully when it is not installed (so the scaffold runs today).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

# Datalog string literals: escape embedded quotes/backslashes.
_ESCAPE = re.compile(r'(["\\])')


def _lit(value: str) -> str:
    return '"' + _ESCAPE.sub(r"\\\1", value) + '"'


def compile_dl(facts: Iterable[Mapping[str, object]]) -> str:
    """Render confirmed facts as `relation("s", "r", "o").` lines (sorted, unique).

    Accepts any row-like mapping with subject/relation/object keys (sqlite3.Row
    included). Only this projection becomes engine input.
    """
    lines = set()
    for f in facts:
        s, r, o = str(f["subject"]), str(f["relation"]), str(f["object"])
        lines.add(f"relation({_lit(s)}, {_lit(r)}, {_lit(o)}).")
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


@dataclass
class CheckReport:
    ok: bool
    errors: int
    warnings: int
    text: str
    findings: list[str] = field(default_factory=list)
    engine_available: bool = True


def run_check(dl_text: str) -> CheckReport:
    """Run the wirelog engine over compiled facts (+ policy/query, future work).

    Returns a structured report. If pyrewire is absent we still return a valid
    report flagged `engine_available=False` so the UI renders during scaffolding.
    """
    try:
        import pyrewire  # noqa: F401
    except ImportError:
        n = dl_text.count("relation(")
        return CheckReport(
            ok=True,
            errors=0,
            warnings=0,
            engine_available=False,
            text=(
                "wirelog engine (pyrewire) not installed — showing compiled input only.\n"
                f"compiled facts: {n}\n\n{dl_text}"
            ),
        )

    # TODO(v1): feed dl_text + policy/*.dl + query.dl to pyrewire and parse its
    # report into errors/warnings/findings. Wiring tracked in the v1 slice.
    return CheckReport(
        ok=True,
        errors=0,
        warnings=0,
        text="engine wiring pending (see TODO in engine/wirelog.py)",
    )
