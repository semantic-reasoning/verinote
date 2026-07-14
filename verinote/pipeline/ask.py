# SPDX-License-Identifier: MPL-2.0
"""Read-only factlog-style Ask routing for free-form questions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from verinote.engine import CheckReport
from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.llm.base import LLMClient, LLMError
from verinote.pipeline.corroboration import CorroborationPolicyError, store_relation_aliases
from verinote.pipeline.engine_input import engine_relation_rows
from verinote.pipeline.query import expand_query_relation_aliases, schema_aware_query_flow
from verinote.pipeline.query_candidate_eval import RELATION_DECL
from verinote.pipeline.report_trace import trace_query_answers
from verinote.store import Store, engine_statuses
from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError

ASK_QID = 0
MAX_CONTEXT_CHARS = 12000
MAX_EXCERPTS = 8
MAX_GROUNDING_FACTS = 8
_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}|[가-힣一-龥ぁ-んァ-ン]{1,}")
# The engine formats each answer as ``q<id>: value`` for the /report view. That
# report prefix is never part of the answer itself, so strip it when it leaks
# into an Ask answer body.
_ANSWER_QID_PREFIX = re.compile(r"^q\d+:\s*")


@dataclass(frozen=True)
class AskExcerpt:
    path: str
    excerpt: str
    score: int


@dataclass(frozen=True)
class AskGroundingFact:
    answer: str
    subject: str
    relation: str
    object: str
    source: str
    evidence: str = ""


@dataclass(frozen=True)
class AskResult:
    route: str
    label: str
    question: str
    status: str
    answer: str
    query_dl: str | None
    engine_answers: tuple[str, ...]
    reason: str
    excerpts: tuple[AskExcerpt, ...] = ()
    grounding_facts: tuple[AskGroundingFact, ...] = ()
    warning: str | None = None


def ask_question(
    store: Store,
    client: LLMClient,
    *,
    root: Path,
    question: str,
) -> AskResult:
    """Answer one question without persisting query state or mutating questions."""
    question = " ".join(question.split())
    if not question:
        return AskResult(
            route="fallback",
            label="UNVERIFIED — source exploration",
            question=question,
            status="empty",
            answer="Question is required.",
            query_dl=None,
            engine_answers=(),
            reason="empty question",
        )

    try:
        status, query_dl, reason = schema_aware_query_flow(
            store,
            client,
            qid=ASK_QID,
            question=question,
            llm_error_status="review_required",
        )
    except DuckDBFactTermStoreError as exc:
        return _fallback_answer(
            store,
            client,
            root=root,
            question=question,
            reason=f"engine fact-term error: {_short_reason(exc)}",
        )
    if status == "translated" and query_dl:
        report, expanded_query = _run_engine_query(store, query_dl)
        if report.engine_available and report.ok and not report.errors:
            answers = tuple(dict.fromkeys(report.answers))
            if answers:
                source_facts = tuple(_engine_source_facts(store, expanded_query))
                return AskResult(
                    route="engine",
                    label="VERIFIED — engine",
                    question=question,
                    status=status,
                    answer=_render_engine_answer_body(answers, source_facts),
                    query_dl=query_dl,
                    engine_answers=answers,
                    reason="deterministic query matched confirmed/accepted facts",
                    grounding_facts=source_facts,
                    warning=(
                        None
                        if source_facts
                        else "source trace unavailable for this verified query shape"
                    ),
                )
            return AskResult(
                route="engine",
                label="VERIFIED — engine (negative)",
                question=question,
                status="no_answer",
                answer="No confirmed facts match.",
                query_dl=query_dl,
                engine_answers=(),
                reason="no confirmed facts match",
            )
        reason = _short_reason("; ".join(report.findings) or report.text)
        return _fallback_answer(store, client, root=root, question=question, reason=reason)

    if status == "no_answer":
        return AskResult(
            route="engine",
            label="VERIFIED — engine (negative)",
            question=question,
            status=status,
            answer="No confirmed facts match.",
            query_dl=query_dl,
            engine_answers=(),
            reason=reason or "no confirmed facts match",
        )

    return _fallback_answer(
        store,
        client,
        root=root,
        question=question,
        reason=reason or f"deterministic query status: {status}",
    )


def _run_engine_query(store: Store, query_dl: str) -> tuple[CheckReport, str]:
    try:
        expanded = expand_query_relation_aliases(query_dl, store_relation_aliases(store))
        return (
            run_check_duckdb(
                engine_relation_rows(store),
                policy_dl=RELATION_DECL,
                query_dl=expanded,
            ),
            expanded,
        )
    except CorroborationPolicyError as exc:
        return (
            CheckReport(
                ok=False,
                errors=1,
                warnings=0,
                text=f"policy error: {exc}",
                findings=[f"ERROR policy error: {exc}"],
            ),
            query_dl,
        )
    except Exception as exc:  # noqa: BLE001 - keep Ask from failing closed
        return (
            CheckReport(
                ok=False,
                errors=1,
                warnings=0,
                text=f"ask engine error: {exc}",
                findings=[f"ERROR engine error: {exc}"],
            ),
            query_dl,
        )


def _engine_source_facts(store: Store, query_dl: str) -> list[AskGroundingFact]:
    facts: list[AskGroundingFact] = []
    seen: set[tuple[str, int]] = set()
    for answer in trace_query_answers(store, query_dl):
        for fact in answer.facts:
            key = (answer.value, fact.id)
            if key in seen:
                continue
            seen.add(key)
            facts.append(
                AskGroundingFact(
                    # `display_value`, not `value`: ask.html renders this as a
                    # single table cell, not as an entry in the report's
                    # `, `-joined answer line, so the join's comma escape has
                    # nothing to defend here and would only contradict the
                    # `object` cell printed beside it (issue #167). `seen` still
                    # keys on `value`, the answer's identity.
                    answer=answer.display_value,
                    subject=fact.subject,
                    relation=fact.relation,
                    object=fact.object,
                    source=fact.source,
                    evidence=fact.evidence,
                )
            )
    return facts


def _render_engine_answer_body(
    answers: tuple[str, ...],
    source_facts: tuple[AskGroundingFact, ...],
) -> str:
    """Render a verified engine answer as factlog-style fact rows.

    Each verified triple is restated as ``subject, relation, object`` with its
    backing source(s) cited inline beneath (``    ← <source>``), mirroring
    factlog's ``render_engine_answer`` — so the answer states *which fact* is
    verified, not a bare object value. When the source trace is unavailable, fall
    back to the raw engine answer values with the internal ``q<id>:`` report
    prefix stripped (that prefix is a /report artifact, never part of the answer).
    """
    if source_facts:
        sources_by_triple: dict[tuple[str, str, str], list[str]] = {}
        for fact in source_facts:
            triple = (fact.subject, fact.relation, fact.object)
            sources = sources_by_triple.setdefault(triple, [])
            if fact.source and fact.source not in sources:
                sources.append(fact.source)
        lines: list[str] = []
        for (subject, relation, obj), sources in sources_by_triple.items():
            lines.append(f"{subject}, {relation}, {obj}")
            lines.extend(f"    ← {source}" for source in sources)
        return "\n".join(lines)
    return "\n".join(_ANSWER_QID_PREFIX.sub("", line) for line in answers)


def _fallback_answer(
    store: Store,
    client: LLMClient,
    *,
    root: Path,
    question: str,
    reason: str,
) -> AskResult:
    excerpts = tuple(search_source_excerpts(store, root=root, question=question))
    grounding = tuple(grounding_facts(store, question=question))
    context = _fallback_context(excerpts, grounding)
    warning = None
    try:
        answer = client.answer_question(question=question, context=context)
    except LLMError as exc:
        warning = _short_reason(exc)
        answer = "The deterministic engine could not answer. Source excerpts are shown below."
    if not answer:
        answer = "The deterministic engine could not answer. Source excerpts are shown below."
    return AskResult(
        route="fallback",
        label="UNVERIFIED — source exploration",
        question=question,
        status="fallback",
        answer=answer,
        query_dl=None,
        engine_answers=(),
        reason=reason,
        excerpts=excerpts,
        grounding_facts=grounding,
        warning=warning,
    )


def search_source_excerpts(
    store: Store,
    *,
    root: Path,
    question: str,
    limit: int = MAX_EXCERPTS,
) -> list[AskExcerpt]:
    patterns = _question_patterns(question)
    if not patterns:
        return []
    matches: list[AskExcerpt] = []
    seen_paths: set[Path] = set()
    for label, path in _source_text_paths(store, root):
        resolved = path.expanduser().resolve()
        if resolved in seen_paths or not resolved.is_file():
            continue
        seen_paths.add(resolved)
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        excerpt, score = _best_excerpt(text, patterns)
        if score:
            matches.append(AskExcerpt(path=label, excerpt=excerpt, score=score))
    return sorted(matches, key=lambda item: (-item.score, item.path))[:limit]


def grounding_facts(
    store: Store,
    *,
    question: str,
    limit: int = MAX_GROUNDING_FACTS,
) -> list[AskGroundingFact]:
    normalized_question = _fold(question)
    rows: list[AskGroundingFact] = []
    for fact in store.facts(statuses=engine_statuses()):
        subject = str(fact["subject"])
        relation = str(fact["relation"])
        obj = str(fact["object"])
        if _fold(subject) not in normalized_question and _fold(obj) not in normalized_question:
            continue
        rows.append(
            AskGroundingFact(
                answer="",
                subject=subject,
                relation=relation,
                object=obj,
                source=str(fact["source_path"] or ""),
            )
        )
        if len(rows) >= limit:
            break
    return rows


def _source_text_paths(store: Store, root: Path) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    for row in store.source_text_inputs():
        artifact = str(row["artifact_path"])
        paths.append((artifact, root / artifact))
    for row in store.sources():
        source = str(row["path"])
        path = Path(source)
        paths.append((source, path if path.is_absolute() else root / path))
    return paths


def _question_patterns(question: str) -> tuple[str, ...]:
    tokens = [_fold(match.group(0)) for match in _TOKEN.finditer(question)]
    return tuple(dict.fromkeys(token for token in tokens if token))


def _best_excerpt(text: str, patterns: tuple[str, ...]) -> tuple[str, int]:
    folded = _fold(text)
    best_pos = -1
    best_score = 0
    for pattern in patterns:
        pos = folded.find(pattern)
        if pos < 0:
            continue
        score = sum(1 for item in patterns if item in folded[max(0, pos - 300) : pos + 300])
        if score > best_score:
            best_score = score
            best_pos = pos
    if best_pos < 0:
        return "", 0
    start = max(0, best_pos - 240)
    end = min(len(text), best_pos + 420)
    excerpt = " ".join(text[start:end].split())
    if start:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt += "..."
    return excerpt, best_score


def _fallback_context(
    excerpts: tuple[AskExcerpt, ...],
    grounding: tuple[AskGroundingFact, ...],
) -> str:
    parts: list[str] = []
    if grounding:
        parts.append("Verified grounding facts:")
        for fact in grounding:
            source = f" ({fact.source})" if fact.source else ""
            parts.append(f"- {fact.subject} | {fact.relation} | {fact.object}{source}")
    if excerpts:
        parts.append("Source excerpts:")
        for excerpt in excerpts:
            parts.append(f"- Source: {excerpt.path}\n  Excerpt: {excerpt.excerpt}")
    if not parts:
        return "No source excerpts or verified grounding facts matched the question."
    context = "\n".join(parts)
    return context[:MAX_CONTEXT_CHARS]


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _short_reason(value: object) -> str:
    return " ".join(str(value).split())[:240]
