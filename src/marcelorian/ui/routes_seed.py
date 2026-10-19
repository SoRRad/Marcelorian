"""Import a lead list (research seed) from the browser: upload, preview, then import.

The same rules as ``python -m marcelorian.tools.seed_import`` apply: the file is parsed
locally (never a network connection), any invalid row refuses the whole file, and an
import only stages research leads; it never creates contacts, drafts, approvals or
campaigns. The uploaded file is held in memory until the applicant imports or cancels it.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Actor
from marcelorian.domain.seed import ParsedSeed, SeedFormatError, read_seed
from marcelorian.services.research_seed import SeedRejected, import_seed, preview
from marcelorian.ui.context import UIContext

MAX_BYTES = 20 * 1024 * 1024
MAX_LISTED_ERRORS = 25


@dataclass(frozen=True)
class _Upload:
    name: str
    sha256: str
    parsed: ParsedSeed


_pending: dict[str, _Upload] = {}
_lock = threading.Lock()


def _back(*, error: str = "", notice: str = "") -> RedirectResponse:
    params = {k: v[:600] for k, v in (("error", error), ("notice", notice)) if v}
    return RedirectResponse(
        "/research" + (f"?{urlencode(params)}" if params else ""), status_code=303
    )


def register(app: FastAPI, ui: UIContext) -> None:
    @app.post("/research/import")
    async def upload(file: UploadFile = File(...)) -> RedirectResponse:  # noqa: B008
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return _back(error="The file is larger than 20 MB.")
        name = (file.filename or "seed.csv").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:255]
        if not name.lower().endswith(".csv"):
            return _back(error="Choose the lead list .csv file.")
        try:
            text = data.decode("utf-8-sig")
            parsed = read_seed(io.StringIO(text, newline=""))
        except UnicodeDecodeError:
            return _back(error="The file is not UTF-8 text. Save it as CSV UTF-8 and try again.")
        except SeedFormatError as exc:
            return _back(error=f"This is not a Marcelorian lead list: {exc}.")
        token = secrets.token_urlsafe(16)
        with _lock:
            _pending.clear()  # one pending upload at a time
            _pending[token] = _Upload(name, hashlib.sha256(data).hexdigest(), parsed)
        return RedirectResponse(f"/research/import/{token}", status_code=303)

    @app.get("/research/import/{token}", response_class=HTMLResponse)
    def preview_page(request: Request, token: str) -> HTMLResponse:
        item = _pending.get(token)
        if item is None:
            raise HTTPException(status_code=404)
        with transaction(ui.session_factory) as db:
            plan = preview(db, item.parsed)
        context: dict[str, Any] = {
            "token": token,
            "name": item.name,
            "plan": plan,
            "counts": plan.counts(),
            "errors": plan.invalid[:MAX_LISTED_ERRORS],
            "more_errors": max(0, len(plan.invalid) - MAX_LISTED_ERRORS),
        }
        return ui.render(request, "seed_import.html", context)

    @app.post("/research/import/{token}/commit")
    def commit(token: str) -> RedirectResponse:
        with _lock:
            item = _pending.pop(token, None)
        if item is None:
            return _back(error="That upload has expired. Choose the file again.")
        try:
            with transaction(ui.session_factory) as db:
                result = import_seed(
                    db,
                    item.parsed,
                    source_name=item.name,
                    file_sha256=item.sha256,
                    now=ui.clock.now(),
                    actor=Actor.APPLICANT,
                )
        except SeedRejected:
            return _back(error="The file has invalid rows, so nothing was imported.")
        return _back(notice=f"Imported {result.row_count} leads (import #{result.import_id}).")

    @app.post("/research/import/{token}/cancel")
    def cancel(token: str) -> RedirectResponse:
        with _lock:
            _pending.pop(token, None)
        return _back(notice="Import cancelled. Nothing was changed.")
