# SPDX-License-Identifier: MPL-2.0
"""Gated self-correction: re-translate `review_required` questions, engine-gated.

The LLM proposes a corrected query line for each `review_required` question, but
the proposal is only accepted if the deterministic engine validates it — the
engine, not the model, has the final say. Rejected proposals leave the question
untouched and the reason is logged.
"""

from __future__ import annotations

import logging
from pathlib import Path

from verinote.engine import validate_query
from verinote.llm.base import LLMClient, LLMError
from verinote.pipeline.query import write_query_file
from verinote.store import Store

_log = logging.getLogger("verinote.repair")


def repair_questions(store: Store, client: LLMClient, *, root: Path) -> list[dict]:
    """Attempt to repair every `review_required` question. Returns per-question
    results: {id, accepted, reason}. Only engine-validated proposals are applied.
    """
    results: list[dict] = []
    for q in store.questions():
        if q["status"] != "review_required":
            continue
        qid = q["id"]
        try:
            line = client.translate_query(question=q["text"], qid=qid)
        except LLMError as exc:
            results.append({"id": qid, "accepted": False, "reason": f"llm error: {exc}"})
            _log.warning("repair q%d: llm error: %s", qid, exc)
            continue

        if line.lstrip().startswith("review_required"):
            results.append({"id": qid, "accepted": False, "reason": "model still cannot express it"})
            _log.warning("repair q%d: still review_required", qid)
            continue

        proposal = f".decl answer_q{qid}(value: symbol)\n{line}"
        ok, reason = validate_query(proposal)
        if ok:
            store.set_question_query(qid, proposal, "translated")
            results.append({"id": qid, "accepted": True, "reason": ""})
        else:
            results.append({"id": qid, "accepted": False, "reason": reason})
            _log.warning("repair q%d rejected by engine: %s", qid, reason)

    write_query_file(store, root)
    return results
