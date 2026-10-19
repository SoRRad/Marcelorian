"""Your data: exports for the program owner, backups and restore, delete everything (P4)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Mode
from marcelorian.platform.paths import AppPaths
from marcelorian.services import backup, deletion, exports
from marcelorian.ui.context import UIContext

EXPORT_NAMES = frozenset(
    {"owner_report.csv", "status_summary.csv", "outreach_contacts.csv", "email_log.csv"}
)
EXPORT_MODES = {m.value: m for m in exports.MODE_FOLDERS}


def _back(*, error: str = "", notice: str = "") -> RedirectResponse:
    params = {k: v[:600] for k, v in (("error", error), ("notice", notice)) if v}
    return RedirectResponse("/data" + (f"?{urlencode(params)}" if params else ""), status_code=303)


def _paths(ui: UIContext) -> AppPaths:
    if ui.paths is None:
        raise HTTPException(status_code=404, detail="data folders are not configured")
    return ui.paths


def register(app: FastAPI, ui: UIContext) -> None:
    @app.get("/data", response_class=HTMLResponse)
    def data_page(request: Request) -> HTMLResponse:
        paths = ui.paths
        with transaction(ui.session_factory) as db:
            state = exports.load_state(db)
        context: dict[str, Any] = {
            "paths": paths,
            "export_state": state,
            "files": exports.list_files(paths.exports_dir) if paths else [],
            "backups": backup.list_backups(paths.backups_dir) if paths else [],
            "restore_pending": backup.restore_pending(paths.data_dir) if paths else False,
            "keep": backup.KEEP,
            "confirm_word": deletion.CONFIRM_WORD,
            "error": request.query_params.get("error", ""),
            "notice": request.query_params.get("notice", ""),
        }
        return ui.render(request, "data.html", context)

    @app.post("/data/export")
    def export_now() -> RedirectResponse:
        paths = _paths(ui)
        state = exports.refresh(ui.session_factory, paths.exports_dir, ui.clock, force=True)
        if state.stale:
            return _back(error=state.error)
        return _back(notice="Exports updated.")

    @app.get("/data/exports/{mode}/{name}")
    def download_export(mode: str, name: str) -> FileResponse:
        paths = _paths(ui)
        if mode not in EXPORT_MODES or name not in EXPORT_NAMES:
            raise HTTPException(status_code=404)
        path = exports.folder_for(paths.exports_dir, EXPORT_MODES[mode]) / name
        if not path.is_file():
            raise HTTPException(status_code=404)
        prefix = "" if mode == Mode.LIVE.value else f"{mode}-"
        return FileResponse(path, media_type="text/csv", filename=f"{prefix}{name}")

    @app.post("/data/backup")
    def backup_now() -> RedirectResponse:
        paths = _paths(ui)
        try:
            made = backup.backup_now(
                paths.db_path, paths.backups_dir, ui.clock.now(), label="manual"
            )
        except backup.BackupError as exc:
            return _back(error=str(exc))
        return _back(notice=f"Backup saved: {made.name}")

    @app.post("/data/restore")
    def restore(name: str = Form(""), confirm: str = Form("")) -> RedirectResponse:
        paths = _paths(ui)
        if confirm != "yes":
            return _back(error="Tick the box to confirm the restore.")
        try:
            backup.stage_restore(paths.backups_dir, name, paths.data_dir)
        except backup.BackupError as exc:
            return _back(error=str(exc))
        return _back(
            notice="Restore is ready. Close Marcelorian and start it again to finish; "
            "sending will be paused until you resume it."
        )

    @app.post("/data/restore/cancel")
    def cancel_restore() -> RedirectResponse:
        backup.cancel_restore(_paths(ui).data_dir)
        return _back(notice="Restore cancelled.")

    @app.post("/data/delete", response_class=HTMLResponse)
    def delete_all(
        request: Request,
        tasks: BackgroundTasks,
        word: str = Form(""),
        confirm: str = Form(""),
    ) -> Any:
        paths = _paths(ui)
        if word.strip() != deletion.CONFIRM_WORD or confirm != "yes":
            return _back(error=f"Type {deletion.CONFIRM_WORD} and tick the box to delete.")
        with transaction(ui.session_factory) as db:
            deletion.request_deletion(db, paths.data_dir, now=ui.clock.now())
        if ui.request_shutdown is not None:
            tasks.add_task(ui.request_shutdown)  # after this page is sent
        return ui.render(request, "deleting.html", {"remains": deletion.REMAINS})
