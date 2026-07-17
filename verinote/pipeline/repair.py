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

from verinote.llm.base import LLMClient
from verinote.pipeline.query import (
    _schema_aware_query_flow_result,
    _translate_direct_datalog_fallback,
    write_query_file,
)
from verinote.store import Store

_log = logging.getLogger("verinote.repair")


def repair_questions(
    store: Store,
    client: LLMClient,
    *,
    root: Path,
    allow_direct_datalog_fallback: bool = True,
) -> list[dict]:
    """Attempt to repair every `review_required` question. Returns per-question
    results: {id, accepted, reason}. Only engine-validated proposals are applied.

    The model can propose but never retire the review flag: a question leaves
    `review_required` only when the engine validates a query that answers *that*
    question. A model declaring `no_answer`/`ambiguous` is recorded as a reason
    and the question stays flagged, so a later run can still repair it.

    With `allow_direct_datalog_fallback` (the default), a question the planner
    cannot map costs two provider calls: intent extraction, then the direct
    Datalog fallback. That includes the case where intent extraction *failed* —
    during a provider outage or rate limit, each question is tried twice.
    """
    results: list[dict] = []
    for q in store.questions():
        if q["status"] != "review_required":
            continue
        qid = q["id"]
        flow = _schema_aware_query_flow_result(
            store,
            client,
            qid=qid,
            question=q["text"],
            llm_error_status="review_required",
        )
        status, query_dl, reason = flow.status, flow.query_dl, flow.reason
        if (
            allow_direct_datalog_fallback
            and status == "review_required"
            and flow.allow_direct_datalog_fallback
        ):
            status, query_dl, reason = _translate_direct_datalog_fallback(
                store,
                client,
                qid=qid,
                question=q["text"],
                llm_error_status="review_required",
            )
        if (
            status == "review_required"
            and query_dl is not None
            and reason.startswith("llm error:")
        ):
            query_dl = q["query_dl"]
        store.set_question_query(qid, query_dl, status, reason)
        accepted = status == "translated"
        results.append(
            {"id": qid, "accepted": accepted, "reason": "" if accepted else reason}
        )
        if not accepted:
            _log.warning("repair q%d kept %s: %s", qid, status, reason)

    write_query_file(store, root)
    return results
