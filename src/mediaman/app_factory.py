"""FastAPI application factory and startup lifecycle.

This module owns the FastAPI-facing surface of startup:

- :func:`create_app` — assembles the ``FastAPI`` instance, registers
  middleware, mounts static files, and wires all routers.
- :func:`lifespan` — orchestrates startup and shutdown in the correct
  order (DB → crypto canary → scheduler → reconciliation).

The bootstrap helpers themselves live under :mod:`mediaman.bootstrap`:

- :mod:`mediaman.bootstrap.db` — DB open, migrations, ``app.state``.
- :mod:`mediaman.bootstrap.crypto` — AES canary + legacy-ciphertext sweep.
- :mod:`mediaman.bootstrap.scan_jobs` — scheduler start/stop.
- :mod:`mediaman.bootstrap.data_dir` — data-dir writability probe.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediaman.bootstrap.crypto import bootstrap_crypto
from mediaman.bootstrap.data_dir import DataDirNotWritableError
from mediaman.bootstrap.db import bootstrap_db
from mediaman.bootstrap.scan_jobs import bootstrap_scheduling, shutdown_scheduling
from mediaman.config import ConfigError, load_config
from mediaman.core.scrub_filter import install_root_filter

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle.

    Orchestrates the bootstrap steps in dependency order:

    1. DB first — every later step needs an open connection.
    2. Crypto canary — must run before the scheduler so a key mismatch
       refuses to spawn background jobs that would silently fail.
    3. Scheduling — opt-in; failures here are logged but do NOT take
       the web UI down.

    Fatal startup errors are converted into a single clean log line
    followed by ``sys.exit(1)`` rather than allowed to surface as an
    ASGI traceback. The traceback path buries the actionable line under
    fifteen frames of uvicorn/FastAPI internals; the single-line path
    fits in an orchestrator's restart-loop log without truncation.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        logger.critical("Configuration error at startup: %s", exc)
        sys.exit(1)

    # Install the root scrub filter on every handler of the mediaman logger.
    # Secrets (OMDB key, Plex token) are runtime-resolved from the DB after
    # bootstrap_db completes; callers register them via register_secret().
    # Attaching to *handlers* rather than the logger itself ensures that any
    # child logger (getLogger(__name__)) automatically inherits redaction
    # regardless of the logger hierarchy depth.
    install_root_filter()

    try:
        bootstrap_db(app, config)
    except DataDirNotWritableError as exc:
        logger.critical("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.critical("Database bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    try:
        bootstrap_crypto(app, config)
    except Exception as exc:  # pragma: no cover — bootstrap_crypto already swallows
        logger.critical("Crypto bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    scheduler_started = bootstrap_scheduling(app, config)

    # Reconcile any in-flight manual delete operations that crashed
    # between the external Radarr/Sonarr call and the local DB cleanup.
    # Idempotent — safe to run on every cold start.
    try:
        from mediaman.web.repository.delete_intents import reconcile_pending_delete_intents

        reconciled = reconcile_pending_delete_intents()
        if reconciled:
            logger.info("Reconciled %d pending delete intent(s) at startup", reconciled)
    except Exception:
        logger.exception("delete-intent reconciliation failed at startup; continuing")

    # Reconcile download_notifications rows stranded at notified=2 by a
    # crashed worker.
    try:
        from mediaman.services.downloads.notifications import (
            reconcile_stranded_notifications,
        )

        reset = reconcile_stranded_notifications(app.state.db)
        if reset:
            logger.info("Reconciled %d stranded download notification(s) at startup", reset)
    except Exception:
        logger.exception("download-notification reconciliation failed at startup; continuing")

    logger.info("Mediaman started on port %s", config.port)
    yield

    if scheduler_started:
        shutdown_scheduling()

    app.state.db.close()
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
    from mediaman.web.routes.library_api import router as library_api_router
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
    app.include_router(library_api_router)
    app.include_router(poster_router)
    app.include_router(kept_router)
    app.include_router(scan_router)
    app.include_router(settings_router)
    app.include_router(subscribers_router)
    app.include_router(search_router)
    app.include_router(recommended_router)
    app.include_router(users_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness probe for the container HEALTHCHECK.

        Returns 200 with a tiny JSON body whenever the ASGI event loop
        is responsive. Deliberately no DB or Plex round-trip — those are
        failure modes the healthcheck should not conflate with the
        process being alive.
        """
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness probe — ``/healthz`` says "alive", this says "alive AND configured".

        The scheduler is the most consequential background service; if
        it did not come up the deletion executor and library sync will
        never run, so the app is technically responsive but
        operationally broken. Returning 503 here gives orchestrators a
        signal they can switch their healthcheck to without confusing
        liveness and readiness.

        When the probe is failing the body now carries the *reason* —
        the last scheduler bootstrap error stashed on
        ``app.state.scheduler_error`` — so an operator looking at the
        orchestrator status doesn't have to ssh into the container and
        tail the python logs to discover *why*.
        """
        scheduler_healthy = bool(getattr(app.state, "scheduler_healthy", False))
        canary_ok = bool(getattr(app.state, "canary_ok", True))
        ready = scheduler_healthy and canary_ok
        body: dict[str, str] = {
            "status": "ready" if ready else "not_ready",
            "scheduler": "ok" if scheduler_healthy else "down",
            "crypto": "ok" if canary_ok else "down",
        }
        if not ready:
            scheduler_error = getattr(app.state, "scheduler_error", None)
            if scheduler_error:
                body["scheduler_error"] = str(scheduler_error)
        return JSONResponse(body, status_code=200 if ready else 503)

    return app
