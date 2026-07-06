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

from itertools import product
from pathlib import Path
import re
import unicodedata

from verinote.engine.datalog import AtomExpr, Comparison, DatalogParseError, parse_program
from verinote.engine.terms import Atom, StringLit, render_term
from verinote.llm.base import LLMClient, LLMError
from verinote.pipeline.corroboration import (
    CorroborationPolicyError,
    relation_canonical_variant,
    store_relation_aliases,
)
from verinote.pipeline.query_intent import (
    KOREAN_ROLE_RELATION_CANDIDATES,
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

# Query draft, relative to the KB root (the db file's directory).
QUERY_RELPATH = Path("facts") / "query.dl"
MAX_ALIAS_EXPANDED_RULES_PER_RULE = 64
_ESCAPE = re.compile(r'(["\\])')
_GENERIC_ROLE_RELATIONS = ("역할", "직책", "직위", "role", "has_role")
_STATUS_LINE = re.compile(
    r"^\s*(?P<status>review_required|no_answer|ambiguous)\s*\((?P<reason>.*)\)\s*\.?\s*$",
    re.DOTALL,
)


def query_path(root: Path) -> Path:
    return root / QUERY_RELPATH


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
        labels = [relation.relation.display]
        if relation.canonical_relation != relation.relation.display:
            labels.append(relation.canonical_relation)
        labels.extend(alias.alias for alias in relation.aliases)
        labels.extend(alias.canonical for alias in relation.aliases)
        if relation.typed is not None:
            labels.extend((relation.typed.relation, relation.typed.alias))
        label_text = ", ".join(dict.fromkeys(labels))
        lines.append(
            f"- {label_text} "
            f"(subjects={relation.distinct_subject_count}, "
            f"objects={relation.distinct_object_count})"
        )
    if snapshot.relations_truncated:
        lines.append("- relation list truncated")
    return "\n".join(lines)


def deterministic_query_dl(question: str, qid: int) -> str | None:
    """Return deterministic query drafts for common shapes LLMs mistranslate."""
    intent = deterministic_query_intent(question)
    return deterministic_query_intent_dl(intent, qid)


def deterministic_query_intent_dl(intent: QueryIntent, qid: int) -> str | None:
    """Bridge supported deterministic intents to the legacy query draft shape."""
    if (
        intent.kind != QueryIntentKind.LOOKUP_OBJECT
        or intent.subject is None
        or intent.relation_candidates != KOREAN_ROLE_RELATION_CANDIDATES
    ):
        return None
    person = intent.subject.value

    person_lit = _lit(person)
    person_term = f"person({person_lit})"
    person_term_lit = _lit(f'person("{person}")')
    excluded = ", ".join(f"R != {_lit(rel)}" for rel in _GENERIC_ROLE_RELATIONS)
    role_object_rules = "\n".join(
        f"answer_q{qid}(O) :- relation({person_lit}, {_lit(rel)}, O)."
        for rel in _GENERIC_ROLE_RELATIONS
    )
    return "\n".join(
        [
            f".decl answer_q{qid}(value: symbol)",
            role_object_rules,
            f"answer_q{qid}(O) :- relation({person_term}, has_role, O).",
            f"answer_q{qid}(R) :- relation({person_lit}, R, O), {excluded}.",
            f"answer_q{qid}(R) :- relation(S, R, {person_lit}).",
            f"answer_q{qid}(R) :- relation(S, R, {person_term}).",
            f"answer_q{qid}(R) :- relation(S, R, {person_term_lit}).",
        ]
    )


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
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    snapshot = build_query_schema_snapshot(store)
    intent = deterministic_query_intent(question)
    if intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED:
        try:
            intent = client.extract_query_intent(
                question=question,
                schema_hint=query_schema_hint(snapshot),
            )
        except LLMError as exc:
            reason = _short_reason(exc)
            if llm_error_status == "translation_failed":
                return "translation_failed", None, reason
            reason = _short_reason(f"llm error: {exc}")
            return "review_required", f"review_required({_lit(reason)})", reason

    if intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED:
        reason = _short_reason(intent.reason or "unsupported query intent")
        return "review_required", f"review_required({_lit(reason)})", reason

    exact_entities = _intent_exact_entities(intent)
    if exact_entities:
        snapshot = build_query_schema_snapshot(store, exact_entities=exact_entities)
    plan = plan_query_candidates(intent, snapshot, qid=qid)
    evaluation = evaluate_query_candidate_plan(store, plan)

    if evaluation.outcome == QueryCandidateSetOutcome.VALID and evaluation.selected:
        return "translated", evaluation.selected.query_dl, ""
    if evaluation.outcome == QueryCandidateSetOutcome.NO_ANSWER:
        reason = "no confirmed facts match"
        return "no_answer", f"no_answer({_lit(reason)})", reason
    if evaluation.outcome == QueryCandidateSetOutcome.AMBIGUOUS_CONFLICTING:
        reason = "multiple query candidates returned conflicting answers"
        return "ambiguous", f"ambiguous({_lit(reason)})", reason
    if evaluation.outcome == QueryCandidateSetOutcome.EMPTY:
        reason = _short_reason(plan.reason or "no query candidates matched the schema")
    elif evaluation.outcome == QueryCandidateSetOutcome.ENGINE_POLICY_ERROR:
        reason = _short_reason("engine/policy error: " + _evaluation_reason(evaluation))
    else:
        reason = _short_reason("invalid query: " + _evaluation_reason(evaluation))
    return "review_required", f"review_required({_lit(reason)})", reason


def _intent_exact_entities(intent: QueryIntent) -> tuple[str, ...]:
    values = []
    for target in (intent.subject, intent.object):
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
) -> tuple[str, str | None, str]:
    try:
        line = client.translate_query(question=question, qid=qid)
    except LLMError as exc:
        reason = _short_reason(exc)
        if llm_error_status == "translation_failed":
            return "translation_failed", None, reason
        reason = _short_reason(f"llm error: {exc}")
        return "review_required", f"review_required({_lit(reason)})", reason

    outcome = _non_executable_outcome(line)
    if outcome is not None:
        status, reason = outcome
        return status, line, reason
    if _is_review_required(line):
        reason = _short_reason(line.removeprefix("review_required"))
        return "review_required", line, reason
    proposal = f".decl answer_q{qid}(value: symbol)\n{line}"
    return classify_query_draft(store, qid, proposal)


def _evaluation_reason(evaluation) -> str:
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
    allow_direct_datalog_fallback: bool = False,
) -> list[dict]:
    """Translate every pending question, persist drafts, rewrite `query.dl`.

    Returns one dict per processed question: {id, status, query_dl, reason}.
    """
    results: list[dict] = []
    for q in store.questions(pending_only=True):
        status, query_dl, reason = schema_aware_query_flow(
            store,
            client,
            qid=q["id"],
            question=q["text"],
            llm_error_status="translation_failed",
        )
        if allow_direct_datalog_fallback and status == "review_required":
            status, query_dl, reason = _translate_direct_datalog_fallback(
                store,
                client,
                qid=q["id"],
                question=q["text"],
                llm_error_status="translation_failed",
            )
        store.set_question_query(q["id"], query_dl, status, reason)
        results.append(
            {"id": q["id"], "status": status, "query_dl": query_dl, "reason": reason}
        )
    write_query_file(store, root)
    return results


def write_query_file(store: Store, root: Path) -> Path:
    """Write the engine query draft (translated rules only) to `<root>/facts/query.dl`."""
    lines = [
        q["query_dl"]
        for q in store.questions()
        if q["status"] == "translated" and q["query_dl"]
    ]
    path = query_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
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
            canonical = relation_canonical_variant(raw, aliases)
            if canonical == unicodedata.normalize("NFC", raw):
                alternatives.append([item])
                continue
            alternatives.append(
                [
                    item,
                    AtomExpr(
                        "relation",
                        (item.args[0], StringLit(canonical), item.args[2]),
                    ),
                ]
            )
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
