"""Start Marcelorian on this computer: ``python -m marcelorian``.

1. Takes the single-instance lock for the data folder (spec §2), and finishes an
   interrupted "Delete all my data" if one is pending.
2. Finishes a staged restore (sending then starts paused), backs up before an update,
   upgrades the database and makes sure the applicant profile exists.
3. Serves the local UI on 127.0.0.1 at a free port and opens it in the browser with
   a one-time sign-in link (the link is never logged).
4. Starts the background worker (startup recovery, then a check every minute: the
   missed-window rule, due follow-up drafts, notices).
5. Keeps running until the window is closed or Ctrl+C is pressed. Pressing Enter opens
   the app in the browser again (a sign-in link is valid for two minutes).

Nothing here sends email: in the Outlook hand-off channel only the applicant's own
click in Outlook sends (owner decision S1).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from marcelorian.clock import Clock, SystemClock
from marcelorian.db.engine import make_engine, make_session_factory, transaction
from marcelorian.db.migrate import needs_upgrade, upgrade
from marcelorian.domain.enums import Actor, RuntimePauseReason
from marcelorian.logging_setup import UVICORN_LOG_KWARGS, configure_logging
from marcelorian.platform.instance_lock import AlreadyRunning, InstanceLock
from marcelorian.platform.paths import AppPaths, resolve_app_paths
from marcelorian.platform.secrets import open_secret_store
from marcelorian.services import backup, deletion, profile, runtime
from marcelorian.ui.app import create_app, worker_of
from marcelorian.ui.security import UISecurity

log = logging.getLogger(__name__)
HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def prepare_database(db_path: Path, clock: Clock, backups_dir: Path | None = None) -> Any:
    """Finish a staged restore, back up before an update, migrate, seed first-run state.

    Returns the session factory. A restored database starts with sending paused (§14).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backups = backups_dir or db_path.parent / "backups"
    restored = backup.apply_pending_restore(db_path, backups, clock.now())
    existed = db_path.is_file()
    engine = make_engine(db_path)
    if existed and needs_upgrade(engine):
        backup.backup_now(db_path, backups, clock.now(), label="before-update")
    upgrade(engine)
    factory = make_session_factory(engine)
    with transaction(factory) as session:
        now = clock.now()
        profile.applicant(session, now)
        runtime.get_runtime(session, now)  # first run: sandbox mode (spec §16)
        if restored:
            runtime.pause_dispatch(
                session, actor=Actor.SYSTEM, now=now, reason=RuntimePauseReason.RESTORED_BACKUP
            )
    return factory


def open_in_browser(security: UISecurity, opener: Callable[[str], Any] = webbrowser.open) -> None:
    opener(security.bootstrap_url(security.issue_bootstrap_token()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start Marcelorian.")
    parser.add_argument("--data-dir", type=Path, default=None, help="default: the app data folder")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    resolved = resolve_app_paths()
    data_dir: Path = args.data_dir or resolved.data_dir
    paths = AppPaths(
        data_dir=data_dir,
        exports_dir=data_dir / "exports" if args.data_dir else resolved.exports_dir,
        exports_synced_by_onedrive=False if args.data_dir else resolved.exports_synced_by_onedrive,
    )
    configure_logging(paths.logs_dir)
    logging.getLogger("alembic").setLevel(logging.WARNING)  # keep the window readable
    clock = SystemClock()
    try:
        lock = InstanceLock(paths.lock_path).acquire()
    except AlreadyRunning:
        print("Marcelorian is already running. Use the window that is already open.")
        return 2
    try:
        if deletion.requested(data_dir):  # an interrupted "Delete all my data"
            return _finish_deletion(paths, None)
        factory = prepare_database(paths.db_path, clock, paths.backups_dir)
        port = free_port()
        security = UISecurity(HOST, port, clock)
        holder: dict[str, Any] = {}
        app = create_app(
            security, factory, clock, paths=paths, request_shutdown=lambda: _stop(holder)
        )
        worker = worker_of(app)
        worker.start()  # recovery first, then a check every minute; never sends
        try:
            code = _serve(app, security, port, open_browser=not args.no_browser, holder=holder)
        finally:
            worker.stop()
        if deletion.requested(data_dir):
            return _finish_deletion(paths, factory)
        return code
    finally:
        lock.release()


def _stop(holder: dict[str, Any]) -> None:
    server = holder.get("server")
    if server is not None:
        server.should_exit = True


def _finish_deletion(paths: AppPaths, factory: Any) -> int:
    """Close the database and logs, then remove everything (Windows cannot delete open files)."""
    if factory is not None:
        factory.kw["bind"].dispose()
    logging.shutdown()
    report = deletion.perform_deletion(paths, open_secret_store)
    if report.failed:
        print("Some items could not be deleted: " + ", ".join(report.failed) + ".")
        print("Close any program using them and start Marcelorian again to finish.")
        return 1
    print("Your Marcelorian data has been deleted from this computer.")
    print("What stays outside the app:")
    for line in deletion.REMAINS:
        print(f"  - {line}")
    return 0


def _serve(
    app: Any,
    security: UISecurity,
    port: int,
    *,
    open_browser: bool,
    holder: dict[str, Any] | None = None,
) -> int:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=port, log_level="warning", **UVICORN_LOG_KWARGS)  # type: ignore[arg-type]
    )
    if holder is not None:
        holder["server"] = server  # lets "Delete all my data" stop the server

    def when_ready() -> None:
        while not server.started and not server.should_exit:
            threading.Event().wait(0.1)
        if server.should_exit:
            return
        print("Marcelorian is running. Keep this window open while you use it.")
        print("Press Enter to open it in your browser again. Close this window to stop.")
        if open_browser:
            open_in_browser(security)
        for _line in sys.stdin:
            open_in_browser(security)

    threading.Thread(target=when_ready, daemon=True).start()
    with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover - interactive
        server.run()
    print("Marcelorian has stopped.")
    return 0
