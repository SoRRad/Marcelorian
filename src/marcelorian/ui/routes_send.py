"""Sending and replies (P2): Outlook hand-off, practice send, reply tags, reviews, tasks.

* Real modes: the approved email is handed to Outlook only at the applicant's click,
  after every final check passes (``handoff.hand_off``); the applicant presses Send in
  Outlook and records Sent or Not sent (owner decisions S1-S6).
* Practice mode: the practice button runs the same gateway with the fake transport.
"""

from __future__ import annotations

import random
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Institution, Message, Pi, SendAttempt
from marcelorian.domain.enums import Actor, Channel, MessageState, Mode
from marcelorian.services import approvals, handoff, replies
from marcelorian.ui.context import UIContext
from marcelorian.ui.reasons import explain

_RNG = random.SystemRandom()


def _redirect(url: str, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    params = {k: v[:800] for k, v in (("error", error), ("notice", notice)) if v}
    return RedirectResponse(url + (f"?{urlencode(params)}" if params else ""), status_code=303)


def send_context(db: Session, message: Message, now: datetime) -> dict[str, Any]:
    """What the message page's send section shows (approved, waiting to be sent)."""
    if message.state != MessageState.APPROVED_QUEUED.value:
        return {}
    settings = load_settings(db)
    status = handoff.readiness(db, message, settings, now)
    campaign = db.get(Campaign, message.campaign_id)
    assert campaign is not None
    pi = db.get(Pi, campaign.pi_id)
    institution = db.get(Institution, pi.institution_id) if pi else None
    unlock_local = None
    if status.unlock_at and institution and institution.iana_tz:
        unlock_local = status.unlock_at.astimezone(ZoneInfo(institution.iana_tz))
    channel = approvals.message_channel(db, Mode(message.mode))
    return {
        "send": {
            "ready": status.ready,
            "reason": explain(status.reason),
            "unlock_at": status.unlock_at,
            "unlock_local": unlock_local,
            "pi_zone": institution.iana_tz if institution else "",
            "confirmation": status.confirmation,
            "practice": Mode(message.mode) is Mode.SANDBOX,
            "channel": channel.value if channel else None,
            "addresses": handoff.addresses_to_search(db, message),
        }
    }


def register(app: FastAPI, ui: UIContext) -> None:
    @app.post("/messages/{message_id}/handoff")
    def open_in_outlook(
        message_id: int, confirm: str = Form(""), method: str = Form("link")
    ) -> RedirectResponse:
        url = f"/messages/{message_id}"
        with transaction(ui.session_factory) as db:
            now = ui.campaign_clock.now()
            message = db.get(Message, message_id)
            if message is None:
                raise HTTPException(status_code=404)
            confirmations = handoff.Confirmations(
                prior_contact_checked=confirm == "yes" and message.step == 1,
                no_reply=confirm == "yes" and message.step > 1,
            )
            result = handoff.hand_off(
                db,
                message,
                confirmations,
                settings=load_settings(db),
                rng=_RNG,
                actor=Actor.APPLICANT,
                now=now,
            )
        if result.outcome != handoff.HANDED_OFF or result.attempt_uid is None:
            return _redirect(url, error=explain(result.reason) or result.outcome)
        query = urlencode({"method": method if method in ("link", "file", "copy") else "link"})
        return RedirectResponse(f"/handoffs/{result.attempt_uid}?{query}", status_code=303)

    @app.get("/handoffs/{attempt_uid}", response_class=HTMLResponse)
    def handoff_page(request: Request, attempt_uid: str) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            attempt = db.scalar(select(SendAttempt).where(SendAttempt.attempt_uid == attempt_uid))
            if attempt is None:
                raise HTTPException(status_code=404)
            message = db.get(Message, attempt.message_id)
            assert message is not None
            campaign = db.get(Campaign, message.campaign_id)
            assert campaign is not None
            context: dict[str, Any] = {
                "attempt": attempt,
                "message": message,
                "campaign": campaign,
                "pi": db.get(Pi, campaign.pi_id),
                "open": attempt.status == "handed_off",
                "method": request.query_params.get("method", "link"),
                "error": request.query_params.get("error", ""),
            }
            if attempt.status == "handed_off":
                context["package"] = handoff.package_for(db, attempt_uid)
        return ui.render(request, "handoff.html", context)

    @app.get("/handoffs/{attempt_uid}/email.eml")
    def handoff_file(attempt_uid: str) -> Response:
        with transaction(ui.session_factory) as db:
            try:
                package = handoff.package_for(db, attempt_uid)
            except (handoff.HandoffError, LookupError, ValueError) as exc:
                raise HTTPException(status_code=404) from exc
        return Response(
            content=package.eml,
            media_type="message/rfc822",
            headers={"Content-Disposition": f'attachment; filename="{package.eml_filename}"'},
        )

    @app.post("/handoffs/{attempt_uid}/sent")
    def handoff_sent(attempt_uid: str, edited: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            message_id = _attempt_message(db, attempt_uid)
            try:
                handoff.record_sent(
                    db,
                    attempt_uid,
                    edited=edited == "yes",
                    settings=load_settings(db),
                    actor=Actor.APPLICANT,
                    now=ui.campaign_clock.now(),
                )
            except (handoff.HandoffError, ValueError) as exc:
                return _redirect(f"/handoffs/{attempt_uid}", error=str(exc))
        return _redirect(f"/messages/{message_id}", notice="sent")

    @app.post("/handoffs/{attempt_uid}/not-sent")
    def handoff_not_sent(attempt_uid: str, note: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            message_id = _attempt_message(db, attempt_uid)
            try:
                handoff.record_not_sent(
                    db,
                    attempt_uid,
                    note=note[:500] or None,
                    actor=Actor.APPLICANT,
                    now=ui.campaign_clock.now(),
                )
            except (handoff.HandoffError, ValueError) as exc:
                return _redirect(f"/handoffs/{attempt_uid}", error=str(exc))
        return _redirect(f"/messages/{message_id}", notice="not_sent")

    @app.post("/messages/{message_id}/practice-send")
    def practice_send(message_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            message = db.get(Message, message_id)
            if message is None:
                raise HTTPException(status_code=404)
            if Mode(message.mode) is not Mode.SANDBOX:
                raise HTTPException(status_code=400, detail="practice send is sandbox-only")
        if ui.gateway is None:  # pragma: no cover - always configured by create_app
            raise HTTPException(status_code=503)
        result = ui.gateway.dispatch(message_id, actor=Actor.APPLICANT)
        if result.outcome.value != "sent":
            return _redirect(f"/messages/{message_id}", error=explain(result.reason))
        return _redirect(f"/messages/{message_id}", notice="sent")

    @app.post("/campaigns/{campaign_id}/reply")
    def campaign_reply(
        campaign_id: int,
        tag: str = Form(""),
        note: str = Form(""),
        later: str = Form(""),
    ) -> RedirectResponse:
        url = f"/campaigns/{campaign_id}"
        later_on = None
        if later:
            try:
                later_on = date.fromisoformat(later)
            except ValueError:
                return _redirect(url, error="Use a date like 2027-03-01.")
        with transaction(ui.session_factory) as db:
            campaign = _campaign(db, campaign_id)
            try:
                replies.record_reply(
                    db,
                    campaign,
                    tag,
                    note=note,
                    contact_later_on=later_on,
                    now=ui.campaign_clock.now(),
                )
            except (replies.ReplyError, handoff.HandoffError) as exc:
                return _redirect(url, error=str(exc))
        return _redirect(url, notice="reply_recorded")

    @app.post("/campaigns/{campaign_id}/simulate")
    def campaign_simulate(campaign_id: int, kind: str = Form("")) -> RedirectResponse:
        url = f"/campaigns/{campaign_id}"
        with transaction(ui.session_factory) as db:
            campaign = _campaign(db, campaign_id)
            try:
                replies.simulate(db, campaign, kind, now=ui.campaign_clock.now())
            except replies.ReplyError as exc:
                return _redirect(url, error=str(exc))
        return _redirect(url, notice=f"simulated_{kind}")

    @app.post("/campaigns/{campaign_id}/close")
    def campaign_close(campaign_id: int, reason: str = Form("")) -> RedirectResponse:
        url = f"/campaigns/{campaign_id}"
        with transaction(ui.session_factory) as db:
            campaign = _campaign(db, campaign_id)
            try:
                replies.close(db, campaign, reason, now=ui.campaign_clock.now())
            except replies.ReplyError as exc:
                return _redirect(url, error=str(exc))
        return _redirect(url, notice="closed")

    @app.post("/campaigns/{campaign_id}/prior-contact")
    def campaign_prior_contact(campaign_id: int, note: str = Form("")) -> RedirectResponse:
        url = f"/campaigns/{campaign_id}"
        with transaction(ui.session_factory) as db:
            campaign = _campaign(db, campaign_id)
            try:
                handoff.report_prior_contact(
                    db, campaign, note=note, actor=Actor.APPLICANT, now=ui.campaign_clock.now()
                )
            except handoff.HandoffError as exc:
                return _redirect(url, error=str(exc))
        return _redirect(url, notice="review_opened")

    @app.post("/reviews/{pause_id}")
    def resolve_review(
        pause_id: int, action: str = Form(""), campaign_id: int = Form(0)
    ) -> RedirectResponse:
        url = f"/campaigns/{campaign_id}" if campaign_id else "/"
        with transaction(ui.session_factory) as db:
            mailbox = approvals._active_mailbox(db)
            try:
                replies.resolve_review(
                    db,
                    pause_id,
                    action,
                    now=ui.campaign_clock.now(),
                    mailbox_email=mailbox.email if mailbox else None,
                )
            except (replies.ReplyError, ValueError) as exc:
                return _redirect(url, error=str(exc))
        return _redirect(url, notice="review_resolved")

    @app.get("/tasks", response_class=HTMLResponse)
    def task_page(request: Request) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            rows = []
            for task in replies.open_tasks(db):
                campaign = db.get(Campaign, task.campaign_id)
                pi = db.get(Pi, campaign.pi_id) if campaign else None
                rows.append((task, campaign, pi))
        return ui.render(request, "tasks.html", {"rows": rows})

    @app.post("/tasks/{task_id}/done")
    def task_done(task_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                replies.complete_task(db, task_id, now=ui.campaign_clock.now())
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
        return RedirectResponse("/tasks", status_code=303)


def _campaign(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404)
    return campaign


def _attempt_message(db: Session, attempt_uid: str) -> int:
    attempt = db.scalar(select(SendAttempt).where(SendAttempt.attempt_uid == attempt_uid))
    if attempt is None:
        raise HTTPException(status_code=404)
    return attempt.message_id


def channel_label(channel: str | None) -> str:
    return {Channel.OUTLOOK_HANDOFF.value: "Outlook", Channel.SANDBOX.value: "practice"}.get(
        channel or "", channel or "none"
    )
