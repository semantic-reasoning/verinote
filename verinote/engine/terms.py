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
import unicodedata
from dataclasses import dataclass
from typing import TypeAlias

_VAR_RE = re.compile(r"[A-Z][A-Za-z0-9_]*\Z")
_ATOM_RE = re.compile(r"[a-z_][A-Za-z0-9_]*\Z")

# Readable short forms for the control characters that appear in real text.
# Everything else non-printing falls back to a numeric \uXXXX/\UXXXXXXXX escape.
_SHORT_ESCAPES = {
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
# Unicode general categories that cannot render as visible text and are exactly
# what starts a new line: controls (Cc), line separators (Zl) and paragraph
# separators (Zp). See `escape_string_value`.
_ESCAPED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
# Explicit bidi controls (a subset of Cf). They do not break lines, but they
# reorder the visual run around them, so a value can make a report line read as
# something the engine never derived. Neutralized for the same reason.
_ESCAPED_CODEPOINTS = frozenset(
    {
        0x202A,  # LEFT-TO-RIGHT EMBEDDING
        0x202B,  # RIGHT-TO-LEFT EMBEDDING
        0x202C,  # POP DIRECTIONAL FORMATTING
        0x202D,  # LEFT-TO-RIGHT OVERRIDE
        0x202E,  # RIGHT-TO-LEFT OVERRIDE
        0x2066,  # LEFT-TO-RIGHT ISOLATE
        0x2067,  # RIGHT-TO-LEFT ISOLATE
        0x2068,  # FIRST STRONG ISOLATE
        0x2069,  # POP DIRECTIONAL ISOLATE
    }
)
_HEX_RE = re.compile(r"[0-9A-Fa-f]+\Z")


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


def term_compare_key(term: Term) -> str:
    """Return the equality key the inference engine compares terms on.

    This is the single owner of "when are two terms one value?", and every
    consumer must ask it rather than reach for dataclass `==`. The engine's
    equality is *human-surface* equality for leaves: `Atom("x")`, `StringLit("x")`
    and `NumberLit(5)`/`StringLit("5")` are one value, so an entry that happened
    to be stored as a structural term does not occupy a universe of its own.
    Storage stays type-tagged and lossless (`term_to_duckdb_value`); this key is
    only for equality.

    It lives here, beside the term model, rather than in `duckdb_terms` where it
    started: it is the semantics of the term language itself, not of one
    backend's storage. `pipeline.report_trace` needs the same rule to match a
    query against a fact -- and a pipeline module importing a backend's storage
    encoder to learn what equality means is a layering inversion, the same one
    that moved `bare_label` here.

    Compounds compare structurally, on `canonical_term_key`: `f(Atom("x"))` and
    `f(StringLit("x"))` stay distinct, which is the pre-existing behaviour and is
    preserved here deliberately -- only the leaf twins collapse.
    """
    if isinstance(term, Atom):
        return f"s:{term.name}"
    if isinstance(term, StringLit):
        return f"s:{term.value}"
    if isinstance(term, NumberLit):
        return f"s:{term.value}"
    if isinstance(term, Compound):
        return f"c:{canonical_term_key(term)}"
    if isinstance(term, Var):
        return f"v:{term.name}"
    raise TypeError(f"not a term: {term!r}")


def terms_equal(left: Term, right: Term) -> bool:
    """Return whether the engine treats two terms as the same value."""
    return term_compare_key(left) == term_compare_key(right)


def escape_string_value(value: str) -> str:
    """Escape backslashes, line-breaking characters and bidi controls.

    This is the single owner of the "how do we neutralize control characters in
    a string value" question. Quoted rendering (`render_term`) and unquoted
    report rendering both build on it, so the two paths cannot drift apart.

    The line-forging rule is a category whitelist, not a blacklist: anything
    whose Unicode general category is a control (Cc), a line separator (Zl), or
    a paragraph separator (Zp) is escaped. Enumerating "the dangerous
    characters" does not work here, because `str.splitlines()` breaks on far
    more than LF/CR -- VT, FF, FS, GS, RS, NEL, U+2028 and U+2029 all start a
    new line, so a blacklist that stops at `\\n`/`\\r` still lets a fact value
    forge a report line. Cc/Zl/Zp is a superset of everything `splitlines()`
    splits on (a full 0..0x10FFFF scan is pinned in the tests), and it also
    covers NUL and ESC.

    On top of that, the nine explicit bidi controls in `_ESCAPED_CODEPOINTS`
    are escaped. They break no line, but they reverse the visual order of the
    text around them, so a value could make a report line read as a claim the
    engine never derived.

    The escape set stops there, and deliberately does not take all of Cf: ZWJ
    (U+200D), ZWNJ (U+200C) and soft hyphen carry meaning in ordinary text --
    they join emoji sequences and are required spelling in Persian and Indic
    scripts. Escaping them would corrupt the source document's spelling, which
    the subject/object rendering promises to preserve, and they cannot forge a
    line anyway.

    Backslash is handled first: without escaping it, a literal backslash-n in
    the value would be indistinguishable from a real newline after escaping.
    """
    out: list[str] = []
    for ch in value:
        short = _SHORT_ESCAPES.get(ch)
        if short is not None:
            out.append(short)
        elif (
            unicodedata.category(ch) in _ESCAPED_CATEGORIES
            or ord(ch) in _ESCAPED_CODEPOINTS
        ):
            out.append(_unicode_escape(ch))
        else:
            out.append(ch)
    return "".join(out)


def render_display_value(term: Term) -> str:
    """Render one value the way a reader should see it standing on its own.

    This is `render_term` with the quotes taken off a `StringLit`: a value shown
    in its own right (an Ask grounding cell) is text the source said, not
    Datalog syntax the reader must un-quote. Control characters still go through
    `escape_string_value`, because a value able to start a new line can forge a
    line wherever it is shown.

    What it deliberately does *not* do is escape surface commas. That escape
    belongs to `render_answer_value` below and exists only to defend the `, `
    join between answers. Where there is no join there is nothing to defend, and
    the backslash would be a character the reader's source never contained --
    which is what made an Ask Answer cell disagree with the `object` column
    printed beside it (issue #167).
    """
    if isinstance(term, StringLit):
        return escape_string_value(term.value)
    return render_term(term)


def render_answer_value(term: Term) -> str:
    """Render one answer value so a comma inside it cannot forge two answers.

    This is the single owner of "how does one answer reach a reader *through the
    `, ` join*", so the two places /report shows the same answer cannot drift
    apart. The engine backend renders the "Query answers" line and
    `pipeline.report_trace` renders the "Traceability" section; when each escaped
    in its own way the one answer `Analytical Engine, Ltd` appeared escaped in
    one section and ambiguous in the other (issue #167).

    Use `render_display_value` instead wherever a value is shown alone rather
    than joined; this function's escape is a property of the join, not of the
    value.

    Multiple answers for a question are joined with `, `. A `StringLit` whose
    surface text already contains a comma would then be indistinguishable from
    two separate answers, so those surface commas are escaped as `\\,`.
    Backslash is escaped first by `escape_string_value`, so `\\,` round-trips
    unambiguously.

    A `Compound` is rendered by `render_term`. Its `, ` separators are
    structural, not surface text, and callers (and tests such as
    `role(person("Ada"), "PI")`) depend on that rendering staying intact, so a
    compound's commas are left alone.
    """
    if isinstance(term, StringLit):
        return render_display_value(term).replace(",", "\\,")
    return render_term(term)


def _unicode_escape(ch: str) -> str:
    code = ord(ch)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _render_string(value: str) -> str:
    escaped = escape_string_value(value).replace('"', '\\"')
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
                elif esc == "u":
                    chars.append(self.parse_unicode_escape(4))
                elif esc == "U":
                    chars.append(self.parse_unicode_escape(8))
                else:
                    raise self.error(f"unsupported escape \\{esc}")
            else:
                chars.append(ch)
        raise self.error("unterminated string")

    def parse_unicode_escape(self, width: int) -> str:
        """Read the `width` hex digits of a \\uXXXX / \\UXXXXXXXX escape."""
        digits = self.text[self.pos : self.pos + width]
        if len(digits) < width or not _HEX_RE.fullmatch(digits):
            raise self.error(f"expected {width} hex digits in unicode escape")
        code = int(digits, 16)
        if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            raise self.error("unicode escape is not a valid code point")
        self.pos += width
        return chr(code)

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
