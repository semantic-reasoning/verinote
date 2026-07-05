# SPDX-License-Identifier: MPL-2.0
"""Translate NL questions to Datalog query drafts and persist them for the engine.

Each pending question is translated by the `LLMClient` into either an
`answer_q<id>` rule (status `translated`) or a `review_required(...)` line
(status `review_required`). The translated rules are written to
`<root>/facts/query.dl`, which `verify()` feeds to the DuckDB backend so
`/report` shows each query's evaluation. `review_required` questions are tracked
in the DB only.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path
import re
import unicodedata

from verinote.engine.datalog import AtomExpr, Comparison, DatalogParseError, parse_program
from verinote.engine.terms import Atom, StringLit, render_term
from verinote.llm.base import LLMClient
from verinote.pipeline.corroboration import CorroborationPolicyError, store_relation_aliases
from verinote.store import Store

# Query draft, relative to the KB root (the db file's directory).
QUERY_RELPATH = Path("facts") / "query.dl"
MAX_ALIAS_EXPANDED_RULES_PER_RULE = 64
_ESCAPE = re.compile(r'(["\\])')
_ROLE_QUESTION = re.compile(
    r'["“”\']?(?P<person>[^"“”\'?？\n]{1,80}?)["“”\']?\s*'
    r"(?:의|에\s*대한)\s*(?:역할|직책|직위)"
)
_GENERIC_ROLE_RELATIONS = ("역할", "직책", "직위", "role", "has_role")


def query_path(root: Path) -> Path:
    return root / QUERY_RELPATH


def _is_review_required(line: str) -> bool:
    return line.lstrip().startswith("review_required")


def _lit(value: str) -> str:
    return '"' + _ESCAPE.sub(r"\\\1", value) + '"'


def deterministic_query_dl(question: str, qid: int) -> str | None:
    """Return deterministic query drafts for common shapes LLMs mistranslate."""
    match = _ROLE_QUESTION.search(question.strip())
    if not match:
        return None
    person = match.group("person").strip()
    if not person:
        return None

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


def translate_questions(store: Store, client: LLMClient, *, root: Path) -> list[dict]:
    """Translate every pending question, persist drafts, rewrite `query.dl`.

    Returns one dict per translated question: {id, status, query_dl}.
    """
    results: list[dict] = []
    for q in store.questions(pending_only=True):
        query_dl = deterministic_query_dl(q["text"], q["id"])
        if query_dl is not None:
            status = "translated"
        else:
            line = client.translate_query(question=q["text"], qid=q["id"])
            if _is_review_required(line):
                status, query_dl = "review_required", line
            else:
                status = "translated"
                query_dl = f".decl answer_q{q['id']}(value: symbol)\n{line}"
        store.set_question_query(q["id"], query_dl, status)
        results.append({"id": q["id"], "status": status, "query_dl": query_dl})
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
        for index, item in enumerate(rule.body):
            if not (isinstance(item, AtomExpr) and item.predicate == "relation"):
                alternatives.append([item])
                continue
            if len(item.args) != 3:
                alternatives.append([item])
                continue
            raw = _relation_name(item.args[1])
            if raw is None or raw not in aliases:
                alternatives.append([item])
                continue
            canonical = aliases[raw]
            alternatives.append(
                [item, AtomExpr("relation", (item.args[0], StringLit(canonical), item.args[2]))]
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
