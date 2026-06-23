# SPDX-License-Identifier: Apache-2.0
"""Extract candidate facts from a source and persist them as `candidate` rows."""

from __future__ import annotations

from verinote.llm.base import LLMClient
from verinote.store import Store


def extract_source(
    store: Store,
    client: LLMClient,
    *,
    source_path: str,
    source_text: str,
    schema_hint: str = "",
) -> int:
    """Run extraction for one source; insert candidates. Returns count inserted.

    Newly extracted facts land as `candidate` — they only become engine input
    after passing the human review gate (see the web review queue).
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
            note=f.note,
        )
    return len(facts)
