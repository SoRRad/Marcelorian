"""Profile pages: facts, CV, signature and the Outlook address (spec §4, §16)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from marcelorian.config.settings import SettingsError, load_settings, set_signature
from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Actor
from marcelorian.domain.recipients import RecipientError
from marcelorian.lookups import enrich
from marcelorian.services import approvals, profile
from marcelorian.ui.context import UIContext


def _back(anchor: str, error: str | None = None) -> RedirectResponse:
    query = f"?{urlencode({'error': error[:300]})}" if error else ""
    return RedirectResponse(f"/profile{query}#{anchor}", status_code=303)


def register(app: FastAPI, ui: UIContext) -> None:
    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            now = ui.clock.now()
            me = profile.applicant(db, now)
            settings = load_settings(db)
            context: dict[str, Any] = {
                "fields": profile.FIELDS,
                "facts": profile.facts(db, me.id),
                "missing": profile.missing_required(db, me.id),
                "cvs": profile.cv_versions(db, me.id),
                "signature": settings.email_signature,
                "signature_done": settings.signature_step_done,
                "mailbox": approvals._active_mailbox(db),
                "error": request.query_params.get("error", ""),
                "publications": enrich.applicant_publications(db, me.id),
                "pub_run": enrich.last_runs(db, None).get("pubmed"),
            }
        return ui.render(request, "profile.html", context)

    @app.post("/profile/fact")
    def save_fact(
        key: str = Form(...), value: str = Form(""), action: str = Form("draft")
    ) -> RedirectResponse:
        if key not in profile.FIELD_BY_KEY:
            raise HTTPException(status_code=400, detail="unknown field")
        with transaction(ui.session_factory) as db:
            now = ui.clock.now()
            me = profile.applicant(db, now)
            try:
                profile.set_fact(
                    db,
                    me.id,
                    key,
                    value,
                    confirm=action == "confirm",
                    actor=Actor.APPLICANT,
                    now=now,
                )
            except profile.ProfileError as exc:
                return _back(f"f-{key}", str(exc))
        return _back(f"f-{key}")

    @app.post("/profile/cv")
    async def upload_cv(file: UploadFile = File(...)) -> RedirectResponse:  # noqa: B008
        data = await file.read(profile.MAX_CV_BYTES + 1)
        with transaction(ui.session_factory) as db:
            now = ui.clock.now()
            me = profile.applicant(db, now)
            try:
                name = profile.check_cv(file.filename or "cv.pdf", data)
            except profile.ProfileError as exc:
                return _back("cv", str(exc))
            approvals.upload_cv(
                db,
                applicant_id=me.id,
                filename=name,
                content_type="application/pdf",
                data=data,
                actor=Actor.APPLICANT,
                now=now,
            )
        return _back("cv")

    @app.post("/profile/publications/import")
    def import_publications() -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            me = profile.applicant(db, ui.clock.now())
            facts = profile.facts(db, me.id)
            name = facts["full_name"].value if "full_name" in facts else ""
            orcid = facts["orcid"].value if "orcid" in facts else ""
        result = enrich.gather_applicant_publications(name, orcid, ui.fetcher)  # no DB open
        with transaction(ui.session_factory) as db:
            enrich.store_applicant_publications(db, me.id, result, ui.clock.now())
        return _back("publications", None if result.ok else result.message)

    @app.post("/profile/publications/{publication_id}")
    def decide_publication(publication_id: int, decision: str = Form(...)) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            me = profile.applicant(db, ui.clock.now())
            try:
                enrich.decide_publication(
                    db, me.id, publication_id, confirm=decision == "confirm", now=ui.clock.now()
                )
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
        return _back("publications")

    @app.post("/profile/signature")
    def save_signature(signature: str = Form("")) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                set_signature(db, signature, actor=Actor.APPLICANT, now=ui.clock.now())
            except SettingsError as exc:
                return _back("signature", str(exc))
        return _back("signature")

    @app.post("/profile/outlook")
    def save_outlook(email: str = Form(...)) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                approvals.connect_handoff_mailbox(
                    db, email, actor=Actor.APPLICANT, now=ui.clock.now()
                )
            except (RecipientError, approvals.MailboxIdentityRefused, ValueError) as exc:
                return _back("outlook", str(exc))
        return _back("outlook")
