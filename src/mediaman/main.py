"""Mediaman application entry point."""

import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediaman.config import load_config
from mediaman.db import init_db, set_connection
from mediaman.services.settings_reader import get_string_setting as _get_setting

logger = logging.getLogger("mediaman")

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"

_SCAN_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _validate_scan_time(s: str) -> tuple[int, int]:
    """Parse and validate a scan time string in ``HH:MM`` 24-hour format.

    Returns ``(hour, minute)`` on success. Raises :class:`ValueError` with
    a descriptive message on any invalid input so the operator sees a clear
    startup error rather than a silent misconfiguration.

    Validation is two-stage: a regex confirms the shape, then
    :func:`datetime.strptime` confirms the value is a real time (e.g.
    ``"25:00"`` would pass the regex but fail strptime).
    """
    if not _SCAN_TIME_RE.match(s):
        raise ValueError(
            f"scan_time {s!r} is invalid — expected HH:MM in 24-hour format (e.g. '09:00')"
        )
    try:
        dt = datetime.strptime(s, "%H:%M")
    except ValueError:
        raise ValueError(
            f"scan_time {s!r} is not a valid time — expected HH:MM in 24-hour format"
        )
    return dt.hour, dt.minute


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    config = load_config()
    db_path = f"{config.data_dir}/mediaman.db"
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    set_connection(conn)
    app.state.config = config
    app.state.db = conn
    app.state.db_path = db_path

    # ── Ensure poster cache directory exists at startup ───────────────────────
    # Doing this here means the per-request _get_cache_dir() path is never
    # the first to create the directory, so it never races with the first
    # incoming request.
    from mediaman.web.routes.poster import _get_cache_dir
    _get_cache_dir(config.data_dir)

    # ── AES key canary: detect a rotated/mismatched MEDIAMAN_SECRET_KEY ──────
    # Does NOT refuse to start — the admin must still be able to log in and
    # re-enter secrets — but a mismatch means every scheduled scan would
    # silently fail forever (the closure below captures the wrong key). So
    # the canary state is tracked on app.state and the scheduler refuses to
    # start when the check failed. A LOUD warning is logged by canary_check.
    canary_ok = True
    try:
        from mediaman.crypto import canary_check
        canary_ok = canary_check(conn, config.secret_key)
    except Exception:
        logger.exception("AES canary check failed unexpectedly")
        canary_ok = False
    app.state.canary_ok = canary_ok
    app.state.scheduler_healthy = False  # set True below on successful start

    # ── Start scheduler if scan settings are configured ──────────────────────
    from mediaman.scanner.scheduler import start_scheduler, stop_scheduler

    scheduler_started = False
    try:
        if not canary_ok:
            raise RuntimeError(
                "Refusing to start scheduler: AES canary check failed. "
                "Fix MEDIAMAN_SECRET_KEY (or re-enter encrypted settings) "
                "and restart. The web UI is still accessible so an admin "
                "can investigate."
            )

        # Reconcile any rows left in the 'deleting' state by a previous
        # crash — safe even if no scan runs this boot.
        try:
            from mediaman.scanner.engine import _recover_stuck_deletions
            _recover_stuck_deletions(conn)
        except Exception:
            logger.exception("Stuck-deletion recovery failed at startup")

        scan_day = _get_setting(conn, "scan_day", default="mon")
        scan_time = _get_setting(conn, "scan_time", default="09:00")
        scan_tz = _get_setting(conn, "scan_timezone", default="UTC")
        hour, minute = _validate_scan_time(scan_time)

        # Capture the secret key now; conn is accessed at scan time via get_db()
        _secret_key = config.secret_key

        def run_scheduled_scan() -> None:
            """Execute a scheduled scan, reading all settings fresh from the DB.

            Uses the DB-backed ``scan_runs`` lock so a concurrent manual
            trigger and a cron firing cannot both run simultaneously — only
            the first caller to acquire the lock proceeds; the other exits
            immediately.
            """
            from mediaman.db import finish_scan_run, get_db, start_scan_run
            from mediaman.scanner.runner import run_scan_from_db

            db_conn = get_db()
            run_id = start_scan_run(db_conn)
            if run_id is None:
                logger.info("Scheduled scan skipped — another scan is already running")
                return
            try:
                run_scan_from_db(db_conn, _secret_key)
                finish_scan_run(db_conn, run_id, "done")
            except Exception as exc:
                try:
                    finish_scan_run(db_conn, run_id, "error", str(exc))
                except Exception:
                    pass
                logger.exception("Scheduled scan failed")

        def run_library_sync_job() -> None:
            """Execute a lightweight library sync from Plex."""
            from mediaman.db import get_db
            from mediaman.scanner.runner import run_library_sync

            try:
                db_conn = get_db()
                run_library_sync(db_conn, _secret_key)
            except Exception:
                logger.exception("Library sync failed")

        sync_interval = int(_get_setting(conn, "library_sync_interval", default="30"))

        start_scheduler(
            scan_fn=run_scheduled_scan,
            day_of_week=scan_day,
            hour=hour,
            minute=minute,
            timezone=scan_tz,
            sync_fn=run_library_sync_job,
            sync_interval_minutes=sync_interval,
        )
        scheduler_started = True
        app.state.scheduler_healthy = True
        logger.info(
            "Scheduler started: scan every %s at %02d:%02d %s, library sync every %d min",
            scan_day, hour, minute, scan_tz, sync_interval,
        )
    except Exception as e:
        # On failure keep scheduler_healthy=False so the UI can surface
        # a banner in a future cluster. Don't take the app down.
        logger.error("Could not start scheduler: %s", e)

    logger.info("Mediaman started on port %s", config.port)
    yield

    if scheduler_started:
        from mediaman.scanner.scheduler import stop_scheduler
        stop_scheduler()

    conn.close()
    logger.info("Mediaman shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Mediaman",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    from mediaman.web import register_security_middleware
    register_security_middleware(app)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    from mediaman.web.routes.auth import router as auth_router
    from mediaman.web.routes.dashboard import router as dashboard_router
    from mediaman.web.routes.download import router as download_router
    from mediaman.web.routes.downloads import router as downloads_router
    from mediaman.web.routes.force_password_change import router as force_pw_router
    from mediaman.web.routes.history import router as history_router
    from mediaman.web.routes.keep import router as keep_router
    from mediaman.web.routes.kept import router as kept_router
    from mediaman.web.routes.library import router as library_router
    from mediaman.web.routes.poster import router as poster_router
    from mediaman.web.routes.recommended import router as recommended_router
    from mediaman.web.routes.scan import router as scan_router
    from mediaman.web.routes.search import router as search_router
    from mediaman.web.routes.settings import router as settings_router
    from mediaman.web.routes.subscribers import router as subscribers_router
    from mediaman.web.routes.users import router as users_router

    app.include_router(auth_router)
    app.include_router(force_pw_router)
    app.include_router(dashboard_router)
    app.include_router(download_router)
    app.include_router(downloads_router)
    app.include_router(history_router)
    app.include_router(keep_router)
    app.include_router(library_router)
    app.include_router(poster_router)
    app.include_router(kept_router)
    app.include_router(scan_router)
    app.include_router(settings_router)
    app.include_router(subscribers_router)
    app.include_router(search_router)
    app.include_router(recommended_router)
    app.include_router(users_router)

    return app



def _resolve_bind_host() -> str:
    """Return the host address uvicorn should bind to.

    Reads ``MEDIAMAN_BIND_HOST`` from the environment. Defaults to
    ``127.0.0.1`` (localhost-only) so a fresh deployment is not
    accidentally exposed to the network. Operators who want to bind
    all interfaces must explicitly set ``MEDIAMAN_BIND_HOST=0.0.0.0``.
    """
    import os
    return os.environ.get("MEDIAMAN_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"


def cli_main() -> None:
    """CLI entry point — run the server or handle subcommands."""
    if len(sys.argv) > 1 and sys.argv[1] == "create-user":
        sys.argv = sys.argv[1:]
        from mediaman.auth.cli import create_user_cli
        create_user_cli()
        return

    import uvicorn
    config = load_config()
    app = create_app()

    bind_host = config.bind_host
    trusted_proxies = config.trusted_proxies

    # Uvicorn's ``proxy_headers`` machinery rewrites BOTH
    # ``request.url.scheme`` (from X-Forwarded-Proto) AND
    # ``request.client.host`` (from the first X-Forwarded-For entry)
    # whenever the direct peer is in ``forwarded_allow_ips``. The
    # second rewrite is a rate-limit-bypass footgun: if we trust any
    # peer, an attacker supplying ``X-Forwarded-For: 1.2.3.4`` gets
    # ``client.host`` replaced with their spoofed value, breaking the
    # per-prefix bucketing.
    #
    # So: only enable uvicorn's proxy_headers rewrite when the
    # operator has EXPLICITLY set ``MEDIAMAN_TRUSTED_PROXIES`` to the
    # reverse-proxy CIDR they control. Default is off. The app's own
    # HSTS / Secure-cookie logic defaults to "secure" regardless so
    # the plaintext-scheme deployment still ships a Secure cookie.
    #
    # ``server_header=False`` and ``date_header=False`` suppress the
    # ``Server: uvicorn`` and ``Date: ...`` response headers, which
    # leak implementation and version details.
    if trusted_proxies:
        uvicorn.run(
            app,
            host=bind_host,
            port=config.port,
            forwarded_allow_ips=trusted_proxies,
            proxy_headers=True,
            server_header=False,
            date_header=False,
        )
    else:
        # No trusted proxy → don't let uvicorn rewrite client.host at
        # all. Rate limiter sees the actual peer, which is good on
        # bare metal and correct (if imperfect) behind a single proxy
        # whose IP isn't available at config time.
        uvicorn.run(
            app,
            host=bind_host,
            port=config.port,
            proxy_headers=False,
            server_header=False,
            date_header=False,
        )


# Module-level instantiation for uvicorn targets such as
# ``uvicorn mediaman.main:app``. Importing this module triggers all route
# imports AND the lifespan (DB open, scheduler setup) on server start, so
# it is gated behind ``MEDIAMAN_EAGER_APP=1`` to avoid import-time side
# effects for anything that just wants to introspect the module (tests,
# ``python -m mediaman.main``, CLI subcommands). The CLI path constructs
# its own app via ``cli_main`` so the module-level instance is only
# needed when uvicorn is invoked with this dotted path.
import os as _os

if _os.environ.get("MEDIAMAN_EAGER_APP", "").strip() == "1":
    app = create_app()
