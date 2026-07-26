# SPDX-License-Identifier: MPL-2.0
"""Trace direct query/report outputs back to engine-input facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

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
    term_compare_key,
    terms_equal,
)
from verinote.engine.wirelog import answer_bucket_sort_key, answer_qid
from verinote.pipeline.corroboration import CorroborationPolicyError
from verinote.pipeline.engine_input import engine_relation_rows
from verinote.pipeline.query import load_query
from verinote.pipeline.trust import fact_trust_summary
from verinote.store import Store, review_statuses

_RELATION_DECL = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"


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
    below it shows that answer the same way. It is a rendered value, not the
    trace identity: grouping and dedupe key on the engine compare key before
    this dataclass is created, because distinct answers can render alike.

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


@dataclass(frozen=True)
class TraceBounds:
    """Fail-closed limits for direct relation provenance tracing.

    The inference engine is responsible for evaluating a query.  Tracing is a
    separate reconstruction pass, so it must not return a partial proof when a
    join would exceed a bounded amount of work.
    """

    max_relation_atoms: int = 3
    max_atom_matches: int = 512
    max_partial_bindings: int = 512
    max_proof_sets: int = 256

    def __post_init__(self) -> None:
        if any(
            limit < 1
            for limit in (
                self.max_relation_atoms,
                self.max_atom_matches,
                self.max_partial_bindings,
                self.max_proof_sets,
            )
        ):
            raise ValueError("trace bounds must be positive")


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


def trace_query_answers(
    store: Store,
    query: str,
    *,
    bounds: TraceBounds = TraceBounds(),
    engine_rows: Sequence[Mapping[str, object]] | None = None,
    fact_rows: Mapping[int, Mapping[str, object] | None] | None = None,
) -> tuple[AnswerTrace, ...]:
    """Trace direct answer_q rules in one query back to engine-input facts.

    Callers that execute a query may pass the exact engine rows and display rows
    they used for that execution.  The default keeps report tracing's existing
    read-from-store behavior, while Ask can avoid attaching post-execution
    changes to an already evaluated answer.
    """
    try:
        program = parse_and_validate_program(_RELATION_DECL + query)
    except (DatalogParseError, DatalogValidationError):
        return ()
    facts = list(engine_rows) if engine_rows is not None else engine_relation_rows(store)
    display_rows = (
        dict(fact_rows)
        if fact_rows is not None
        else {int(row["id"]): store.get_fact(int(row["id"])) for row in facts}
    )
    traces = []
    seen: set[tuple[str, str, tuple[int, ...]]] = set()
    for rule in program.rules:
        qid = answer_qid(rule.head.predicate)
        if qid is None:
            continue
        relation_atoms = _traceable_relation_atoms(rule.body, bounds)
        if relation_atoms is None:
            continue
        matches = _match_relation_atoms(
            relation_atoms, facts, rule.head.args, bounds=bounds
        )
        if len(relation_atoms) <= 2:
            matches = _aggregate_legacy_proofs(matches)
        for (identity, fact_id_key), (value, display_value, fact_ids) in sorted(
            matches.items()
        ):
            # Direct and two-relation traces have historically exposed one row
            # per answer, with every supporting fact aggregated into that row.
            # Three-relation traces retain distinct complete proof sets so that
            # facts from independent paths are never presented as one witness.
            key = (qid, identity, fact_id_key)
            if key in seen:
                continue
            seen.add(key)
            trace_facts = tuple(
                _trace_fact(store, fact_id, display_rows[fact_id])
                for fact_id in sorted(fact_ids)
                if display_rows.get(fact_id) is not None
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
    # Same key the engine orders its "Query answers" line by, so the
    # Traceability section below it lists the questions in the same order.
    return tuple(
        sorted(
            traces,
            key=lambda trace: (
                *answer_bucket_sort_key(trace.qid),
                trace.value,
                tuple(fact.id for fact in trace.facts),
            ),
        )
    )


def _aggregate_legacy_proofs(
    proofs: dict[tuple[str, tuple[int, ...]], tuple[str, str, set[int]]],
) -> dict[tuple[str, tuple[int, ...]], tuple[str, str, set[int]]]:
    """Restore the established one-row-per-answer trace contract for <=2 atoms."""
    grouped: dict[str, tuple[str, str, set[int]]] = {}
    for (identity, _fact_ids), (value, display_value, fact_ids) in proofs.items():
        existing = grouped.get(identity)
        if existing is None:
            grouped[identity] = (value, display_value, set(fact_ids))
            continue
        existing[2].update(fact_ids)
    return {
        (identity, tuple(sorted(fact_ids))): (value, display_value, fact_ids)
        for identity, (value, display_value, fact_ids) in grouped.items()
    }


def _traceable_relation_atoms(
    body: tuple[AtomExpr | Comparison, ...],
    bounds: TraceBounds,
) -> tuple[AtomExpr, ...] | None:
    atoms = [item for item in body if isinstance(item, AtomExpr)]
    comparisons = [item for item in body if isinstance(item, Comparison)]
    if not 1 <= len(atoms) <= bounds.max_relation_atoms or comparisons:
        return None
    if any(
        atom.predicate != "relation"
        or len(atom.args) != 3
        or any(_has_vars(term) and not isinstance(term, Var) for term in atom.args)
        for atom in atoms
    ):
        return None
    if not _atoms_are_connected(atoms):
        return None
    return tuple(atoms)


def _match_relation_atoms(
    atoms: tuple[AtomExpr, ...],
    facts: list[dict[str, object]],
    head_args: tuple[Term, ...],
    *,
    bounds: TraceBounds,
) -> dict[tuple[str, tuple[int, ...]], tuple[str, str, set[int]]]:
    """Group matching facts by answer value.

    Keyed on `term_compare_key`, not on either display rendering. Two terms the
    engine calls one answer (`Atom("x")` and `StringLit("x")`) must land in one
    group rather than two rows naming the same value, while structurally distinct
    answers that render alike (`f(x)` as a compound versus a string) must keep
    separate provenance rows. The renderings ride along as payload, not identity.
    """
    if len(head_args) != 1:
        return {}
    atom_matches = []
    for atom in atoms:
        matches = _atom_matches(atom, facts, max_matches=bounds.max_atom_matches)
        if matches is None:
            return {}
        atom_matches.append(matches)

    states = _join_relation_atom_matches(atoms, atom_matches, bounds)
    if states is None:
        return {}

    proofs: dict[tuple[str, tuple[int, ...]], tuple[str, str, set[int]]] = {}
    for bindings, fact_ids in states:
        value = _head_value(head_args[0], bindings)
        if value is None:
            continue
        identity = term_compare_key(value)
        proof_key = (identity, tuple(sorted(fact_ids)))
        proofs.setdefault(
            proof_key,
            (render_answer_value(value), render_display_value(value), set(fact_ids)),
        )
        if len(proofs) > bounds.max_proof_sets:
            return {}
    return proofs


def _join_relation_atom_matches(
    atoms: tuple[AtomExpr, ...],
    atom_matches: list[list[tuple[dict[str, Term], int]]],
    bounds: TraceBounds,
) -> list[tuple[dict[str, Term], set[int]]] | None:
    """Return complete, exact proof bindings in a deterministic join order."""
    remaining = set(range(len(atoms)))
    first = min(remaining, key=lambda index: (len(atom_matches[index]), index))
    ordered = [first]
    remaining.remove(first)
    bound_vars = set(_atom_variables(atoms[first]))
    while remaining:
        candidates = [
            index
            for index in remaining
            if bound_vars & _atom_variables(atoms[index])
        ]
        if not candidates:
            return None
        next_index = min(
            candidates,
            key=lambda index: (
                -len(bound_vars & _atom_variables(atoms[index])),
                len(atom_matches[index]),
                index,
            ),
        )
        ordered.append(next_index)
        remaining.remove(next_index)
        bound_vars.update(_atom_variables(atoms[next_index]))

    states: list[tuple[dict[str, Term], set[int]]] = [({}, set())]
    for atom_index in ordered:
        next_states: dict[
            tuple[tuple[tuple[str, str], ...], tuple[int, ...]],
            tuple[dict[str, Term], set[int]],
        ] = {}
        for current_bindings, current_ids in states:
            for bindings, fact_id in atom_matches[atom_index]:
                merged = _merge_bindings(current_bindings, bindings)
                if merged is None:
                    continue
                fact_ids = current_ids | {fact_id}
                key = (_binding_key(merged), tuple(sorted(fact_ids)))
                next_states.setdefault(key, (merged, fact_ids))
                if len(next_states) > bounds.max_partial_bindings:
                    return None
        states = list(next_states.values())
    return states


def _merge_bindings(
    left: dict[str, Term], right: dict[str, Term]
) -> dict[str, Term] | None:
    merged = dict(left)
    for name, value in right.items():
        existing = merged.get(name)
        if existing is not None and not terms_equal(existing, value):
            return None
        merged.setdefault(name, value)
    return merged


def _binding_key(bindings: dict[str, Term]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((name, term_compare_key(value)) for name, value in bindings.items()))


def _atom_matches(
    atom: AtomExpr,
    facts: list[dict[str, object]],
    *,
    max_matches: int,
) -> list[tuple[dict[str, Term], int]] | None:
    matches = []
    for fact in facts:
        bindings: dict[str, Term] = {}
        if not _match_term(atom.args[0], fact["subject"], bindings):
            continue
        if not _match_term(atom.args[1], fact["relation"], bindings):
            continue
        if not _match_term(atom.args[2], fact["object"], bindings):
            continue
        matches.append((bindings, int(fact["id"])))
        if len(matches) > max_matches:
            return None
    return matches


def _atom_variables(atom: AtomExpr) -> set[str]:
    return {term.name for term in atom.args if isinstance(term, Var)}


def _atoms_are_connected(atoms: list[AtomExpr]) -> bool:
    if len(atoms) == 1:
        return True
    remaining = set(range(1, len(atoms)))
    reachable = set(_atom_variables(atoms[0]))
    while remaining:
        newly_reachable = [
            index
            for index in remaining
            if reachable & _atom_variables(atoms[index])
        ]
        if not newly_reachable:
            return False
        for index in newly_reachable:
            reachable.update(_atom_variables(atoms[index]))
            remaining.remove(index)
    return True


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
