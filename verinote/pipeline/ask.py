# SPDX-License-Identifier: MPL-2.0
"""Read-only factlog-style Ask routing for free-form questions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re

from verinote.engine.datalog import (
    AtomExpr,
    DatalogParseError,
    DatalogValidationError,
    parse_and_validate_program,
)
from verinote.engine import CheckReport, FindingDetail
from verinote.engine.wirelog import answer_qid
from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.engine.wirelog import strip_answer_line_prefix
from verinote.llm.base import LLMClient, LLMError
from verinote.pipeline.corroboration import CorroborationPolicyError, store_relation_aliases
from verinote.pipeline.engine_input import engine_relation_rows
from verinote.pipeline.query import (
    _schema_aware_query_flow_result,
    expand_query_relation_aliases,
)
from verinote.pipeline.query_candidate_eval import RELATION_DECL
from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch
from verinote.pipeline.report_trace import trace_query_answers
from verinote.store import Store, engine_statuses
from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError
from verinote.text import nfc

ASK_QID = 0
MAX_CONTEXT_CHARS = 12000
MAX_EXCERPTS = 8
MAX_GROUNDING_FACTS = 8
_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}|[가-힣一-龥ぁ-んァ-ン]{1,}")
_RELATION_DECL = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"

# Shown when the flow reports `provider_failed`, so Ask sends no second request.
#
# It deliberately does not say the provider failed. `provider_failed` is set by
# the `except LLMError` handlers around the flow's provider calls, and an
# `LLMError` also arrives from a request that succeeded: the
# `"anthropic response contained no tool_use block"` raise sits outside the block
# that normalises transport errors, so it is reachable only once
# `messages.create` has returned. On that shape "the provider failed" would be
# printed directly above a `reason` line reporting the provider's own output as
# unusable -- the confusion `query_intent.py` refuses to create one level down,
# where it keeps a local wiring bug out of the parse path rather than let it be
# reported as "the provider violated the schema".
#
# `ask.html` renders the warning slot as text, so this carries no markup.
_PROVIDER_SKIPPED_WARNING = (
    "verinote did not get a usable reading of the question from the provider and "
    "did not send it another request, so no model-composed answer is shown"
)


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


@dataclass(frozen=True)
class _EngineQuerySnapshot:
    """The facts and display metadata used for one Ask engine evaluation."""

    engine_rows: tuple[dict[str, object], ...]
    fact_rows: Mapping[int, Mapping[str, object] | None]


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
        flow = _schema_aware_query_flow_result(
            store,
            client,
            qid=ASK_QID,
            question=question,
            llm_error_status="review_required",
        )
    except DuckDBFactTermStoreError as exc:
        # The flow raised, so it reported nothing -- including no provider
        # verdict. `provider_skipped` stays False here: a fact-term error is
        # not a provider failure, and the fallback model is still worth asking.
        return _fallback_answer(
            store,
            client,
            root=root,
            question=question,
            reason=f"engine fact-term error: {_short_reason(exc)}",
        )
    status, query_dl, reason = flow.status, flow.query_dl, flow.reason
    # Read once, here, rather than re-deriving it per branch: every downstream
    # `_fallback_answer` gets the same verdict about the same flow.
    provider_skipped = flow.provider_failed
    if status == "translated" and query_dl:
        report, expanded_query, snapshot = _run_engine_query(store, query_dl)
        if report.engine_available and report.ok and not report.errors:
            answers = tuple(dict.fromkeys(report.answers))
            if answers:
                evaluated_values = {
                    strip_answer_line_prefix(answer, ASK_QID) for answer in answers
                }
                traces = tuple(
                    trace
                    for trace in trace_query_answers(
                        store,
                        expanded_query,
                        engine_rows=snapshot.engine_rows,
                        fact_rows=snapshot.fact_rows,
                    )
                    if trace.value in evaluated_values
                )
                if _is_three_hop_answer_query(expanded_query) and not _has_complete_three_hop_trace(
                    answers, traces
                ):
                    # Unexercisable today: no _QueryFlowResult pairs
                    # provider_failed with "translated", and this branch needs
                    # that status. Wired so a future site that does is
                    # suppressed by default rather than silently re-requesting.
                    return _fallback_answer(
                        store,
                        client,
                        root=root,
                        question=question,
                        reason="three-hop query source trace is incomplete",
                        provider_skipped=provider_skipped,
                    )
                source_facts = tuple(_grounding_facts_from_traces(traces))
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
                    warning=_engine_answer_warning(question, source_facts),
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
        # Unexercisable today, for the same reason as the three-hop site above:
        # reaching here requires status "translated", which no construction
        # pairs with provider_failed. Wired for the same default-safe reason.
        return _fallback_answer(
            store,
            client,
            root=root,
            question=question,
            reason=reason,
            provider_skipped=provider_skipped,
        )

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
        provider_skipped=provider_skipped,
    )


def _engine_answer_warning(
    question: str, source_facts: tuple[AskGroundingFact, ...]
) -> str | None:
    """The caveat shown beside a verified engine answer, if there is one.

    Two unrelated caveats share this slot because the template renders one. The
    standing one is that there is no source trace, so the answer cannot be shown
    as facts; it wins when both would apply, because the unit caveat needs the
    trace to find the fact the answer came from. A multi-valued answer never
    reaches the unit caveat, and not because it is an unreachable state: it is
    answered and verified, and it produces no source trace, so it takes that
    first branch.

    The second says the verified value is in a different unit from the one the
    question asked in (#445). It declines nothing, converts nothing, and changes
    no label, route, or reason: `VERIFIED — engine` is a claim about provenance
    and that claim is still accurate.

    The answering fact is the one whose object is the answer; a two-hop proof
    also lists its intermediate fact, whose object is not. `_fold` is applied to
    both sides because the two strings arrive by different routes -- `answer` is
    composed through NFC by the trace, while `object` is the store's text as
    written -- so without it an NFD value would fail this test on every fact and
    the caveat would silently never fire. The comparison is still between a
    rendered string and a stored one, and rendering escapes more than it
    normalises: a stored `2년<TAB>` gives object `'2년\\t'` and answer
    `'2년\\\\t'`, which the fold does not reconcile, so that answer gets no
    caveat. Silence, never a wrong sentence.

    The sentence carries no backticks or other markup: `ask.html` renders this
    slot as text, and no warning text this module writes uses any. (The fallback
    path also writes to this slot -- either a provider error message or the
    provider-skipped notice; neither is written here.)
    """
    if not source_facts:
        return "source trace unavailable for this verified query shape"
    answering = next(
        (f for f in source_facts if _fold(f.object) == _fold(f.answer)), None
    )
    if answering is None:
        return None
    mismatch = korean_measure_unit_mismatch(question, answering.object)
    if mismatch is None:
        return None
    counter, stated = mismatch
    return (
        f"the question's counter is {counter}; the verified value states "
        f"{stated}. verinote shows stored values as recorded and applies "
        f"no unit conversion"
    )


def _run_engine_query(
    store: Store, query_dl: str
) -> tuple[CheckReport, str, _EngineQuerySnapshot]:
    try:
        expanded = expand_query_relation_aliases(query_dl, store_relation_aliases(store))
        engine_rows = tuple(dict(row) for row in engine_relation_rows(store))
        snapshot = _EngineQuerySnapshot(
            engine_rows=engine_rows,
            fact_rows={
                int(row["id"]): store.get_fact(int(row["id"])) for row in engine_rows
            },
        )
        return (
            run_check_duckdb(
                list(engine_rows),
                policy_dl=RELATION_DECL,
                query_dl=expanded,
            ),
            expanded,
            snapshot,
        )
    except CorroborationPolicyError as exc:
        finding = f"ERROR policy error: {exc}"
        return (
            CheckReport(
                ok=False,
                errors=1,
                warnings=0,
                text=f"policy error: {exc}",
                findings=[finding],
                finding_details=[FindingDetail(finding, "error", "policy_error")],
            ),
            query_dl,
            _EngineQuerySnapshot(engine_rows=(), fact_rows={}),
        )
    except Exception as exc:  # noqa: BLE001 - keep Ask from failing closed
        finding = f"ERROR engine error: {exc}"
        return (
            CheckReport(
                ok=False,
                errors=1,
                warnings=0,
                text=f"ask engine error: {exc}",
                findings=[finding],
                finding_details=[FindingDetail(finding, "error", "engine_error")],
            ),
            query_dl,
            _EngineQuerySnapshot(engine_rows=(), fact_rows={}),
        )


def _engine_source_facts(store: Store, query_dl: str) -> list[AskGroundingFact]:
    return _grounding_facts_from_traces(trace_query_answers(store, query_dl))


def _grounding_facts_from_traces(traces) -> list[AskGroundingFact]:
    facts: list[AskGroundingFact] = []
    seen: set[tuple[str, int]] = set()
    for answer in traces:
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


def _is_three_hop_answer_query(query_dl: str) -> bool:
    """Whether the expanded query contains a direct three-relation answer rule."""
    try:
        program = parse_and_validate_program(_RELATION_DECL + query_dl)
    except (DatalogParseError, DatalogValidationError):
        return False
    return any(
        answer_qid(rule.head.predicate) is not None
        and len(rule.body) == 3
        and all(
            isinstance(item, AtomExpr)
            and item.predicate == "relation"
            and len(item.args) == 3
            for item in rule.body
        )
        for rule in program.rules
    )


def _has_complete_three_hop_trace(answers: tuple[str, ...], traces) -> bool:
    """Require one exact three-fact proof for each engine answer.

    The engine evaluates the query before provenance is reconstructed.  For a
    bounded three-hop rule, a missing or partial reconstruction therefore means
    the result cannot be presented as verified.
    """
    complete_answers = {trace.value for trace in traces if len(trace.facts) == 3}
    return all(
        strip_answer_line_prefix(answer, ASK_QID) in complete_answers
        for answer in answers
    )


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
    # Ask asks exactly one question, `ASK_QID`, and `classify_query_draft`
    # refuses a draft that answers any other one — so the prefix the engine put
    # on these lines is this qid's, and undoing it by name beats re-guessing it
    # with a pattern that would also eat an answer of the shape `q3: ...`.
    return "\n".join(strip_answer_line_prefix(line, ASK_QID) for line in answers)


def _fallback_answer_body(
    excerpts: tuple[AskExcerpt, ...],
    grounding: tuple[AskGroundingFact, ...],
) -> str:
    """Describe what this answer has below it, naming only what is there.

    `ask.html` puts the `Source excerpts` section on the page only when
    `result.excerpts` is non-empty, and the grounding table only when
    `result.grounding_facts` is, heading that table `Verified grounding facts`
    on the `fallback` route this helper serves. A body sentence naming a
    section built from an empty collection therefore points at nothing on the
    page, which is what the unconditional "Source excerpts are shown below."
    did on a fallback result carrying grounding facts and no excerpt.

    Each branch points at only the collection it has just found non-empty; the
    last points at neither and says so. Naming one present section is enough,
    so a result carrying both keeps the excerpt sentence it has today.

    The last branch reports what the page shows rather than why. An excerpt is
    also absent when the source file is missing from disk or is not UTF-8:
    `search_source_excerpts` passes over both before any comparison against the
    question runs, so "nothing matched the question" would be a claim about
    text that was never read.

    Neither the rendering claim nor the skipping one is left as prose a later
    edit could quietly falsify: `tests/test_ask_verdict.py` re-derives the first
    from a render of `ask.html`, and `tests/test_ask.py` re-derives the second
    from `search_source_excerpts` itself.
    """
    if excerpts:
        return "The deterministic engine could not answer. Source excerpts are shown below."
    if grounding:
        return (
            "The deterministic engine could not answer. Verified grounding facts "
            "are shown below."
        )
    return (
        "The deterministic engine could not answer, and no source excerpt or "
        "verified grounding fact is shown below."
    )


def _fallback_answer(
    store: Store,
    client: LLMClient,
    *,
    root: Path,
    question: str,
    reason: str,
    provider_skipped: bool = False,
) -> AskResult:
    """Assemble the unverified answer, asking the model unless it just failed.

    `provider_skipped` is the flow's `provider_failed` verdict. When it is set,
    the provider has already been asked to read this question and produced
    nothing usable, so asking it again would double the failed requests for one
    question -- the whole of #438. Everything the page shows apart from the
    model's prose is built here without a model, which is why the skipped path
    still returns excerpts, grounding facts, route, label, status and reason.

    Default `False` so a caller that has no verdict cannot accidentally suppress
    a healthy provider; the callers that do have one pass it explicitly.
    """
    excerpts = tuple(search_source_excerpts(store, root=root, question=question))
    grounding = tuple(grounding_facts(store, question=question))
    if provider_skipped:
        # No context is built: nothing consumes it on this path, and composing a
        # prompt for a request that is not sent would only invite one later.
        warning = _PROVIDER_SKIPPED_WARNING
        answer = _fallback_answer_body(excerpts, grounding)
    else:
        context = _fallback_context(excerpts, grounding)
        warning = None
        try:
            answer = client.answer_question(question=question, context=context)
        except LLMError as exc:
            warning = _short_reason(exc)
            answer = _fallback_answer_body(excerpts, grounding)
        if not answer:
            answer = _fallback_answer_body(excerpts, grounding)
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
    """Find the best-scoring pattern in `text` and quote a window around it.

    The match position is found in `folded`, the casefold of `normalized`
    (`nfc(text)`). The excerpt window is cut from `normalized` -- the same
    string `folded` was derived from -- rather than from `text`, which makes
    the two coordinate systems reconcilable (not identical: a `folded`
    position and a `normalized` position can still name different offsets,
    which is exactly why `_unfold_offset`, below, exists). `folded` is not
    itself guaranteed to be NFC-normalized; only `normalized` is.

    `folded` and `normalized` can differ in length: casefold can expand one
    character into several (`ß` to `ss`, `ﬄ` to `ffl`) but never contracts one
    away or into fewer characters, a property `test_no_code_point_casefolds_to_nothing`
    sweeps exhaustively over every code point. When the lengths agree, every
    character folded 1:1 and a `folded` position is already a `normalized`
    position, so `best_pos` is used as-is -- `test_a_length_coincidence_with_text_does_not_fool_the_shortcut`
    pins that this comparison is against `len(normalized)`, not `len(text)`,
    which can coincidentally equal `len(folded)` even when `len(normalized)`
    does not. When the lengths differ, `_unfold_offset` walks `normalized` to
    translate; this rests on `s.casefold()` being the concatenation of each of
    `s`'s characters' folds, a property `test_casefold_is_the_concatenation_of_its_characters_folds`
    checks over the entire repertoire as one string and over ordered pairs
    drawn from every code point with a multi-character fold, every code point
    with a nonzero canonical combining class, and the Greek sigma/iota code
    points.

    Consequence: a source stored decomposed (NFD) is quoted composed (NFC) --
    canonically equivalent to what is stored, not a byte-for-byte copy of it.

    Given patterns containing no whitespace -- true of every pattern
    `_question_patterns` can produce, since `_TOKEN`'s character classes
    contain none -- the excerpt contains the source character whose fold
    covers the best-scoring match's first folded character: `anchor` is
    always a valid index into `normalized`, so the window always extends past
    it, and `" ".join(...split())` cannot strip that character, because no
    whitespace code point casefolds to a non-whitespace one
    (`test_no_whitespace_code_point_casefolds_to_a_non_whitespace_one`). This
    is a claim about the source character behind the match, not about the
    pattern text itself -- when a fold expands (`ß` to `ss`), the character
    the excerpt carries is `ß`, not `s`.

    The trailing "..." reports whether `normalized` continues past the
    window, not whether `text` does -- honest about `normalized`'s length,
    not a general guarantee. In particular, trailing whitespace already
    dropped by `" ".join(...split())` can leave an appended "..." after the
    true last visible character; this is a pre-existing condition, unrelated
    to normalization, and not addressed here.

    The score above is measured on a +/-300-character window of `folded`,
    which is not the same window this function quotes below (240 characters
    before `anchor` / 420 after, in `normalized` characters) -- the two
    windows were different sizes before this function's fix as well as after.
    On the leading side, 240 < 300, so up to `300 - 240 = 60` folded
    characters counted toward the score can fall outside the excerpt. On the
    trailing side, 420 exceeds 300 by more than a single fold's maximum width
    (3, pinned by `test_no_code_point_casefold_exceeds_three_characters`), so
    nothing counted toward the score falls outside the excerpt on that side.
    """
    normalized = nfc(text)
    folded = normalized.casefold()
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
    anchor = best_pos if len(folded) == len(normalized) else _unfold_offset(normalized, best_pos)
    start = max(0, anchor - 240)
    end = min(len(normalized), anchor + 420)
    excerpt = " ".join(normalized[start:end].split())
    if start:
        excerpt = "..." + excerpt
    if end < len(normalized):
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
    return nfc(value).casefold()


def _unfold_offset(normalized: str, index: int) -> int:
    """Map `index`, a position in `normalized.casefold()`, back into `normalized`.

    Walks `normalized` one character at a time, accumulating each character's
    casefolded width, and returns the position of the character whose fold
    covers `index`. Sound because casefold is decomposable per character --
    `s.casefold()` equals the concatenation of each of `s`'s characters'
    folds, checked by `test_casefold_is_the_concatenation_of_its_characters_folds`
    (exhaustively over every code point as a whole-repertoire string; over
    ordered pairs for a curated alphabet, not the full repertoire) -- and
    never contracts a character to nothing or to fewer characters than it
    started with, swept exhaustively over every code point by
    `test_no_code_point_casefolds_to_nothing`.

    If `index >= len(normalized.casefold())`, falls through to
    `len(normalized)`, which is not a character position -- unreachable from
    this module's only call site, where `index` is always a `str.find` hit
    inside `folded`, but callers outside that guarantee must check for it.
    """
    consumed = 0
    for position, char in enumerate(normalized):
        width = len(char.casefold())
        if consumed + width > index:
            return position
        consumed += width
    return len(normalized)


def _short_reason(value: object) -> str:
    return " ".join(str(value).split())[:240]
