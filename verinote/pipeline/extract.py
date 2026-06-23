# SPDX-License-Identifier: MPL-2.0
"""Extract candidate facts from sources and persist them as `candidate` rows.

`extract_source` handles one source; `sync_sources` wraps a batch in a single
`runs` row so the whole pass can later be inspected or retired as a unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from verinote.llm.base import LLMClient
from verinote.store import Store


def extract_source(
    store: Store,
    client: LLMClient,
    *,
    source_path: str,
    source_text: str,
    schema_hint: str = "",
    run_id: int | None = None,
) -> int:
    """Run extraction for one source; insert candidates. Returns count inserted.

    Newly extracted facts land as `candidate` — they only become engine input
    after passing the human review gate (see the web review queue). Each fact
    cites its `source` and (when given) the `run` that produced it.
    """
    source_id = store.add_source(source_path)
    facts = client.extract_facts(source_text=source_text, schema_hint=schema_hint)
    for f in facts:
        store.add_fact(
            f.subject,
            f.relation,
            f.object,
            status="candidate",
            confidence=f.confidence,
            source_id=source_id,
            run_id=run_id,
            note=f.note,
        )
    return len(facts)


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one `sync_sources` pass over a batch of sources."""

    run_id: int
    per_source: list[tuple[str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(n for _, n in self.per_source)


def sync_sources(
    store: Store,
    client: LLMClient,
    sources: Iterable[tuple[str, str]],
    *,
    provider: str | None,
    model: str | None,
    schema_hint: str = "",
) -> SyncResult:
    """Extract a batch of `(source_path, source_text)` pairs under one run.

    Opens a `runs` row (recording provider/model), links every produced fact to
    it, then writes a one-line summary. Any `LLMError` raised by the client
    propagates to the caller — the partial run row is left for inspection.
    """
    run_id = store.add_run(provider=provider, model=model)
    per_source: list[tuple[str, int]] = []
    for source_path, source_text in sources:
        n = extract_source(
            store,
            client,
            source_path=source_path,
            source_text=source_text,
            schema_hint=schema_hint,
            run_id=run_id,
        )
        per_source.append((source_path, n))
    result = SyncResult(run_id=run_id, per_source=per_source)
    store.set_run_summary(
        run_id, f"{len(per_source)} source(s), {result.total} candidate(s)"
    )
    return result
