"""Contacts: promotion from the research catalog, and the candidates pages (P1)."""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Institution, Pi, PiEmail, ResearchRecord
from marcelorian.domain.enums import Mode
from marcelorian.domain.research import is_web_url
from marcelorian.lookups import bulk, enrich
from marcelorian.services import (
    candidates,
    contact_emails,
    dnc,
    pipeline,
    promotion,
    reservations,
    runtime,
    tailoring,
)
from marcelorian.services.promotion import EMAIL_SOURCES
from marcelorian.services.recipients import send_ready_email
from marcelorian.ui.context import UIContext

US_ZONES = (
    "America/New_York",
    "America/Detroit",
    "America/Indiana/Indianapolis",
    "America/Chicago",
    "America/Denver",
    "America/Boise",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Puerto_Rico",
)


def promotion_context(db: Session, record: ResearchRecord, today: date) -> dict[str, Any]:
    parts = record.name.split()
    last = parts[-1] if parts else ""
    identity, same_name = promotion.possible_duplicates(db, record)
    zones = list(US_ZONES)
    if record.timezone_iana and record.timezone_iana not in zones:
        zones.insert(0, record.timezone_iana)
    source = (
        "recent_publication"
        if record.email_status == "publication_attribution_observed"
        else "institutional_page"
    )
    return {
        "blockers": promotion.blockers(db, record, today),
        "identity": identity,
        "same_name": same_name,
        "last_name": last,
        "salutation": f"Dr. {last}" if last else "",
        "zones": zones,
        "zone": record.timezone_iana,
        "email_sources": promotion.EMAIL_SOURCES,
        "email_source": source,
        "email_source_url": record.email_source_url,
    }


def _candidate_filters(params: Any) -> candidates.Filters:
    institution = params.get("institution", "")
    return candidates.Filters(
        fit=params.get("fit", "")[:16],
        grants=params.get("grants", "")[:4],
        state=params.get("state", "")[:16],
        institution_id=int(institution) if institution.isdigit() else None,
        contact=params.get("contact", "")[:4],
        show_skipped=params.get("skipped", "") == "yes",
        query=params.get("query", "")[:200],
    )


def register(app: FastAPI, ui: UIContext) -> None:
    @app.get("/candidates", response_class=HTMLResponse)
    def candidate_list(request: Request) -> HTMLResponse:
        filters = _candidate_filters(request.query_params)
        with transaction(ui.session_factory) as db:
            now = ui.clock.now()
            view = runtime.get_runtime(db, now)
            scope = load_settings(db).same_place_scope
            rows = candidates.list_candidates(db, filters, view.active_mode, scope, now)
            institutions = candidates.institutions(db)
            lookup_needed = len(bulk.needing_lookup(db, now))
        return ui.render(
            request,
            "candidates.html",
            {
                "lookup": ui.bulk.progress() if ui.bulk else None,
                "lookup_needed": lookup_needed,
                "rows": rows,
                "filters": filters,
                "fits": candidates.FITS,
                "institutions": institutions,
                "mode": view.active_mode.value,
                "notice": request.query_params.get("notice", ""),
            },
        )

    @app.get("/candidates/{pi_id}", response_class=HTMLResponse)
    def candidate_detail(request: Request, pi_id: int) -> HTMLResponse:
        with transaction(ui.session_factory) as db:
            now = ui.clock.now()
            pi = db.get(Pi, pi_id)
            if pi is None:
                raise HTTPException(status_code=404)
            view = runtime.get_runtime(db, now)
            scope = load_settings(db).same_place_scope
            context = {
                "pi": pi,
                "institution": db.get(Institution, pi.institution_id),
                "emails": db.scalars(select(PiEmail).where(PiEmail.pi_id == pi.id)).all(),
                "ready": send_ready_email(db, pi.id),
                "assessment": candidates.assessment(db, pi.id),
                "fits": candidates.FITS,
                "evidence": enrich.evidence_for(db, pi.id),
                "grants": enrich.active_grants(db, pi.id, now.date()),
                "runs": enrich.last_runs(db, pi.id),
                "busy": reservations.place_conflicts_for_pi(
                    db, pi.id, view.active_mode, scope, now
                ),
                "records": candidates.research_records_for(db, pi.id),
                "postings": tailoring.linked_postings(db, pi.id),
                "verified_topics": tailoring.verified_topics(db, pi.id),
                "campaigns": db.scalars(select(Campaign).where(Campaign.pi_id == pi.id)).all(),
                "dnc": dnc.matches(db, pi.id, Mode.LIVE),
                "is_web_url": is_web_url,
                "error": request.query_params.get("error", ""),
                "notice": request.query_params.get("notice", ""),
                "email_sources": {k: v[1] for k, v in EMAIL_SOURCES.items()},
                "card": next(
                    (
                        c
                        for col in pipeline.board(db, view.active_mode, now.date()).columns
                        for c in col.cards
                        if c.key == f"pi-{pi.id}"
                    ),
                    None,
                ),
                "stages": pipeline.STAGES,
            }
        return ui.render(request, "candidate_detail.html", context)

    @app.post("/candidates/{pi_id}/lookup")
    def candidate_lookup(pi_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                query = enrich.pi_query(db, pi_id)
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
        keys = ui.lookup_keys()  # no DB open from here to the store
        results = enrich.gather_for_pi(query, ui.fetcher, ui.clock.now().date(), keys)
        with transaction(ui.session_factory) as db:
            enrich.store_pi_results(db, pi_id, results, ui.clock.now())
        return RedirectResponse(f"/candidates/{pi_id}#evidence", status_code=303)

    @app.post("/candidates/{pi_id}/emails")
    def add_email(
        pi_id: int,
        address: str = Form(""),
        source: str = Form(""),
        source_url: str = Form(""),
        replace_current: str = Form(""),
    ) -> RedirectResponse:
        """Change or add the contact's address (owner request 2026-10-27)."""
        url = f"/candidates/{pi_id}"
        with transaction(ui.session_factory) as db:
            if db.get(Pi, pi_id) is None:
                raise HTTPException(status_code=404)
            try:
                contact_emails.add_address(
                    db,
                    pi_id,
                    address,
                    source=source,
                    source_url=source_url,
                    replace_current=replace_current == "yes",
                    now=ui.clock.now(),
                )
            except (contact_emails.AddressError, ValueError) as exc:
                return RedirectResponse(
                    f"{url}?{urlencode({'error': str(exc)[:300]})}#emails", status_code=303
                )
        return RedirectResponse(
            f"{url}?{urlencode({'notice': 'Address saved.'})}#emails", status_code=303
        )

    @app.post("/candidates/{pi_id}/emails/{email_id}/remove")
    def remove_email(pi_id: int, email_id: int) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                contact_emails.retire(
                    db, pi_id, email_id, reason="removed by the applicant", now=ui.clock.now()
                )
            except contact_emails.AddressError as exc:
                raise HTTPException(status_code=404) from exc
        return RedirectResponse(f"/candidates/{pi_id}#emails", status_code=303)

    @app.post("/candidates/lookup-all")
    def lookup_all(back: str = Form("")) -> RedirectResponse:
        """Start the background lookup for every contact that needs one."""
        if ui.bulk is None:  # pragma: no cover - always configured by create_app
            raise HTTPException(status_code=503)
        count = ui.bulk.start()
        notice = f"Looking up {count} contacts in the background." if count else "Nothing to do."
        page = "/" if back == "home" else "/candidates"
        return RedirectResponse(
            f"{page}?{urlencode({'notice': notice})}#lookup-all", status_code=303
        )

    @app.post("/candidates/lookup-all/stop")
    def lookup_all_stop(back: str = Form("")) -> RedirectResponse:
        if ui.bulk is not None:
            ui.bulk.stop()
        return RedirectResponse(f"{'/' if back == 'home' else '/candidates'}#lookup-all", 303)

    @app.post("/candidates/{pi_id}/postings/{record_id}")
    def candidate_posting(pi_id: int, record_id: int, action: str = Form("")) -> RedirectResponse:
        """Confirm (or withdraw) that a linked posted position is current (T2)."""
        url = f"/candidates/{pi_id}"
        with transaction(ui.session_factory) as db:
            if db.get(Pi, pi_id) is None:
                raise HTTPException(status_code=404)
            try:
                if action == "confirm":
                    tailoring.confirm_posting(db, pi_id, record_id, now=ui.clock.now())
                elif action == "withdraw":
                    tailoring.withdraw_posting(db, pi_id, record_id, now=ui.clock.now())
                else:
                    raise HTTPException(status_code=400)
            except tailoring.TailoringError as exc:
                return RedirectResponse(f"{url}?{urlencode({'error': str(exc)})}#postings", 303)
        return RedirectResponse(f"{url}#postings", status_code=303)

    @app.post("/candidates/{pi_id}/assessment")
    def candidate_assessment(
        pi_id: int, fit: str = Form(""), note: str = Form("")
    ) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                candidates.set_fit(db, pi_id, fit, note, now=ui.clock.now())
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/candidates/{pi_id}#assessment", status_code=303)

    @app.post("/candidates/{pi_id}/skip")
    def candidate_skip(
        pi_id: int, skipped: str = Form(...), reason: str = Form("")
    ) -> RedirectResponse:
        with transaction(ui.session_factory) as db:
            try:
                candidates.set_skipped(db, pi_id, skipped == "yes", reason, now=ui.clock.now())
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
        return RedirectResponse(f"/candidates/{pi_id}", status_code=303)

    @app.post("/candidates/{pi_id}/dnc")
    def candidate_dnc(
        pi_id: int, confirm: str = Form(""), reason: str = Form("")
    ) -> RedirectResponse:
        if confirm != "yes":
            query = urlencode({"error": "Tick the box to confirm do-not-contact."})
            return RedirectResponse(f"/candidates/{pi_id}?{query}#dnc", status_code=303)
        with transaction(ui.session_factory) as db:
            try:
                candidates.add_to_dnc(db, pi_id, reason, now=ui.clock.now())
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
        return RedirectResponse(f"/candidates/{pi_id}#dnc", status_code=303)

    @app.post("/research/{record_id}/promote")
    def promote(
        record_id: int,
        last_name: str = Form(""),
        salutation: str = Form(""),
        email_source: str = Form(""),
        email_source_url: str = Form(""),
        timezone: str = Form(""),
        timezone_confirmed: str = Form(""),
        department: str = Form(""),
        not_same_person: str = Form(""),
    ) -> RedirectResponse:
        data = promotion.PromotionInput(
            last_name=last_name[:120],
            salutation=salutation[:120],
            email_source=email_source,
            email_source_url=email_source_url[:2000],
            timezone=timezone[:64],
            timezone_confirmed=timezone_confirmed == "yes",
            department=department[:255],
            not_same_person=not_same_person == "yes",
        )
        with transaction(ui.session_factory) as db:
            try:
                promotion.promote(db, record_id, data, now=ui.clock.now())
            except LookupError as exc:
                raise HTTPException(status_code=404) from exc
            except promotion.PromotionError as exc:
                query = urlencode({"promote_error": " ".join(exc.reasons)[:600]})
                return RedirectResponse(f"/research/{record_id}?{query}#promote", status_code=303)
        return RedirectResponse(f"/research/{record_id}#promote", status_code=303)
