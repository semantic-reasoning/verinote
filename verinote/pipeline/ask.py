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
from verinote.pipeline.query import expand_query_relation_aliases, schema_aware_query_flow
from verinote.pipeline.query_candidate_eval import RELATION_DECL
from verinote.pipeline.query_intent import QueryIntentKind, deterministic_query_intent
from verinote.store import ENGINE_STATUSES, Store

ASK_QID = 0
MAX_CONTEXT_CHARS = 12000
MAX_EXCERPTS = 8
MAX_GROUNDING_FACTS = 8
_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}|[가-힣一-龥ぁ-んァ-ン]{1,}")


@dataclass(frozen=True)
class AskExcerpt:
    path: str
    excerpt: str
    score: int


@dataclass(frozen=True)
class AskGroundingFact:
    subject: str
    relation: str
    object: str
    source: str


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

    status, query_dl, reason = schema_aware_query_flow(
        store,
        client,
        qid=ASK_QID,
        question=question,
        llm_error_status="review_required",
    )
    if status == "translated" and query_dl:
        report = _run_engine_query(store, query_dl)
        if report.engine_available and report.ok and not report.errors:
            answers = tuple(dict.fromkeys(report.answers))
            if answers:
                return AskResult(
                    route="engine",
                    label="VERIFIED — engine",
                    question=question,
                    status=status,
                    answer="\n".join(answers),
                    query_dl=query_dl,
                    engine_answers=answers,
                    reason="deterministic query matched confirmed/accepted facts",
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

    if status == "review_required" and _known_scope_without_candidate(store, question):
        return AskResult(
            route="engine",
            label="VERIFIED — engine (negative)",
            question=question,
            status="no_answer",
            answer="No confirmed facts match.",
            query_dl='no_answer("no confirmed facts match")',
            engine_answers=(),
            reason="no confirmed facts match",
        )

    return _fallback_answer(
        store,
        client,
        root=root,
        question=question,
        reason=reason or f"deterministic query status: {status}",
    )


def _run_engine_query(store: Store, query_dl: str) -> CheckReport:
    try:
        expanded = expand_query_relation_aliases(query_dl, store_relation_aliases(store))
        return run_check_duckdb(
            store.engine_fact_terms(),
            policy_dl=RELATION_DECL,
            query_dl=expanded,
        )
    except CorroborationPolicyError as exc:
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"policy error: {exc}",
            findings=[f"ERROR policy error: {exc}"],
        )
    except Exception as exc:  # noqa: BLE001 - keep Ask from failing closed
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"ask engine error: {exc}",
            findings=[f"ERROR engine error: {exc}"],
        )


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
    for fact in store.facts(statuses=ENGINE_STATUSES):
        subject = str(fact["subject"])
        relation = str(fact["relation"])
        obj = str(fact["object"])
        if _fold(subject) not in normalized_question and _fold(obj) not in normalized_question:
            continue
        rows.append(
            AskGroundingFact(
                subject=subject,
                relation=relation,
                object=obj,
                source=str(fact["source_path"] or ""),
            )
        )
        if len(rows) >= limit:
            break
    return rows


def _known_scope_without_candidate(store: Store, question: str) -> bool:
    """Classify deterministic known-entity misses as verified negative answers."""
    intent = deterministic_query_intent(question)
    if intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED or intent.subject is None:
        return False
    subject = _fold(intent.subject.value)
    if not subject:
        return False
    return any(
        _fold(str(fact["subject"])) == subject or _fold(str(fact["object"])) == subject
        for fact in store.facts(statuses=ENGINE_STATUSES)
    )


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
