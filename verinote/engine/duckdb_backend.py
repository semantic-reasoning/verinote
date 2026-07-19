# SPDX-License-Identifier: MPL-2.0
"""Experimental DuckDB inference backend for the supported Datalog subset."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Iterable, Mapping

from verinote.engine.datalog import (
    AtomExpr,
    Comparison,
    DatalogParseError,
    DatalogValidationError,
    Declaration,
    Fact,
    Program,
    Rule,
    parse_and_validate_program,
)
from verinote.engine.duckdb_terms import (
    duckdb_value_to_term,
    term_compare_key,
    term_to_duckdb_value,
)
from verinote.engine.terms import (
    Atom,
    Compound,
    NumberLit,
    StringLit,
    Term,
    Var,
    bare_label,
    escape_string_value,
    render_answer_value,
    render_term,
)
from verinote.engine.wirelog import (
    CheckReport,
    DEFAULT_POLICY,
    NO_FINDINGS_TEXT,
    FindingRow,
)

_ERROR_PREFIX = "error_"
_WARN_PREFIX = "warn_"
_ANSWER_PREFIX = "answer_q"
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ZERO_ARITY_COLUMN = "__present"
_RELATION_COLUMNS = ("subject", "rel", "object")


class DuckDBBackendError(ValueError):
    """Raised when the DuckDB backend cannot compile or execute a program."""


@dataclass(frozen=True)
class _RuleSql:
    sql: str
    params: tuple[str, ...]


@dataclass(frozen=True)
class _Binding:
    value_sql: str
    compare_sql: str


def run_check_duckdb(
    facts: Iterable[Mapping[str, object]],
    *,
    policy_dl: str | None = None,
    query_dl: str | None = None,
) -> CheckReport:
    """Run supported non-recursive Datalog rules through an in-memory DuckDB DB."""
    cache = DuckDBInferenceCache()
    try:
        return cache.run_check(facts, policy_dl=policy_dl, query_dl=query_dl)
    finally:
        cache.close()


class DuckDBInferenceCache:
    """Reusable DuckDB inference session with cached engine-input facts.

    SQLite remains the source-of-truth. This cache only keeps the derived
    `relation(subject, rel, object)` table in an in-memory DuckDB connection and
    refreshes it when the engine-input facts change. Policy/query derived tables
    are recreated on every run so rule output cannot leak across checks.
    """

    def __init__(self) -> None:
        self._con = None
        self._relation_fingerprint: tuple[tuple[object, ...], ...] | None = None
        self._decl_tables: set[str] = set()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None
            self._relation_fingerprint = None
            self._decl_tables.clear()

    def run_check(
        self,
        facts: Iterable[Mapping[str, object]],
        *,
        policy_dl: str | None = None,
        query_dl: str | None = None,
    ) -> CheckReport:
        """Run a check while reusing the cached DuckDB base relation."""
        try:
            import duckdb
        except ImportError:
            return _engine_error("DuckDB is not installed", engine_available=False)

        policy = policy_dl if policy_dl is not None else DEFAULT_POLICY
        source = policy + ("\n" + query_dl if query_dl else "")
        try:
            program = parse_and_validate_program(source)
            _validate_relation_decl(program)
            ordered_rules = _topological_rules(program)
        except (DatalogParseError, DatalogValidationError, DuckDBBackendError) as exc:
            return _engine_error(str(exc))

        try:
            fact_rows = list(facts)
            with self._lock:
                if self._con is None:
                    self._con = duckdb.connect()
                    self._con.execute(_create_decl_table_sql(_relation_decl(program)))
                con = self._con
                self._reset_derived_tables()
                fingerprint = _relation_fingerprint(fact_rows)
                if fingerprint != self._relation_fingerprint:
                    # Invalidate before mutating: if the reload fails partway,
                    # the stale fingerprint must not let a later run with the
                    # same facts skip reloading a now-empty/corrupt relation.
                    self._relation_fingerprint = None
                    con.execute('DELETE FROM "relation"')
                    _load_relation_facts(con, fact_rows)
                    self._relation_fingerprint = fingerprint

                declarations = {decl.name: decl for decl in program.declarations}
                for decl in program.declarations:
                    if decl.name == "relation":
                        continue
                    con.execute(_create_decl_table_sql(decl))
                    self._decl_tables.add(decl.name)
                _load_extensional_facts(con, program.facts, declarations)
                for rule in ordered_rules:
                    compiled = _compile_rule(rule, declarations)
                    con.execute(compiled.sql, list(compiled.params))
                return _collect_report(
                    con,
                    declarations,
                    fact_rows,
                    policy_dl=policy,
                    query_dl=query_dl,
                )
        except DuckDBBackendError as exc:
            return _engine_error(str(exc))
        except Exception as exc:
            return _internal_engine_error(exc)

    def _reset_derived_tables(self) -> None:
        assert self._con is not None
        for name in sorted(self._decl_tables):
            self._con.execute(f"DROP TABLE IF EXISTS {_quote_ident(name)}")
        self._decl_tables.clear()


def _relation_fingerprint(facts: list[Mapping[str, object]]) -> tuple[tuple[object, ...], ...]:
    """Stable fingerprint for the facts materialized into DuckDB."""
    rows: list[tuple[object, ...]] = []
    for row in facts:
        rows.append(
            tuple(
                term_to_duckdb_value(_coerce_fact_term(row[key]))
                for key in ("subject", "relation", "object")
            )
        )
    return tuple(sorted(set(rows)))


def _engine_error(message: str, *, engine_available: bool = True) -> CheckReport:
    return CheckReport(
        ok=False,
        errors=1,
        warnings=0,
        text=f"backend: DuckDB\n\npolicy/engine error: {message}",
        findings=[f"ERROR engine error: {message}"],
        engine_available=engine_available,
    )


def _internal_engine_error(exc: Exception) -> CheckReport:
    """Fail-closed report for a backend failure that is not a policy fault.

    A `DuckDBBackendError` means the policy/query could not be compiled and is
    surfaced via `_engine_error`. Any other exception is an internal engine
    failure, so its message must not read as if the user's policy were at fault.
    """
    message = f"internal engine error: {exc}"
    return CheckReport(
        ok=False,
        errors=1,
        warnings=0,
        text=f"backend: DuckDB\n\n{message}",
        findings=[f"ERROR {message}"],
        engine_available=True,
    )


def _validate_relation_decl(program: Program) -> None:
    _relation_decl(program)


def _relation_decl(program: Program) -> Declaration:
    relation = next((decl for decl in program.declarations if decl.name == "relation"), None)
    if relation is None:
        raise DuckDBBackendError("program must declare relation/3")
    columns = tuple(column.name for column in relation.columns)
    if columns != ("subject", "rel", "object"):
        raise DuckDBBackendError("relation declaration must be relation(subject, rel, object)")
    return relation


def _load_relation_facts(con, facts: Iterable[Mapping[str, object]]) -> None:
    rows = {
        tuple(
            value
            for term in (
                _coerce_fact_term(row["subject"]),
                _coerce_fact_term(row["relation"]),
                _coerce_fact_term(row["object"]),
            )
            for value in _stored_term_values(term)
        )
        for row in facts
    }
    if rows:
        con.executemany(
            f'INSERT INTO "relation" ({_insert_columns(_RELATION_COLUMNS)}) '
            "VALUES (?, ?, ?, ?, ?, ?)",
            sorted(rows),
        )


def _load_extensional_facts(
    con, facts: tuple[Fact, ...], declarations: dict[str, Declaration]
) -> None:
    for fact in facts:
        if fact.atom.predicate == "relation":
            raise DuckDBBackendError("relation facts must come from SQLite engine input")
        decl = declarations[fact.atom.predicate]
        params = _insert_values(fact.atom.args)
        placeholders = ", ".join("?" for _ in params)
        con.execute(
            f"INSERT INTO {_quote_ident(fact.atom.predicate)} "
            f"({_insert_columns(decl)}) VALUES ({placeholders})",
            params,
        )


def _coerce_fact_term(value: object) -> Term:
    if isinstance(value, (Atom, Compound, NumberLit, StringLit, Var)):
        return value
    return StringLit(str(value))


def _topological_rules(program: Program) -> list[Rule]:
    derived = {rule.head.predicate for rule in program.rules}
    deps: dict[str, set[str]] = {name: set() for name in derived}
    rules_by_head: dict[str, list[Rule]] = {name: [] for name in derived}
    for rule in program.rules:
        rules_by_head[rule.head.predicate].append(rule)
        for item in rule.body:
            if isinstance(item, AtomExpr) and item.predicate in derived:
                deps[rule.head.predicate].add(item.predicate)

    _reject_cycles(deps)

    ordered_heads: list[str] = []
    remaining = {name: set(values) for name, values in deps.items()}
    while remaining:
        ready = sorted(name for name, values in remaining.items() if not values)
        if not ready:
            raise DuckDBBackendError("recursive rules are not supported")
        for name in ready:
            ordered_heads.append(name)
            del remaining[name]
        for values in remaining.values():
            values.difference_update(ready)

    return [rule for head in ordered_heads for rule in rules_by_head[head]]


def _reject_cycles(deps: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in visiting:
            cycle = " -> ".join(path + (name,))
            raise DuckDBBackendError(f"recursive rules are not supported: {cycle}")
        if name in visited:
            return
        visiting.add(name)
        for dep in deps.get(name, set()):
            visit(dep, path + (name,))
        visiting.remove(name)
        visited.add(name)

    for name in sorted(deps):
        visit(name, ())


def _compile_rule(rule: Rule, declarations: dict[str, Declaration]) -> _RuleSql:
    aliases: list[tuple[AtomExpr, str, Declaration]] = []
    where_sql: list[str] = []
    where_params: list[str] = []
    bindings: dict[str, _Binding] = {}

    for atom_index, item in enumerate(item for item in rule.body if isinstance(item, AtomExpr)):
        alias = f"a{atom_index}"
        decl = declarations[item.predicate]
        aliases.append((item, alias, decl))
        for column, term in zip(decl.columns, item.args, strict=True):
            expr = f"{alias}.{_quote_ident(column.name)}"
            compare_expr = f"{alias}.{_quote_ident(_compare_column(column.name))}"
            if isinstance(term, Var):
                prior = bindings.get(term.name)
                if prior is None:
                    bindings[term.name] = _Binding(expr, compare_expr)
                else:
                    where_sql.append(f"{compare_expr} = {prior.compare_sql}")
            elif _has_vars(term):
                raise DuckDBBackendError(
                    f"variable-bearing compound terms are not supported in body atom {item.predicate}"
                )
            else:
                where_sql.append(f"{compare_expr} = ?")
                where_params.append(term_compare_key(term))

    for item in rule.body:
        if isinstance(item, Comparison):
            sql, params = _compile_comparison(item, bindings)
            where_sql.append(sql)
            where_params.extend(params)

    select_sql: list[str] = []
    select_params: list[str] = []
    for term in rule.head.args:
        if isinstance(term, Var):
            try:
                binding = bindings[term.name]
            except KeyError as exc:
                raise DuckDBBackendError(f"unbound head variable: {term.name}") from exc
            select_sql.extend([binding.value_sql, binding.compare_sql])
        elif _has_vars(term):
            raise DuckDBBackendError(
                "variable-bearing compound terms are not supported in rule heads"
            )
        else:
            select_sql.extend(["?", "?"])
            select_params.extend(_stored_term_values(term))
    if not select_sql:
        select_sql.append("TRUE")

    from_sql = ", ".join(
        f"{_quote_ident(atom.predicate)} AS {alias}" for atom, alias, _decl in aliases
    )
    where_clause = " WHERE " + " AND ".join(where_sql) if where_sql else ""
    if from_sql:
        query = f"SELECT DISTINCT {', '.join(select_sql)} FROM {from_sql}{where_clause}"
    else:
        query = f"SELECT DISTINCT {', '.join(select_sql)}{where_clause}"
    head_decl = declarations[rule.head.predicate]
    head_columns = _insert_columns(head_decl)
    sql = f"INSERT INTO {_quote_ident(rule.head.predicate)} ({head_columns}) {query}"
    return _RuleSql(sql, tuple(select_params + where_params))


def _compile_comparison(
    comparison: Comparison, bindings: dict[str, _Binding]
) -> tuple[str, list[str]]:
    left_sql, left_params = _term_sql(comparison.left, bindings)
    right_sql, right_params = _term_sql(comparison.right, bindings)
    op = "=" if comparison.op == "==" else "!="
    return f"{left_sql} {op} {right_sql}", left_params + right_params


def _term_sql(term: Term, bindings: dict[str, _Binding]) -> tuple[str, list[str]]:
    if isinstance(term, Var):
        try:
            return bindings[term.name].compare_sql, []
        except KeyError as exc:
            raise DuckDBBackendError(f"unbound comparison variable: {term.name}") from exc
    if _has_vars(term):
        raise DuckDBBackendError("variable-bearing compound comparisons are not supported")
    return "?", [term_compare_key(term)]


def _collect_report(
    con,
    declarations: dict[str, Declaration],
    facts: list[Mapping[str, object]],
    *,
    policy_dl: str,
    query_dl: str | None,
) -> CheckReport:
    errors: list[_Derived] = []
    warnings: list[_Derived] = []
    answers_by_q: dict[str, list[str]] = {}
    for name in sorted(declarations):
        if not (
            name.startswith(_ERROR_PREFIX)
            or name.startswith(_WARN_PREFIX)
            or name.startswith(_ANSWER_PREFIX)
        ):
            continue
        rows = con.execute(
            f"SELECT DISTINCT {_select_columns(declarations[name])} "
            f"FROM {_quote_ident(name)}"
        ).fetchall()
        if not declarations[name].columns:
            rows = [() for _row in rows]
        rows = _dedupe_rows_by_compare_key(rows)
        rendered_rows = [_render_finding_row(row) for row in rows]
        columns = tuple(column.name for column in declarations[name].columns)
        if name.startswith(_ERROR_PREFIX):
            for row, rendered in zip(rows, rendered_rows):
                _record_finding(
                    errors,
                    f"{name[len(_ERROR_PREFIX) :]}: {rendered}",
                    row,
                    name,
                    columns,
                )
        elif name.startswith(_WARN_PREFIX):
            for row, rendered in zip(rows, rendered_rows):
                _record_finding(
                    warnings,
                    f"{name[len(_WARN_PREFIX) :]}: {rendered}",
                    row,
                    name,
                    columns,
                )
        elif name.startswith(_ANSWER_PREFIX):
            if rows:
                qid = name[len(_ANSWER_PREFIX) :]
                answers_by_q.setdefault(qid, []).extend(
                    _render_answer_row(row) for row in rows
                )

    answers = [
        f"q{qid}: {', '.join(sorted(vals))}"
        for qid, vals in sorted(answers_by_q.items())
    ]
    rendered_errors = sorted(f"ERROR {derived.text}" for derived in errors)
    rendered_warnings = sorted(f"WARN {derived.text}" for derived in warnings)
    findings = rendered_errors + rendered_warnings
    finding_rows = [
        derived.finding_row(f"ERROR {derived.text}")
        for derived in sorted(errors)
    ] + [
        derived.finding_row(f"WARN {derived.text}")
        for derived in sorted(warnings)
    ]
    summary = f"errors: {len(errors)}  warnings: {len(warnings)}  facts: {len(facts)}"
    body = "\n".join(findings) if findings else NO_FINDINGS_TEXT
    if answers:
        body += "\n\n--- answers ---\n" + "\n".join(answers)
    debug = (
        "\n\n--- policy input ---\n"
        + policy_dl
        + ("\n--- query input ---\n" + query_dl if query_dl else "")
        + "\n--- fact input ---\n"
        + _render_fact_input(facts)
    )
    return CheckReport(
        ok=not errors,
        errors=len(errors),
        warnings=len(warnings),
        answers=answers,
        text=f"backend: DuckDB\n{summary}\n\n{body}{debug}",
        findings=findings,
        finding_rows=finding_rows,
    )


@dataclass(frozen=True, order=True)
class _Derived:
    """One derived tuple with the identity of the rule that derived it."""

    text: str
    values: tuple[str, ...]
    rule: str
    columns: tuple[str, ...]
    identity: tuple[str, ...]

    def finding_row(self, text: str) -> FindingRow:
        return FindingRow(text, self.values, self.rule, self.columns, self.identity)


def _record_finding(
    bucket: list[_Derived],
    text: str,
    row: tuple[object, ...],
    rule: str,
    columns: tuple[str, ...],
) -> None:
    """Record one finding row after engine-equality dedupe.

    Rendering is lossy: two engine-distinct rows can produce the same line. The
    report count and findings list still have to preserve both rows; consumers
    that need a single row for a rendered line can treat duplicate text as
    ambiguous when they build their own text-to-row map.
    """
    bucket.append(_Derived(text, _row_values(row), rule, columns, _row_identity(row)))


def _row_values(row: tuple[object, ...]) -> tuple[str, ...]:
    """The row's values as labels, unescaped, for exact comparison."""
    return tuple(bare_label(duckdb_value_to_term(value)) for value in row)


def _row_identity(row: tuple[object, ...]) -> tuple[str, ...]:
    """The row's values as engine comparison keys, before lossy rendering."""
    return tuple(term_compare_key(duckdb_value_to_term(value)) for value in row)


def _render_fact_input(facts: list[Mapping[str, object]]) -> str:
    if not facts:
        return "(none)"
    lines = []
    for row in facts:
        lines.append(
            "relation("
            + ", ".join(
                render_term(_coerce_fact_term(row[key]))
                for key in ("subject", "relation", "object")
            )
            + ")"
        )
    return "\n".join(sorted(set(lines)))


def _dedupe_rows_by_compare_key(
    rows: list[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    """Drop rows that are the same tuple under the engine's equality.

    ``SELECT DISTINCT`` only removes rows that are identical in their *typed
    storage* (the JSON term encoding), so ``Atom("ada")`` and ``StringLit("ada")``
    survive as two rows even though the engine treats them as equal
    (`term_compare_key` maps both to ``s:ada``). Deduping on the compare-key
    tuple collapses those representation twins into one finding/answer, while
    still keeping genuinely different tuples such as ``("A B", "C")`` and
    ``("A", "B C")`` apart -- which is the whole point of issue #167. Order is
    preserved so the later sort is stable and deterministic.
    """
    seen: set[tuple[str, ...]] = set()
    deduped: list[tuple[object, ...]] = []
    for row in rows:
        key = tuple(term_compare_key(duckdb_value_to_term(value)) for value in row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _join_row_columns(values: list[str]) -> str:
    """Join already-rendered column values so no column can forge another.

    Columns are joined by a bare space. A single-column row is emitted verbatim
    (its own spaces are kept), because with no neighbouring column a space cannot
    blur into a boundary. But once a row has two or more columns, a bare space
    inside a value would be indistinguishable from the column separator --
    ``("A B", "C")`` and ``("A", "B C")`` would both read ``A B C`` and collapse
    into one row (issue #167). So for multi-column rows we escape the spaces
    *inside* each rendered value as ``\\ `` and keep the column separator bare.
    Backslash is already escaped by :func:`escape_string_value`, so ``\\ ``
    round-trips back to an in-value space with no ambiguity.

    Both findings and answers share this rule so a multi-column answer is guarded
    exactly like a multi-column finding.
    """
    if len(values) <= 1:
        return " ".join(values)
    return " ".join(value.replace(" ", "\\ ") for value in values)


def _render_finding_row(row: tuple[object, ...]) -> str:
    """Render one finding tuple so its columns cannot be forged into each other."""
    return _join_row_columns(
        [_render_output_term(duckdb_value_to_term(value)) for value in row]
    )


def _render_answer_row(row: tuple[object, ...]) -> str:
    """Render one answer tuple, guarding column boundaries like a finding row."""
    return _join_row_columns(
        [render_answer_value(duckdb_value_to_term(value)) for value in row]
    )


def _render_output_term(term: Term) -> str:
    """Render a value for a report line.

    String values keep their bare (unquoted) surface, but control characters are
    escaped: an unescaped newline in a fact value would otherwise let that value
    forge extra `ERROR `/`WARN ` lines in the report body.
    """
    if isinstance(term, StringLit):
        return escape_string_value(term.value)
    return render_term(term)


def _has_vars(term: Term) -> bool:
    if isinstance(term, Var):
        return True
    if isinstance(term, Compound):
        return any(_has_vars(arg) for arg in term.args)
    return False


def _quote_ident(identifier: str) -> str:
    if not _IDENT_RE.fullmatch(identifier):
        raise DuckDBBackendError(f"invalid SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _create_decl_table_sql(decl: Declaration) -> str:
    if decl.columns:
        columns = ", ".join(
            f"{_quote_ident(name)} VARCHAR NOT NULL"
            for name in _expanded_columns(tuple(column.name for column in decl.columns))
        )
        return f"CREATE TABLE {_quote_ident(decl.name)} ({columns})"
    return (
        f"CREATE TABLE {_quote_ident(decl.name)} "
        f"({_quote_ident(_ZERO_ARITY_COLUMN)} BOOLEAN NOT NULL)"
    )


def _insert_columns(decl_or_columns: Declaration | tuple[str, ...]) -> str:
    columns = (
        tuple(column.name for column in decl_or_columns.columns)
        if isinstance(decl_or_columns, Declaration)
        else decl_or_columns
    )
    if columns:
        return ", ".join(_quote_ident(name) for name in _expanded_columns(columns))
    return _quote_ident(_ZERO_ARITY_COLUMN)


def _select_columns(decl: Declaration) -> str:
    if decl.columns:
        return ", ".join(_quote_ident(column.name) for column in decl.columns)
    return _quote_ident(_ZERO_ARITY_COLUMN)


def _insert_values(values: tuple[Term, ...]) -> list[str] | list[bool]:
    if values:
        return [value for term in values for value in _stored_term_values(term)]
    return [True]


def _stored_term_values(term: Term) -> tuple[str, str]:
    return term_to_duckdb_value(term), term_compare_key(term)


def _compare_column(name: str) -> str:
    return f"__cmp_{name}"


def _expanded_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    expanded = tuple(name for column in columns for name in (column, _compare_column(column)))
    normalized = tuple(name.casefold() for name in expanded)
    if len(set(normalized)) != len(normalized):
        raise DuckDBBackendError(
            "declaration column names collide with reserved comparison columns"
        )
    return expanded
