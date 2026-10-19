"""Start Marcelorian on this computer: ``python -m marcelorian``.

1. Takes the single-instance lock for the data folder (spec §2).
2. Upgrades the database (migrations) and makes sure the applicant profile exists.
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
from marcelorian.db.migrate import upgrade
from marcelorian.logging_setup import UVICORN_LOG_KWARGS, configure_logging
from marcelorian.platform.instance_lock import AlreadyRunning, InstanceLock
from marcelorian.platform.paths import resolve_app_paths
from marcelorian.services import profile, runtime
from marcelorian.ui.app import create_app, worker_of
from marcelorian.ui.security import UISecurity

log = logging.getLogger(__name__)
HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def prepare_database(db_path: Path, clock: Clock) -> Any:
    """Migrate and seed first-run state; returns the session factory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(db_path)
    upgrade(engine)
    factory = make_session_factory(engine)
    with transaction(factory) as session:
        now = clock.now()
        profile.applicant(session, now)
        runtime.get_runtime(session, now)  # first run: sandbox mode (spec §16)
    return factory


def open_in_browser(security: UISecurity, opener: Callable[[str], Any] = webbrowser.open) -> None:
    opener(security.bootstrap_url(security.issue_bootstrap_token()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start Marcelorian.")
    parser.add_argument("--data-dir", type=Path, default=None, help="default: the app data folder")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    paths = resolve_app_paths()
    data_dir: Path = args.data_dir or paths.data_dir
    db_path = data_dir / "marcelorian.db"
    configure_logging(data_dir / "logs")
    logging.getLogger("alembic").setLevel(logging.WARNING)  # keep the window readable
    clock = SystemClock()
    try:
        lock = InstanceLock(data_dir / "instance.lock").acquire()
    except AlreadyRunning:
        print("Marcelorian is already running. Use the window that is already open.")
        return 2
    try:
        factory = prepare_database(db_path, clock)
        port = free_port()
        security = UISecurity(HOST, port, clock)
        app = create_app(security, factory, clock)
        worker = worker_of(app)
        worker.start()  # recovery first, then a check every minute; never sends
        try:
            return _serve(app, security, port, open_browser=not args.no_browser)
        finally:
            worker.stop()
    finally:
        lock.release()


def _serve(app: Any, security: UISecurity, port: int, *, open_browser: bool) -> int:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=port, log_level="warning", **UVICORN_LOG_KWARGS)  # type: ignore[arg-type]
    )

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
