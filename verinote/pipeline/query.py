# SPDX-License-Identifier: MPL-2.0
"""Translate NL questions to Datalog query drafts and persist them for the engine.

Each pending question is translated by the `LLMClient` into either an
`answer_q<id>` rule (status `translated`) or a durable non-executable lifecycle
state such as `review_required`, `no_answer`, `ambiguous`, or
`translation_failed`. The translated rules are written to `<root>/facts/query.dl`,
which `verify()` feeds to the DuckDB backend so `/report` shows each query's
evaluation. Non-executable outcomes are tracked in the DB only.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
from itertools import product
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unicodedata
from typing import TYPE_CHECKING, Callable

from verinote.engine.datalog import AtomExpr, Comparison, DatalogParseError, parse_program
from verinote.engine.terms import Atom, StringLit, render_term
from verinote.llm.base import LLMClient, LLMError
from verinote.pipeline.corroboration import (
    CorroborationPolicyError,
    store_relation_aliases,
)
from verinote.pipeline.query_intent import (
    QueryIntent,
    QueryIntentKind,
    deterministic_query_intent,
)
from verinote.pipeline.query_planner import plan_query_candidates
from verinote.pipeline.query_schema import (
    QuerySchemaSnapshot,
    build_query_schema_snapshot,
)
from verinote.store import Store

if TYPE_CHECKING:
    # Runtime imports of this module are deliberately kept inside the functions
    # that need it: query_candidate_eval imports back from here, so a module-level
    # import would be circular. Annotations are strings under
    # `from __future__ import annotations`, so the name is only ever needed by a
    # type checker.
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

# Query draft, relative to the KB root (the db file's directory).
QUERY_RELPATH = Path("facts") / "query.dl"
# Must match the DuckDB backend's answer-predicate prefix.
ANSWER_PREFIX = "answer_q"
MAX_ALIAS_EXPANDED_RULES_PER_RULE = 64
_ESCAPE = re.compile(r'(["\\])')
_STATUS_LINE = re.compile(
    r"^\s*(?P<status>review_required|no_answer|ambiguous)\s*\((?P<reason>.*)\)\s*\.?\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class _QueryFlowResult:
    status: str
    query_dl: str | None
    reason: str
    # Repair-only eligibility: ordinary translation is always schema-only.
    allow_direct_datalog_fallback: bool = False
    provider_failed: bool = False


def query_path(root: Path) -> Path:
    return root / QUERY_RELPATH


_UNSUPPORTED_DIRECTORY_SYNC_ERRORS = frozenset(
    {
        errno.EINVAL,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
    }
)


def _fsync_directory(path: Path) -> None:
    """Persist a replacement's directory entry where the platform supports it."""
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        fd = os.open(path, os.O_RDONLY | directory_flag)
    except OSError as exc:
        if _directory_sync_is_unsupported(exc):
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if _directory_sync_is_unsupported(exc):
            return
        raise
    finally:
        os.close(fd)


def _directory_sync_is_unsupported(exc: OSError) -> bool:
    return exc.errno in _UNSUPPORTED_DIRECTORY_SYNC_ERRORS or (
        os.name == "nt" and exc.errno in {errno.EACCES, errno.EPERM}
    )


def _is_review_required(line: str) -> bool:
    return line.lstrip().startswith("review_required")


def _non_executable_outcome(line: str) -> tuple[str, str] | None:
    match = _STATUS_LINE.match(line)
    if not match:
        return None
    return match.group("status"), _short_reason(match.group("reason"))


def _short_reason(value: object) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    text = text.replace('\\"', '"').replace("\\\\", "\\")
    return " ".join(text.split())[:240]


def _lit(value: str) -> str:
    return '"' + _ESCAPE.sub(r"\\\1", value) + '"'


def query_schema_hint(snapshot: QuerySchemaSnapshot) -> str:
    """Render a bounded schema-only hint for structured intent extraction."""
    lines = [f"Confirmed relation/3 schema snapshot: {snapshot.fact_count} fact(s)."]
    if snapshot.relations:
        lines.append("Observed relations:")
    for relation in snapshot.relations:
        aliases = []
        labels = [relation.canonical_relation]
        if relation.relation.display != relation.canonical_relation:
            aliases.append(relation.relation.display)
        aliases.extend(alias.alias for alias in relation.aliases)
        if relation.typed is not None:
            labels.append(relation.typed.relation)
            aliases.append(relation.typed.alias)
        label_text = ", ".join(dict.fromkeys(labels))
        alias_text = ", ".join(dict.fromkeys(aliases))
        alias_suffix = f" (aliases: {alias_text})" if alias_text else ""
        typed_suffix = ""
        if relation.typed is not None:
            typed_suffix = f" (typed: {relation.typed.type})"
            if relation.typed.units:
                units = ", ".join(
                    f"{unit.unit}={unit.scale}" for unit in relation.typed.units
                )
                typed_suffix = f" (typed: {relation.typed.type}; units: {units})"
        lines.append(
            f"- {label_text}{alias_suffix}{typed_suffix} "
            f"(subjects={relation.distinct_subject_count}, "
            f"objects={relation.distinct_object_count})"
        )
    if snapshot.relations_truncated:
        lines.append("- relation list truncated")
    return "\n".join(lines)


def _foreign_answer_predicate(qid: int, query_dl: str) -> str | None:
    """Return the first answer predicate in `query_dl` that does not answer `qid`.

    The dry-run only reports *that* a draft produced answers, not *which* question
    they answer, so a draft declaring `answer_q999` would otherwise be accepted as
    a repair for q1 while leaving q1 unanswered.
    """
    expected = f"{ANSWER_PREFIX}{qid}"
    try:
        program = parse_program(query_dl)
    except DatalogParseError:
        return None
    names = [decl.name for decl in program.declarations]
    names.extend(fact.atom.predicate for fact in program.facts)
    names.extend(rule.head.predicate for rule in program.rules)
    for name in names:
        if name.startswith(ANSWER_PREFIX) and name != expected:
            return name
    return None


def classify_query_draft(store: Store, qid: int, query_dl: str) -> tuple[str, str, str]:
    """Validate and dry-run one query draft before it can be persisted.

    Returns ``(status, query_dl, reason)`` using question lifecycle states.
    """
    from verinote.pipeline.query_candidate_eval import (
        QueryCandidateSetOutcome,
        evaluate_query_candidate_plan,
    )
    from verinote.pipeline.query_planner import (
        QueryCandidate,
        QueryCandidateFamily,
        QueryCandidatePlan,
    )

    foreign = _foreign_answer_predicate(qid, query_dl)
    if foreign is not None:
        reason = _short_reason(
            f"invalid query: answer predicate must be {ANSWER_PREFIX}{qid}, got {foreign}"
        )
        return "review_required", f"review_required({_lit(reason)})", reason

    candidate = QueryCandidate(
        query_dl=query_dl,
        family=QueryCandidateFamily.MANUAL_DRAFT,
        direction=None,
        relation_display=None,
        relation_executable=None,
        subject_executable=None,
        object_executable=None,
    )
    evaluation = evaluate_query_candidate_plan(
        store,
        QueryCandidatePlan(qid=qid, candidates=(candidate,)),
    )
    if evaluation.outcome == QueryCandidateSetOutcome.VALID:
        return "translated", query_dl, ""
    if evaluation.outcome == QueryCandidateSetOutcome.NO_ANSWER:
        reason = "no confirmed facts match"
        return "no_answer", f"no_answer({_lit(reason)})", reason
    if evaluation.outcome == QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING:
        reason = "multiple query candidates returned conflicting answers"
        return "ambiguous", f"ambiguous({_lit(reason)})", reason
    if evaluation.outcome == QueryCandidateSetOutcome.ENGINE_POLICY_ERROR:
        reason = _short_reason("engine/policy error: " + _evaluation_reason(evaluation))
    else:
        reason = _short_reason("invalid query: " + _evaluation_reason(evaluation))
    return "review_required", f"review_required({_lit(reason)})", reason


def schema_aware_query_flow(
    store: Store,
    client: LLMClient,
    *,
    qid: int,
    question: str,
    llm_error_status: str,
) -> tuple[str, str | None, str]:
    """Translate one question through intent extraction, planning, and dry-run evaluation."""
    result = _schema_aware_query_flow_result(
        store,
        client,
        qid=qid,
        question=question,
        llm_error_status=llm_error_status,
    )
    return result.status, result.query_dl, result.reason


def _schema_aware_query_flow_result(
    store: Store,
    client: LLMClient,
    *,
    qid: int,
    question: str,
    llm_error_status: str,
) -> _QueryFlowResult:
    snapshot = build_query_schema_snapshot(store)
    intent = deterministic_query_intent(question)
    deterministic_intent_supported = (
        intent.kind != QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    )
    if intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED:
        try:
            intent = client.extract_query_intent(
                question=question,
                schema_hint=query_schema_hint(snapshot),
            )
        except LLMError as exc:
            reason = _short_reason(exc)
            if llm_error_status == "translation_failed":
                return _QueryFlowResult("translation_failed", None, reason)
            reason = _short_reason(f"llm error: {exc}")
            # No fallback here. The direct-Datalog fallback answers "planning
            # reports it cannot support this question"; an LLMError reports
            # nothing at all about the question, only that the provider did not
            # answer. Treating unknown as unsupported would send a second
            # request to the provider that just failed — during an outage or
            # rate limit that doubles the failed calls per question, and it
            # would be the wrong call even if it were free. `LLMError` carries
            # no outage/rate-limit taxonomy to narrow this on (it is a bare
            # RuntimeError subclass with no subclasses or codes), so the
            # provider-failed path declines the fallback wholesale.
            return _QueryFlowResult(
                "review_required",
                f"review_required({_lit(reason)})",
                reason,
                provider_failed=True,
            )

    if intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED:
        reason = _short_reason(intent.reason or "unsupported query intent")
        return _QueryFlowResult(
            "review_required",
            f"review_required({_lit(reason)})",
            reason,
            allow_direct_datalog_fallback=True,
        )

    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    result, outcome = _plan_and_evaluate_intent(
        store,
        intent,
        qid=qid,
        base_snapshot=snapshot,
        deterministic_intent_supported=deterministic_intent_supported,
    )
    # `EMPTY` is the only outcome worth re-reading the question for, and only
    # when the deterministic parser is the one that read it. The parser is
    # schema-blind -- it sees the question and nothing else -- so it cannot tell
    # a relation the KB really holds from a chain of them: `X의 회의 일정은?` and
    # `X의 담당자의 상사는?` are the same shape to it, and it hands both to the
    # planner as one relation name. When that subject really does hold a
    # relation by that name the plan is VALID and nothing here runs; otherwise
    # the schema has just answered the question the parser was guessing at, and
    # the model -- which gets the observed relations in its hint -- deserves a
    # look. The population that reaches here is therefore every deterministic
    # empty plan, not only chains: a subject with no fact for an otherwise real
    # relation is re-read too, and pays a provider call to arrive back at the
    # same place.
    #
    # Every other outcome is the engine's own verdict over candidates that were
    # actually built, and re-reading the question until a verdict changes is how
    # a system talks itself out of an answer it already has. A truncated plan
    # reports no outcome at all (`None`), so it cannot reach this either.
    if (
        deterministic_intent_supported
        and outcome == QueryCandidateSetOutcome.EMPTY
    ):
        return _reinterpret_empty_plan(
            store,
            client,
            qid=qid,
            question=question,
            base_snapshot=snapshot,
            deterministic_result=result,
        )
    return result


def _reinterpret_empty_plan(
    store: Store,
    client: LLMClient,
    *,
    qid: int,
    question: str,
    base_snapshot: QuerySchemaSnapshot,
    deterministic_result: _QueryFlowResult,
) -> _QueryFlowResult:
    """Re-read a question the deterministic parser could plan nothing for.

    Only a `VALID` re-reading may replace the deterministic result, plus an
    engine or policy error, which is a failure signal rather than an answer and
    must never be hidden behind a stale reason. A model-supplied `no_answer` or
    `ambiguous` is refused on purpose even though the engine produced it: Ask
    renders `no_answer` as `VERIFIED — engine (negative)`, the strongest claim
    the system makes, and both statuses are terminal -- `translate_questions`
    re-picks only `pending`/`translation_failed` and `repair_questions` only
    `review_required`, so nothing would ever revisit the question. Keeping the
    deterministic `review_required` also keeps its
    `allow_direct_datalog_fallback`, so a declining re-reading costs the
    question nothing: repair still reaches the direct-Datalog rescue it reaches
    today.

    An `LLMError` is the one case that returns a *changed* deterministic result:
    the reason gains the provider error, and the fallback is switched off
    because `_prepare_repair_question` consults that flag before it looks at
    `provider_failed`, and would otherwise send a second request to a provider
    that has just failed.
    """
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    try:
        intent = client.extract_query_intent(
            question=question,
            schema_hint=query_schema_hint(base_snapshot),
        )
    except LLMError as exc:
        reason = _short_reason(
            f"{deterministic_result.reason}; schema-aware reinterpretation "
            f"unavailable: llm error: {exc}"
        )
        return _QueryFlowResult(
            "review_required",
            f"review_required({_lit(reason)})",
            reason,
            allow_direct_datalog_fallback=False,
            provider_failed=True,
        )

    if intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED:
        return deterministic_result

    # `deterministic_intent_supported=False`: this intent came from the model.
    # The flag only decides the EMPTY branch's fallback permission, and that
    # branch's result is discarded below in favour of the deterministic one,
    # which carries the permission the question already had.
    result, outcome = _plan_and_evaluate_intent(
        store,
        intent,
        qid=qid,
        base_snapshot=base_snapshot,
        deterministic_intent_supported=False,
    )
    if outcome in {
        QueryCandidateSetOutcome.VALID,
        QueryCandidateSetOutcome.ENGINE_POLICY_ERROR,
    }:
        return result
    return deterministic_result


def _plan_and_evaluate_intent(
    store: Store,
    intent: QueryIntent,
    *,
    qid: int,
    base_snapshot: QuerySchemaSnapshot,
    deterministic_intent_supported: bool,
) -> tuple[_QueryFlowResult, QueryCandidateSetOutcome | None]:
    """Plan one intent's candidates and turn the engine's verdict into a result.

    The outcome is returned beside the result because the two say different
    things: the result is what the question's lifecycle becomes, while the
    outcome is *why*. Several outcomes share the `review_required` status, and a
    caller that needs to tell them apart cannot recover the distinction from the
    result -- the reason string is human-facing prose, not a discriminator.

    `None` is returned in place of an outcome when the candidate set was
    truncated, and it is a different state from every `QueryCandidateSetOutcome`
    including `EMPTY`. Neither reaches the engine -- the evaluator returns for a
    candidate-less plan before executing anything -- but they mean opposite
    things: truncated is candidates built and deliberately left unevaluated,
    while `EMPTY` is no candidates built at all.

    `base_snapshot` is taken by value. The entity-narrowed rebuild below replaces
    it for planning only, so the caller keeps the unnarrowed snapshot it built.
    """
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    snapshot = base_snapshot
    exact_entities = _intent_exact_entities(intent)
    uses_join_facts = intent.kind in {
        QueryIntentKind.CONJUNCTIVE_LOOKUP,
        QueryIntentKind.CONJUNCTIVE_FILTER,
        QueryIntentKind.CONJUNCTIVE_THREE_HOP_LOOKUP,
    }
    if exact_entities or uses_join_facts:
        snapshot = build_query_schema_snapshot(
            store,
            exact_entities=exact_entities,
            include_join_facts=uses_join_facts,
        )
    plan = plan_query_candidates(intent, snapshot, qid=qid)
    # A truncated candidate set is not a safe basis for a verified answer: an
    # omitted path could return a different result and evade the ambiguity gate.
    if plan.truncated:
        reason = "too many query candidates matched the schema"
        return (
            _QueryFlowResult(
                "review_required", f"review_required({_lit(reason)})", reason
            ),
            None,
        )
    evaluation = evaluate_query_candidate_plan(store, plan)

    if evaluation.outcome == QueryCandidateSetOutcome.VALID and evaluation.selected:
        return (
            _QueryFlowResult("translated", evaluation.selected.query_dl, ""),
            evaluation.outcome,
        )
    if evaluation.outcome == QueryCandidateSetOutcome.NO_ANSWER:
        reason = "no confirmed facts match"
        return (
            _QueryFlowResult("no_answer", f"no_answer({_lit(reason)})", reason),
            evaluation.outcome,
        )
    if evaluation.outcome == QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING:
        reason = "multiple query candidates returned conflicting answers"
        return (
            _QueryFlowResult("ambiguous", f"ambiguous({_lit(reason)})", reason),
            evaluation.outcome,
        )
    if evaluation.outcome == QueryCandidateSetOutcome.REVIEW_REQUIRED:
        reason = _short_reason(_evaluation_reason(evaluation))
        return (
            _QueryFlowResult(
                "review_required", f"review_required({_lit(reason)})", reason
            ),
            evaluation.outcome,
        )
    if evaluation.outcome == QueryCandidateSetOutcome.EMPTY:
        reason = _short_reason(_empty_plan_reason(plan, intent))
        return (
            _QueryFlowResult(
                "review_required",
                f"review_required({_lit(reason)})",
                reason,
                allow_direct_datalog_fallback=deterministic_intent_supported,
            ),
            evaluation.outcome,
        )
    elif evaluation.outcome == QueryCandidateSetOutcome.ENGINE_POLICY_ERROR:
        reason = _short_reason("engine/policy error: " + _evaluation_reason(evaluation))
    else:
        reason = _short_reason("invalid query: " + _evaluation_reason(evaluation))
    return (
        _QueryFlowResult("review_required", f"review_required({_lit(reason)})", reason),
        evaluation.outcome,
    )


def _empty_plan_reason(plan, intent: QueryIntent) -> str:
    """Say which half of the question the KB does not have, and name it.

    "No query candidates matched the schema" is true of three situations whose
    remedies differ -- add a relation alias, fix the entity's spelling, or
    accept that the fact is simply absent -- so on its own it tells a user only
    that something did not work. The planner has already established which one
    it was; this turns that into the sentence the user acts on.

    Falls back to the planner's own reason whenever the diagnosis is absent,
    which is every kind whose emptiness does not reduce to one relation and one
    entity. Those kinds carry more specific messages already.
    """
    diagnosis = plan.diagnosis
    if diagnosis is None:
        return plan.reason or "no query candidates matched the schema"

    relation = ", ".join(
        f'"{value}"' for value in _intent_relation_labels(intent)
    )
    entities = ", ".join(
        f'"{target.value}"'
        for target in (intent.subject, intent.object)
        if target is not None
    )
    missing: list[str] = []
    if diagnosis.relation_in_schema is False and relation:
        missing.append(
            f"relation {relation} is not in the schema or its aliases "
            "(a policy/relation-aliases.md entry would map it)"
        )
    if diagnosis.absent_entities:
        # Only the endpoints that are actually missing. A question naming two
        # entities can have one the KB holds and one it does not, and saying
        # "entity A, B is not in the knowledge base" would be false about A.
        absent = ", ".join(f'"{value}"' for value in diagnosis.absent_entities)
        missing.append(f"entity {absent} is not in the knowledge base")
    if missing:
        return "; ".join(missing)
    if (
        diagnosis.relation_in_schema
        and diagnosis.entity_in_kb
        and diagnosis.join_search_complete
    ):
        # The reading is named only when some requested label did NOT resolve,
        # because that is the only case where naming it tells the reader
        # something: one of the words the question carried was substituted for
        # another. When every label resolved they are aliases of one relation,
        # and singling one out would put a sibling on screen that the user may
        # never have typed.
        if diagnosis.any_unmatched and diagnosis.matched_relations:
            resolved = ", ".join(f'"{value}"' for value in diagnosis.matched_relations)
            return (
                f"entity {entities} is in the knowledge base and relation "
                f"{resolved} resolved, but no confirmed fact joins them"
            )
        return (
            f"entity {entities} is in the knowledge base and the requested "
            "relation resolved, but no confirmed fact joins them"
        )
    return plan.reason or "no query candidates matched the schema"


def _intent_relation_labels(intent: QueryIntent) -> tuple[str, ...]:
    values = [intent.relation.value] if intent.relation is not None else []
    values.extend(intent.relation_candidates)
    return tuple(dict.fromkeys(values))


def _intent_exact_entities(intent: object) -> tuple[str, ...]:
    values = []
    for target in (getattr(intent, "subject", None), getattr(intent, "object", None)):
        if target is not None and target.kind in {"entity", "value", "typed_value"}:
            values.append(target.value)
    return tuple(dict.fromkeys(values))


def evaluate_query_candidate_plan(store: Store, plan):
    from verinote.pipeline.query_candidate_eval import (
        evaluate_query_candidate_plan as _evaluate_query_candidate_plan,
    )

    return _evaluate_query_candidate_plan(store, plan)


def _translate_direct_datalog_fallback(
    store: Store,
    client: LLMClient,
    *,
    qid: int,
    question: str,
    llm_error_status: str,
) -> _QueryFlowResult:
    """Run the repair-only direct-Datalog fallback with a schema-only hint."""
    try:
        line = client.translate_query(
            question=question,
            qid=qid,
            schema_hint=query_schema_hint(build_query_schema_snapshot(store)),
        )
    except LLMError as exc:
        reason = _short_reason(exc)
        if llm_error_status == "translation_failed":
            return _QueryFlowResult("translation_failed", None, reason, provider_failed=True)
        reason = _short_reason(f"llm error: {exc}")
        return _QueryFlowResult(
            "review_required", f"review_required({_lit(reason)})", reason, provider_failed=True
        )

    outcome = _non_executable_outcome(line)
    if outcome is None and _is_review_required(line):
        outcome = ("review_required", _short_reason(line.removeprefix("review_required")))
    if outcome is not None:
        declared, claim = outcome
        # The model may raise the review flag but never retire it: `no_answer`
        # and `ambiguous` are durable and no command re-picks them, so promoting
        # a claim the engine never checked would close the question for good.
        # Every such claim stays `review_required` and is attributed, so a reason
        # the engine never checked cannot read as the engine's own verdict. Only
        # engine-derived `no_answer`/`ambiguous` (the candidate dry-run finding
        # no rows) keep those statuses.
        reason = _short_reason(f"unvalidated model claim: {declared}: {claim}")
        return _QueryFlowResult("review_required", f"review_required({_lit(reason)})", reason)
    proposal = f".decl {ANSWER_PREFIX}{qid}(value: symbol)\n{line}"
    status, query_dl, reason = classify_query_draft(store, qid, proposal)
    return _QueryFlowResult(status, query_dl, reason)


def _evaluation_reason(evaluation) -> str:
    for item in evaluation.evaluations:
        if item.review_reason:
            return item.review_reason
    for item in evaluation.evaluations:
        if item.validation_reason:
            return item.validation_reason
        if item.report is not None:
            if item.report.findings:
                return item.report.findings[0]
            return item.report.text
    return evaluation.outcome.value


def translate_questions(
    store: Store,
    client: LLMClient,
    *,
    root: Path,
) -> list[dict]:
    """Translate pending and previously failed questions through the schema-only flow.

    Returns one dict per processed question: {id, status, query_dl, reason}.
    """
    results: list[dict] = []
    for q in store.questions():
        if q["status"] not in {"pending", "translation_failed"}:
            continue
        flow = _schema_aware_query_flow_result(
            store,
            client,
            qid=q["id"],
            question=q["text"],
            llm_error_status="translation_failed",
        )
        status, query_dl, reason = flow.status, flow.query_dl, flow.reason
        store.set_question_query(q["id"], query_dl, status, reason)
        results.append(
            {"id": q["id"], "status": status, "query_dl": query_dl, "reason": reason}
        )
    write_query_file(store, root)
    return results


def write_query_file(
    store: Store,
    root: Path,
    *,
    publication_guard: Callable[[sqlite3.Connection], bool] | None = None,
) -> Path | None:
    """Write the engine query draft (translated rules only) to `<root>/facts/query.dl`."""
    path = query_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with store.immediate_transaction() as conn:
            if publication_guard is not None and not publication_guard(conn):
                return None
            rows = conn.execute(
                "SELECT query_dl FROM questions WHERE status = 'translated' "
                "AND query_dl IS NOT NULL ORDER BY id"
            )
            lines = [row["query_dl"] for row in rows]
            contents = "\n".join(lines) + ("\n" if lines else "")
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            fd, staged_name = tempfile.mkstemp(prefix=".query.dl.", dir=path.parent)
            staged = Path(staged_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(contents)
                    handle.flush()
                    # chmod(path) is available on Windows, unlike os.fchmod().
                    os.chmod(staged, mode)
                    os.fsync(handle.fileno())
                os.replace(staged, path)
                staged = None
                # The old name is no longer available to restore if this fails.
                _fsync_directory(path.parent)
            except BaseException:
                if fd != -1:
                    os.close(fd)
                raise
    finally:
        if staged is not None:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
    return path


def load_query(store: Store) -> str | None:
    """Read the KB's query draft, or None when none has been generated."""
    path = store.db_path.parent / QUERY_RELPATH
    if path.is_file():
        return expand_query_relation_aliases(
            path.read_text(encoding="utf-8"), store_relation_aliases(store)
        )
    return None


def expand_query_relation_aliases(query_dl: str, aliases: dict[str, str]) -> str:
    """Append alias-expanded answer rules for relation/3 query bodies.

    Relation aliases are user policy, so the engine should honor them even when a
    query draft was translated before the alias existed. For example, with
    ``role -> 역할`` this appends a canonical rule for a model-generated
    ``relation(S, "role", O)`` body without mutating the stored draft.
    """
    if not aliases:
        return query_dl
    try:
        program = parse_program(query_dl)
    except DatalogParseError:
        return query_dl

    existing = {_render_rule(rule) for rule in program.rules}
    extra: list[str] = []
    for rule in program.rules:
        alternatives: list[list[object]] = []
        has_alias = False
        for item in rule.body:
            if not (isinstance(item, AtomExpr) and item.predicate == "relation"):
                alternatives.append([item])
                continue
            if len(item.args) != 3:
                alternatives.append([item])
                continue
            raw = _relation_name(item.args[1])
            if raw is None:
                alternatives.append([item])
                continue
            variants = _query_relation_alias_variants(raw, aliases)
            if len(variants) == 1:
                alternatives.append([item])
                continue
            choices = [item]
            choices.extend(
                AtomExpr("relation", (item.args[0], StringLit(variant), item.args[2]))
                for variant in variants
                if variant != unicodedata.normalize("NFC", raw)
            )
            alternatives.append(choices)
            has_alias = True
        if not has_alias:
            continue
        expanded_count = 1
        for choices in alternatives:
            expanded_count *= len(choices)
        if expanded_count > MAX_ALIAS_EXPANDED_RULES_PER_RULE:
            raise CorroborationPolicyError(
                "relation-aliases.md: query alias expansion exceeds "
                f"{MAX_ALIAS_EXPANDED_RULES_PER_RULE} rules for {rule.head.predicate}"
            )
        for body in product(*alternatives):
            rendered = _render_rule_with_body(rule.head, body)
            if rendered not in existing:
                existing.add(rendered)
                extra.append(rendered)
    if not extra:
        return query_dl
    suffix = "\n" if query_dl.endswith("\n") else "\n"
    return query_dl + suffix + "\n".join(extra) + "\n"


def _relation_name(term: object) -> str | None:
    if isinstance(term, StringLit):
        return unicodedata.normalize("NFC", term.value)
    if isinstance(term, Atom):
        return unicodedata.normalize("NFC", term.name)
    return None


def _query_relation_alias_variants(relation: str, aliases: dict[str, str]) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", relation)
    normalized_aliases = {
        unicodedata.normalize("NFC", raw): unicodedata.normalize("NFC", canonical)
        for raw, canonical in aliases.items()
    }
    if normalized in normalized_aliases:
        canonical = normalized_aliases[normalized]
        return (normalized, canonical) if canonical != normalized else (normalized,)
    variants = [normalized]
    for raw, canonical in sorted(
        normalized_aliases.items(), key=lambda item: (item[1], item[0])
    ):
        if canonical == normalized and raw not in variants:
            variants.append(raw)
    return tuple(variants)


def _render_rule(rule) -> str:
    return _render_rule_with_body(rule.head, rule.body)


def _render_rule_with_body(head: AtomExpr, body: tuple[object, ...]) -> str:
    return _render_atom(head) + " :- " + ", ".join(_render_body_item(item) for item in body) + "."


def _render_body_item(item: object) -> str:
    if isinstance(item, AtomExpr):
        return _render_atom(item)
    if isinstance(item, Comparison):
        return f"{render_term(item.left)} {item.op} {render_term(item.right)}"
    raise TypeError(f"unsupported body item: {item!r}")


def _render_atom(atom: AtomExpr) -> str:
    return atom.predicate + "(" + ", ".join(render_term(arg) for arg in atom.args) + ")"
