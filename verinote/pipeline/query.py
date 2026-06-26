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

from pathlib import Path

from verinote.llm.base import LLMClient
from verinote.store import Store

# Query draft, relative to the KB root (the db file's directory).
QUERY_RELPATH = Path("facts") / "query.dl"


def query_path(root: Path) -> Path:
    return root / QUERY_RELPATH


def _is_review_required(line: str) -> bool:
    return line.lstrip().startswith("review_required")


def translate_questions(store: Store, client: LLMClient, *, root: Path) -> list[dict]:
    """Translate every pending question, persist drafts, rewrite `query.dl`.

    Returns one dict per translated question: {id, status, query_dl}.
    """
    results: list[dict] = []
    for q in store.questions(pending_only=True):
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
        return path.read_text(encoding="utf-8")
    return None
