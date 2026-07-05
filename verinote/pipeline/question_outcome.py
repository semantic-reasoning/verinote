# SPDX-License-Identifier: MPL-2.0
"""Display helpers for durable question lifecycle states."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


@dataclass(frozen=True)
class QuestionOutcomeView:
    id: int
    text: str
    status: str
    label: str
    message: str
    action: str
    query_dl: str | None
    badge_class: str


_STATUS_META = {
    "pending": (
        "Pending",
        "Waiting for translation.",
        "Translate to generate a query candidate.",
    ),
    "translated": (
        "Translated",
        "Executable query is ready.",
        "Run the report to inspect answers.",
    ),
    "review_required": (
        "Review required",
        "The question needs manual review before it can run.",
        "Repair can retry after facts or schema improve.",
    ),
    "translation_failed": (
        "Translation failed",
        "The provider output could not be used.",
        "Check the message, provider, and model settings, then translate again.",
    ),
    "no_answer": (
        "No answer",
        "No confirmed or accepted facts matched the generated query.",
        "Confirm relevant facts or add sources, then re-analyze and translate again.",
    ),
    "ambiguous": (
        "Ambiguous",
        "Multiple query candidates produced conflicting answers.",
        "Narrow the question or review the candidate facts before retrying.",
    ),
}
_NON_EXECUTABLE_RE = re.compile(
    r"^\s*(review_required|no_answer|ambiguous|translation_failed)\((?P<reason>.*)\)\s*\.?\s*$"
)


def question_outcome_view(question: Mapping[str, object]) -> QuestionOutcomeView:
    status = str(question["status"])
    label, default_message, action = _STATUS_META.get(
        status,
        (status.replace("_", " ").title(), "Unknown question state.", ""),
    )
    reason = str(_question_value(question, "reason") or "").strip()
    query_dl = _question_value(question, "query_dl")
    if not reason and query_dl:
        reason = _reason_from_query_dl(str(query_dl))
    return QuestionOutcomeView(
        id=int(question["id"]),
        text=str(_question_value(question, "text") or ""),
        status=status,
        label=label,
        message=reason or default_message,
        action=action,
        query_dl=str(query_dl) if query_dl else None,
        badge_class=f"badge-question-{status.replace('_', '-')}",
    )


def format_question_outcome(question: Mapping[str, object]) -> str:
    view = question_outcome_view(question)
    return f"q{view.id}: {view.status} - {view.message}"


def _reason_from_query_dl(query_dl: str) -> str:
    match = _NON_EXECUTABLE_RE.match(query_dl)
    if match is None:
        return ""
    raw = match.group("reason").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return raw


def _question_value(question: Mapping[str, object], key: str) -> object | None:
    try:
        return question[key]
    except (IndexError, KeyError):
        return None
