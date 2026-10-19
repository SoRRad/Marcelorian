"""Local web UI (FastAPI + Jinja2; spec §2, §16).

P0 provides only the secured shell: bootstrap/session exchange, a minimal
dashboard (mode banner, dispatch pause state, open review items) and the
pause/resume actions. The research catalog (docs/SEED_IMPORT.md) adds a read-only
list and detail of staged research leads plus the applicant's own review form;
nothing on those pages drafts, approves or sends. Screens are added in later phases.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import resources
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, CampaignPause, Pi, ResearchRecord
from marcelorian.domain.enums import Actor
from marcelorian.domain.research import (
    FRESHNESS,
    LEVEL_LABELS,
    LEVELS,
    LOCAL_STATUSES,
    LOCAL_VERIFICATIONS,
    confidence,
    is_web_url,
)
from marcelorian.services import research_seed, runtime
from marcelorian.ui.security import SESSION_COOKIE, SecurityMiddleware, UISecurity, UISession

SLOGAN = "May the Force Hire You"
RESEARCH_PAGE_SIZE = 50
FILTER_FIELDS = (
    "record_type",
    "review_lane",
    "institution_key",
    "funding_status",
    "restrictions",
    "email_status",
    "freshness",
    "confidence",
    "local_verification",
    "query",
)


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

    @app.get("/research", response_class=HTMLResponse)
    def research_list(request: Request) -> HTMLResponse:
        params = request.query_params
        chosen = {name: params.get(name, "")[:200] for name in FILTER_FIELDS}
        try:
            page = max(1, int(params.get("page", "1")))
        except ValueError:
            page = 1
        today = clock.now().date()
        with transaction(session_factory) as db:
            rows = research_seed.list_records(db, research_seed.Filters(**chosen), today)
            options = research_seed.filter_options(db)
            institutions = research_seed.institution_names(db)
            last_import = next(iter(research_seed.imports(db)), None)
        start = (page - 1) * RESEARCH_PAGE_SIZE
        query_base = urlencode({k: v for k, v in chosen.items() if v})
        return templates.TemplateResponse(
            request,
            "research_list.html",
            {
                "slogan": SLOGAN,
                "rows": rows[start : start + RESEARCH_PAGE_SIZE],
                "total": len(rows),
                "page": page,
                "pages": max(1, -(-len(rows) // RESEARCH_PAGE_SIZE)),
                "chosen": chosen,
                "options": options,
                "institutions": institutions,
                "levels": [lvl for lvl in reversed(LEVELS)],
                "level_labels": LEVEL_LABELS,
                "freshness": FRESHNESS,
                "verifications": LOCAL_VERIFICATIONS,
                "last_import": last_import,
                "query_base": query_base,
            },
        )

    @app.get("/research/{record_id}", response_class=HTMLResponse)
    def research_detail(request: Request, record_id: int) -> HTMLResponse:
        ui_session = current_session(request)
        today = clock.now().date()
        with transaction(session_factory) as db:
            record = db.get(ResearchRecord, record_id)
            if record is None:
                raise HTTPException(status_code=404)
            facts = research_seed.facts_for(record, today)
            context = {
                "slogan": SLOGAN,
                "csrf_token": ui_session.csrf_token,
                "record": record,
                "confidence": confidence(facts),
                "freshness": facts.freshness,
                "level_labels": LEVEL_LABELS,
                "evidence": research_seed.evidence_for(db, record),
                "claims": [
                    (key, _claim_text(value)) for key, value in research_seed.claims_for(db, record)
                ],
                "route_group": research_seed.route_group(db, record),
                "matches": research_seed.local_matches(db, record),
                "raw": research_seed.raw_fields(record),
                "source_urls": research_seed.source_urls(record),
                "flags": research_seed.review_flags(record),
                "statuses": LOCAL_STATUSES,
                "verifications": LOCAL_VERIFICATIONS,
                "is_web_url": is_web_url,
            }
        return templates.TemplateResponse(request, "research_detail.html", context)

    @app.post("/research/{record_id}/review")
    def research_review(
        record_id: int,
        local_status: str = Form(...),
        local_verification: str = Form(...),
        source: str = Form(""),
        note: str = Form(""),
    ) -> RedirectResponse:
        with transaction(session_factory) as db:
            try:
                research_seed.set_local_review(
                    db,
                    record_id,
                    status=local_status,
                    verification=local_verification,
                    source=source,
                    note=note,
                    now=clock.now(),
                )
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/research/{record_id}", status_code=303)

    return SecurityMiddleware(app, security)


def _claim_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "; ".join(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
