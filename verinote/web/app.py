# SPDX-License-Identifier: Apache-2.0
"""The verinote web application.

Server-rendered with Jinja; interactivity via HTMX (the review toggle posts and
swaps a single row partial). No JS build step. The app owns one `Store` (SQLite).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from verinote.config import Config
from verinote.llm import LLMError, get_client
from verinote.pipeline import sync_sources, verify
from verinote.store import Store

_UPLOAD_SUFFIXES = {".txt", ".md"}

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

    def _row(request: Request, fact):
        # Starlette's current API is TemplateResponse(request, name, context).
        return templates.TemplateResponse(request, "partials/fact_row.html", {"f": fact})

    def _dashboard(request: Request, *, error: str | None = None, status_code: int = 200):
        counts = store.status_counts()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "counts": counts,
                "total": sum(counts.values()),
                "sources": store.sources(),
                "provider": cfg.provider,
                "model": cfg.model,
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return _dashboard(request)

    @app.post("/sources", response_class=HTMLResponse)
    async def upload_source(request: Request, file: UploadFile = File(...)):
        filename = Path(file.filename or "").name
        if Path(filename).suffix.lower() not in _UPLOAD_SUFFIXES:
            return _dashboard(
                request,
                error=f"unsupported file type: {filename or '(none)'!r} (upload a .txt or .md file)",
                status_code=400,
            )
        try:
            text = (await file.read()).decode("utf-8")
        except UnicodeDecodeError:
            return _dashboard(request, error="file is not valid UTF-8 text", status_code=400)

        sources_dir = cfg.root / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        (sources_dir / filename).write_text(text, encoding="utf-8")
        citation = f"sources/{filename}"

        try:
            client = get_client(cfg)
            sync_sources(store, client, [(citation, text)], provider=cfg.provider, model=cfg.model)
        except LLMError as e:
            return _dashboard(request, error=f"extraction failed: {e}", status_code=502)
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

    @app.get("/report", response_class=HTMLResponse)
    def report(request: Request):
        return templates.TemplateResponse(request, "report.html", {"rep": verify(store)})

    return app


# Module-level app for `uvicorn verinote.web.app:app`.
def _default() -> FastAPI:  # pragma: no cover - convenience for uvicorn
    return create_app()
