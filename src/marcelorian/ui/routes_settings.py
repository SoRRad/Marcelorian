"""Settings: mode switch with the live readiness checklist, practice time, templates, Claude."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Actor, Mode
from marcelorian.platform.secrets import SecretStoreUnavailable
from marcelorian.services import ai, drafting, practice, runtime
from marcelorian.services.profile import FIELD_BY_KEY
from marcelorian.services.runtime import ModeSwitchError
from marcelorian.ui.context import UIContext

ADVANCE = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}
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
    """Keep the practice clock in step with the active mode."""
    with transaction(ui.session_factory) as db:
        mode = runtime.get_runtime(db, ui.clock.now()).active_mode
    ui.campaign_clock.set_active(mode is Mode.SANDBOX)


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
                "ai_on": ai.key_saved(db),
                "ai_usage": ai.usage(db),
                "ai_model": ai.MODEL,
                "ai_shared": [drafting_label(k) for k in ai.SHARED_FACTS],
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
        return _back("practice", notice="Practice time moved forward.")

    @app.post("/settings/template/{step}")
    def save_template(step: int, text: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                drafting.set_template(db, step, text, now=ui.clock.now())
            except drafting.DraftError as exc:
                return _back(f"t{step}", error=f"Email {step}: {exc}")
        return _back(f"t{step}", notice=f"Email {step} template saved.")

    @app.post("/settings/ai-key")
    def save_ai_key(api_key: str = Form("")) -> RedirectResponse:
        try:  # credential store and one free key check: outside any database transaction
            ai.check_and_store_key(ui.secrets(), ui.ai_client, api_key)
        except SecretStoreUnavailable:
            return _back(
                "ai", error="The Windows Credential Manager is unavailable on this computer."
            )
        except ai.AIError as exc:
            return _back("ai", error=str(exc))
        with transaction(ui.session_factory) as db:
            ai.record_key(db, True, now=ui.clock.now())
        return _back("ai", notice="Key checked and saved. Claude suggestions are on.")

    @app.post("/settings/ai-key/remove")
    def remove_ai_key() -> RedirectResponse:
        try:
            ui.secrets().delete(ai.KEY_NAME)
        except SecretStoreUnavailable:
            return _back(
                "ai", error="The Windows Credential Manager is unavailable on this computer."
            )
        with transaction(ui.session_factory) as db:
            ai.record_key(db, False, now=ui.clock.now())
        return _back("ai", notice="Key removed. Claude suggestions are off.")


def drafting_label(key: str) -> str:
    field = FIELD_BY_KEY.get(key)
    return field.label if field else key
