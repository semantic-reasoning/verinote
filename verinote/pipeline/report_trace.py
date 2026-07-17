# SPDX-License-Identifier: MPL-2.0
"""Trace direct query/report outputs back to engine-input facts."""

from __future__ import annotations

from dataclasses import dataclass
import re

from verinote.engine.datalog import (
    AtomExpr,
    Comparison,
    DatalogParseError,
    DatalogValidationError,
    parse_and_validate_program,
)
from verinote.engine.terms import (
    Compound,
    Term,
    Var,
    render_answer_value,
    render_display_value,
    terms_equal,
)
from verinote.pipeline.corroboration import CorroborationPolicyError
from verinote.pipeline.query import load_query
from verinote.pipeline.trust import fact_trust_summary
from verinote.store import Store, review_statuses

_RELATION_DECL = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
_ANSWER_RE = re.compile(r"answer_q(?P<qid>[0-9]+)\Z")


@dataclass(frozen=True)
class TraceFact:
    id: int
    subject: str
    relation: str
    object: str
    source: str
    evidence: str
    conflicted: bool


@dataclass(frozen=True)
class AnswerTrace:
    """One traced answer, in the two forms its two readers need.

    `value` is the answer as /report writes it -- the same rendering the engine
    backend uses for the "Query answers" line, so the "Traceability" section
    below it shows that answer the same way and the two can be matched to each
    other (issue #167). It is the answer's identity here: grouping and dedupe
    key on it.

    `display_value` is the same answer standing on its own, without the escape
    that only the report's `, ` join needs. Ask puts one answer in one table
    cell with no join, so it reads this one; showing `value` there printed a
    backslash the `object` cell beside it did not have.
    """

    qid: str
    value: str
    display_value: str
    facts: tuple[TraceFact, ...]
    conflicted: bool


@dataclass(frozen=True)
class ReportTrace:
    answers: tuple[AnswerTrace, ...]
    excluded_review_count: int
    # (status, count) for the review statuses actually present, so the report can
    # name what was held back without spelling the vocabulary out a second time.
    excluded_by_status: tuple[tuple[str, int], ...]


def _excluded_by_status(store: Store) -> tuple[tuple[str, int], ...]:
    counts = store.status_counts()
    # Ask the tier accessor rather than binding the frozenset: widening the tier at
    # its definition site then moves this count (which is what lets the mutation
    # test prove a derivation rather than a coincidence between two hardcodings),
    # and an empty tier raises here as it does for every other consumer instead of
    # rendering a report that quietly claims nothing was held back.
    return tuple(
        (status, count)
        for status in sorted(review_statuses())
        if (count := counts.get(status, 0))
    )


def report_trace(store: Store) -> ReportTrace:
    by_status = _excluded_by_status(store)
    excluded = sum(count for _, count in by_status)
    try:
        query = load_query(store)
    except CorroborationPolicyError:
        query = None
    if not query:
        return ReportTrace(
            answers=(),
            excluded_review_count=excluded,
            excluded_by_status=by_status,
        )

    return ReportTrace(
        answers=trace_query_answers(store, query),
        excluded_review_count=excluded,
        excluded_by_status=by_status,
    )


def trace_query_answers(store: Store, query: str) -> tuple[AnswerTrace, ...]:
    """Trace direct answer_q rules in one query back to engine-input facts."""
    try:
        program = parse_and_validate_program(_RELATION_DECL + query)
    except (DatalogParseError, DatalogValidationError):
        return ()
    facts = store.engine_fact_terms()
    fact_rows = {int(row["id"]): store.get_fact(int(row["id"])) for row in facts}
    traces = []
    seen: set[tuple[str, str, tuple[int, ...]]] = set()
    for rule in program.rules:
        qid = _answer_qid(rule.head.predicate)
        if qid is None:
            continue
        relation_atom = _direct_relation_atom(rule.body)
        if relation_atom is None:
            continue
        matches = _match_relation_atom(relation_atom, facts, rule.head.args)
        for value, (display_value, fact_ids) in sorted(matches.items()):
            key = (qid, value, tuple(sorted(fact_ids)))
            if key in seen:
                continue
            seen.add(key)
            trace_facts = tuple(
                _trace_fact(store, fact_id, fact_rows[fact_id])
                for fact_id in sorted(fact_ids)
                if fact_rows[fact_id] is not None
            )
            traces.append(
                AnswerTrace(
                    qid=qid,
                    value=value,
                    display_value=display_value,
                    facts=trace_facts,
                    conflicted=any(fact.conflicted for fact in trace_facts),
                )
            )
    return tuple(sorted(traces, key=lambda trace: (int(trace.qid), trace.value)))


def _answer_qid(predicate: str) -> str | None:
    match = _ANSWER_RE.fullmatch(predicate)
    return match.group("qid") if match else None


def _direct_relation_atom(body: tuple[AtomExpr | Comparison, ...]) -> AtomExpr | None:
    atoms = [item for item in body if isinstance(item, AtomExpr)]
    comparisons = [item for item in body if isinstance(item, Comparison)]
    if len(atoms) != 1 or comparisons:
        return None
    atom = atoms[0]
    if atom.predicate != "relation" or len(atom.args) != 3:
        return None
    if any(_has_vars(term) and not isinstance(term, Var) for term in atom.args):
        return None
    return atom


def _match_relation_atom(
    atom: AtomExpr,
    facts: list[dict[str, object]],
    head_args: tuple[Term, ...],
) -> dict[str, tuple[str, set[int]]]:
    """Group matching facts by answer value.

    Keyed on `render_answer_value` -- the report's rendering -- because that is
    the answer's identity on /report, and two terms the engine calls one answer
    (`Atom("x")` and `StringLit("x")`) must land in one group rather than two
    rows naming the same value. The display form rides along per group; it is a
    function of the same term, so every member of a group agrees on it.
    """
    if len(head_args) != 1:
        return {}
    matches: dict[str, tuple[str, set[int]]] = {}
    for fact in facts:
        bindings: dict[str, Term] = {}
        if not _match_term(atom.args[0], fact["subject"], bindings):
            continue
        if not _match_term(atom.args[1], fact["relation"], bindings):
            continue
        if not _match_term(atom.args[2], fact["object"], bindings):
            continue
        value = _head_value(head_args[0], bindings)
        if value is None:
            continue
        group = matches.setdefault(
            render_answer_value(value), (render_display_value(value), set())
        )
        group[1].add(int(fact["id"]))
    return matches


def _match_term(pattern: Term, value: object, bindings: dict[str, Term]) -> bool:
    """Match one query term against one fact term, the way the engine matches.

    Equality is `terms_equal` (the engine's compare-key), not dataclass `==`.
    The DuckDB backend compiles a body constant to `__cmp_<col> = ?` bound with
    `term_compare_key`, and a repeated variable to `__cmp_a = __cmp_b`, so
    `Atom("x")` and `StringLit("x")` are one value to it -- and to the legacy
    wirelog path too, which renders both to `"x"` before pyrewire sees them.
    Dataclass `==` splits that pair, so the trace found no fact behind an answer
    the engine had just derived, and /report showed provenance-less answers
    (issue #167).
    """
    if not isinstance(value, Term):
        return False
    if isinstance(pattern, Var):
        bound = bindings.get(pattern.name)
        if bound is None:
            bindings[pattern.name] = value
            return True
        return terms_equal(bound, value)
    return terms_equal(pattern, value)


def _head_value(term: Term, bindings: dict[str, Term]) -> Term | None:
    if isinstance(term, Var):
        return bindings.get(term.name)
    if _has_vars(term):
        return None
    return term


def _trace_fact(
    store: Store,
    fact_id: int,
    row,
) -> TraceFact:
    summary = fact_trust_summary(store, fact_id)
    evidence = ""
    if summary is not None and summary.evidence:
        evidence = summary.evidence[0].snippet or summary.evidence[0].source_path or ""
    conflicted = summary is not None and summary.conflict is not None
    return TraceFact(
        id=fact_id,
        subject=str(row["subject"]),
        relation=str(row["relation"]),
        object=str(row["object"]),
        source=str(row["source_path"] or ""),
        evidence=evidence,
        conflicted=conflicted,
    )


def _has_vars(term: Term) -> bool:
    if isinstance(term, Var):
        return True
    if isinstance(term, Compound):
        return any(_has_vars(arg) for arg in term.args)
    return False
