"""Settings: mode switch, live readiness checklist, practice time, templates, Claude, lookups."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from marcelorian.config.settings import load_settings, update_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Message
from marcelorian.domain.enums import Actor, Mode
from marcelorian.lookups import keys as lookup_keys
from marcelorian.platform.secrets import SecretStoreUnavailable
from marcelorian.services import drafting, handoff, practice, runtime, status
from marcelorian.services.profile import FIELD_BY_KEY
from marcelorian.services.runtime import ModeSwitchError
from marcelorian.ui.context import UIContext

ADVANCE = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}
SCOPE_LABELS = {
    "institution": "Institution (or institution group)",
    "department": "Department within an institution",
    "lab": "Lab",
}
MODES = {
    Mode.SANDBOX.value: "Practice (sandbox): nothing reaches a real PI",
    Mode.LIVE.value: "Live: real emails, sent by you from Outlook",
}


def _back(anchor: str, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    params = {k: v[:600] for k, v in (("error", error), ("notice", notice)) if v}
    return RedirectResponse(
        "/settings" + (f"?{urlencode(params)}" if params else "") + f"#{anchor}", status_code=303
    )


def sync_practice_clock(ui: UIContext) -> None:
    """Keep the practice clock in step with the active mode; the worker re-checks soon."""
    with transaction(ui.session_factory) as db:
        mode = runtime.get_runtime(db, ui.clock.now()).active_mode
    ui.campaign_clock.set_active(mode is Mode.SANDBOX)
    ui.wake_worker()


def register(app: FastAPI, ui: UIContext) -> None:
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            now = ui.clock.now()
            view = runtime.get_runtime(db, now)
            context: dict[str, Any] = {
                "runtime": view,
                "modes": MODES,
                "readiness": practice.readiness(db, now),
                "practice_now": ui.campaign_clock.now(),
                "practice_offset": ui.campaign_clock.offset,
                "templates": {step: drafting.template(db, step) for step in (1, 2, 3, 4)},
                "settings": load_settings(db),
                "places": status.outreach_status(
                    db, view.active_mode, load_settings(db).same_place_scope, now
                ).places,
                "scope_labels": SCOPE_LABELS,
                "scope_error": request.query_params.get("scope_error", ""),
                "lookup_keys": lookup_keys.saved(db),
                "lookup_labels": lookup_keys.LABELS,
                "ncbi_email": lookup_keys.contact_email(db),
                "error": request.query_params.get("error", ""),
                "notice": request.query_params.get("notice", ""),
            }
        return ui.render(request, "settings.html", context)

    @app.post("/settings/readiness")
    def readiness(key: str = Form(...), value: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                practice.set_attestation(db, key, value == "yes", now=ui.clock.now())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        sync_practice_clock(ui)
        return _back("mode")

    @app.post("/settings/mode")
    def switch_mode(
        mode: str = Form(...), confirm: str = Form(""), resume: str = Form("")
    ) -> RedirectResponse:
        if mode not in MODES:
            raise HTTPException(status_code=400, detail="unknown mode")
        if confirm != "yes":
            return _back("mode", error="Tick the box to confirm the mode switch.")
        with transaction(ui.session_factory) as db:
            now = ui.clock.now()
            practice.refresh_readiness(db, now)
            try:
                runtime.switch_mode(db, Mode(mode), confirmed=True, actor=Actor.APPLICANT, now=now)
            except ModeSwitchError as exc:
                return _back("mode", error=f"{exc}. Complete the checklist first.")
            if resume == "yes":
                runtime.resume_dispatch(db, actor=Actor.APPLICANT, now=now)
        sync_practice_clock(ui)
        return _back("mode", notice=f"Switched to {mode}.")

    @app.post("/settings/practice-time")
    def advance(step: str = Form(...)) -> RedirectResponse:
        if step not in ADVANCE:
            raise HTTPException(status_code=400, detail="unknown step")
        if not ui.campaign_clock.active:
            return _back("practice", error="Practice time moves only in practice mode.")
        offset = ui.campaign_clock.advance(ADVANCE[step])
        with transaction(ui.session_factory) as db:
            practice.save_offset(db, offset, now=ui.clock.now())
        ui.wake_worker()
        return _back("practice", notice="Practice time moved forward.")

    @app.post("/settings/practice-time/until")
    def skip_to_send_time(message_id: int = Form(...)) -> RedirectResponse:
        """Practice only: move the practice clock forward to when this email unlocks."""
        url = f"/messages/{message_id}"
        if not ui.campaign_clock.active:
            return RedirectResponse(url, status_code=303)
        with transaction(ui.session_factory) as db:
            message = db.get(Message, message_id)
            if message is None or Mode(message.mode) is not Mode.SANDBOX:
                raise HTTPException(status_code=404)
            status = handoff.readiness(db, message, load_settings(db), ui.campaign_clock.now())
        if status.unlock_at is None or status.unlock_at <= ui.campaign_clock.now():
            return RedirectResponse(url, status_code=303)
        offset = ui.campaign_clock.advance(status.unlock_at - ui.campaign_clock.now())
        with transaction(ui.session_factory) as db:
            practice.save_offset(db, offset, now=ui.clock.now())
        ui.wake_worker()
        return RedirectResponse(url + "#send", status_code=303)

    @app.post("/settings/template/{step}")
    def save_template(step: int, text: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                drafting.set_template(db, step, text, now=ui.clock.now())
            except drafting.DraftError as exc:
                return _back(f"t{step}", error=f"Email {step}: {exc}")
        return _back(f"t{step}", notice=f"Email {step} template saved.")

    @app.post("/settings/strict")
    def save_strict(strict: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            update_settings(
                db,
                {"strict_send_rules": strict == "yes"},
                actor=Actor.APPLICANT,
                now=ui.clock.now(),
            )
        ui.wake_worker()
        return _back(
            "sending",
            notice="Strict send rules are on." if strict == "yes" else "Strict send rules are off.",
        )

    @app.post("/settings/lookup-key/{name}")
    def save_lookup_key(name: str, api_key: str = Form("")) -> RedirectResponse:
        if name not in lookup_keys.SECRET_NAMES:
            raise HTTPException(status_code=404)
        try:  # credential store only: outside any database transaction
            lookup_keys.check_and_store(ui.secrets(), name, api_key)
        except SecretStoreUnavailable:
            return _back(
                "lookups", error="The Windows Credential Manager is unavailable on this computer."
            )
        except lookup_keys.KeyInputError as exc:
            return _back("lookups", error=str(exc))
        with transaction(ui.session_factory) as db:
            lookup_keys.record(db, name, True, now=ui.clock.now())
        return _back("lookups", notice=f"{lookup_keys.LABELS[name]} key saved.")

    @app.post("/settings/lookup-key/{name}/remove")
    def remove_lookup_key(name: str) -> RedirectResponse:
        if name not in lookup_keys.SECRET_NAMES:
            raise HTTPException(status_code=404)
        try:
            ui.secrets().delete(lookup_keys.SECRET_NAMES[name])
        except SecretStoreUnavailable:
            return _back(
                "lookups", error="The Windows Credential Manager is unavailable on this computer."
            )
        with transaction(ui.session_factory) as db:
            lookup_keys.record(db, name, False, now=ui.clock.now())
        return _back("lookups", notice=f"{lookup_keys.LABELS[name]} key removed.")

    @app.post("/settings/ncbi-email")
    def save_ncbi_email(email: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                lookup_keys.set_contact_email(db, email, now=ui.clock.now())
            except lookup_keys.KeyInputError as exc:
                return _back("lookups", error=str(exc))
        return _back("lookups", notice="NCBI contact address saved.")


def drafting_label(key: str) -> str:
    field = FIELD_BY_KEY.get(key)
    return field.label if field else key
