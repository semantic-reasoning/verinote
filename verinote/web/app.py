# SPDX-License-Identifier: MPL-2.0
"""The verinote web application.

Server-rendered with Jinja; interactivity via HTMX (the review toggle posts and
swaps a single row partial). No JS build step. The app owns one `Store` (SQLite).
"""

from __future__ import annotations

from importlib import resources
import logging
from pathlib import Path
import threading
import unicodedata
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from verinote.config import (
    PROVIDER_LABELS,
    PROVIDERS,
    TESTABLE_PROVIDERS,
    Config,
    app_config_path,
    save_active_root,
    save_settings,
)
from verinote.llm import LLMError, get_client
from verinote.policy_defaults import DEFAULT_RELATION_ALIASES
from verinote.pipeline import (
    create_chunked_extraction_job,
    ExtractionJobBusyError,
    fact_trust_summary,
    IngestError,
    ingest_bytes,
    is_live_extraction_job,
    latest_source_job_ids,
    process_extraction_job,
    store_source,
    supported_suffixes,
    repair_questions,
    translate_questions,
    verify,
    write_query_file,
)
from verinote.pipeline.policy_state import (
    PolicyMissingError,
    PolicyStatus,
    assert_writable,
    ensure_policy_marker,
    resolve_policy,
    write_default_policy,
)
from verinote.pipeline.query import load_query
from verinote.pipeline.question_outcome import question_outcome_view
from verinote.pipeline.ask import ask_question
from verinote.pipeline.acceptance import (
    accept_recommendations,
    accept_recommendations_for,
    apply_auto_accept_recommendations,
)
from verinote.pipeline.report_trace import ReportTrace, report_trace
from verinote.pipeline.corroboration import (
    canonical_relation,
    CorroborationPolicyError,
    merge_default_relation_aliases,
    normalize_typed_value,
    RELATION_ALIASES_RELPATH,
    relation_aliases,
    store_corroboration,
    store_relation_aliases,
    store_single_valued_conflicts,
    store_typed_relations,
)
from verinote.pipeline.workbench import trust_workbench
from verinote.prompts import (
    PromptError,
    delete_prompt_override,
    get_prompt,
    list_prompts,
    save_prompt_override,
)
from verinote.engine.terms import StringLit, render_term
from verinote.store import (
    DEFAULT_REVIEW_PAGE_SIZE,
    REVIEW_PAGE_SIZES,
    ReviewQueuePage,
    Store,
    review_statuses,
)
# Imported as a module, not `from ... import ENGINE_STATUSES`: the tier must be
# read at call time so the web layer cannot pin a stale copy of the constant.
from verinote.store import db as store_db
from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError
from verinote.store.fact_input import structural_term, term_input_kind

logger = logging.getLogger(__name__)

_TEMPLATES = resources.files("verinote.web").joinpath("templates")
_STATIC = resources.files("verinote.web").joinpath("static")

# What is served while a KB's recorded policy file is missing. Default-deny, and
# the allowlist is keyed by (method, path) rather than path alone: a page needed
# to *diagnose* the halt is not licence to *write* under the same prefix. The
# only writes allowed are the ones that leave this KB (switching root), because
# every other write — facts, and policy files like relation-aliases.md — would be
# a change made while this KB's rules are not being applied.
_POLICY_GUARD_READ_PATHS = ("/report", "/settings", "/static")
_POLICY_GUARD_WRITE_PATHS = ("/kb/select", "/settings/root")

# Full-page halt shown when a fact's logical terms cannot be read. htmx partial
# swaps are redirected here (HX-Redirect) because htmx will not swap an error
# response into the DOM -- see `_fact_terms_unreadable_handler`.
FACT_TERMS_UNAVAILABLE_PATH = "/fact-terms-unavailable"


def _matches(path: str, allowed: tuple[str, ...]) -> bool:
    return any(path == a or path.startswith(a + "/") for a in allowed)


def _policy_guard_exempt(method: str, path: str) -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return _matches(path, _POLICY_GUARD_READ_PATHS)
    return path in _POLICY_GUARD_WRITE_PATHS


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg if cfg is not None else Config.load_for_ui()

    app = FastAPI(title="verinote")
    app.state.cfg = cfg
    app.state.store = None
    if cfg is not None:
        store = Store(cfg.db_path)
        store.init_schema()
        ensure_policy_marker(store, cfg.root)
        app.state.store = store

    templates = Jinja2Templates(directory=str(_TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    def _policy_halted(request: Request, message: str):
        return templates.TemplateResponse(
            request,
            "policy_halted.html",
            {"message": message},
            status_code=409,
        )

    @app.middleware("http")
    async def policy_halted_guard(request: Request, call_next):
        """Fail closed while this KB's recorded logic policy is missing.

        A halted KB must not be *written* to either: an accept/reject decision
        taken while the KB's rules are not being applied is a fake review gate,
        and SQLite autocommits the status change long before rendering fails. So
        the guard runs before the route does, and only the recovery paths pass.
        """
        store = app.state.store
        if store is not None and not _policy_guard_exempt(request.method, request.url.path):
            # Same predicate the CLI dispatch and the extraction worker use, so the
            # three enforcement points cannot disagree about what "halted" means.
            try:
                assert_writable(store)
            except PolicyMissingError as exc:
                return _policy_halted(request, str(exc))
        return await call_next(request)

    def _policy_missing_handler(request: Request, exc: Exception):
        """Backstop for *display* only — it cannot prevent a write.

        An exception handler runs after the route body has already run, so a
        route that writes before it reads the policy would commit (SQLite
        autocommits) and still render this page: "looks rejected, actually
        written". Write blocking is the middleware's default-deny above; this
        only guarantees that a route added outside the guard shows the loud page
        instead of a stack trace.
        """
        return _policy_halted(request, str(exc))

    app.add_exception_handler(PolicyMissingError, _policy_missing_handler)

    def _fact_terms_unavailable_page(request: Request):
        # Deliberately generic: DuckDBFactTermStoreError covers a corrupt/unopenable
        # sidecar but also stale/missing-term and malformed-input conditions, so the
        # copy must not diagnose one specific cause it cannot be sure of.
        return templates.TemplateResponse(
            request,
            "sidecar_unreadable.html",
            {},
            status_code=409,
        )

    @app.get(FACT_TERMS_UNAVAILABLE_PATH, response_class=HTMLResponse)
    def fact_terms_unavailable(request: Request):
        # The full-page halt and the HX-Redirect target below. It reads no fact
        # terms, so it still renders while the term store cannot be read.
        return _fact_terms_unavailable_page(request)

    def _fact_terms_unreadable_handler(request: Request, exc: Exception):
        """One loud, non-lying halt for every surface that cannot read a fact's
        logical terms.

        Read routes (`/review`, `/provenance`, `GET /facts/{id}/edit`, `/report`'s
        trace) let `DuckDBFactTermStoreError` propagate here rather than degrading
        a structural fact to a silent `kind="string"`. What reaching this handler
        means on a POST is route-dependent: `amend_fact` refuses in the store
        *before* it commits, so nothing was written; but `accept`/`reject`/`toggle`
        do a bare SQLite status UPDATE that autocommits immediately and only reach
        this handler on the follow-on row re-render, so for those the decision
        already succeeded and merely could not be displayed. This page therefore
        never claims the action was rejected -- only that the terms could not be
        read.

        htmx will NOT swap a 4xx/5xx response into the DOM -- it fires
        `htmx:responseError` and swaps nothing -- so answering an htmx partial
        swap (the edit form, the amend save) with an inline page would be a
        *silent* no-op, the exact failure #173 forbids. For htmx requests we send
        HX-Redirect to force a full-page navigation to the halt page; htmx 2.x
        acts on HX-Redirect regardless of status, so the 409 stays honest.
        Full-page (non-htmx) requests render the halt page inline.
        """
        if request.headers.get("HX-Request") == "true":
            return Response(
                status_code=409,
                headers={"HX-Redirect": FACT_TERMS_UNAVAILABLE_PATH},
            )
        return _fact_terms_unavailable_page(request)

    app.add_exception_handler(
        DuckDBFactTermStoreError, _fact_terms_unreadable_handler
    )

    def _active_store() -> Store:
        store = app.state.store
        if store is None:
            raise RuntimeError("no active KB")
        return store

    def _active_cfg() -> Config:
        cfg = app.state.cfg
        if cfg is None:
            raise RuntimeError("no active KB")
        return cfg

    def _short_error(exc: BaseException) -> str:
        return " ".join(str(exc).split())[:240]

    def _fail_pending_translations(store: Store, cfg: Config, exc: LLMError) -> None:
        reason = _short_error(exc)
        for q in store.questions(pending_only=True):
            store.set_question_query(q["id"], None, "translation_failed", reason)
        write_query_file(store, cfg.root)

    def _extraction_schema_hint(cfg: Config) -> str:
        try:
            return cfg.extraction_schema_hint()
        except PromptError as exc:
            raise LLMError(str(exc)) from exc

    def _relation_aliases_path() -> Path:
        return _active_cfg().root / RELATION_ALIASES_RELPATH

    def _relation_aliases_text() -> str:
        path = _relation_aliases_path()
        if not path.is_file():
            return DEFAULT_RELATION_ALIASES
        text = path.read_text(encoding="utf-8")
        try:
            existing = relation_aliases(text)
        except CorroborationPolicyError:
            return text
        merged = merge_default_relation_aliases(existing)
        missing_defaults = {
            alias: canonical
            for alias, canonical in merged.items()
            if alias not in existing
        }
        if not missing_defaults:
            return text
        missing_text = "\n".join(
            f"- `{alias}` -> `{canonical}`"
            for alias, canonical in sorted(missing_defaults.items())
        )
        return f"{text.rstrip()}\n\n# Default aliases not yet saved in this KB\n{missing_text}\n"

    def _prompts_page(
        request: Request,
        *,
        prompt_id: str = "extraction",
        message: str | None = None,
        error: str | None = None,
        prompt_text: str | None = None,
        status_code: int = 200,
    ):
        cfg = app.state.cfg
        if cfg is None:
            return _kb_select(request)
        try:
            prompt = get_prompt(cfg.root, prompt_id)
        except PromptError as exc:
            return templates.TemplateResponse(
                request,
                "prompts.html",
                {
                    "prompts": list_prompts(),
                    "prompt": None,
                    "selected_prompt": prompt_id,
                    "prompt_text": prompt_text,
                    "message": message,
                    "error": str(exc),
                },
                status_code=400,
            )
        return templates.TemplateResponse(
            request,
            "prompts.html",
            {
                "prompts": list_prompts(),
                "prompt": prompt,
                "selected_prompt": prompt.id,
                "prompt_text": prompt.text if prompt_text is None else prompt_text,
                "message": message,
                "error": error,
            },
            status_code=status_code,
        )

    def _open_root(root: Path) -> None:
        """Point this running app at a KB root, creating it if needed."""
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        next_cfg = Config.for_root(root)
        next_store = Store(next_cfg.db_path)
        next_store.init_schema()

        # Adopt an existing policy file, then scaffold a default one *only* for a
        # KB that never recorded a policy. A KB whose recorded policy file is gone
        # is opened as-is: re-writing the default here would hide the loss, so the
        # KB stays open and /report surfaces the error instead.
        ensure_policy_marker(next_store, root)
        if resolve_policy(next_store).status is PolicyStatus.UNRECORDED_DEFAULT:
            write_default_policy(next_store, root, origin="scaffold")

        save_active_root(root)
        old_store = app.state.store
        if old_store is not None:
            old_store.close()
        app.state.cfg = next_cfg
        app.state.store = next_store

    def _kb_select(request: Request, *, error: str | None = None, status_code: int = 200):
        return templates.TemplateResponse(
            request,
            "kb_select.html",
            {"error": error, "config_path": app_config_path()},
            status_code=status_code,
        )

    def _fact_view(fact):
        if fact is None:
            return None
        store = _active_store()
        view = dict(fact)
        # A raise here (corrupt/unreadable sidecar) must propagate to the shared
        # DuckDBFactTermStoreError handler, not be swallowed: silently treating it
        # as `terms is None` would render a structural fact as a plain string,
        # making genuine corruption indistinguishable from a real string fact.
        terms = store.get_fact_terms(fact["id"])
        if terms is None:
            for field in ("subject", "relation", "object"):
                view[f"{field}_display"] = fact[field]
                view[f"{field}_edit"] = fact[field]
                view[f"{field}_kind"] = "string"
            return view
        for field, term in zip(("subject", "relation", "object"), terms, strict=True):
            view[f"{field}_display"] = render_term(term)
            view[f"{field}_edit"] = term.value if isinstance(term, StringLit) else render_term(term)
            view[f"{field}_kind"] = term_input_kind(term)
        return view

    def _fact_row_context(fact, recommendations=None):
        view = _fact_view(fact)
        trust = fact_trust_summary(_active_store(), int(fact["id"])) if fact else None
        if fact and recommendations is None:
            recommendations = accept_recommendations(_active_store())
        recommendation = recommendations.get(int(fact["id"])) if fact else None
        return {"f": view, "trust": trust, "recommendation": recommendation}

    def _maybe_apply_auto_accept(exclude_fact_ids: tuple[int, ...] = ()) -> list:
        if _active_cfg().auto_accept_recommendations:
            return apply_auto_accept_recommendations(
                _active_store(), exclude_fact_ids=exclude_fact_ids
            )
        return []

    def _row(request: Request, fact):
        # Starlette's current API is TemplateResponse(request, name, context).
        return templates.TemplateResponse(
            request, "partials/fact_row.html", _fact_row_context(fact)
        )

    def _row_after_decision(
        request: Request,
        fact,
        acted_fact_id: int | None,
        *,
        rule_may_act: bool = True,
        decided: bool = True,
    ):
        """Render the acted row, running auto-accept for the corroboration it
        may have unblocked.

        A human decision changes the corroboration landscape, so re-run the
        recommender here just as the extraction worker does. When it promotes
        *other* facts, a single-row HTMX swap can't reveal them, so ask the
        client for a full refresh; when nothing (or only the acted fact) moved,
        the row swap is enough. The acted row is re-read afterwards so it
        reflects an auto-accept that landed on the fact itself.

        `decided=False` means the POST changed nothing — a replayed accept on an
        already-confirmed fact, a toggle a reject beat to the row. The follow-on
        pass is owed to a *transition*, not to a request arriving: with no new
        human decision there is no newly unblocked corroboration, and running the
        rule anyway would promote siblings and stamp `auto_accepted` audit events
        off a click the user may have made hours ago (or never made — HTMX and
        browsers both retry). Such a request only re-renders the row.

        `rule_may_act=False` bars the rule from the acted fact while still
        letting it promote everything else — for the one decision that parks a
        fact back in the tier auto-accept harvests from (see `toggle`).
        """
        excluded = () if rule_may_act or acted_fact_id is None else (acted_fact_id,)
        applied = _maybe_apply_auto_accept(excluded) if decided else []
        if acted_fact_id is not None:
            refreshed = _active_store().get_fact(acted_fact_id)
            if refreshed is not None:
                fact = refreshed
        response = _row(request, fact)
        if any(rec.fact_id != acted_fact_id for rec in applied):
            response.headers["HX-Refresh"] = "true"
        return response

    def _fact_edit_context(fact, *, error: str | None = None):
        kinds = {"subject": "string", "relation": "string", "object": "string"}
        if fact is not None:
            store = _active_store()
            terms = store.get_fact_terms(fact["id"])
            if terms is not None:
                kinds = {
                    "subject": term_input_kind(terms[0]),
                    "relation": term_input_kind(terms[1]),
                    "object": term_input_kind(terms[2]),
                }
        return {"f": _fact_view(fact), "kinds": kinds, "error": error}

    def _fact_input(value: str, kind: str):
        if kind == "string":
            return value
        if kind == "term":
            return structural_term(value)
        raise ValueError(f"unknown fact input kind: {kind}")

    def _review_filters() -> list[tuple[str, str]]:
        return [
            ("needs-human-decision", "Needs decision"),
            ("unsupported", "Unsupported"),
            ("single-source", "Single source"),
            ("corroborated", "Corroborated"),
            ("conflicted", "Conflicted"),
        ]

    def _active_review_filter(active_filter: str) -> str:
        labels = {key for key, _ in _review_filters()}
        return active_filter if active_filter in labels else "needs-human-decision"

    def _review_url(
        *,
        active_filter: str,
        sort: str,
        page_size: int,
        page: int = 1,
    ) -> str:
        return "/review?" + urlencode(
            {
                "filter": active_filter,
                "sort": sort,
                "page_size": page_size,
                "page": page,
            }
        )

    def _review_filter_links(active_filter: str, sort: str, page_size: int):
        return [
            {
                "key": key,
                "label": label,
                "href": _review_url(
                    active_filter=key,
                    sort=sort,
                    page_size=page_size,
                    page=1,
                ),
                "active": active_filter == key,
            }
            for key, label in _review_filters()
        ]

    def _review_pages(active_filter: str, sort: str, page_size: int, page: int, page_count: int):
        candidates = {1, page_count}
        for nearby in range(page - 2, page + 3):
            if 1 <= nearby <= page_count:
                candidates.add(nearby)
        pages = []
        last = 0
        for number in sorted(candidates):
            if last and number > last + 1:
                pages.append({"ellipsis": True})
            pages.append(
                {
                    "number": number,
                    "active": number == page,
                    "href": _review_url(
                        active_filter=active_filter,
                        sort=sort,
                        page_size=page_size,
                        page=number,
                    ),
                }
            )
            last = number
        return pages

    def _review_pager(active_filter: str, page_data):
        page_count = page_data.page_count
        page = page_data.page
        return {
            "total": page_data.total,
            "start": page_data.start,
            "end": page_data.end,
            "page": page,
            "page_size": page_data.page_size,
            "page_count": page_count,
            "sort": page_data.sort,
            "page_sizes": REVIEW_PAGE_SIZES,
            "sort_options": [
                ("newest", "Newest"),
                ("oldest", "Oldest"),
                ("updated", "Recently updated"),
                ("confidence", "Confidence"),
                ("source", "Source"),
            ],
            "prev_href": (
                _review_url(
                    active_filter=active_filter,
                    sort=page_data.sort,
                    page_size=page_data.page_size,
                    page=page - 1,
                )
                if page > 1
                else None
            ),
            "next_href": (
                _review_url(
                    active_filter=active_filter,
                    sort=page_data.sort,
                    page_size=page_data.page_size,
                    page=page + 1,
                )
                if page < page_count
                else None
            ),
            "pages": _review_pages(
                active_filter, page_data.sort, page_data.page_size, page, page_count
            ),
        }

    def _review_page(store: Store, active_filter: str, page: str, page_size: str, sort: str):
        if active_filter == "needs-human-decision":
            return store.review_queue_page(page=page, page_size=page_size, sort=sort)
        label = active_filter.replace("-", "_")
        matching_ids = [
            fact_id
            for fact_id in store.review_queue_ids(sort=sort)
            if (summary := fact_trust_summary(store, fact_id)) is not None
            and label in summary.trust_labels
        ]
        page_data = ReviewQueuePage.from_rows(
            rows=[],
            total=len(matching_ids),
            page=page,
            page_size=page_size,
            sort=sort,
        )
        start = (page_data.page - 1) * page_data.page_size
        rows = store.facts_by_ids(matching_ids[start : start + page_data.page_size])
        return ReviewQueuePage.from_rows(
            rows=rows,
            total=len(matching_ids),
            page=page_data.page,
            page_size=page_data.page_size,
            sort=page_data.sort,
        )

    def _source_inspector_rows(store: Store) -> list[dict[str, object]]:
        facts = store.facts()
        trust_rollup = _source_trust_rollup(store, facts)
        rows = []
        for source in store.sources_with_counts():
            row = dict(source)
            source_id = int(source["id"])
            counts = trust_rollup.get(
                source_id,
                {"unsupported": 0, "conflicted": 0, "corroborated": 0},
            )
            row["unsupported_count"] = counts["unsupported"]
            row["conflicted_count"] = counts["conflicted"]
            row["corroborated_count"] = counts["corroborated"]
            row["evidence_snippets"] = store.source_evidence_snippets(source_id)
            row["artifacts"] = [dict(artifact) for artifact in store.source_artifacts(source_id)]
            row["failed_chunk_details"] = []
            row["pending_chunks"] = 0
            if source["job_id"]:
                chunks = store.source_chunks(int(source["job_id"]))
                row["failed_chunk_details"] = [
                    dict(chunk) for chunk in chunks if chunk["status"] == "failed"
                ]
                row["pending_chunks"] = sum(
                    1 for chunk in chunks if chunk["status"] in {"pending", "running"}
                )
            rows.append(row)
        return rows

    def _source_trust_rollup(store: Store, facts) -> dict[int, dict[str, int]]:
        aliases = store_relation_aliases(store)
        typed = store_typed_relations(store)
        support_sources: dict[tuple[str, str, tuple[str, object]], set[str]] = {}
        for fact in facts:
            if str(fact["status"]) not in store_db.ENGINE_STATUSES:
                continue
            source_path = str(fact["source_path"] or "").strip()
            if not source_path:
                continue
            relation = canonical_relation(str(fact["relation"]), aliases)
            support_sources.setdefault(
                (
                    str(fact["subject"]),
                    relation,
                    _source_object_key(relation, str(fact["object"]), typed),
                ),
                set(),
            ).add(source_path)

        conflict_keys = {
            (conflict.subject, conflict.relation)
            for conflict in store_single_valued_conflicts(store)
        }
        counts: dict[int, dict[str, int]] = {}
        for fact in facts:
            if fact["source_id"] is None:
                continue
            source_id = int(fact["source_id"])
            bucket = counts.setdefault(
                source_id,
                {"unsupported": 0, "conflicted": 0, "corroborated": 0},
            )
            relation = canonical_relation(str(fact["relation"]), aliases)
            support_count = len(
                support_sources.get(
                    (
                        str(fact["subject"]),
                        relation,
                        _source_object_key(relation, str(fact["object"]), typed),
                    ),
                    set(),
                )
            )
            if support_count == 0:
                bucket["unsupported"] += 1
            elif support_count > 1:
                bucket["corroborated"] += 1
            if (str(fact["subject"]), relation) in conflict_keys:
                bucket["conflicted"] += 1
        return counts

    def _source_object_key(relation: str, obj: str, typed) -> tuple[str, object]:
        spec = typed.get(relation) or typed.get(unicodedata.normalize("NFC", relation))
        if spec is not None:
            scalar = normalize_typed_value(spec.type, obj, spec.units)
            if scalar is not None:
                return ("scalar", scalar)
        return ("raw", obj)

    def _dashboard_queues(store: Store) -> list[dict[str, object]]:
        review_summaries = [
            fact_trust_summary(store, int(fact["id"])) for fact in store.review_queue()
        ]
        review_summaries = [summary for summary in review_summaries if summary is not None]
        jobs = store.source_extraction_jobs()
        workbench = trust_workbench(store)
        corroboration = store_corroboration(store)
        recent_lifecycle = store.count_facts_with_events(("amended", "reanalyzed"))
        return [
            {
                "label": "Unsupported review items",
                "count": sum(1 for summary in review_summaries if "unsupported" in summary.trust_labels),
                "href": "/review?filter=unsupported",
                "detail": "candidate facts without deterministic source support",
            },
            {
                "label": "Corroborated review targets",
                "count": sum(1 for summary in review_summaries if "corroborated" in summary.trust_labels),
                "href": "/review?filter=corroborated",
                "detail": "review items backed by repeated source support",
            },
            {
                "label": "Single-valued conflicts",
                "count": len(workbench.conflicts),
                "href": "/workbench",
                "detail": "accepted/confirmed values competing under functional rules",
            },
            {
                "label": "Failed source analyses",
                "count": sum(1 for job in jobs if job["status"] == "failed"),
                "href": "/sources",
                "detail": "sources with failed extraction chunks ready for retry",
            },
            {
                "label": "Recent lifecycle changes",
                "count": int(recent_lifecycle),
                "href": "/review",
                "detail": "facts amended or reanalyzed after extraction",
            },
            {
                "label": "Source-backed engine facts",
                "count": len(corroboration),
                "href": "/workbench",
                "detail": "accepted/confirmed facts with source support",
            },
        ]

    def _dashboard(request: Request, *, error: str | None = None, status_code: int = 200):
        from verinote.engine import coverage

        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        store = _active_store()
        cfg = _active_cfg()
        counts = store.status_counts()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "counts": counts,
                "total": sum(counts.values()),
                # Derived here, not summed in the template: the dashboard's
                # "engine input" card must answer the same question as coverage
                # and the Sources badge, from the same constant.
                "engine_input": sum(
                    counts.get(status, 0) for status in store_db.ENGINE_STATUSES
                ),
                "sources": store.sources(),
                "coverage": coverage(store, root=cfg.root),
                "corroboration": store_corroboration(store),
                "single_valued_conflicts": store_single_valued_conflicts(store),
                "queues": _dashboard_queues(store),
                "provider": app.state.cfg.provider,
                "provider_label": PROVIDER_LABELS.get(
                    app.state.cfg.provider, app.state.cfg.provider
                ),
                "model": app.state.cfg.model,
                "root": app.state.cfg.root,
                "error": error,
            },
            status_code=status_code,
        )

    def _sources(request: Request, *, error: str | None = None, status_code: int = 200):
        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        store = _active_store()
        jobs = store.source_extraction_jobs()
        latest_job_ids = latest_source_job_ids(jobs)
        # A superseded `pending` row is dead work, and counting it here is what
        # leaves the page polling every 2s forever and claiming an analysis is in
        # flight when nothing is processing it.
        has_running_jobs = any(is_live_extraction_job(job, latest_job_ids) for job in jobs)
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "sources": _source_inspector_rows(store),
                "suffixes": ", ".join(sorted(supported_suffixes())),
                "accept": ",".join(sorted(supported_suffixes())),
                "error": error,
                "jobs": jobs,
                "has_running_jobs": has_running_jobs,
                "chunk_chars": app.state.cfg.extraction_chunk_chars,
                "max_facts_per_chunk": app.state.cfg.extraction_max_facts_per_chunk,
            },
            status_code=status_code,
        )

    def _start_source_extraction(
        job_id: int,
        cfg: Config,
        *,
        retry: bool = False,
        retry_max_attempts: int | None = None,
    ) -> None:
        def run() -> None:
            try:
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    client = get_client(cfg)
                    result = process_extraction_job(
                        worker_store,
                        client,
                        job_id=job_id,
                        schema_hint=_extraction_schema_hint(cfg),
                        retry=retry,
                        retry_max_attempts=retry_max_attempts,
                    )
                    # A clean run judges staleness: a confirmed/accepted citation
                    # whose source text changed under it returns to review (#329).
                    # The sweep is a SIBLING of `process_extraction_job`, not folded
                    # inside it, for separation of concerns: extraction stays a pure
                    # primitive, while the sweep's return value (the demoted fact
                    # ids) is needed HERE to thread into `exclude_fact_ids` below.
                    # Sibling placement does not by itself avoid the outer `except
                    # Exception -> fail_extraction_job` — this call still sits in the
                    # same try — so the local guard below is what actually keeps a
                    # sweep error from retroactively flipping an already-`done` job
                    # to `failed`. `assert_writable` runs first (and OUTSIDE that
                    # guard) so a policy that vanished post-completion routes to the
                    # PolicyMissingError handler instead of demoting facts against a
                    # halted KB (#194) — the store layer trusts its caller for this,
                    # exactly as `process_extraction_job` and auto-accept do.
                    demoted_ids: tuple[int, ...] = ()
                    if result.failed_chunks == 0:
                        assert_writable(worker_store)
                        try:
                            demoted_ids = tuple(
                                int(row["id"])
                                for row in worker_store.surface_stale_engine_facts(job_id)
                            )
                        except Exception:  # noqa: BLE001 - a sweep error must not fail a done job
                            # The sweep does no LLM/network I/O, so a raise here is a
                            # rare sqlite/WAL-lock-class error. Contain it: the
                            # extraction genuinely succeeded, so leave the job `done`
                            # and take no demotions this pass rather than letting the
                            # outer handler bury a completed run as `failed`.
                            logger.warning(
                                "stale-citation sweep failed for job %s; leaving it done",
                                job_id,
                                exc_info=True,
                            )
                    if cfg.auto_accept_recommendations:
                        # Exclude just-demoted facts so THIS request's auto-accept
                        # pass can't demote-then-immediately-re-promote them. Part
                        # C's `stale` flag is what blocks re-promotion on later
                        # syncs; this only ever covered the same-pass case.
                        try:
                            apply_auto_accept_recommendations(
                                worker_store, exclude_fact_ids=demoted_ids
                            )
                        except PolicyMissingError:
                            # ORDER IS LOAD-BEARING — this must stay ABOVE `except
                            # Exception`. Auto-accept runs `assert_writable` as its
                            # own first act (acceptance.py); a policy lost
                            # post-completion is a #194 halt that must reach the
                            # outer PolicyMissingError handler (which writes
                            # NOTHING), never be contained here as if it were an
                            # ordinary failure.
                            raise
                        except Exception:  # noqa: BLE001 - an auto-accept error must not fail a done job
                            # Auto-accept does no LLM/network I/O, so a raise here is
                            # a rare sqlite/WAL-lock-class error. The extraction
                            # genuinely succeeded and its facts are already
                            # committed; leave the job `done` rather than letting the
                            # outer handler bury a completed run as `failed` (#340;
                            # sibling of the #329 sweep guard directly above).
                            logger.warning(
                                "auto-accept failed for job %s; leaving it done",
                                job_id,
                                exc_info=True,
                            )
            except PolicyMissingError as e:
                # ORDER IS LOAD-BEARING — this must stay ABOVE `except Exception`.
                # The worker runs outside the request middleware, so a halt surfaces
                # here as an ordinary exception; the generic handler below would
                # "report" it by calling `fail_extraction_job` — a WRITE to the very
                # KB the halt exists to protect, and one that buries the job in a
                # `failed` state nothing resumes. So this handler writes NOTHING and
                # only logs. (#194)
                #
                # It catches halts from three places, and they leave the job in
                # different states: `process_extraction_job` has already rolled a
                # mid-job halt back to `pending`, while the pre-sweep
                # `assert_writable` and `apply_auto_accept_recommendations` both halt
                # *after* the job finished `done`, with no rollback at all. The
                # message must therefore not assert a rollback
                # — a log line claiming one for a `done` job would be the same class
                # of falsehood this change removes. Whoever rewinds, rewinds; this
                # handler reports.
                logger.warning("extraction job %s halted (KB policy missing): %s", job_id, e)
            except ExtractionJobBusyError:
                # Another worker owns this job (a concurrent sync, a second UI
                # worker, or another startup resume). It may have a chunk in
                # flight; ANY write here — including `fail_extraction_job` — would
                # corrupt a job we do not own. Log and leave it entirely. (#240)
                logger.info(
                    "extraction job %s already owned by another worker; not started here",
                    job_id,
                )
            except LLMError as e:
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    worker_store.fail_extraction_job(job_id, f"extraction failed: {e}")
            except Exception as e:  # noqa: BLE001 - keep background failures visible in UI
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    worker_store.fail_extraction_job(job_id, f"analysis failed: {e}")

        threading.Thread(
            target=run,
            name=f"verinote-source-extract-{job_id}",
            daemon=True,
        ).start()

    def _source_file_path(source_path: str, root: Path) -> Path:
        path = (root / source_path).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as e:
            raise OSError(f"refusing to delete source outside KB root: {source_path}") from e
        return path

    def _delete_source_file(source_path: str, root: Path) -> None:
        path = _source_file_path(source_path, root)
        if path.is_file():
            path.unlink()

    def _delete_source_files(paths: set[str], root: Path) -> None:
        for path in paths:
            _source_file_path(path, root)
        for path in sorted(paths):
            _delete_source_file(path, root)

    def _resume_source_extraction_jobs() -> None:
        """Revive interrupted extraction jobs — but never on a halted KB.

        This runs at `create_app()` time, *outside* the request middleware, so the
        middleware's guard does not cover it: before this gate existed, merely
        launching `verinote ui` against a halted KB with a pending job wrote to it
        (the worker raised, and `except Exception` "helpfully" marked the job
        `failed`) — a write to a halted KB with zero HTTP requests made (#194).

        Same predicate as the middleware and the CLI dispatch: one judgement,
        three enforcement points.

        A job left `running` by a crash is rolled back to `pending` before it is
        restarted, because `process_extraction_job` now claims only a `pending`
        job (#240). SCOPE BOUNDARY (#242): DB state alone cannot tell a crashed
        zombie from a job a DIFFERENT live process genuinely owns — SQLite has no
        row-level liveness signal — so in that rare case this rollback still
        resets that live job's in-flight chunk (exactly as today's unconditional
        resume already does; not a regression introduced here). Closing it needs a
        liveness lease (owner token + heartbeat, or a staleness threshold on
        `updated_at`) and is filed as a follow-up, not solved here.
        """
        if app.state.store is None or app.state.cfg is None:
            return
        try:
            assert_writable(app.state.store)
        except PolicyMissingError as exc:
            logger.warning("not resuming extraction jobs: %s", exc)
            return
        jobs = app.state.store.source_extraction_jobs()
        latest_job_ids = latest_source_job_ids(jobs)
        for job in jobs:
            if not is_live_extraction_job(job, latest_job_ids):
                continue
            if job["status"] == "running":
                app.state.store.rollback_extraction_job(
                    int(job["id"]), "Resuming analysis interrupted by a restart."
                )
            _start_source_extraction(int(job["id"]), app.state.cfg)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return _dashboard(request)

    @app.post("/kb/select", response_class=HTMLResponse)
    def select_kb(request: Request, root: str = Form(...)):
        try:
            _open_root(Path(root))
        except OSError as e:
            return _kb_select(request, error=f"could not open KB: {e}", status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        return _sources(request)

    @app.post("/sources", response_class=HTMLResponse)
    async def upload_source(request: Request, file: UploadFile = File(...)):
        store = _active_store()
        cfg = _active_cfg()
        filename = Path(file.filename or "").name
        raw = await file.read()
        try:
            text, kind = ingest_bytes(raw, filename)
        except IngestError as e:
            return _sources(request, error=str(e), status_code=400)

        result = store_source(store, cfg.root, filename, raw, text, kind)
        source = store.get_source_by_path(result["citation"])
        if source is None:
            return _sources(
                request,
                error=f"source registration failed: {result['citation']}",
                status_code=500,
            )
        job_id = create_chunked_extraction_job(
            store,
            source_id=int(source["id"]),
            artifact_id=int(result["artifact_id"]),
            source_text=text,
            provider=cfg.provider,
            model=cfg.model,
            chunk_chars=cfg.extraction_chunk_chars,
            chunk_overlap_chars=cfg.extraction_chunk_overlap_chars,
        )
        _start_source_extraction(job_id, app.state.cfg)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/jobs/{job_id}/retry", response_class=HTMLResponse)
    def retry_source_job(request: Request, job_id: int):
        # The atomic claim-for-retry inside the worker resets the failed chunks AND
        # takes ownership in one locked step, so a concurrent `verinote sync`
        # auto-retry on the same job_id cannot collide: whoever wins the CAS owns
        # it and the loser backs off via ExtractionJobBusyError (handled in the
        # worker above). `retry_max_attempts=None` makes this a human override that
        # resets EVERY failed chunk regardless of attempt count, unlike the capped
        # auto-retry — the escape hatch for a job the sync loop has given up on (#323).
        _start_source_extraction(
            job_id, _active_cfg(), retry=True, retry_max_attempts=None
        )
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/reanalyze", response_class=HTMLResponse)
    def reanalyze_source(request: Request, source_id: int):
        store = _active_store()
        cfg = _active_cfg()
        source = store.get_source(source_id)
        if source is None:
            return _sources(request, error="source not found", status_code=404)
        jobs = store.source_extraction_jobs()
        latest_job_ids = latest_source_job_ids(jobs)
        # Only a LIVE job blocks re-analysis. A superseded `pending` row is not an
        # analysis in progress, and treating it as one wedges this button shut for
        # the one source whose analysis most needs redoing.
        if any(
            int(job["source_id"]) == source_id
            and is_live_extraction_job(job, latest_job_ids)
            for job in jobs
        ):
            return _sources(
                request,
                error=f"analysis already running for {source['path']}",
                status_code=409,
            )
        artifact = store.latest_source_text_artifact(source_id)
        if artifact is None:
            return _sources(
                request,
                error=f"source has no extraction text artifact: {source['path']}",
                status_code=400,
            )
        try:
            artifact_path = _source_file_path(str(artifact["path"]), cfg.root)
            source_text = artifact_path.read_text(encoding="utf-8")
        except OSError as e:
            return _sources(
                request,
                error=f"could not read source artifact: {e}",
                status_code=500,
            )

        store.clear_source_analysis(source_id)
        job_id = create_chunked_extraction_job(
            store,
            source_id=source_id,
            artifact_id=int(artifact["id"]),
            source_text=source_text,
            provider=cfg.provider,
            model=cfg.model,
            chunk_chars=cfg.extraction_chunk_chars,
            chunk_overlap_chars=cfg.extraction_chunk_overlap_chars,
        )
        _start_source_extraction(job_id, cfg)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/accept-all", response_class=HTMLResponse)
    def accept_all_source_facts(request: Request, source_id: int):
        accepted = _active_store().accept_review_facts_for_source(source_id)
        # Bulk-confirming a source can corroborate facts elsewhere; the redirect
        # reloads the page so no HX-Refresh header is needed here. `accepted` is
        # the same transition test the single-fact routes apply: a source with
        # nothing left in the review tier confirms nothing, so the POST decided
        # nothing and the rule stays out of it.
        if accepted:
            _maybe_apply_auto_accept()
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/delete", response_class=HTMLResponse)
    def delete_source(request: Request, source_id: int):
        store = _active_store()
        cfg = _active_cfg()
        source = store.get_source(source_id)
        paths = {source["path"]} if source is not None else set()
        paths.update(row["path"] for row in store.source_artifacts(source_id))
        try:
            for path in paths:
                _source_file_path(path, cfg.root)
        except OSError as e:
            return _sources(request, error=f"source removal failed: {e}", status_code=500)
        source = store.delete_source(source_id)
        if source is not None:
            try:
                _delete_source_files(paths, cfg.root)
            except OSError as e:
                return _sources(
                    request,
                    error=f"source deleted, but file removal failed: {e}",
                    status_code=500,
                )
        return RedirectResponse("/sources", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    def review(
        request: Request,
        filter: str = "needs-human-decision",
        page: str = "1",
        page_size: str = str(DEFAULT_REVIEW_PAGE_SIZE),
        sort: str = "newest",
    ):
        store = _active_store()
        active_filter = _active_review_filter(filter)
        page_data = _review_page(store, active_filter, page, page_size, sort)
        recommendations = accept_recommendations_for(
            store, [int(f["id"]) for f in page_data.rows]
        )
        rows = [_fact_row_context(f, recommendations) for f in page_data.rows]
        return templates.TemplateResponse(
            request,
            "review.html",
            {
                "queue": rows,
                "active_filter": active_filter,
                "filters": _review_filter_links(
                    active_filter, page_data.sort, page_data.page_size
                ),
                "pager": _review_pager(active_filter, page_data),
            },
        )

    @app.get("/workbench", response_class=HTMLResponse)
    def workbench(request: Request):
        return templates.TemplateResponse(
            request,
            "workbench.html",
            {"workbench": trust_workbench(_active_store())},
        )

    @app.post("/facts/{fact_id}/toggle", response_class=HTMLResponse)
    def toggle(request: Request, fact_id: int):
        toggled = _active_store().toggle_review(fact_id)
        # A demotion parks the fact in exactly the tier auto-accept promotes
        # from, so an unrestricted pass would undo the user's click inside their
        # own request. The demotion is the decision; the rule may act on the
        # siblings it unblocks, but not on this fact.
        demoted = (
            toggled.changed
            and toggled.fact is not None
            and toggled.fact["status"] in review_statuses()
        )
        return _row_after_decision(
            request,
            toggled.fact,
            fact_id,
            rule_may_act=not demoted,
            decided=toggled.changed,
        )

    @app.post("/facts/{fact_id}/accept", response_class=HTMLResponse)
    def accept(request: Request, fact_id: int):
        accepted = _active_store().accept_fact(fact_id)
        return _row_after_decision(
            request, accepted.fact, fact_id, decided=accepted.changed
        )

    @app.post("/facts/{fact_id}/reject", response_class=HTMLResponse)
    def reject(request: Request, fact_id: int):
        # Reject runs auto-accept too: removing a fact's support (or freeing a
        # single-valued slot it conflicted on) also reshapes corroboration, so
        # keeping the trigger here matches the other decision routes.
        rejected = _active_store().reject_fact(fact_id)
        return _row_after_decision(
            request, rejected.fact, fact_id, decided=rejected.changed
        )

    @app.get("/facts/{fact_id}/edit", response_class=HTMLResponse)
    def edit_fact(request: Request, fact_id: int):
        return templates.TemplateResponse(
            request,
            "partials/fact_edit.html",
            _fact_edit_context(_active_store().get_fact(fact_id)),
        )

    @app.get("/facts/{fact_id}/row", response_class=HTMLResponse)
    def fact_row(request: Request, fact_id: int):
        # Re-render the read-only row (used to cancel an inline edit).
        return _row(request, _active_store().get_fact(fact_id))

    @app.post("/facts/{fact_id}/amend", response_class=HTMLResponse)
    def amend_fact(
        request: Request,
        fact_id: int,
        subject: str = Form(...),
        relation: str = Form(...),
        object: str = Form(...),
        subject_kind: str = Form("string"),
        relation_kind: str = Form("string"),
        object_kind: str = Form("string"),
        note: str = Form(""),
    ):
        try:
            subject_value = _fact_input(subject, subject_kind)
            relation_value = _fact_input(relation, relation_kind)
            object_value = _fact_input(object, object_kind)
        except ValueError as e:
            return templates.TemplateResponse(
                request,
                "partials/fact_edit.html",
                _fact_edit_context(_active_store().get_fact(fact_id), error=str(e)),
                status_code=400,
            )
        amended = _active_store().amend_fact(
            fact_id,
            subject=subject_value,
            relation=relation_value,
            obj=object_value,
            note=note,
        )
        # The rule may act on the amended fact itself, unlike a toggle demotion.
        # An amend decides the fact's content, not its tier: correcting a term so
        # it finally matches a second source's wording *is* corroboration
        # arriving, and promoting on it is the recommender working as intended.
        return _row_after_decision(
            request, amended.fact, fact_id, decided=amended.changed
        )

    @app.get("/facts/{fact_id}/provenance", response_class=HTMLResponse)
    def provenance(request: Request, fact_id: int):
        store = _active_store()
        fact = store.get_fact(fact_id)
        trust = fact_trust_summary(store, fact_id) if fact else None
        run = store.get_run(fact["run_id"]) if fact and fact["run_id"] else None
        job = (
            store.get_extraction_job_detail(fact["job_id"])
            if fact and fact["job_id"]
            else None
        )
        return templates.TemplateResponse(
            request,
            "provenance.html",
            {
                "f": _fact_view(fact),
                "trust": trust,
                "run": run,
                "job": job,
                "log": store.fact_log(fact_id) if fact else [],
            },
        )

    def _questions(request: Request, *, error: str | None = None, status_code: int = 200):
        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        store = _active_store()
        rep = verify(store)
        page_error = error
        if page_error is None:
            # Ask the thing that owns the answer, never the report's prose: a
            # finding string is human-readable output, not a state field. (The
            # missing-policy state is handled by the guard, which never routes
            # here.) The query policy's own error type is what surfaces below.
            try:
                load_query(store)
            except CorroborationPolicyError as exc:
                page_error = f"policy error: {exc}"
        return templates.TemplateResponse(
            request,
            "questions.html",
            {
                "questions": [question_outcome_view(q) for q in store.questions()],
                "answers": rep.answers,
                "error": page_error,
            },
            status_code=status_code,
        )

    @app.get("/questions", response_class=HTMLResponse)
    def questions_page(request: Request):
        return _questions(request)

    def _ask(
        request: Request,
        *,
        question: str = "",
        result=None,
        error: str | None = None,
        status_code: int = 200,
    ):
        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        return templates.TemplateResponse(
            request,
            "ask.html",
            {"question": question, "result": result, "error": error},
            status_code=status_code,
        )

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request):
        return _ask(request)

    @app.post("/ask", response_class=HTMLResponse)
    def ask_submit(request: Request, question: str = Form(...)):
        store = _active_store()
        cfg = _active_cfg()
        try:
            client = get_client(app.state.cfg)
        except LLMError as e:
            return _ask(request, question=question, error=f"ask failed: {e}", status_code=502)
        result = ask_question(store, client, root=cfg.root, question=question)
        return _ask(request, question=question, result=result)

    @app.post("/questions", response_class=HTMLResponse)
    def add_question(request: Request, text: str = Form(...)):
        _active_store().add_question(text)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/{question_id}/delete", response_class=HTMLResponse)
    def delete_question(request: Request, question_id: int):
        store = _active_store()
        store.delete_question(question_id)
        write_query_file(store, _active_cfg().root)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/translate", response_class=HTMLResponse)
    def translate(request: Request):
        store = _active_store()
        cfg = _active_cfg()
        try:
            client = get_client(app.state.cfg)
        except LLMError as e:
            _fail_pending_translations(store, cfg, e)
            return RedirectResponse("/questions", status_code=303)
        translate_questions(store, client, root=cfg.root)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/repair", response_class=HTMLResponse)
    def repair(request: Request):
        store = _active_store()
        cfg = _active_cfg()
        try:
            client = get_client(app.state.cfg)
        except LLMError as e:
            return _questions(request, error=f"repair failed: {e}", status_code=502)
        repair_questions(store, client, root=cfg.root)
        return RedirectResponse("/questions", status_code=303)

    @app.get("/report", response_class=HTMLResponse)
    def report(request: Request):
        store = _active_store()
        rep = verify(store)
        try:
            trace = report_trace(store)
        except PolicyMissingError:
            # The report itself already carries the policy_missing error; the
            # trace needs the same (lost) policy, so it has nothing to say.
            trace = ReportTrace(
                answers=(), excluded_review_count=0, excluded_by_status=()
            )
        return templates.TemplateResponse(
            request,
            "report.html",
            {"rep": rep, "trace": trace},
        )

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request):
        from verinote.store.analytics import compute

        return templates.TemplateResponse(request, "analytics.html", {"a": compute(_active_cfg().db_path)})

    def _settings(request: Request, *, test_result=None, error=None, status_code=200):
        c = app.state.cfg
        if c is None:
            return _kb_select(request)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "providers": PROVIDERS,
                "provider_labels": PROVIDER_LABELS,
                "provider": c.provider,
                "provider_label": PROVIDER_LABELS.get(c.provider, c.provider),
                "model": c.model,
                "base_url": c.base_url or "",
                "extraction_chunk_chars": c.extraction_chunk_chars,
                "extraction_chunk_overlap_chars": c.extraction_chunk_overlap_chars,
                "extraction_max_facts_per_chunk": c.extraction_max_facts_per_chunk,
                "auto_accept_recommendations": c.auto_accept_recommendations,
                "root": c.root,
                "has_key": bool(c.api_key),  # never render the key itself
                "connection_test_enabled": c.provider in TESTABLE_PROVIDERS,
                "relation_aliases": _relation_aliases_text(),
                "test_result": test_result,
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return _settings(request)

    @app.get("/prompts", response_class=HTMLResponse)
    def prompts_page(request: Request, prompt: str = "extraction"):
        return _prompts_page(request, prompt_id=prompt)

    @app.post("/prompts", response_class=HTMLResponse)
    def save_prompt_route(
        request: Request,
        prompt_id: str = Form(...),
        prompt_text: str = Form(""),
    ):
        cfg = _active_cfg()
        try:
            save_prompt_override(cfg.root, prompt_id, prompt_text)
        except PromptError as exc:
            return _prompts_page(
                request,
                prompt_id=prompt_id,
                prompt_text=prompt_text,
                error=str(exc),
                status_code=400,
            )
        return RedirectResponse(f"/prompts?{urlencode({'prompt': prompt_id})}", status_code=303)

    @app.post("/prompts/reset", response_class=HTMLResponse)
    def reset_prompt_route(request: Request, prompt_id: str = Form(...)):
        cfg = _active_cfg()
        try:
            delete_prompt_override(cfg.root, prompt_id)
        except PromptError as exc:
            return _prompts_page(
                request,
                prompt_id=prompt_id,
                error=str(exc),
                status_code=400,
            )
        return RedirectResponse(f"/prompts?{urlencode({'prompt': prompt_id})}", status_code=303)

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings_route(
        request: Request,
        provider: str = Form(...),
        model: str = Form(""),
        base_url: str = Form(""),
        extraction_chunk_chars: int = Form(300),
        extraction_chunk_overlap_chars: int = Form(40),
        extraction_max_facts_per_chunk: int = Form(8),
        auto_accept_recommendations: str | None = Form(None),
    ):
        cfg = _active_cfg()
        save_settings(
            cfg.root,
            provider=provider,
            model=model,
            base_url=base_url or None,
            extraction_chunk_chars=extraction_chunk_chars,
            extraction_chunk_overlap_chars=extraction_chunk_overlap_chars,
            extraction_max_facts_per_chunk=extraction_max_facts_per_chunk,
            auto_accept_recommendations=auto_accept_recommendations == "on",
        )
        # reload from the app's own root so the change takes effect on next sync
        app.state.cfg = Config.for_root(cfg.root)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/relation-aliases", response_class=HTMLResponse)
    def save_relation_aliases(request: Request, relation_aliases_text: str = Form("")):
        text = relation_aliases_text.strip()
        try:
            relation_aliases(text)
        except CorroborationPolicyError as e:
            return _settings(request, error=str(e), status_code=400)
        path = _relation_aliases_path()
        if text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/root", response_class=HTMLResponse)
    def switch_root(request: Request, root: str = Form(...)):
        path = root.strip()
        if not path:
            return _settings(request, error="KB directory is required", status_code=400)
        try:
            _open_root(Path(path))
        except OSError as e:
            return _settings(request, error=f"could not open KB directory: {e}", status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/test", response_class=HTMLResponse)
    def test_connection(request: Request):
        c = app.state.cfg
        if c is None:
            return _kb_select(request)
        if c.provider not in TESTABLE_PROVIDERS:
            return _settings(
                request,
                error="Connection test is not available for this provider.",
                status_code=400,
            )
        try:
            client = get_client(c)
            facts = client.extract_facts(
                source_text="verinote connection test: Ada Lovelace is a mathematician."
            )
        except LLMError as e:
            return _settings(request, error=f"connection failed: {e}", status_code=502)
        return _settings(
            request,
            test_result=f"{client.name} answered with {len(facts)} fact(s) from {c.model}",
        )

    _resume_source_extraction_jobs()

    return app


# Module-level app for `uvicorn verinote.web.app:app`.
def _default() -> FastAPI:  # pragma: no cover - convenience for uvicorn
    return create_app()
