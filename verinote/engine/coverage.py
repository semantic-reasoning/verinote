# SPDX-License-Identifier: MPL-2.0
"""Coverage critic — the deterministic guard against silent omission.

A free-text source can't tell you what the extractor *failed* to capture, but the
store can tell you which sources contributed nothing to engine input. For each
source we report how many confirmed/accepted facts cite it and flag:

- **gap**: a source with zero engine-input facts (its text was never turned into
  a confirmed fact — only `candidate`/`needs_review` ones, or none at all).
- **orphan**: a source whose backing file is missing under the KB root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from verinote.store import Store


@dataclass(frozen=True)
class SourceCoverage:
    path: str
    kind: str
    engine_facts: int  # confirmed/accepted facts citing this source
    total_facts: int
    is_gap: bool  # zero engine-input facts
    is_orphan: bool  # backing file missing under the KB root


@dataclass(frozen=True)
class Coverage:
    sources: list[SourceCoverage]

    @property
    def gaps(self) -> list[SourceCoverage]:
        return [s for s in self.sources if s.is_gap]

    @property
    def orphans(self) -> list[SourceCoverage]:
        return [s for s in self.sources if s.is_orphan]

    @property
    def covered(self) -> list[SourceCoverage]:
        return [s for s in self.sources if not s.is_gap]


def coverage(store: Store, *, root: Path | None = None) -> Coverage:
    """Compute per-source engine-input coverage; flag gaps and orphan files."""
    kb_root = root if root is not None else store.db_path.parent
    rows = []
    for r in store.source_fact_counts():
        engine = int(r["engine"])
        path = r["path"]
        rows.append(
            SourceCoverage(
                path=path,
                kind=r["kind"],
                engine_facts=engine,
                total_facts=int(r["total"]),
                is_gap=engine == 0,
                is_orphan=not (kb_root / path).exists(),
            )
        )
    return Coverage(sources=rows)
