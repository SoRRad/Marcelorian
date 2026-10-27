"""Drafting and the approval queue (P2; spec §8, §9.1, §16 "Approval queue").

Approving never sends (owner decision S1): the applicant opens the approved email in
Outlook and presses Send there (hand-off screens), or, in practice mode, presses the
practice send button.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.config.settings import load_settings
from marcelorian.config.specialty import load_specialty
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Institution, Message, Pi, SendAttempt, Task
from marcelorian.domain.enums import Actor, MessageState, Mode
from marcelorian.lookups.enrich import evidence_for
from marcelorian.services import (
    approvals,
    autofill,
    drafting,
    profile,
    replies,
    runtime,
    tailoring,
    worker,
)
from marcelorian.services.reservations import CapacityError
from marcelorian.ui.context import UIContext
from marcelorian.ui.reasons import explain

REVIEW_STATES = (
    MessageState.AWAITING_APPROVAL.value,
    MessageState.NEEDS_REAPPROVAL.value,
    MessageState.FAILED_DEFINITE.value,
)
WAITING_STATES = (MessageState.APPROVED_QUEUED.value, MessageState.HANDED_OFF.value)


def _specialty() -> str:
    return load_specialty().display_name


def _error_redirect(url: str, reasons: list[str], anchor: str = "") -> RedirectResponse:
    query = urlencode({"error": " ".join(explain(r) for r in reasons)[:800]})
    return RedirectResponse(f"{url}?{query}{anchor}", status_code=303)


def _draft_message(db: Session, campaign: Campaign, step: int) -> Message | None:
    return db.scalar(
        select(Message).where(Message.campaign_id == campaign.id, Message.step == step)
    )


def _plain_redirect(url: str, *, error: str = "", notice: str = "") -> RedirectResponse:
    params = {k: v[:800] for k, v in (("error", error), ("notice", notice)) if v}
    return RedirectResponse(url + (f"?{urlencode(params)}" if params else ""), status_code=303)


def _cited(db: Session, campaign: Campaign, fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per Claude-suggested line, the evidence and profile answers it cites."""
    basis = fields.get("ai") if isinstance(fields.get("ai"), dict) else {}
    if not basis:
        return {}
    evidence = {e.id: e for e in evidence_for(db, campaign.pi_id)}
    facts = {f.id: f for f in profile.facts(db, campaign.applicant_id).values()}
    cited: dict[str, dict[str, Any]] = {}
    for key, item in basis.items():
        if not isinstance(item, dict):
            continue
        cited[key] = {
            "label": drafting.PERSONAL_FIELDS.get(key, key),
            "text": item.get("text", ""),
            "evidence": [evidence[i] for i in item.get("evidence_ids", []) if i in evidence],
            "facts": [
                (profile.FIELD_BY_KEY[facts[i].key].label, facts[i].value)
                for i in item.get("fact_ids", [])
                if i in facts and facts[i].key in profile.FIELD_BY_KEY
            ],
        }
    return cited


def _approve(db: Session, message: Message, now: Any) -> str | None:
    """Approve after the checklist; returns what to show the applicant if it can't."""
    checks = drafting.checklist(db, message, specialty_display_name=_specialty(), today=now.date())
    failed = [c.label for c in checks if not c.ok and not c.warn_only]
    if failed:
        return "Fix the checklist first: " + "; ".join(failed)
    try:
        approvals.approve(
            db,
            message,
            actor=Actor.APPLICANT,
            now=now,
            settings=load_settings(db),
            specialty_display_name=_specialty(),
        )
    except approvals.ApprovalError as exc:
        return " ".join(explain(r) for r in exc.reasons)
    except CapacityError as exc:
        return " ".join(explain(r) for r in exc.result.reasons)
    return None


def register(app: FastAPI, ui: UIContext) -> None:
    @app.post("/candidates/{pi_id}/draft")
    def start_draft(pi_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            now = ui.campaign_clock.now()
            if db.get(Pi, pi_id) is None:
                raise HTTPException(status_code=404)
            mode = runtime.get_runtime(db, now).active_mode
            me = profile.applicant(db, now)
            campaign = drafting.ensure_campaign(db, me.id, pi_id, mode, now)
            campaign_id = campaign.id
        return RedirectResponse(f"/campaigns/{campaign_id}/draft", status_code=303)

    @app.get("/campaigns/{campaign_id}/draft", response_class=HTMLResponse)
    def draft_page(request: Request, campaign_id: int) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            now = ui.campaign_clock.now()
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise HTTPException(status_code=404)
            step = drafting.draftable_step(db, campaign, now)
            message = _draft_message(db, campaign, step) if step else None
            fields = drafting.draft_fields(message) if message else {}
            saved = fields.get("personal", {}) if isinstance(fields, dict) else {}
            suggestions = (
                autofill.suggest(db, campaign, step, ui.clock.now().date()) if step else None
            )
            # Prefilled from the PI's interests and your profile; your saved lines win.
            personal = {**(suggestions.first() if suggestions else {}), **saved}
            preview = (
                drafting.render(db, campaign, step, personal, specialty_display_name=_specialty())
                if step
                else None
            )
            edited = bool(fields.get("body_edited")) and message is not None
            checks = (
                drafting.checklist(
                    db, message, specialty_display_name=_specialty(), today=now.date()
                )
                if message is not None and message.state in REVIEW_STATES
                else []
            )
            latest = db.scalar(
                select(Message)
                .where(Message.campaign_id == campaign.id)
                .order_by(Message.step.desc())
            )
            context: dict[str, Any] = {
                "campaign": campaign,
                "pi": db.get(Pi, campaign.pi_id),
                "step": step,
                "message": message,
                "latest": latest,
                "personal": personal,
                "options": suggestions.options if suggestions else {},
                "personal_fields": {
                    k: drafting.PERSONAL_FIELDS[k] for k in drafting.personal_fields_for(db, step)
                }
                if step
                else {},
                "preview": preview,
                "body": (message.draft_body if edited and message else None)
                or (preview.body if preview else ""),
                "checks": checks,
                "practice": Mode(campaign.mode) is Mode.SANDBOX,
                "interests": tailoring.interests(db, campaign.pi_id).sources[:12],
                "position": tailoring.confirmed_position(db, campaign.pi_id) if step == 1 else None,
                "pi_line": tailoring.PI_LINE.get(step or 0),
                "error": request.query_params.get("error", ""),
                "notice": request.query_params.get("notice", ""),
            }
        return ui.render(request, "draft.html", context)

    @app.post("/campaigns/{campaign_id}/draft")
    async def save_draft(request: Request, campaign_id: int) -> RedirectResponse:
        form = await request.form()
        action = str(form.get("action", "save"))
        with transaction(ui.session_factory) as db:
            now = ui.campaign_clock.now()
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise HTTPException(status_code=404)
            step = drafting.draftable_step(db, campaign, now)
            if step is None:
                return _error_redirect(
                    f"/campaigns/{campaign_id}/draft", ["campaign_not_draftable"]
                )
            personal = {k: str(form.get(k, "")) for k in drafting.PERSONAL_FIELDS}
            sources, basis = drafting.carry_forward(_draft_message(db, campaign, step), personal)
            if action == "practice":
                if Mode(campaign.mode) is not Mode.SANDBOX:
                    raise HTTPException(status_code=400, detail="practice text is sandbox-only")
                canned = drafting.practice_text(tailoring.practice_topic(db, campaign.pi_id))
                for key, text in canned.items():
                    if not personal.get(key):
                        personal[key], sources[key] = text, "practice"
            # The email box is the preview; the page marks it edited once you type in it.
            body = str(form.get("body", "")) if form.get("edit_body") == "yes" else None
            try:
                message = drafting.save(
                    db,
                    campaign,
                    step,
                    personal,
                    sources,
                    body_override=body,
                    specialty_display_name=_specialty(),
                    now=now,
                    ai_basis=basis,
                )
            except approvals.ApprovalError as exc:
                return _error_redirect(f"/campaigns/{campaign_id}/draft", exc.reasons)
            message_id = message.id
            if action == "approve":
                failure = _approve(db, message, now)
                if failure is not None:
                    return _plain_redirect(f"/campaigns/{campaign_id}/draft", error=failure)
                return RedirectResponse(f"/messages/{message_id}?notice=approved#send", 303)
            if action == "save_here":
                return _plain_redirect(f"/campaigns/{campaign_id}/draft", notice="Draft saved.")
        return RedirectResponse(f"/messages/{message_id}", status_code=303)

    @app.get("/approvals", response_class=HTMLResponse)
    def approval_queue(request: Request) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            mode = runtime.get_runtime(db, ui.campaign_clock.now()).active_mode
            rows = db.execute(
                select(Message, Campaign, Pi)
                .join(Campaign, Campaign.id == Message.campaign_id)
                .join(Pi, Pi.id == Campaign.pi_id)
                .where(
                    Message.mode == mode.value,
                    Message.state.in_((*REVIEW_STATES, *WAITING_STATES)),
                )
                .order_by(Message.updated_at)
            ).all()
            followups = [
                (c, db.get(Pi, c.pi_id))
                for c in worker.due_followups(db, mode, ui.campaign_clock.now())
            ]
        review = [r for r in rows if r[0].state in REVIEW_STATES]
        waiting = [r for r in rows if r[0].state in WAITING_STATES]
        return ui.render(
            request,
            "approvals.html",
            {"review": review, "waiting": waiting, "mode": mode.value, "followups": followups},
        )

    @app.get("/messages/{message_id}", response_class=HTMLResponse)
    def message_page(request: Request, message_id: int) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            now = ui.campaign_clock.now()
            message = db.get(Message, message_id)
            if message is None:
                raise HTTPException(status_code=404)
            campaign = db.get(Campaign, message.campaign_id)
            assert campaign is not None
            pi = db.get(Pi, campaign.pi_id)
            assert pi is not None
            checks = (
                drafting.checklist(
                    db, message, specialty_display_name=_specialty(), today=now.date()
                )
                if message.state in REVIEW_STATES
                else []
            )
            context: dict[str, Any] = {
                "message": message,
                "campaign": campaign,
                "pi": pi,
                "institution": db.get(Institution, pi.institution_id),
                "fields": drafting.draft_fields(message),
                "cited": _cited(db, campaign, drafting.draft_fields(message)),
                "checks": checks,
                "can_approve": bool(checks) and drafting.ready_to_approve(checks),
                "reviewable": message.state in REVIEW_STATES,
                "attempts": db.scalars(
                    select(SendAttempt).where(SendAttempt.message_id == message.id)
                ).all(),
                "error": request.query_params.get("error", ""),
                "notice": request.query_params.get("notice", ""),
                "explain": explain,
            }
            context.update(_send_context(ui, db, message, now))
        return ui.render(request, "message.html", context)

    @app.post("/messages/{message_id}/approve")
    def approve(message_id: int) -> RedirectResponse:
        url = f"/messages/{message_id}"
        with transaction(ui.session_factory) as db:
            message = db.get(Message, message_id)
            if message is None:
                raise HTTPException(status_code=404)
            failure = _approve(db, message, ui.campaign_clock.now())
            if failure is not None:
                return _plain_redirect(url, error=failure)
        return RedirectResponse(f"{url}?notice=approved", status_code=303)

    @app.post("/messages/{message_id}/revoke")
    def revoke(message_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            message = db.get(Message, message_id)
            if message is None:
                raise HTTPException(status_code=404)
            try:
                approvals.revoke(db, message, actor=Actor.APPLICANT, now=ui.campaign_clock.now())
            except (approvals.ApprovalError, approvals.CannotRecall) as exc:
                return _error_redirect(f"/messages/{message_id}", [str(exc)])
        return RedirectResponse(f"/messages/{message_id}?notice=revoked", status_code=303)

    @app.post("/campaigns/{campaign_id}/cancel")
    def cancel_initial(campaign_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise HTTPException(status_code=404)
            try:
                result = approvals.cancel_initial(
                    db, campaign, actor=Actor.APPLICANT, now=ui.campaign_clock.now()
                )
            except (approvals.ApprovalError, approvals.CancelRefused) as exc:
                return _error_redirect(f"/campaigns/{campaign_id}", [str(exc)])
        query = urlencode({"notice": result})
        return RedirectResponse(f"/campaigns/{campaign_id}?{query}", status_code=303)

    @app.post("/campaigns/{campaign_id}/stop")
    def stop_sequence(campaign_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise HTTPException(status_code=404)
            approvals.stop_sequence(
                db, campaign, actor=Actor.APPLICANT, now=ui.campaign_clock.now()
            )
        return RedirectResponse(f"/campaigns/{campaign_id}?notice=stopped", status_code=303)

    @app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
    def campaign_page(request: Request, campaign_id: int) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            now = ui.campaign_clock.now()
            campaign = db.get(Campaign, campaign_id)
            if campaign is None:
                raise HTTPException(status_code=404)
            pi = db.get(Pi, campaign.pi_id)
            messages = db.scalars(
                select(Message).where(Message.campaign_id == campaign.id).order_by(Message.step)
            ).all()
            reviews = replies.open_reviews(db, campaign.id)
            context = {
                "campaign": campaign,
                "pi": pi,
                "messages": messages,
                "reviews": [(p, replies.actions_for(p)) for p in reviews],
                "reply_tags": replies.REPLY_TAGS,
                "reply_possible": replies.reply_possible(db, campaign),
                "practice": Mode(campaign.mode) is Mode.SANDBOX,
                "tasks": db.scalars(select(Task).where(Task.campaign_id == campaign.id)).all(),
                "explain": explain,
                "draftable": drafting.draftable_step(db, campaign, now),
                "error": request.query_params.get("error", ""),
                "notice": request.query_params.get("notice", ""),
            }
        return ui.render(request, "campaign.html", context)


def _send_context(ui: UIContext, db: Any, message: Message, now: Any) -> dict[str, Any]:
    from marcelorian.ui.routes_send import send_context

    return send_context(db, message, now)
