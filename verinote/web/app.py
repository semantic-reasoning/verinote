# SPDX-License-Identifier: Apache-2.0
"""The verinote web application.

Server-rendered with Jinja; interactivity via HTMX (the review toggle posts and
swaps a single row partial). No JS build step. The app owns one `Store` (SQLite).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from verinote.config import Config
from verinote.pipeline import verify
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

    def _row(request: Request, fact):
        # Starlette's current API is TemplateResponse(request, name, context).
        return templates.TemplateResponse(request, "partials/fact_row.html", {"f": fact})

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
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
            },
        )

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
