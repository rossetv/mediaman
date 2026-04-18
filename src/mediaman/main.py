"""Mediaman application entry point."""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediaman.config import load_config
from mediaman.db import init_db, set_connection

logger = logging.getLogger("mediaman")

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"


def _get_setting(conn, key: str, default: str = "") -> str:
    """Read a plain-text setting from the DB, falling back to *default*."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


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

    # ── AES key canary: detect a rotated/mismatched MEDIAMAN_SECRET_KEY ──────
    # Does NOT refuse to start on mismatch — the admin must still be able to
    # log in and re-enter secrets. A LOUD warning is logged by canary_check.
    try:
        from mediaman.crypto import canary_check
        canary_check(conn, config.secret_key)
    except Exception:
        logger.exception("AES canary check failed unexpectedly")

    # ── Start scheduler if scan settings are configured ──────────────────────
    from mediaman.scanner.scheduler import start_scheduler, stop_scheduler

    scheduler_started = False
    try:
        scan_day = _get_setting(conn, "scan_day", "mon")
        scan_time = _get_setting(conn, "scan_time", "09:00")
        scan_tz = _get_setting(conn, "scan_timezone", "UTC")
        hour, minute = (int(x) for x in scan_time.split(":"))

        # Capture the secret key now; conn is accessed at scan time via get_db()
        _secret_key = config.secret_key

        def run_scheduled_scan() -> None:
            """Execute a scheduled scan, reading all settings fresh from the DB."""
            from mediaman.db import get_db
            from mediaman.scanner.runner import run_scan_from_db

            try:
                db_conn = get_db()
                run_scan_from_db(db_conn, _secret_key)
            except Exception:
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

        sync_interval = int(_get_setting(conn, "library_sync_interval", "30"))

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
        logger.info(
            "Scheduler started: scan every %s at %02d:%02d %s, library sync every %d min",
            scan_day, hour, minute, scan_tz, sync_interval,
        )
    except Exception as e:
        logger.warning("Could not start scheduler: %s", e)

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

    from mediaman.web.routes.auth_routes import router as auth_router
    from mediaman.web.routes.dashboard import router as dashboard_router
    from mediaman.web.routes.download import router as download_router
    from mediaman.web.routes.downloads import router as downloads_router
    from mediaman.web.routes.history import router as history_router
    from mediaman.web.routes.keep import router as keep_router
    from mediaman.web.routes.library import router as library_router
    from mediaman.web.routes.poster import router as poster_router
    from mediaman.web.routes.protected import router as protected_router
    from mediaman.web.routes.scan import router as scan_router
    from mediaman.web.routes.settings_routes import router as settings_router
    from mediaman.web.routes.subscribers import router as subscribers_router
    from mediaman.web.routes.search import router as search_router
    from mediaman.web.routes.recommended import router as recommended_router

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(download_router)
    app.include_router(downloads_router)
    app.include_router(history_router)
    app.include_router(keep_router)
    app.include_router(library_router)
    app.include_router(poster_router)
    app.include_router(protected_router)
    app.include_router(scan_router)
    app.include_router(settings_router)
    app.include_router(subscribers_router)
    app.include_router(search_router)
    app.include_router(recommended_router)

    return app


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
    uvicorn.run(app, host="0.0.0.0", port=config.port)


app = create_app()
