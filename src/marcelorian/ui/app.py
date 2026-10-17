"""Local web UI (FastAPI + Jinja2; spec §2, §16).

P0 provides only the secured shell: bootstrap/session exchange, a minimal
dashboard (mode banner, dispatch pause state, open review items) and the
pause/resume actions. Screens are added in later phases.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, CampaignPause, Pi
from marcelorian.domain.enums import Actor
from marcelorian.services import runtime
from marcelorian.ui.security import SESSION_COOKIE, SecurityMiddleware, UISecurity, UISession

SLOGAN = "May the Force Hire You"


def _templates() -> Jinja2Templates:
    directory = str(resources.files("marcelorian.ui").joinpath("templates"))
    templates = Jinja2Templates(directory=directory)
    templates.env.autoescape = True  # escape all untrusted text
    return templates


def create_app(
    security: UISecurity,
    session_factory: sessionmaker[Session],
    clock: Clock,
    on_dispatch_change: Callable[[], None] | None = None,
) -> Any:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = _templates()
    static_dir = str(resources.files("marcelorian.ui").joinpath("static"))
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def current_session(request: Request) -> UISession:
        session: UISession = request.scope["state"]["ui_session"]
        return session

    @app.get("/bootstrap", response_class=HTMLResponse)
    def bootstrap_page(request: Request) -> HTMLResponse:
        # Read-only: the token is in the URL fragment and is POSTed by bootstrap.js.
        return templates.TemplateResponse(request, "bootstrap.html", {"slogan": SLOGAN})

    @app.post("/bootstrap/exchange")
    async def bootstrap_exchange(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"ok": False}, status_code=400)
        token = payload.get("token") if isinstance(payload, dict) else None
        session = security.exchange(token if isinstance(token, str) else "")
        if session is None:
            return JSONResponse({"ok": False}, status_code=403)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            session.session_id,
            httponly=True,
            samesite="strict",
            secure=False,  # plain http on 127.0.0.1
            path="/",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        ui_session = current_session(request)
        with transaction(session_factory) as db:
            view = runtime.get_runtime(db, clock.now())
            counts: dict[str, int] = {
                state: int(number)
                for state, number in db.execute(
                    select(Campaign.state, func.count())
                    .where(Campaign.mode == view.active_mode.value)
                    .group_by(Campaign.state)
                ).all()
            }
            attention = db.execute(
                select(Pi.full_name, CampaignPause.reason)
                .join(Campaign, Campaign.id == CampaignPause.campaign_id)
                .join(Pi, Pi.id == Campaign.pi_id)
                .where(
                    CampaignPause.resolved_at.is_(None),
                    Campaign.mode == view.active_mode.value,
                )
                .order_by(CampaignPause.opened_at)
            ).all()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "slogan": SLOGAN,
                "csrf_token": ui_session.csrf_token,
                "runtime": view,
                "counts": counts,
                "attention": attention,
            },
        )

    def _after_change() -> RedirectResponse:
        if on_dispatch_change is not None:
            on_dispatch_change()
        return RedirectResponse("/", status_code=303)

    @app.post("/actions/pause")
    def pause(request: Request) -> RedirectResponse:
        with transaction(session_factory) as db:
            runtime.pause_dispatch(db, actor=Actor.APPLICANT, now=clock.now())
        return _after_change()

    @app.post("/actions/resume")
    def resume(request: Request) -> RedirectResponse:
        with transaction(session_factory) as db:
            runtime.resume_dispatch(db, actor=Actor.APPLICANT, now=clock.now())
        return _after_change()

    return SecurityMiddleware(app, security)
