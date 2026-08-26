# SPDX-License-Identifier: MPL-2.0
"""Gated self-correction: re-translate `review_required` questions, engine-gated.

The LLM proposes a corrected query line for each `review_required` question, but
the proposal is only accepted if the deterministic engine validates it — the
engine, not the model, has the final say. Rejected proposals leave the question
untouched and the reason is logged.

Schema vocabulary supplied to the repair fallback is advisory; only the engine
gate approves a proposal.
"""

from __future__ import annotations

import logging
from pathlib import Path
from dataclasses import dataclass
import sqlite3
import threading
from uuid import uuid4

from verinote.llm.base import LLMClient
from verinote.pipeline.query import (
    _schema_aware_query_flow_result,
    _translate_direct_datalog_fallback,
    write_query_file,
)
from verinote.store import Store
from verinote.pipeline.policy_state import PolicyMissingError

_log = logging.getLogger("verinote.repair")


@dataclass(frozen=True)
class RepairQuestionResult:
    id: int
    accepted: bool
    reason: str
    provider_failed: bool = False


@dataclass(frozen=True)
class _PreparedRepair:
    result: RepairQuestionResult
    status: str
    query_dl: str | None
    # #592. True when no usable provider output existed -- the provider was
    # never reached, or a policy file could not be read so translation was never
    # attempted. Both repair writers key their write suppression on this.
    # `result.provider_failed` alone is not enough: it does not cover the policy
    # fault, where no provider is asked at all and so nothing sets that flag.
    infrastructure_fault: bool = False


def _prepare_repair_question(
    store: Store, client: LLMClient, *, question_id: int, question: str,
    previous_query_dl: str | None, allow_direct_datalog_fallback: bool = True,
) -> _PreparedRepair:
    """Run the shared repair decision without committing its result."""
    flow = _schema_aware_query_flow_result(
        store, client, qid=question_id, question=question, llm_error_status="review_required"
    )
    if (
        allow_direct_datalog_fallback
        and flow.status == "review_required"
        and flow.allow_direct_datalog_fallback
    ):
        flow = _translate_direct_datalog_fallback(
            store, client, qid=question_id, question=question, llm_error_status="review_required"
        )
    query_dl = flow.query_dl
    if flow.status == "review_required" and query_dl is not None and flow.provider_failed:
        query_dl = previous_query_dl
    accepted = flow.status == "translated"
    return _PreparedRepair(
        RepairQuestionResult(question_id, accepted, "" if accepted else flow.reason, flow.provider_failed),
        flow.status,
        query_dl,
        infrastructure_fault=flow.infrastructure_fault,
    )


def repair_question(
    store: Store,
    client: LLMClient,
    *,
    question_id: int,
    question: str,
    root: Path,
    allow_direct_datalog_fallback: bool = True,
) -> RepairQuestionResult:
    """Repair one current question and persist its engine-gated outcome.

    ``provider_failed`` is deliberately structured metadata, rather than a
    conclusion drawn from the human-facing reason string. Job workers use it to
    stop after the first provider failure while the synchronous CLI retains its
    historic all-question behavior.
    """
    previous = store.repair_job_question(question_id)
    prepared = _prepare_repair_question(
        store, client, question_id=question_id, question=question,
        previous_query_dl=previous["query_dl"] if previous is not None else None,
        allow_direct_datalog_fallback=allow_direct_datalog_fallback,
    )
    # #592. REPORTED, NEVER RECORDED. A policy file that cannot be read reaches
    # here as `translation_failed`, which claims "The provider output could not
    # be used" about a translation that was never attempted. The row keeps the
    # status it had; the caller still receives the result and logs it below.
    if not prepared.infrastructure_fault:
        store.set_question_query(
            question_id, prepared.query_dl, prepared.status, prepared.result.reason
        )
    write_query_file(store, root)
    if not prepared.result.accepted:
        _log.warning("repair q%d kept %s: %s", question_id, prepared.status, prepared.result.reason)
    return prepared.result


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

    With `allow_direct_datalog_fallback` (the default), an LLM-confirmed
    unsupported intent costs two provider calls: intent extraction, then the
    direct Datalog fallback. A deterministically supported intent whose planner
    returns no candidates costs up to two: the schema-aware reinterpretation of
    the question, and then the direct Datalog fallback if that reinterpretation
    declines. It costs one whenever the reinterpretation settles the question --
    either because it produced an executable query, so the question leaves
    `review_required`, or because it surfaced an engine or policy error, which
    replaces the result and withholds the fallback rather than handing a draft
    to an engine that is already failing. An intent-extraction LLM error costs
    one failed call and never retries the provider; so does a reinterpretation
    LLM error, which switches the fallback off for that reason.
    """
    results: list[dict] = []
    for q in store.questions():
        if q["status"] != "review_required":
            continue
        qid = q["id"]
        result = repair_question(
            store, client, question_id=qid, question=q["text"], root=root,
            allow_direct_datalog_fallback=allow_direct_datalog_fallback,
        )
        accepted = result.accepted
        results.append(
            {"id": qid, "accepted": accepted, "reason": result.reason}
        )
    return results


def process_repair_job(
    store: Store, client: LLMClient, *, job_id: int, root: Path, policy_guard=lambda: None,
) -> None:
    """Process one durable snapshot sequentially, stopping on provider failure.

    A heartbeat keeps normal long provider calls owned. Crash recovery is still
    at-least-once: an expired caller may already have sent a provider request.
    """
    owner_token = uuid4().hex
    if not store.claim_repair_job(job_id, owner_token):
        return
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        # A second worker-owned connection avoids sharing SQLite state with the
        # provider call. Policy is checked before this durable renewal too.
        with Store(store.db_path) as heartbeat_store:
            heartbeat_store.init_schema()
            while not stop_heartbeat.wait(5):
                try:
                    policy_guard()
                except PolicyMissingError:
                    return
                if not heartbeat_store.renew_repair_job_lease(job_id, owner_token):
                    return

    thread = threading.Thread(target=heartbeat, name=f"verinote-repair-heartbeat-{job_id}", daemon=True)
    thread.start()

    def publish_query_file() -> bool:
        def guard(conn: sqlite3.Connection) -> bool:
            policy_guard()
            return Store.repair_query_publication_owned(conn, job_id, owner_token)

        return write_query_file(store, root, publication_guard=guard) is not None

    try:
        while True:
            item = store.claim_next_repair_item(job_id, owner_token)
            if item is None:
                try:
                    if not publish_query_file():
                        return
                except PolicyMissingError:
                    raise
                except Exception as exc:
                    store.defer_repair_job(
                        job_id, owner_token, f"Query draft regeneration pending: {' '.join(str(exc).split())[:200]}"
                    )
                    return
                store.finish_repair_job(job_id, owner_token)
                return
            question = store.repair_job_question(int(item["question_id"]))
            if question is None:
                store.finish_repair_item(int(item["id"]), owner_token, status="skipped", reason="question deleted")
                continue
            if question["status"] != "review_required":
                store.finish_repair_item(
                    int(item["id"]), owner_token, status="skipped", reason="question no longer requires review"
                )
                continue
            # This is immediately before provider work, not merely before job claim.
            policy_guard()
            try:
                prepared = _prepare_repair_question(
                    store, client, question_id=int(question["id"]), question=str(question["text"]),
                    previous_query_dl=question["query_dl"],
                )
                # A policy can disappear during the provider call. Check before
                # every DB/query-write boundary, before any result is persisted.
                policy_guard()
                # #592. Same rule on the async path, and it has to be here
                # rather than only in the sync writer: this is a SEPARATE write,
                # and `persist_repair_question` would set the row to a status
                # describing provider output that never existed. Skipping it
                # leaves the row `review_required`, which is still true of it.
                # The fault is reported below, where the item and job are
                # finished as failed with the same reason.
                if not prepared.infrastructure_fault:
                    persisted = store.persist_repair_question(
                        job_id, int(item["id"]), owner_token, int(question["id"]),
                        prepared.query_dl, prepared.status, prepared.result.reason,
                    )
                    if not persisted:
                        return
            except PolicyMissingError:
                raise
            except Exception as exc:
                reason = " ".join(str(exc).split())[:240]
                store.finish_repair_item(int(item["id"]), owner_token, status="failed", reason=reason)
                store.finish_repair_job(job_id, owner_token, failed=True, message=f"Repair failed: {reason}")
                raise
            # #592 ADDS a disjunct here; it does not replace one. An earlier
            # draft substituted `infrastructure_fault` for
            # `result.provider_failed`, which NARROWED the report: an answer
            # that arrived unusable sets `provider_failed` and clears
            # `infrastructure_fault`, so the job stopped failing on it --
            # measured, the item went from `failed` on the parent to `done`.
            # The two disjuncts answer different questions. `provider_failed`
            # is "the provider work did not produce a usable translation",
            # which is what the job reports on. `infrastructure_fault` adds the
            # policy fault, where no provider was asked at all, so nothing was
            # translated and the job must not finish "done" over a row it
            # deliberately did not write.
            if prepared.result.provider_failed or prepared.infrastructure_fault:
                store.finish_repair_item(int(item["id"]), owner_token, status="failed", reason=prepared.result.reason)
                store.finish_repair_job(
                    job_id, owner_token, failed=True, message=f"Repair failed: {prepared.result.reason}"
                )
                return
            store.finish_repair_item(int(item["id"]), owner_token, status="done", reason=prepared.result.reason)
            # Keep the existing derived writer close to each committed result.
            # If it fails, the item remains done and a later worker regenerates
            # the file without calling that question's provider again.
            try:
                if not publish_query_file():
                    return
            except PolicyMissingError:
                raise
            except Exception as exc:
                store.defer_repair_job(
                    job_id, owner_token,
                    f"Query draft regeneration pending: {' '.join(str(exc).split())[:200]}",
                )
                return
    finally:
        stop_heartbeat.set()
        thread.join(timeout=1)
