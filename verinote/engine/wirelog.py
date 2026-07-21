# SPDX-License-Identifier: MPL-2.0
"""Legacy pyrewire/wirelog compatibility helpers.

Production verification loads canonical fact terms from the DuckDB sidecar and
uses `verinote.engine.duckdb_backend.run_check_duckdb`. This module remains for
compatibility/debug rendering of string-only wirelog `.dl` programs:
`compile_dl` is pure and fully tested without pyrewire, while `run_check` executes
the legacy pyrewire path when the optional `wirelog` extra is installed.

Policy contract
---------------
A policy is a Datalog program over the base relation
``relation(subject, rel, object)``.
Any derived relation whose name starts with ``error_`` is a blocking finding and
``warn_`` is a non-blocking one; verinote reads those back, so every column is a
plain symbol it can render. See `DEFAULT_POLICY`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from verinote.engine.datalog import (
    AtomExpr,
    DatalogParseError,
    DatalogValidationError,
    Program,
    parse_and_validate_program,
)
from verinote.engine.policy_vocabulary import (
    FUNCTIONAL_CONFLICT_DECL,
    FUNCTIONAL_CONFLICT_RULE,
)
from verinote.engine.terms import StringLit, escape_string_value

# Prefixes that mark a derived relation as a verinote finding.
_ERROR_PREFIX = "error_"
_WARN_PREFIX = "warn_"
_ANSWER_PREFIX = "answer_q"
# A qid is the question's INTEGER primary key rendered as text, so it has to sort
# by number: plain string order puts q10 ahead of q2. `[0-9]+` matches what
# `_ANSWER_DECL` accepts — `str.isdigit()` would let through digits like "²" that
# `int()` then rejects. A policy is user-authored, so a non-numeric
# `answer_q*` relation does reach here; park those after the numbered ones
# instead of raising.
_NUMERIC_QID = re.compile(r"[0-9]+\Z")


def answer_bucket_sort_key(qid: str) -> tuple[int, int, str]:
    """Order answer buckets by question number, not by how the digits sort."""
    if _NUMERIC_QID.fullmatch(qid):
        # All-digits does not guarantee `int()` succeeds: past
        # `sys.get_int_max_str_digits()` (4300 by default) it raises ValueError.
        # A qid is user-authored, so park a pathologically long number in the
        # trailing bucket alongside malformed qids rather than crash the sort.
        try:
            return (0, int(qid), qid)
        except ValueError:
            pass
    return (1, 0, qid)

# Shipped default policy. `verinote init` scaffolds a copy to
# `<root>/policy/logic-policy.dl`; edit that copy per-KB.
DEFAULT_POLICY = f"""\
// verinote logic policy (wirelog Datalog).
//
// verinote compiles confirmed/accepted facts to
//   relation(subject, rel, object)
// and runs this policy over them. A derived `error_*` relation FAILS the review
// gate; a `warn_*` relation is a non-blocking note. Edit freely — the engine
// re-checks every fact, so the policy is the one place review rules live.

.decl relation(subject: symbol, rel: symbol, object: symbol)

// Relations that may hold at most one object per subject. Add your own.
.decl functional(rel: symbol)
functional("established_on").
functional("born_on").
functional("died_on").

// A functional relation must not carry two distinct objects for one subject.
{FUNCTIONAL_CONFLICT_DECL}
{FUNCTIONAL_CONFLICT_RULE}(S, R) :-
    relation(S, R, A), relation(S, R, B), functional(R), A != B.
"""


def _lit(value: str) -> str:
    return '"' + escape_string_value(value).replace('"', '\\"') + '"'


def compile_dl(facts: Iterable[Mapping[str, object]]) -> str:
    """Render string-display facts as `relation("s", "r", "o").` lines.

    Accepts any row-like mapping with subject/relation/object keys (sqlite3.Row
    included). This is a legacy compatibility/debug helper; production
    verification reads structural terms from the DuckDB fact-term store.
    """
    lines = set()
    for f in facts:
        s, r, o = str(f["subject"]), str(f["relation"]), str(f["object"])
        lines.add(f"relation({_lit(s)}, {_lit(r)}, {_lit(o)}).")
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def _parse_relation_facts(dl_text: str) -> list[tuple[str, str, str]]:
    """Recover (subject, rel, object) triples from `compile_dl` output.

    Mirrors `_lit`'s escaping so the round trip is exact, including embedded
    quotes, backslashes, short control escapes, and numeric Unicode escapes.
    """
    facts: list[tuple[str, str, str]] = []
    for line in dl_text.splitlines():
        line = line.strip()
        if not line.startswith("relation("):
            continue
        out: list[str] = []
        i, n = 0, len(line)
        while i < n and len(out) < 3:
            while i < n and line[i] != '"':
                i += 1
            if i >= n:
                break
            i += 1
            buf: list[str] = []
            while i < n:
                c = line[i]
                if c == "\\" and i + 1 < n:
                    esc = line[i + 1]
                    if esc == '"':
                        buf.append('"')
                        i += 2
                        continue
                    if esc == "\\":
                        buf.append("\\")
                        i += 2
                        continue
                    if esc == "n":
                        buf.append("\n")
                        i += 2
                        continue
                    if esc == "r":
                        buf.append("\r")
                        i += 2
                        continue
                    if esc == "t":
                        buf.append("\t")
                        i += 2
                        continue
                    if esc in {"u", "U"}:
                        width = 4 if esc == "u" else 8
                        digits = line[i + 2 : i + 2 + width]
                        if len(digits) == width and re.fullmatch(r"[0-9A-Fa-f]+", digits):
                            code = int(digits, 16)
                            if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF:
                                buf.append(chr(code))
                                i += 2 + width
                                continue
                    # Preserve unsupported legacy escapes as the old parser did:
                    # drop the escape marker and keep the escaped byte.
                    buf.append(esc)
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                buf.append(c)
                i += 1
            out.append("".join(buf))
        if len(out) == 3:
            facts.append((out[0], out[1], out[2]))
    return facts


def _is_finding_or_query(predicate: str) -> bool:
    """True for predicates verinote reads back as output rather than dependency."""
    return predicate.startswith((_ERROR_PREFIX, _WARN_PREFIX, _ANSWER_PREFIX))


def _read_policy(source: str) -> Program | None:
    """`source` as a program, or None when our parser cannot read it.

    pyrewire is the engine on this path and its dialect is its own, so a policy
    it accepts may be one our parser does not. Every reader of a parsed policy
    here is advisory — the shape a finding may be annotated from, the relation
    names a rule pins itself to — and none of them is a gate: findings and the
    review gate come from the engine either way. So a policy we cannot read is
    not an error, it just leaves those readers with nothing to say, and a reader
    with nothing to say must stay silent rather than guess. A genuinely
    malformed policy is surfaced as an engine error by the engine itself, so
    raising here would report the same fault twice.

    This is the one place that decides what "unreadable" means, so the readers
    cannot drift into disagreeing about which policies they are willing to read.
    """
    try:
        return parse_and_validate_program(source)
    except (DatalogParseError, DatalogValidationError):
        return None


def _declared_columns(program: Program) -> dict[str, tuple[str, ...]]:
    """The column names each predicate in `program` declares, by predicate name."""
    return {
        decl.name: tuple(column.name for column in decl.columns)
        for decl in program.declarations
    }


def dead_rule_warnings(policy_dl: str, present_relations: Iterable[str]) -> list[str]:
    """Warn about relation names a policy pins to string literals that no fact uses.

    A policy reaches the fact vocabulary through columns named ``rel`` — the
    ``relation(subject, rel, object)`` base relation, plus helper decls such as
    ``functional(rel)``. When such a column carries a string literal that names a
    relation, and no compiled engine fact uses that relation, the rule depending
    on it can never fire: a dead rule. These are non-blocking notes, never a gate.

    Only positions that actually *read* the fact vocabulary count: rule bodies and
    the policy's own extensional facts. A rule head is output, not a dependency —
    a literal there is a payload the rule emits, so counting it would flag rules
    that fire perfectly well (a finding reported as ``error_x(S, "some_label")``
    is the obvious shape). Derived predicates are skipped wherever they appear,
    since they resolve through other rules rather than through engine facts.

    An empty fact set (no engine input at all) yields ``[]`` so a genuinely empty
    knowledge base is never flagged. A malformed policy yields ``[]`` too — the
    parse failure is already surfaced as an engine error, so dead-rule detection
    must not invent a second failure mode. Only ``policy_dl`` is inspected; a
    caller's query text is a user question, never a dead rule.
    """
    present = set(present_relations)
    if not present:
        return []
    program = _read_policy(policy_dl)
    if program is None:
        return []

    columns_by_pred = _declared_columns(program)
    derived = {rule.head.predicate for rule in program.rules}
    referenced: set[tuple[str, str]] = set()

    def scan(atom: AtomExpr) -> None:
        if atom.predicate in derived or _is_finding_or_query(atom.predicate):
            return
        columns = columns_by_pred.get(atom.predicate)
        if columns is None:
            return
        for index, arg in enumerate(atom.args):
            if index < len(columns) and columns[index] == "rel" and isinstance(arg, StringLit):
                referenced.add((atom.predicate, arg.value))

    for fact in program.facts:
        scan(fact.atom)
    for rule in program.rules:
        for item in rule.body:
            if isinstance(item, AtomExpr):
                scan(item)

    return sorted(
        f'dead_rule: policy declares {predicate}("{escape_string_value(value)}") '
        "but no engine fact uses that relation"
        for predicate, value in referenced
        if value not in present
    )


# The engine's "clean bill of health" body. Exported because it is a claim about
# the policy that ran, so callers that ran a *different* policy than the KB's own
# (see pipeline.policy_state) must be able to recognise and replace it rather
# than re-declaring the sentence and drifting from it.
NO_FINDINGS_TEXT = "no findings — knowledge base is consistent."


@dataclass(frozen=True)
class FindingRow:
    """The derived tuple behind one finding line, before it became prose.

    A finding line renders its values bare and space-joined, so the line cannot
    be parsed back into fields: `Org 2 established_on` is equally "subject `Org
    2`, relation `established_on`" and "subject `Org`, relation `2
    established_on`". Anything that needs to know *which* subject or relation a
    finding is about must read `values` and compare exactly; matching against
    `text` is a guess that goes wrong precisely when labels overlap.

    `values` holds the labels as the KB stores them (see `bare_label`), while
    `identity` holds the engine comparison keys for the same tuple when an
    engine can provide them. The two differ when distinct structural terms share
    one bare surface, such as compound `f(x)` and string `"f(x)"`.

    `rule` and `columns` say which derived predicate produced the row and how
    that predicate declared its columns. Values alone are anonymous: knowing
    that `values[1]` is a relation label and not an object requires knowing the
    rule, and only verinote's own rules have a shape verinote knows (see
    `policy_vocabulary`). Both are carried from where the engine already has
    them — the rule name is never recovered by parsing `text`, which is the
    guess this class exists to prevent.
    """

    text: str
    values: tuple[str, ...]
    #: The derived predicate's name, e.g. `error_functional_conflict`.
    rule: str = ""
    #: Its declared column names, in order; empty when the policy's declaration
    #: could not be read, which means "shape unknown", not "no columns".
    columns: tuple[str, ...] = ()
    #: Engine comparison identity for the derived tuple. Empty means callers
    #: should fall back to `values`; the legacy wirelog path has only strings.
    identity: tuple[str, ...] = ()


@dataclass
class CheckReport:
    ok: bool
    errors: int
    warnings: int
    text: str
    findings: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    engine_available: bool = True
    #: One entry per derived finding, carrying that finding's structured row.
    #: Empty on reports no engine derived (a degraded run, a policy error): they
    #: have findings but no tuple behind them.
    finding_rows: list[FindingRow] = field(default_factory=list)


def _load_engine():
    """Return the pyrewire module, or None when it is not installed.

    Factored out so tests can exercise the graceful-degradation path.
    """
    try:
        import pyrewire
    except ImportError:
        return None
    return pyrewire


def _degraded_report(dl_text: str, warnings: list[str] | None = None) -> CheckReport:
    warnings = warnings or []
    findings = [f"WARN {w}" for w in warnings]
    n = dl_text.count("relation(")
    body = ("\n".join(findings) + "\n\n") if findings else ""
    return CheckReport(
        ok=True,
        errors=0,
        warnings=len(warnings),
        engine_available=False,
        findings=findings,
        text=(
            "legacy wirelog compatibility engine (pyrewire) not installed — "
            "showing compiled input only.\n"
            f"compiled facts: {n}\n\n{body}{dl_text}"
        ),
    )


_RELATION_DECL = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
_ANSWER_DECL = re.compile(r"answer_q[0-9]+\Z")


def validate_query(query_dl: str) -> tuple[bool, str]:
    """Deterministically check a proposed query line — the DuckDB engine has final say.

    Returns ``(True, "")`` when the line only references the ``relation/3``
    vocabulary (plus its own declared `answer_*` head) and parses+runs in
    DuckDB, else ``(False, reason)``. Used to gate LLM-proposed repairs.
    """
    # 1. structural parse/validation: this catches arity, unsupported syntax, and
    #    unsafe variables before the engine is involved.
    try:
        program = parse_and_validate_program(_RELATION_DECL + query_dl)
        _validate_query_contract(program)
    except (DatalogParseError, DatalogValidationError) as exc:
        return False, str(exc)

    # 2. compile/run check (catches DuckDB-backend subset limitations).
    from verinote.engine.duckdb_backend import run_check_duckdb

    rep = run_check_duckdb([], policy_dl=_RELATION_DECL, query_dl=query_dl)
    if not rep.ok:
        return False, rep.findings[0] if rep.findings else rep.text
    return True, ""


def _validate_query_contract(program: Program) -> None:
    """Ensure LLM-generated query snippets only answer from `relation/3`."""
    answer_decls: set[str] = set()
    for decl in program.declarations:
        if decl.name == "relation":
            continue
        if not _ANSWER_DECL.fullmatch(decl.name):
            raise DatalogValidationError(
                f"query may only declare answer predicates, got: {decl.name}"
            )
        if len(decl.columns) != 1:
            raise DatalogValidationError(
                f"query answer predicate must have arity 1: {decl.name}"
            )
        answer_decls.add(decl.name)
    if program.facts:
        raise DatalogValidationError("query snippets must not contain facts")
    for rule in program.rules:
        if rule.head.predicate not in answer_decls:
            raise DatalogValidationError(
                f"query rule head must be an answer predicate: {rule.head.predicate}"
            )
        for item in rule.body:
            if isinstance(item, AtomExpr) and item.predicate != "relation":
                raise DatalogValidationError(
                    f"query may only reference relation/3, got: {item.predicate}"
                )


@dataclass(frozen=True, order=True)
class _Derived:
    """One derived tuple with the identity of the rule that derived it.

    Ordered because `run_check` sorts each level's tuples, and ordering by
    `text` first keeps the report's lines in the order they used to be in when
    a level was a plain list of rendered strings. The remaining fields only
    break ties, so two tuples rendering to the same line still order
    deterministically rather than by the engine's delta order.
    """

    text: str
    values: tuple[str, ...]
    rule: str
    columns: tuple[str, ...]

    def finding_row(self, level: str) -> FindingRow:
        return FindingRow(
            f"{level} {self.text}",
            self.values,
            self.rule,
            self.columns,
            self.values,
        )


def _row_values(row: Iterable[object]) -> tuple[str, ...]:
    """The derived tuple's values as labels, unescaped, for exact comparison.

    pyrewire hands back symbols already, so this is `str` — the escaping in
    `_render_row` is display-side and must not reach here (see `FindingRow`).
    """
    return tuple(str(value) for value in row)


def _render_row(row: Iterable[object]) -> str:
    """Render one derived tuple into a report line body.

    Values go through `escape_string_value` for the same reason the DuckDB
    backend does it: an unescaped line break inside a value would let that value
    forge extra `ERROR `/`WARN ` lines in the report body. *Control-character*
    escaping has exactly one owner (`verinote.engine.terms`), so on that question
    this legacy path cannot drift away from the production one.

    The shared ownership stops there, and answers on this path really do drift.
    The production answer path escapes a value's surface commas via
    `terms.render_answer_value`, so one value cannot forge two answers across the
    `, ` join that `run_check` uses below (#167); this path does not, so a legacy
    answer `q1: A, B` stays ambiguous between one value and two.

    Sharing that renderer is not a swap: it takes a `Term`, while pyrewire hands
    this path bare values that are `str()`-ed here, so sharing would mean
    asserting that every legacy value is a string literal. That is a rendering
    change on a path CI cannot execute at all (pyrewire is absent there, #234),
    so it is left alone deliberately rather than changed blind.
    """
    return " ".join(escape_string_value(str(value)) for value in row)


def run_check(
    dl_text: str, *, policy_dl: str | None = None, query_dl: str | None = None
) -> CheckReport:
    """Run the legacy pyrewire policy path over compiled facts.

    `dl_text` is `compile_dl` output (the verbatim engine input). `policy_dl`
    defaults to `DEFAULT_POLICY`. `query_dl` holds `answer_q<id>(...)` query rules
    (see pipeline.query). Derived `error_*`/`warn_*` tuples become findings
    (`errors > 0` is the review gate); `answer_q<id>` tuples become answers. If
    pyrewire is absent we still return a legacy compatibility report flagged
    `engine_available=False`.
    """
    policy = policy_dl if policy_dl is not None else DEFAULT_POLICY
    facts = _parse_relation_facts(dl_text)
    present = {rel for _, rel, _ in facts}
    dead = dead_rule_warnings(policy, present)

    pyrewire = _load_engine()
    if pyrewire is None:
        return _degraded_report(dl_text, dead)

    program = policy + ("\n" + query_dl if query_dl else "")

    try:
        with pyrewire.EasySession(program) as session:
            for subject, rel, obj in facts:
                session.insert_sym("relation", subject, rel, obj)
            deltas = session.step()
    except Exception as exc:  # pyrewire parse/exec errors -> blocking, surfaced
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"policy/engine error: {exc}\n\n{dl_text}",
            findings=[f"engine error: {exc}"],
        )

    # The shape a finding may be read positionally by, from the same text the
    # engine ran (policy plus any query rules), or nothing when we cannot read it.
    parsed = _read_policy(program)
    columns_by_rule = _declared_columns(parsed) if parsed is not None else {}
    errors: list[_Derived] = []
    warnings: list[_Derived] = []
    answers_by_q: dict[str, list[str]] = {}
    for name, row, mult in deltas:
        if mult <= 0:
            continue
        if name.startswith(_ERROR_PREFIX):
            errors.append(
                _Derived(
                    f"{name[len(_ERROR_PREFIX) :]}: {_render_row(row)}",
                    _row_values(row),
                    name,
                    columns_by_rule.get(name, ()),
                )
            )
        elif name.startswith(_WARN_PREFIX):
            warnings.append(
                _Derived(
                    f"{name[len(_WARN_PREFIX) :]}: {_render_row(row)}",
                    _row_values(row),
                    name,
                    columns_by_rule.get(name, ()),
                )
            )
        elif name.startswith(_ANSWER_PREFIX):
            qid = name[len(_ANSWER_PREFIX) :]
            answers_by_q.setdefault(qid, []).append(_render_row(row))

    errors.sort()
    warnings.sort()
    answers = [
        f"q{qid}: {', '.join(sorted(vals))}"
        for qid, vals in sorted(
            answers_by_q.items(), key=lambda item: answer_bucket_sort_key(item[0])
        )
    ]
    # A dead-rule note is a statement about the *policy*, not a derived tuple:
    # nothing fired, so there is no row behind it and it gets no `FindingRow`.
    # `_source_note` already omits a note for a finding with no row, which is
    # exactly right here — there are no facts to name for a rule that never fired.
    warning_lines = sorted([row.text for row in warnings] + dead)
    findings = [f"ERROR {row.text}" for row in errors] + [
        f"WARN {line}" for line in warning_lines
    ]
    finding_rows = [row.finding_row("ERROR") for row in errors] + [
        row.finding_row("WARN") for row in warnings
    ]
    summary = (
        f"errors: {len(errors)}  warnings: {len(warning_lines)}  facts: {len(facts)}"
    )
    body = "\n".join(findings) if findings else NO_FINDINGS_TEXT
    if answers:
        body += "\n\n--- answers ---\n" + "\n".join(answers)
    return CheckReport(
        ok=not errors,
        errors=len(errors),
        warnings=len(warning_lines),
        answers=answers,
        text=f"{summary}\n\n{body}\n\n--- engine input ---\n{dl_text}",
        findings=findings,
        finding_rows=finding_rows,
    )
