"""FastAPI application factory and startup lifecycle.

This module is the single place that owns the full application lifecycle:

- :func:`create_app` — assembles the ``FastAPI`` instance, registers
  middleware, mounts static files, and wires all routers.
- :func:`lifespan` — orchestrates startup and shutdown in the correct
  order (DB → crypto canary → scheduler → reconciliation).
- :func:`bootstrap_db`, :func:`bootstrap_crypto` — startup helpers kept
  here for direct use by :func:`lifespan`.

The data-dir writability helpers now live in
:mod:`mediaman.bootstrap.data_dir` and the scheduling lifecycle helpers
live in :mod:`mediaman.bootstrap.scan_jobs`. The ``bootstrap/`` shims
re-export everything so existing import paths keep working.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediaman.config import Config, ConfigError, load_config
from mediaman.core.scrub_filter import install_root_filter

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------


def bootstrap_db(app: FastAPI, config: Config) -> None:
    """Open the SQLite DB, run migrations, register the bootstrap connection.

    Side effects on ``app.state``:

    - ``app.state.config`` — the resolved config object.
    - ``app.state.db`` — the bootstrap :class:`sqlite3.Connection`.
    - ``app.state.db_path`` — absolute path of the DB file.
    """
    from mediaman.bootstrap.data_dir import (
        DataDirNotWritableError,
        _assert_data_dir_writable,
        _remediation_for,
    )
    from mediaman.db import init_db, set_connection

    data_dir = Path(config.data_dir)
    # ``mkdir`` precedes the writability probe — without the wrapper the
    # OSError surfaces as an unhandled traceback (lost behind a wall of
    # ASGI frames) instead of the actionable single-line error the probe
    # produces.
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        proc_uid = os.geteuid()
        proc_gid = os.getegid()
        raise DataDirNotWritableError(
            f"data dir {data_dir} could not be created by uid={proc_uid} "
            f"gid={proc_gid}; {_remediation_for(exc)}; underlying error: {exc}"
        ) from exc
    _assert_data_dir_writable(data_dir)
    db_path = str(Path(config.data_dir) / "mediaman.db")
    logger.info("DB initialised at %s", db_path)
    conn = init_db(db_path)
    set_connection(conn)
    app.state.config = config
    app.state.db = conn
    app.state.db_path = db_path

    # Ensure the poster cache directory exists at startup so the first
    # request doesn't race with the lazy mkdir inside the poster route.
    poster_cache_dir = Path(config.data_dir) / "poster_cache"
    poster_cache_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Crypto bootstrap
# ---------------------------------------------------------------------------


def bootstrap_crypto(app: FastAPI, config: Config) -> None:
    """Run the AES canary check and stash the result on ``app.state``.

    Does NOT refuse to start on a mismatch — the admin must still be
    able to log in to re-enter secrets. The downstream
    :func:`bootstrap_scheduling` reads the flag and refuses to start the
    scheduler when the canary failed.

    The canary state is initialised to ``False`` and only flipped to
    ``True`` after :func:`is_canary_valid` returns a positive result. An
    import failure or any other exception leaves the flag at its
    fail-closed default — without this, a partial import (e.g. a missing
    ``cryptography`` extension) would slip through with the optimistic
    ``True`` and the scheduler would gleefully fire scans against
    settings it cannot decrypt.
    """
    canary_ok = False
    try:
        from mediaman.core.audit import security_event
        from mediaman.crypto import is_canary_valid, migrate_legacy_ciphertexts

        db = app.state.db

        def _on_canary_failure(reason: str) -> None:
            """Best-effort audit-log a canary failure.

            The canary fires before the audit table is guaranteed to exist on
            fresh-DB bootstrap, so any failure in the audit path is logged and
            swallowed — the security verdict (False) is what matters; the audit
            row is the cherry on top.
            """
            try:
                security_event(
                    db,
                    event="aes.canary_failed",
                    actor="",
                    ip="",
                    detail={"reason": reason},
                )
            except sqlite3.Error:  # pragma: no cover
                logger.exception("aes.canary_failed audit write failed reason=%s", reason)

        def _on_migration_complete(migrated_count: int) -> None:
            """Best-effort audit-log after a successful v35 migration commit."""
            try:
                security_event(
                    db,
                    event="aes.v35_migration_complete",
                    actor="",
                    ip="",
                    detail={"migrated_count": migrated_count},
                )
            except sqlite3.Error:  # pragma: no cover
                logger.exception("aes.v35_migration_complete audit write failed")

        canary_ok = bool(is_canary_valid(db, config.secret_key, on_failure=_on_canary_failure))
        if canary_ok:
            # Migration v35: re-encrypt any legacy v1 or no-AAD v2 settings
            # ciphertexts to v2+AAD. Safe to call on every startup —
            # already-migrated rows are skipped. Errors are logged but do
            # not abort startup.
            # rationale: §6.4 site 4 — cold-start recovery; migration is non-fatal.
            try:
                n = migrate_legacy_ciphertexts(
                    db, config.secret_key, on_complete=_on_migration_complete
                )
                if n:
                    logger.info("bootstrap_crypto: migrated %d legacy settings row(s) to v2+AAD", n)
            except Exception:
                logger.exception("bootstrap_crypto: migrate_legacy_ciphertexts failed (non-fatal)")
    # rationale: §6.4 site 4 — cold-start recovery; canary failure leaves the flag fail-closed.
    except Exception:
        logger.exception("AES canary check failed unexpectedly")
        canary_ok = False
    app.state.canary_ok = canary_ok


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
    from mediaman.bootstrap.data_dir import DataDirNotWritableError
    from mediaman.bootstrap.scan_jobs import bootstrap_scheduling, shutdown_scheduling

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

    # rationale: §6.4 site 4 — fatal cold-start; convert startup failure into one log line.
    try:
        bootstrap_db(app, config)
    except DataDirNotWritableError as exc:
        logger.critical("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.critical("Database bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    # rationale: §6.4 site 4 — bootstrap_crypto already swallows internally; defence in depth.
    try:
        bootstrap_crypto(app, config)
    except Exception as exc:  # pragma: no cover
        logger.critical("Crypto bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    scheduler_started = bootstrap_scheduling(app, config)

    # Reconcile any in-flight manual delete operations that crashed
    # between the external Radarr/Sonarr call and the local DB cleanup.
    # Idempotent — safe to run on every cold start.
    try:
        from mediaman.web.routes.library_api import reconcile_pending_delete_intents

        reconciled = reconcile_pending_delete_intents()
        if reconciled:
            logger.info("Reconciled %d pending delete intent(s) at startup", reconciled)
    except Exception:  # rationale: §6.4 site 4 — cold-start recovery
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
    except Exception:  # rationale: §6.4 site 4 — cold-start recovery
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
