# SPDX-License-Identifier: MPL-2.0
"""Logical term model and parser for the supported wirelog/Datalog subset.

This module is deliberately independent from pyrewire, DuckDB, SQLite, and the
verification pipeline. It defines the term semantics future DuckDB-backed
inference must preserve:

- uppercase identifiers are variables: ``A``, ``Subject``
- lowercase/underscore identifiers are atoms: ``wirelog``, ``born_on``
- compound functors use atom identifiers: ``person("Ada")``
- strings, integers, and nested compounds are first-class terms

Floating point and exponent notation are intentionally rejected for now. That
keeps numeric equality unambiguous until a later issue defines float semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypeAlias

_VAR_RE = re.compile(r"[A-Z][A-Za-z0-9_]*\Z")
_ATOM_RE = re.compile(r"[a-z_][A-Za-z0-9_]*\Z")


class TermParseError(ValueError):
    """Raised when term text cannot be parsed completely."""


@dataclass(frozen=True)
class Var:
    name: str

    def __post_init__(self) -> None:
        if not _VAR_RE.fullmatch(self.name):
            raise ValueError(f"invalid variable name: {self.name!r}")


@dataclass(frozen=True)
class Atom:
    name: str

    def __post_init__(self) -> None:
        if not _ATOM_RE.fullmatch(self.name):
            raise ValueError(f"invalid atom name: {self.name!r}")


@dataclass(frozen=True)
class StringLit:
    value: str


@dataclass(frozen=True)
class NumberLit:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise ValueError(f"invalid integer literal: {self.value!r}")


@dataclass(frozen=True)
class Compound:
    functor: str
    args: tuple["Term", ...]

    def __post_init__(self) -> None:
        if not _ATOM_RE.fullmatch(self.functor):
            raise ValueError(f"invalid compound functor: {self.functor!r}")
        if not isinstance(self.args, tuple):
            raise ValueError("compound args must be a tuple")
        for arg in self.args:
            if not isinstance(arg, (Var, Atom, StringLit, NumberLit, Compound)):
                raise ValueError(f"invalid compound arg: {arg!r}")


Term: TypeAlias = Var | Atom | StringLit | NumberLit | Compound


def parse_term(text: str) -> Term:
    """Parse one complete logical term."""
    parser = _Parser(text)
    term = parser.parse_term()
    parser.skip_ws()
    if not parser.done:
        raise parser.error("unexpected trailing input")
    return term


def render_term(term: Term) -> str:
    """Render a term in canonical concrete syntax."""
    if isinstance(term, Var):
        return term.name
    if isinstance(term, Atom):
        return term.name
    if isinstance(term, StringLit):
        return _render_string(term.value)
    if isinstance(term, NumberLit):
        return str(term.value)
    if isinstance(term, Compound):
        return f"{term.functor}(" + ", ".join(render_term(arg) for arg in term.args) + ")"
    raise TypeError(f"not a term: {term!r}")


def canonical_term_key(term: Term) -> str:
    """Return a stable, type-tagged structural key for equality/storage work."""
    if isinstance(term, Var):
        return f"V:{term.name}"
    if isinstance(term, Atom):
        return f"A:{term.name}"
    if isinstance(term, StringLit):
        return "S:" + _render_string(term.value)
    if isinstance(term, NumberLit):
        return f"N:{term.value}"
    if isinstance(term, Compound):
        return (
            f"C:{term.functor}("
            + ",".join(canonical_term_key(arg) for arg in term.args)
            + ")"
        )
    raise TypeError(f"not a term: {term!r}")


def _render_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    @property
    def done(self) -> bool:
        return self.pos >= len(self.text)

    def error(self, message: str) -> TermParseError:
        return TermParseError(f"{message} at position {self.pos}")

    def skip_ws(self) -> None:
        while not self.done and self.text[self.pos].isspace():
            self.pos += 1

    def parse_term(self) -> Term:
        self.skip_ws()
        if self.done:
            raise self.error("expected term")

        ch = self.text[self.pos]
        if ch == '"':
            return self.parse_string()
        if ch == "-" or _is_ascii_digit(ch):
            return self.parse_number()
        if _is_ident_start(ch):
            ident = self.parse_identifier()
            self.skip_ws()
            if not self.done and self.text[self.pos] == "(":
                if not _ATOM_RE.fullmatch(ident):
                    raise self.error("compound functor must be an atom identifier")
                return self.parse_compound(ident)
            if _VAR_RE.fullmatch(ident):
                return Var(ident)
            if _ATOM_RE.fullmatch(ident):
                return Atom(ident)
            raise self.error("invalid identifier")
        raise self.error("expected term")

    def parse_identifier(self) -> str:
        start = self.pos
        self.pos += 1
        while not self.done and _is_ident_tail(self.text[self.pos]):
            self.pos += 1
        return self.text[start:self.pos]

    def parse_string(self) -> StringLit:
        self.pos += 1  # opening quote
        chars: list[str] = []
        while not self.done:
            ch = self.text[self.pos]
            self.pos += 1
            if ch == '"':
                return StringLit("".join(chars))
            if ch == "\\":
                if self.done:
                    raise self.error("unterminated escape")
                esc = self.text[self.pos]
                self.pos += 1
                if esc == '"':
                    chars.append('"')
                elif esc == "\\":
                    chars.append("\\")
                elif esc == "n":
                    chars.append("\n")
                elif esc == "r":
                    chars.append("\r")
                elif esc == "t":
                    chars.append("\t")
                else:
                    raise self.error(f"unsupported escape \\{esc}")
            else:
                chars.append(ch)
        raise self.error("unterminated string")

    def parse_number(self) -> NumberLit:
        start = self.pos
        if self.text[self.pos] == "-":
            self.pos += 1
            if self.done or not _is_ascii_digit(self.text[self.pos]):
                raise self.error("expected digit after '-'")

        digit_start = self.pos
        if self.text[self.pos] == "0":
            self.pos += 1
            if not self.done and _is_ascii_digit(self.text[self.pos]):
                raise self.error("leading zero is not supported")
        else:
            while not self.done and _is_ascii_digit(self.text[self.pos]):
                self.pos += 1

        if digit_start == self.pos:
            raise self.error("expected digit")
        if not self.done and self.text[self.pos] in ".eE":
            raise self.error("only integer numeric terms are supported")
        if not self.done and _is_ident_start(self.text[self.pos]):
            raise self.error("unexpected identifier after number")
        return NumberLit(int(self.text[start:self.pos]))

    def parse_compound(self, functor: str) -> Compound:
        self.pos += 1  # opening paren
        args: list[Term] = []
        self.skip_ws()
        if not self.done and self.text[self.pos] == ")":
            self.pos += 1
            return Compound(functor, ())

        while True:
            args.append(self.parse_term())
            self.skip_ws()
            if self.done:
                raise self.error("expected ',' or ')'")
            ch = self.text[self.pos]
            if ch == ")":
                self.pos += 1
                return Compound(functor, tuple(args))
            if ch != ",":
                raise self.error("expected ',' or ')'")
            self.pos += 1
            self.skip_ws()
            if not self.done and self.text[self.pos] == ")":
                raise self.error("expected term after ','")


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def _is_ident_tail(ch: str) -> bool:
    return _is_ident_start(ch) or _is_ascii_digit(ch)


def _is_ascii_digit(ch: str) -> bool:
    return "0" <= ch <= "9"
