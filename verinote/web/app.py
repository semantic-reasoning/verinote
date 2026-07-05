# SPDX-License-Identifier: MPL-2.0
"""The verinote web application.

Server-rendered with Jinja; interactivity via HTMX (the review toggle posts and
swaps a single row partial). No JS build step. The app owns one `Store` (SQLite).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
import threading
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
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
from verinote.pipeline import (
    create_chunked_extraction_job,
    fact_trust_summary,
    IngestError,
    ingest_bytes,
    process_extraction_job,
    store_source,
    supported_suffixes,
    repair_questions,
    translate_questions,
    verify,
    write_query_file,
)
from verinote.pipeline.acceptance import (
    accept_recommendations,
    accept_recommendations_for,
    apply_auto_accept_recommendations,
)
from verinote.pipeline.report_trace import report_trace
from verinote.pipeline.corroboration import (
    CorroborationPolicyError,
    RELATION_ALIASES_RELPATH,
    relation_aliases,
    store_corroboration,
    store_single_valued_conflicts,
)
from verinote.pipeline.workbench import trust_workbench
from verinote.engine.terms import StringLit, render_term
from verinote.store import (
    DEFAULT_REVIEW_PAGE_SIZE,
    REVIEW_PAGE_SIZES,
    ReviewQueuePage,
    Store,
)
from verinote.store.fact_input import structural_term, term_input_kind

_TEMPLATES = resources.files("verinote.web").joinpath("templates")
_STATIC = resources.files("verinote.web").joinpath("static")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg if cfg is not None else Config.load_for_ui()

    app = FastAPI(title="verinote")
    app.state.cfg = cfg
    app.state.store = None
    if cfg is not None:
        store = Store(cfg.db_path)
        store.init_schema()
        app.state.store = store

    templates = Jinja2Templates(directory=str(_TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

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

    def _relation_aliases_path() -> Path:
        return _active_cfg().root / RELATION_ALIASES_RELPATH

    def _relation_aliases_text() -> str:
        path = _relation_aliases_path()
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _open_root(root: Path) -> None:
        """Point this running app at a KB root, creating it if needed."""
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        next_cfg = Config.for_root(root)
        next_store = Store(next_cfg.db_path)
        next_store.init_schema()

        from verinote.engine import DEFAULT_POLICY
        from verinote.pipeline.verify import POLICY_RELPATH

        policy_path = root / POLICY_RELPATH
        if not policy_path.exists():
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(DEFAULT_POLICY, encoding="utf-8")

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
        try:
            terms = store.get_fact_terms(fact["id"])
        except ValueError:
            terms = None
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

    def _maybe_apply_auto_accept() -> None:
        if _active_cfg().auto_accept_recommendations:
            apply_auto_accept_recommendations(_active_store())

    def _row(request: Request, fact):
        # Starlette's current API is TemplateResponse(request, name, context).
        return templates.TemplateResponse(
            request, "partials/fact_row.html", _fact_row_context(fact)
        )

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
        facts_by_source: dict[int, list[object]] = {}
        for fact in facts:
            if fact["source_id"] is None:
                continue
            facts_by_source.setdefault(int(fact["source_id"]), []).append(fact)

        rows = []
        for source in store.sources_with_counts():
            row = dict(source)
            source_id = int(source["id"])
            summaries = [
                fact_trust_summary(store, int(fact["id"]))
                for fact in facts_by_source.get(source_id, [])
            ]
            summaries = [summary for summary in summaries if summary is not None]
            row["unsupported_count"] = sum(
                1 for summary in summaries if "unsupported" in summary.trust_labels
            )
            row["conflicted_count"] = sum(
                1 for summary in summaries if "conflicted" in summary.trust_labels
            )
            row["corroborated_count"] = sum(
                1 for summary in summaries if "corroborated" in summary.trust_labels
            )
            row["evidence_snippets"] = _source_evidence_snippets(summaries, source_id)
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

    def _source_evidence_snippets(summaries, source_id: int) -> list[str]:
        snippets: list[str] = []
        seen: set[str] = set()
        for summary in summaries:
            for evidence in summary.evidence:
                if evidence.source_id != source_id or not evidence.snippet:
                    continue
                if evidence.snippet in seen:
                    continue
                snippets.append(evidence.snippet)
                seen.add(evidence.snippet)
                if len(snippets) == 2:
                    return snippets
        return snippets

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
        has_running_jobs = any(job["status"] in {"pending", "running"} for job in jobs)
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

    def _start_source_extraction(job_id: int, cfg: Config) -> None:
        def run() -> None:
            try:
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    client = get_client(cfg)
                    process_extraction_job(
                        worker_store,
                        client,
                        job_id=job_id,
                        schema_hint=cfg.extraction_schema_hint(),
                    )
                    if cfg.auto_accept_recommendations:
                        apply_auto_accept_recommendations(worker_store)
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
        if app.state.store is None or app.state.cfg is None:
            return
        for job in app.state.store.source_extraction_jobs():
            if job["status"] in {"pending", "running"}:
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
        store = _active_store()
        retried = store.retry_failed_chunks(job_id)
        if retried:
            _start_source_extraction(job_id, _active_cfg())
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/reanalyze", response_class=HTMLResponse)
    def reanalyze_source(request: Request, source_id: int):
        store = _active_store()
        cfg = _active_cfg()
        source = store.get_source(source_id)
        if source is None:
            return _sources(request, error="source not found", status_code=404)
        if any(
            int(job["source_id"]) == source_id
            and job["status"] in {"pending", "running"}
            for job in store.source_extraction_jobs()
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
        return _row(request, _active_store().toggle_review(fact_id))

    @app.post("/facts/{fact_id}/accept", response_class=HTMLResponse)
    def accept(request: Request, fact_id: int):
        return _row(request, _active_store().set_status(fact_id, "confirmed", action="accepted"))

    @app.post("/facts/{fact_id}/reject", response_class=HTMLResponse)
    def reject(request: Request, fact_id: int):
        return _row(request, _active_store().set_status(fact_id, "superseded", action="rejected"))

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
        return _row(request, amended)

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
            page_error = next(
                (finding for finding in rep.findings if "policy error" in finding),
                None,
            )
        return templates.TemplateResponse(
            request,
            "questions.html",
            {"questions": store.questions(), "answers": rep.answers, "error": page_error},
            status_code=status_code,
        )

    @app.get("/questions", response_class=HTMLResponse)
    def questions_page(request: Request):
        return _questions(request)

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
            translate_questions(store, get_client(app.state.cfg), root=cfg.root)
        except LLMError as e:
            return _questions(request, error=f"translation failed: {e}", status_code=502)
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
        return templates.TemplateResponse(
            request,
            "report.html",
            {"rep": verify(store), "trace": report_trace(store)},
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
