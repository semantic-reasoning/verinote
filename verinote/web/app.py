# SPDX-License-Identifier: MPL-2.0
"""The verinote web application.

Server-rendered with Jinja; interactivity via HTMX (the review toggle posts and
swaps a single row partial). No JS build step. The app owns one `Store` (SQLite).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from verinote.config import (
    PROVIDER_LABELS,
    PROVIDERS,
    TESTABLE_PROVIDERS,
    Config,
    save_settings,
)
from verinote.llm import LLMError, get_client
from verinote.pipeline import (
    IngestError,
    ingest_bytes,
    store_source,
    supported_suffixes,
    repair_questions,
    sync_sources,
    translate_questions,
    verify,
)
from verinote.store import Store

_TEMPLATES = resources.files("verinote.web").joinpath("templates")
_STATIC = resources.files("verinote.web").joinpath("static")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.load()
    store = Store(cfg.db_path)
    store.init_schema()

    app = FastAPI(title="verinote")
    app.state.store = store
    app.state.cfg = cfg

    templates = Jinja2Templates(directory=str(_TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    def _switch_root(root: Path) -> None:
        """Point this running app at a different KB root."""
        nonlocal cfg, store
        next_cfg = Config.for_root(root.expanduser().resolve())
        next_store = Store(next_cfg.db_path)
        next_store.init_schema()
        old_store = store
        cfg = next_cfg
        store = next_store
        app.state.cfg = next_cfg
        app.state.store = next_store
        old_store.close()

    def _row(request: Request, fact):
        # Starlette's current API is TemplateResponse(request, name, context).
        return templates.TemplateResponse(request, "partials/fact_row.html", {"f": fact})

    def _dashboard(request: Request, *, error: str | None = None, status_code: int = 200):
        from verinote.engine import coverage

        counts = store.status_counts()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "counts": counts,
                "total": sum(counts.values()),
                "sources": store.sources(),
                "coverage": coverage(store, root=cfg.root),
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
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "sources": store.sources_with_counts(),
                "suffixes": ", ".join(sorted(supported_suffixes())),
                "accept": ",".join(sorted(supported_suffixes())),
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return _dashboard(request)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        return _sources(request)

    @app.post("/sources", response_class=HTMLResponse)
    async def upload_source(request: Request, file: UploadFile = File(...)):
        filename = Path(file.filename or "").name
        raw = await file.read()
        try:
            text, kind = ingest_bytes(raw, filename)
        except IngestError as e:
            return _sources(request, error=str(e), status_code=400)

        citation = store_source(store, cfg.root, filename, text, kind)
        try:
            client = get_client(app.state.cfg)
            sync_sources(
                store,
                client,
                [(citation, text)],
                provider=app.state.cfg.provider,
                model=app.state.cfg.model,
            )
        except LLMError as e:
            return _sources(request, error=f"extraction failed: {e}", status_code=502)
        return RedirectResponse("/review", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request):
        return templates.TemplateResponse(
            request, "review.html", {"queue": store.review_queue()}
        )

    @app.post("/facts/{fact_id}/toggle", response_class=HTMLResponse)
    def toggle(request: Request, fact_id: int):
        return _row(request, store.toggle_review(fact_id))

    @app.post("/facts/{fact_id}/accept", response_class=HTMLResponse)
    def accept(request: Request, fact_id: int):
        return _row(request, store.set_status(fact_id, "confirmed", action="accepted"))

    @app.post("/facts/{fact_id}/reject", response_class=HTMLResponse)
    def reject(request: Request, fact_id: int):
        return _row(request, store.set_status(fact_id, "superseded", action="rejected"))

    @app.get("/facts/{fact_id}/edit", response_class=HTMLResponse)
    def edit_fact(request: Request, fact_id: int):
        return templates.TemplateResponse(
            request, "partials/fact_edit.html", {"f": store.get_fact(fact_id)}
        )

    @app.get("/facts/{fact_id}/row", response_class=HTMLResponse)
    def fact_row(request: Request, fact_id: int):
        # Re-render the read-only row (used to cancel an inline edit).
        return _row(request, store.get_fact(fact_id))

    @app.post("/facts/{fact_id}/amend", response_class=HTMLResponse)
    def amend_fact(
        request: Request,
        fact_id: int,
        subject: str = Form(...),
        relation: str = Form(...),
        object: str = Form(...),
        note: str = Form(""),
    ):
        amended = store.amend_fact(
            fact_id, subject=subject, relation=relation, obj=object, note=note
        )
        return _row(request, amended)

    @app.get("/facts/{fact_id}/provenance", response_class=HTMLResponse)
    def provenance(request: Request, fact_id: int):
        fact = store.get_fact(fact_id)
        run = store.get_run(fact["run_id"]) if fact and fact["run_id"] else None
        return templates.TemplateResponse(
            request,
            "provenance.html",
            {"f": fact, "run": run, "log": store.fact_log(fact_id) if fact else []},
        )

    def _questions(request: Request, *, error: str | None = None, status_code: int = 200):
        return templates.TemplateResponse(
            request,
            "questions.html",
            {"questions": store.questions(), "answers": verify(store).answers, "error": error},
            status_code=status_code,
        )

    @app.get("/questions", response_class=HTMLResponse)
    def questions_page(request: Request):
        return _questions(request)

    @app.post("/questions", response_class=HTMLResponse)
    def add_question(request: Request, text: str = Form(...)):
        store.add_question(text)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/translate", response_class=HTMLResponse)
    def translate(request: Request):
        try:
            translate_questions(store, get_client(app.state.cfg), root=cfg.root)
        except LLMError as e:
            return _questions(request, error=f"translation failed: {e}", status_code=502)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/repair", response_class=HTMLResponse)
    def repair(request: Request):
        try:
            client = get_client(app.state.cfg)
        except LLMError as e:
            return _questions(request, error=f"repair failed: {e}", status_code=502)
        repair_questions(store, client, root=cfg.root)
        return RedirectResponse("/questions", status_code=303)

    @app.get("/report", response_class=HTMLResponse)
    def report(request: Request):
        return templates.TemplateResponse(request, "report.html", {"rep": verify(store)})

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request):
        from verinote.store.analytics import compute

        return templates.TemplateResponse(request, "analytics.html", {"a": compute(cfg.db_path)})

    def _settings(request: Request, *, test_result=None, error=None, status_code=200):
        c = app.state.cfg
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
                "root": c.root,
                "has_key": bool(c.api_key),  # never render the key itself
                "connection_test_enabled": c.provider in TESTABLE_PROVIDERS,
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
    ):
        save_settings(cfg.root, provider=provider, model=model, base_url=base_url or None)
        # reload from the app's own root so the change takes effect on next sync
        app.state.cfg = Config.for_root(cfg.root)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/root", response_class=HTMLResponse)
    def switch_root(request: Request, root: str = Form(...)):
        path = root.strip()
        if not path:
            return _settings(request, error="KB directory is required", status_code=400)
        try:
            _switch_root(Path(path))
        except OSError as e:
            return _settings(request, error=f"could not open KB directory: {e}", status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/test", response_class=HTMLResponse)
    def test_connection(request: Request):
        c = app.state.cfg
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

    return app


# Module-level app for `uvicorn verinote.web.app:app`.
def _default() -> FastAPI:  # pragma: no cover - convenience for uvicorn
    return create_app()
