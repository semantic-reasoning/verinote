# SPDX-License-Identifier: MPL-2.0
"""Legacy pyrewire/wirelog compatibility helpers.

Production verification uses `verinote.engine.duckdb_backend.run_check_duckdb`.
This module remains for compatibility/debug rendering of wirelog `.dl` programs:
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

# Datalog string literals: escape embedded quotes/backslashes.
_ESCAPE = re.compile(r'(["\\])')

# Prefixes that mark a derived relation as a verinote finding.
_ERROR_PREFIX = "error_"
_WARN_PREFIX = "warn_"

# Shipped default policy. `verinote init` scaffolds a copy to
# `<root>/policy/logic-policy.dl`; edit that copy per-KB.
DEFAULT_POLICY = """\
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
.decl error_functional_conflict(subject: symbol, rel: symbol)
error_functional_conflict(S, R) :-
    relation(S, R, A), relation(S, R, B), functional(R), A != B.
"""


def _lit(value: str) -> str:
    return '"' + _ESCAPE.sub(r"\\\1", value) + '"'


def compile_dl(facts: Iterable[Mapping[str, object]]) -> str:
    """Render confirmed facts as `relation("s", "r", "o").` lines (sorted, unique).

    Accepts any row-like mapping with subject/relation/object keys (sqlite3.Row
    included). Only this projection becomes engine input.
    """
    lines = set()
    for f in facts:
        s, r, o = str(f["subject"]), str(f["relation"]), str(f["object"])
        lines.add(f"relation({_lit(s)}, {_lit(r)}, {_lit(o)}).")
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def _parse_relation_facts(dl_text: str) -> list[tuple[str, str, str]]:
    """Recover (subject, rel, object) triples from `compile_dl` output.

    Mirrors `_lit`'s escaping (``\\"`` -> ``"``, ``\\\\`` -> ``\\``) so the round
    trip is exact, including embedded quotes.
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
                    buf.append(line[i + 1])
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


_ANSWER_PREFIX = "answer_q"


@dataclass
class CheckReport:
    ok: bool
    errors: int
    warnings: int
    text: str
    findings: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    engine_available: bool = True


def _load_engine():
    """Return the pyrewire module, or None when it is not installed.

    Factored out so tests can exercise the graceful-degradation path.
    """
    try:
        import pyrewire
    except ImportError:
        return None
    return pyrewire


def _degraded_report(dl_text: str) -> CheckReport:
    n = dl_text.count("relation(")
    return CheckReport(
        ok=True,
        errors=0,
        warnings=0,
        engine_available=False,
        text=(
            "legacy wirelog compatibility engine (pyrewire) not installed — "
            "showing compiled input only.\n"
            f"compiled facts: {n}\n\n{dl_text}"
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
    pyrewire = _load_engine()
    if pyrewire is None:
        return _degraded_report(dl_text)

    policy = policy_dl if policy_dl is not None else DEFAULT_POLICY
    program = policy + ("\n" + query_dl if query_dl else "")
    facts = _parse_relation_facts(dl_text)

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

    errors: list[str] = []
    warnings: list[str] = []
    answers_by_q: dict[str, list[str]] = {}
    for name, row, mult in deltas:
        if mult <= 0:
            continue
        if name.startswith(_ERROR_PREFIX):
            errors.append(f"{name[len(_ERROR_PREFIX) :]}: {' '.join(map(str, row))}")
        elif name.startswith(_WARN_PREFIX):
            warnings.append(f"{name[len(_WARN_PREFIX) :]}: {' '.join(map(str, row))}")
        elif name.startswith(_ANSWER_PREFIX):
            qid = name[len(_ANSWER_PREFIX) :]
            answers_by_q.setdefault(qid, []).append(" ".join(map(str, row)))

    errors.sort()
    warnings.sort()
    answers = [
        f"q{qid}: {', '.join(sorted(vals))}" for qid, vals in sorted(answers_by_q.items())
    ]
    findings = [f"ERROR {e}" for e in errors] + [f"WARN {w}" for w in warnings]
    summary = f"errors: {len(errors)}  warnings: {len(warnings)}  facts: {len(facts)}"
    body = (
        "\n".join(findings)
        if findings
        else "no findings — knowledge base is consistent."
    )
    if answers:
        body += "\n\n--- answers ---\n" + "\n".join(answers)
    return CheckReport(
        ok=not errors,
        errors=len(errors),
        warnings=len(warnings),
        answers=answers,
        text=f"{summary}\n\n{body}\n\n--- engine input ---\n{dl_text}",
        findings=findings,
    )
