# SPDX-License-Identifier: MPL-2.0
"""Parser and validator for verinote's supported wirelog/Datalog subset.

This module defines the source-language boundary future DuckDB-backed inference
will compile. It is intentionally independent from pyrewire, DuckDB, SQLite, and
the verification runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from verinote.engine.terms import Compound, Term, TermParseError, Var, parse_term

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class DatalogParseError(ValueError):
    """Raised when Datalog source is outside the supported concrete syntax."""


class DatalogValidationError(ValueError):
    """Raised when parsed Datalog source fails semantic validation."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.name):
            raise ValueError(f"invalid column name: {self.name!r}")
        if not _IDENT_RE.fullmatch(self.type):
            raise ValueError(f"invalid column type: {self.type!r}")


@dataclass(frozen=True)
class Declaration:
    name: str
    columns: tuple[Column, ...]

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.name):
            raise ValueError(f"invalid predicate name: {self.name!r}")


@dataclass(frozen=True)
class AtomExpr:
    predicate: str
    args: tuple[Term, ...]

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.predicate):
            raise ValueError(f"invalid predicate name: {self.predicate!r}")


@dataclass(frozen=True)
class Comparison:
    left: Term
    op: Literal["==", "!="]
    right: Term


BodyItem: TypeAlias = AtomExpr | Comparison


@dataclass(frozen=True)
class Fact:
    atom: AtomExpr


@dataclass(frozen=True)
class Rule:
    head: AtomExpr
    body: tuple[BodyItem, ...]


@dataclass(frozen=True)
class Program:
    declarations: tuple[Declaration, ...]
    facts: tuple[Fact, ...]
    rules: tuple[Rule, ...]


def parse_program(source: str) -> Program:
    """Parse supported Datalog source into a structural AST."""
    return _Parser(_strip_comments(source)).parse_program()


def validate_program(program: Program) -> None:
    """Validate declarations, arity, vocabulary, and rule variable safety."""
    arities: dict[str, int] = {}
    for decl in program.declarations:
        if decl.name in arities:
            raise DatalogValidationError(f"duplicate declaration: {decl.name}")
        for column in decl.columns:
            if column.type != "symbol":
                raise DatalogValidationError(
                    f"unsupported type for {decl.name}.{column.name}: {column.type}"
                )
        arities[decl.name] = len(decl.columns)

    for fact in program.facts:
        _validate_atom(fact.atom, arities)
        vars_in_fact = _vars_in_atom(fact.atom)
        if vars_in_fact:
            joined = ", ".join(sorted(vars_in_fact))
            raise DatalogValidationError(f"fact contains variable(s): {joined}")

    for rule in program.rules:
        _validate_atom(rule.head, arities)
        positive_vars: set[str] = set()
        for item in rule.body:
            if isinstance(item, AtomExpr):
                _validate_atom(item, arities)
                positive_vars.update(_vars_in_atom(item))
            elif item.op not in {"==", "!="}:
                raise DatalogValidationError(f"unsupported comparison operator: {item.op}")

        head_vars = _vars_in_atom(rule.head)
        unsafe_head = head_vars - positive_vars
        if unsafe_head:
            joined = ", ".join(sorted(unsafe_head))
            raise DatalogValidationError(f"unsafe head variable(s): {joined}")

        for item in rule.body:
            if isinstance(item, Comparison):
                comparison_vars = _vars_in_term(item.left) | _vars_in_term(item.right)
                unsafe_comparison = comparison_vars - positive_vars
                if unsafe_comparison:
                    joined = ", ".join(sorted(unsafe_comparison))
                    raise DatalogValidationError(
                        f"unsafe comparison variable(s): {joined}"
                    )


def parse_and_validate_program(source: str) -> Program:
    """Parse source and raise on unsupported syntax or invalid semantics."""
    program = parse_program(source)
    validate_program(program)
    return program


def _validate_atom(atom: AtomExpr, arities: dict[str, int]) -> None:
    if atom.predicate not in arities:
        raise DatalogValidationError(f"unknown predicate: {atom.predicate}")
    expected = arities[atom.predicate]
    actual = len(atom.args)
    if actual != expected:
        raise DatalogValidationError(
            f"arity mismatch for {atom.predicate}: expected {expected}, got {actual}"
        )


def _vars_in_atom(atom: AtomExpr) -> set[str]:
    vars_: set[str] = set()
    for arg in atom.args:
        vars_.update(_vars_in_term(arg))
    return vars_


def _vars_in_term(term: Term) -> set[str]:
    if isinstance(term, Var):
        return {term.name}
    if isinstance(term, Compound):
        vars_: set[str] = set()
        for arg in term.args:
            vars_.update(_vars_in_term(arg))
        return vars_
    return set()


def _strip_comments(source: str) -> str:
    out: list[str] = []
    i = 0
    in_string = False
    while i < len(source):
        ch = source[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < len(source):
                out.append(source[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(source) and source[i + 1] == "/":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    @property
    def done(self) -> bool:
        return self.pos >= len(self.source)

    def parse_program(self) -> Program:
        declarations: list[Declaration] = []
        facts: list[Fact] = []
        rules: list[Rule] = []

        while True:
            self.skip_ws()
            if self.done:
                return Program(tuple(declarations), tuple(facts), tuple(rules))
            if self.peek(".decl"):
                declarations.append(self.parse_declaration())
                self.require_line_end_or_eof()
                continue
            statement = self.read_statement()
            if not statement.strip():
                raise self.error("empty statement")
            fact_or_rule = _parse_fact_or_rule(statement)
            if isinstance(fact_or_rule, Fact):
                facts.append(fact_or_rule)
            else:
                rules.append(fact_or_rule)

    def parse_declaration(self) -> Declaration:
        self.expect(".decl")
        self.require_separator_after_directive()
        self.skip_ws()
        name = self.parse_identifier()
        self.skip_ws()
        self.expect("(")
        columns: list[Column] = []
        self.skip_ws()
        if self.peek(")"):
            self.pos += 1
            return Declaration(name, ())

        while True:
            self.skip_ws()
            column_name = self.parse_identifier()
            self.skip_ws()
            self.expect(":")
            self.skip_ws()
            column_type = self.parse_identifier()
            columns.append(Column(column_name, column_type))
            self.skip_ws()
            if self.peek(")"):
                self.pos += 1
                return Declaration(name, tuple(columns))
            self.expect(",")

    def read_statement(self) -> str:
        start = self.pos
        depth = 0
        in_string = False
        while not self.done:
            ch = self.source[self.pos]
            if in_string:
                if ch == "\\" and self.pos + 1 < len(self.source):
                    self.pos += 2
                    continue
                if ch == '"':
                    in_string = False
                self.pos += 1
                continue
            if ch == '"':
                in_string = True
                self.pos += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    raise self.error("unexpected ')'")
            elif ch == "." and depth == 0:
                statement = self.source[start : self.pos]
                self.pos += 1
                return statement
            self.pos += 1
        raise self.error("expected statement terminator '.'")

    def parse_identifier(self) -> str:
        start = self.pos
        if self.done or not _is_ident_start(self.source[self.pos]):
            raise self.error("expected identifier")
        self.pos += 1
        while not self.done and _is_ident_tail(self.source[self.pos]):
            self.pos += 1
        return self.source[start:self.pos]

    def skip_ws(self) -> None:
        while not self.done and self.source[self.pos].isspace():
            self.pos += 1

    def peek(self, text: str) -> bool:
        return self.source.startswith(text, self.pos)

    def expect(self, text: str) -> None:
        if not self.peek(text):
            raise self.error(f"expected {text!r}")
        self.pos += len(text)

    def require_separator_after_directive(self) -> None:
        if not self.done and not self.source[self.pos].isspace():
            raise self.error("expected whitespace after .decl")

    def require_line_end_or_eof(self) -> None:
        while not self.done and self.source[self.pos] in " \t\r":
            self.pos += 1
        if not self.done and self.source[self.pos] != "\n":
            raise self.error("expected newline after declaration")
        while not self.done and self.source[self.pos] == "\n":
            self.pos += 1

    def error(self, message: str) -> DatalogParseError:
        return DatalogParseError(f"{message} at position {self.pos}")


class _FragmentParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    @property
    def done(self) -> bool:
        return self.pos >= len(self.source)

    def parse_atom_expr(self) -> AtomExpr:
        self.skip_ws()
        predicate = self.parse_identifier()
        self.skip_ws()
        self.expect("(")
        args: list[Term] = []
        self.skip_ws()
        if self.peek(")"):
            self.pos += 1
            return AtomExpr(predicate, ())

        while True:
            arg_text = self.read_until_top_level({",", ")"})
            if not arg_text.strip():
                raise self.error("expected term")
            args.append(_parse_term_fragment(arg_text))
            self.skip_ws()
            if self.peek(")"):
                self.pos += 1
                return AtomExpr(predicate, tuple(args))
            self.expect(",")

    def read_until_top_level(self, delimiters: set[str]) -> str:
        start = self.pos
        depth = 0
        in_string = False
        while not self.done:
            ch = self.source[self.pos]
            if in_string:
                if ch == "\\" and self.pos + 1 < len(self.source):
                    self.pos += 2
                    continue
                if ch == '"':
                    in_string = False
                self.pos += 1
                continue
            if ch == '"':
                in_string = True
                self.pos += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0 and ")" in delimiters:
                    break
                depth -= 1
                if depth < 0:
                    raise self.error("unexpected ')'")
            elif depth == 0 and ch in delimiters:
                break
            self.pos += 1
        if in_string:
            raise self.error("unterminated string")
        if depth != 0:
            raise self.error("unbalanced parentheses")
        return self.source[start:self.pos]

    def parse_identifier(self) -> str:
        start = self.pos
        if self.done or not _is_ident_start(self.source[self.pos]):
            raise self.error("expected identifier")
        self.pos += 1
        while not self.done and _is_ident_tail(self.source[self.pos]):
            self.pos += 1
        return self.source[start:self.pos]

    def skip_ws(self) -> None:
        while not self.done and self.source[self.pos].isspace():
            self.pos += 1

    def peek(self, text: str) -> bool:
        return self.source.startswith(text, self.pos)

    def expect(self, text: str) -> None:
        if not self.peek(text):
            raise self.error(f"expected {text!r}")
        self.pos += len(text)

    def require_done(self) -> None:
        self.skip_ws()
        if not self.done:
            raise self.error("unexpected trailing input")

    def error(self, message: str) -> DatalogParseError:
        return DatalogParseError(f"{message} at fragment position {self.pos}")


def _parse_fact_or_rule(statement: str) -> Fact | Rule:
    head_text, sep, body_text = _partition_top_level(statement, ":-")
    head = _parse_atom_fragment(head_text)
    if not sep:
        return Fact(head)
    body_items = tuple(_parse_body_item(part) for part in _split_top_level(body_text, ","))
    if not body_items:
        raise DatalogParseError("rule body must not be empty")
    return Rule(head, body_items)


def _parse_body_item(source: str) -> BodyItem:
    left, op, right = _partition_top_level_comparison(source)
    if op:
        if not left.strip() or not right.strip():
            raise DatalogParseError(f"comparison {op} requires two terms")
        return Comparison(_parse_term_fragment(left), op, _parse_term_fragment(right))
    if _contains_unsupported_operator(source):
        raise DatalogParseError("unsupported operator in rule body")
    return _parse_atom_fragment(source)


def _parse_atom_fragment(source: str) -> AtomExpr:
    parser = _FragmentParser(source)
    atom = parser.parse_atom_expr()
    parser.require_done()
    return atom


def _parse_term_fragment(source: str) -> Term:
    try:
        return parse_term(source)
    except TermParseError as exc:
        raise DatalogParseError(str(exc)) from exc


def _partition_top_level(source: str, token: str) -> tuple[str, str, str]:
    idx = _find_top_level_token(source, token)
    if idx < 0:
        return source, "", ""
    return source[:idx], token, source[idx + len(token) :]


def _partition_top_level_comparison(
    source: str,
) -> tuple[str, Literal["==", "!="] | str, str]:
    hits = [
        (idx, op)
        for op in ("==", "!=")
        if (idx := _find_top_level_token(source, op)) >= 0
    ]
    if not hits:
        return source, "", ""
    hits.sort()
    if len(hits) > 1:
        raise DatalogParseError("body item contains multiple comparisons")
    idx, op = hits[0]
    return source[:idx], op, source[idx + len(op) :]


def _split_top_level(source: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    i = 0
    while i < len(source):
        ch = source[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise DatalogParseError("unexpected ')' in rule body")
        elif ch == delimiter and depth == 0:
            part = source[start:i]
            if not part.strip():
                raise DatalogParseError("empty rule body item")
            parts.append(part)
            start = i + 1
        i += 1
    if in_string:
        raise DatalogParseError("unterminated string in rule body")
    if depth != 0:
        raise DatalogParseError("unbalanced parentheses in rule body")
    tail = source[start:]
    if not tail.strip():
        raise DatalogParseError("empty rule body item")
    parts.append(tail)
    return parts


def _find_top_level_token(source: str, token: str) -> int:
    depth = 0
    in_string = False
    i = 0
    while i < len(source):
        ch = source[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise DatalogParseError("unexpected ')' in source fragment")
        elif depth == 0 and source.startswith(token, i):
            return i
        i += 1
    return -1


def _contains_unsupported_operator(source: str) -> bool:
    for op in ("<=", ">=", "<", ">", "=", "+", "-", "*", "/", ";", "|"):
        if _find_top_level_token(source, op) >= 0:
            return True
    return False


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def _is_ident_tail(ch: str) -> bool:
    return _is_ident_start(ch) or ("0" <= ch <= "9")
