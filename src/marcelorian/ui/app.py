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

from marcelorian.clock import Clock, PracticeClock
from marcelorian.config.settings import (
    ConfirmationRequired,
    SettingsError,
    load_settings,
    update_settings,
)
from marcelorian.config.specialty import load_specialty
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, CampaignPause, Pi, ResearchRecord
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.research import (
    BLOCKING_VERIFICATIONS,
    CONFIRMATION_VALID_DAYS,
    FRESHNESS,
    LEVEL_LABELS,
    LEVELS,
    LOCAL_STATUSES,
    LOCAL_VERIFICATIONS,
    QUEUE_TIERS,
    confidence,
    is_web_url,
)
from marcelorian.lookups.http import Fetcher, HttpFetcher
from marcelorian.platform.secrets import SecretStore, open_secret_store
from marcelorian.services import (
    approvals,
    handoff,
    practice,
    profile,
    replies,
    research_seed,
    runtime,
    status,
)
from marcelorian.services.ai import AnthropicClient, ClientFactory
from marcelorian.services.gateway import SendGateway
from marcelorian.services.sync import SandboxSync
from marcelorian.transport.sandbox import SandboxTransport
from marcelorian.ui import (
    routes_contacts,
    routes_messages,
    routes_profile,
    routes_send,
    routes_settings,
)
from marcelorian.ui.context import SLOGAN, UIContext
from marcelorian.ui.security import SESSION_COOKIE, SecurityMiddleware, UISecurity, UISession

RESEARCH_PAGE_SIZE = 50
SCOPE_LABELS = {
    "institution": "Institution (or institution group)",
    "department": "Department within an institution",
    "lab": "Lab",
}
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
    templates.env.globals["is_web_url"] = is_web_url  # only http(s) links become anchors
    return templates


def create_app(
    security: UISecurity,
    session_factory: sessionmaker[Session],
    clock: Clock,
    on_dispatch_change: Callable[[], None] | None = None,
    fetcher: Fetcher | None = None,
    secrets: Callable[[], SecretStore] | None = None,
    ai_client: ClientFactory | None = None,
) -> Any:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = _templates()
    static_dir = str(resources.files("marcelorian.ui").joinpath("static"))
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def current_session(request: Request) -> UISession:
        session: UISession = request.scope["state"]["ui_session"]
        return session

    with transaction(session_factory) as db:
        practice_clock = PracticeClock(
            clock,
            practice.load_offset(db),
            active=runtime.get_runtime(db, clock.now()).active_mode is Mode.SANDBOX,
        )
    gateway = SendGateway(
        session_factory=session_factory,
        clock=practice_clock,
        transports={Mode.SANDBOX: SandboxTransport(practice_clock)},
        syncs={"sandbox": SandboxSync(session_factory, practice_clock)},
        specialty_display_name=load_specialty().display_name,
    )
    ui = UIContext(
        templates,
        session_factory,
        clock,
        current_session,
        fetcher or HttpFetcher(),
        practice_clock,
        gateway,
        secrets or open_secret_store,
        ai_client or AnthropicClient,
    )
    routes_profile.register(app, ui)
    routes_contacts.register(app, ui)
    routes_messages.register(app, ui)
    routes_send.register(app, ui)
    routes_settings.register(app, ui)

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
            settings = load_settings(db)
            outreach = status.outreach_status(
                db, view.active_mode, settings.same_place_scope, practice_clock.now()
            )
            research = research_seed.progress(db, clock.now().date())
            setup = _setup_checklist(db, clock.now(), settings, research.total)
            open_handoffs = handoff.open_handoffs(db, practice_clock.now())
            due_tasks = [
                t
                for t in replies.open_tasks(db)
                if not t.due_on or t.due_on <= clock.now().date().isoformat()
            ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "slogan": SLOGAN,
                "csrf_token": ui_session.csrf_token,
                "runtime": view,
                "counts": counts,
                "attention": attention,
                "outreach": outreach,
                "status_labels": status.STATUS_LABELS,
                "research": research,
                "scope": settings.same_place_scope,
                "scope_labels": SCOPE_LABELS,
                "scope_error": request.query_params.get("scope_error", ""),
                "setup": setup,
                "open_handoffs": open_handoffs,
                "due_tasks": due_tasks,
            },
        )

    def _after_change() -> RedirectResponse:
        if on_dispatch_change is not None:
            on_dispatch_change()
        return RedirectResponse("/", status_code=303)

    @app.post("/actions/same-place-scope")
    def same_place_scope(scope: str = Form(...), confirm: str = Form("")) -> RedirectResponse:
        with transaction(session_factory) as db:
            try:
                update_settings(
                    db,
                    {"same_place_scope": scope},
                    actor=Actor.APPLICANT,
                    now=clock.now(),
                    confirm_less_conservative=confirm == "yes",
                )
            except ConfirmationRequired:
                return RedirectResponse("/?scope_error=confirm#places", status_code=303)
            except SettingsError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/#places", status_code=303)

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
            queue_total = len(research_seed.queue(db, today))
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
                "queue_total": queue_total,
                "query_base": query_base,
            },
        )

    @app.get("/research/queue", response_class=HTMLResponse)
    def research_queue(request: Request) -> HTMLResponse:
        ui_session = current_session(request)
        params = request.query_params
        tier = _int_param(params.get("tier"), None)
        today = clock.now().date()
        with transaction(session_factory) as db:
            items = research_seed.queue(db, today, tier=tier)
            counts = research_seed.queue_counts(research_seed.queue(db, today))
            pos = max(0, _int_param(params.get("pos"), 0) or 0)
            wanted = _int_param(params.get("id"), None)
            if wanted is not None:
                pos = next((i for i, it in enumerate(items) if it.record.id == wanted), pos)
            near_name = ""
            near = _int_param(params.get("near"), None)
            if near is not None and wanted is None:
                found = research_seed.same_page_next(db, items, near)
                if found is not None:
                    pos, near_name = found
            item = items[pos] if pos < len(items) else None
            context: dict[str, Any] = {
                "slogan": SLOGAN,
                "csrf_token": ui_session.csrf_token,
                "item": item,
                "pos": pos,
                "total": len(items),
                "tier": tier,
                "tiers": QUEUE_TIERS,
                "counts": counts,
                "error": params.get("error", ""),
                "source_required": research_seed.SOURCE_REQUIRED,
                "verifications": LOCAL_VERIFICATIONS,
                "level_labels": LEVEL_LABELS,
                "is_web_url": is_web_url,
                "blocking": BLOCKING_VERIFICATIONS,
                "near_name": near_name,
                "progress": research_seed.progress(db, today),
            }
            if item is not None:
                context["evidence"] = research_seed.evidence_for(db, item.record)
                context["source_urls"] = research_seed.source_urls(item.record)
                context["route_group"] = research_seed.route_group(db, item.record)
        return templates.TemplateResponse(request, "research_queue.html", context)

    @app.post("/research/{record_id}/quick")
    def research_quick(
        record_id: int,
        verification: str = Form(...),
        source: str = Form(""),
        note: str = Form(""),
        pos: int = Form(0),
        tier: str = Form(""),
    ) -> RedirectResponse:
        if verification not in LOCAL_VERIFICATIONS or verification == "unchecked":
            raise HTTPException(status_code=400, detail="unknown finding")
        back = _queue_url(pos=pos, tier=tier, near=record_id)
        with transaction(session_factory) as db:
            try:
                research_seed.quick_finding(
                    db,
                    record_id,
                    verification=verification,
                    source=source,
                    note=note,
                    now=clock.now(),
                )
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
            except ValueError as exc:
                if str(exc) == research_seed.SOURCE_REQUIRED:
                    return RedirectResponse(
                        _queue_url(pos=pos, tier=tier, record_id=record_id, error="source"),
                        status_code=303,
                    )
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(back, status_code=303)

    @app.post("/research/{record_id}/reopen")
    def research_reopen(
        record_id: int,
        back: str = Form("detail"),
        pos: int = Form(0),
        tier: str = Form(""),
    ) -> RedirectResponse:
        with transaction(session_factory) as db:
            try:
                research_seed.reopen(db, record_id, now=clock.now())
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if back == "queue":
            # Stay on the reopened record so it can be checked again right away.
            return RedirectResponse(
                _queue_url(pos=pos, tier=tier, record_id=record_id), status_code=303
            )
        return RedirectResponse(f"/research/{record_id}", status_code=303)

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
                "blocking": BLOCKING_VERIFICATIONS,
                "history": research_seed.history(db, record),
                "error": request.query_params.get("error", ""),
                "source_required": research_seed.SOURCE_REQUIRED,
                "valid_days": CONFIRMATION_VALID_DAYS,
                "promote": routes_contacts.promotion_context(db, record, today),
                "promote_error": request.query_params.get("promote_error", ""),
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
                if str(exc) == research_seed.SOURCE_REQUIRED:
                    return RedirectResponse(f"/research/{record_id}?error=source", status_code=303)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/research/{record_id}", status_code=303)

    return SecurityMiddleware(app, security)


def _setup_checklist(
    db: Session, now: Any, settings: Any, research_total: int
) -> list[tuple[str, bool, str]]:
    """First-use steps: (label, done, link)."""
    me = profile.applicant(db, now)
    return [
        ("Confirm your profile", not profile.missing_required(db, me.id), "/profile"),
        ("Upload your CV", bool(profile.cv_versions(db, me.id)), "/profile#cv"),
        ("Set up your signature", settings.signature_step_done, "/profile#signature"),
        ("Add your Outlook address", approvals._active_mailbox(db) is not None, "/profile#outlook"),
        ("Import a research seed", research_total > 0, "/research"),
    ]


def _int_param(value: str | None, default: int | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _queue_url(
    *,
    pos: int,
    tier: str,
    record_id: int | None = None,
    error: str | None = None,
    near: int | None = None,
) -> str:
    query: dict[str, Any] = {"pos": max(0, pos)}
    if near is not None:
        query["near"] = near
    if tier.isdigit():
        query["tier"] = tier
    if record_id is not None:
        query["id"] = record_id
    if error:
        query["error"] = error
    return "/research/queue?" + urlencode(query)


def _claim_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "; ".join(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
