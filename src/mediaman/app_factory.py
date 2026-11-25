"""FastAPI application factory and startup lifecycle.

This module owns the FastAPI-facing surface of startup:

- create_app — assembles the ``FastAPI`` instance, registers
  middleware, mounts static files, and wires all routers.
- lifespan — orchestrates startup and shutdown in the correct
  order (DB → crypto canary → scheduler → reconciliation).

The bootstrap helpers themselves live under mediaman.bootstrap:

- mediaman.bootstrap.db — DB open, migrations, ``app.state``.
- mediaman.bootstrap.crypto — AES canary + legacy-ciphertext sweep.
- mediaman.bootstrap.scan_jobs — scheduler start/stop.
- mediaman.bootstrap.data_dir — data-dir writability probe.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response
from starlette.types import Scope

from mediaman.bootstrap.crypto import bootstrap_crypto
from mediaman.bootstrap.data_dir import DataDirNotWritableError
from mediaman.bootstrap.db import bootstrap_db
from mediaman.bootstrap.scan_jobs import bootstrap_scheduling, shutdown_scheduling
from mediaman.config import ConfigError, load_config
from mediaman.core.scrub_filter import install_root_filter
from mediaman.core.time import now_utc
from mediaman.db.connection import SchemaTooOldError

logger = logging.getLogger(__name__)

# How often idle Watch streams are looked for; a stream is stopped after three idle minutes.
_STREAM_REAP_INTERVAL_SECONDS = 30.0

_STATIC_DIR = Path(__file__).parent / "web" / "static"

# A file at its own version's URL never changes.
_KEPT = "public, max-age=31536000, immutable"


class _StaticVersion(NamedTuple):
    """The digest of the static files, and what it was read from."""

    digest: str
    read_at: float
    files: dict[str, tuple[int, int]]
    """Each file's real path, with its size and mtime in nanoseconds."""


def _static_version(static_dir: Path) -> _StaticVersion:
    """A short digest of every file under *static_dir*, so a changed asset gets a new URL."""
    read_at = time.time()
    digest = hashlib.sha256()
    files = {}
    for path in sorted(p for p in static_dir.rglob("*") if p.is_file()):
        stat_result = path.stat()
        files[os.path.realpath(path)] = (stat_result.st_size, stat_result.st_mtime_ns)
        digest.update(path.relative_to(static_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return _StaticVersion(digest.hexdigest()[:12], read_at, files)


class _RevalidatedStaticFiles(StaticFiles):
    """Serve /static with ``no-cache``, or kept for good at ``?v=<version>``, on the 200 and the 304.

    The fonts a stylesheet loads carry no version, and a link from before an
    update carries an old one; for those a browser must check the ETag on every
    load, which costs a 304 for an unchanged file. Without a *version* every
    request is checked.
    """

    def __init__(self, *, version: _StaticVersion | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._version = version

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        version = self._version
        # The version was read at startup; a file changed on disk since is not what it names.
        # The time tells a change made in the same tick of the file's clock as the read.
        current = (
            version is not None
            and scope.get("query_string", b"") == f"v={version.digest}".encode()
            and stat_result.st_mtime <= version.read_at
            and version.files.get(os.fspath(full_path))
            == (stat_result.st_size, stat_result.st_mtime_ns)
        )
        response.headers["Cache-Control"] = _KEPT if current else "no-cache"
        return response


_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"


def _reconcile_delete_intents_in_background(secret_key: str, started_before: str) -> None:
    """Close delete intents stranded by a crash, on a connection owned by this thread.

    Only intents started before *started_before* (this process's start) are
    touched; later ones belong to deletes still in flight. An intent is
    booked deleted only when Radarr/Sonarr confirm the item gone.
    """
    # rationale: runs on a daemon thread; an exception here must be logged,
    # never allowed to propagate out of the thread.
    try:
        from mediaman.db import close_db, get_db
        from mediaman.scanner.repository import read_delete_allowed_roots_setting
        from mediaman.web.repository.delete_intents import (
            build_verify_gone,
            reconcile_pending_delete_intents,
        )

        conn = get_db()
        try:
            try:
                delete_roots = read_delete_allowed_roots_setting(conn)
            except Exception:
                # Without roots the reconcile only skips the on-disk check; it
                # must still close the intents it can.
                logger.exception("could not read delete roots; reconciling without them")
                delete_roots = []
            reconciled = reconcile_pending_delete_intents(
                conn,
                verify_gone=build_verify_gone(conn, secret_key),
                started_before=started_before,
                delete_roots=delete_roots,
            )
        finally:
            close_db()
        if reconciled:
            logger.info("Reconciled %d pending delete intent(s) at startup", reconciled)
    except Exception:
        logger.exception("delete-intent reconciliation failed at startup")


def _run_startup_reconciliation(app: FastAPI) -> None:
    """Reconcile state stranded by a crash on a previous run.

    Two idempotent, best-effort sweeps, run after the scheduler has
    started:

    1. In-flight manual delete operations that crashed between the
       external Radarr/Sonarr call and the local DB cleanup (started on
       a background thread).
    2. ``download_notifications`` rows stranded at ``notified=2`` by a
       crashed worker.

    Neither failure aborts startup — the web UI must still come up so an
    operator can investigate.
    """
    # Runs off the startup path: verifying an item against Radarr/Sonarr
    # can be slow and must not hold up the web UI.
    threading.Thread(
        target=_reconcile_delete_intents_in_background,
        args=(app.state.config.secret_key, now_utc().isoformat()),
        name="delete-intent-reconcile",
        daemon=True,
    ).start()

    # Reconcile download_notifications rows stranded at notified=2 by a
    # crashed worker.
    try:
        from mediaman.services.downloads.notifications import (
            reconcile_stranded_notifications,
        )

        reset = reconcile_stranded_notifications(app.state.db)
        if reset:
            logger.info("Reconciled %d stranded download notification(s) at startup", reset)
    # rationale: best-effort reconciliation;
    # don't block startup if the DB query path is briefly unhappy.
    except Exception:
        logger.exception("download-notification reconciliation failed at startup; continuing")


def _start_watch_streams(secret_key: str) -> Callable[[], None]:
    """Create the Watch stream manager, tidy Plex sessions left by an earlier run, start the reaper.

    The tidy runs on its own thread so a Plex that is down never holds up startup. Returns
    the function that stops the reaper and then every live stream.
    """
    from mediaman.db import get_db
    from mediaman.services.infra import allowed_outbound_hosts
    from mediaman.services.media_meta.plex_stream import init_stream_manager
    from mediaman.web.routes.watch.stream_api import read_plex_credentials

    manager = init_stream_manager(
        lambda: read_plex_credentials(get_db(), secret_key),
        allowed_hosts=lambda: allowed_outbound_hosts(get_db()),
    )
    threading.Thread(
        target=_tidy_stream_orphans,
        args=(manager.tidy_orphans,),
        name="watch-stream-tidy",
        daemon=True,
    ).start()
    stop_reaper = _start_stream_reaper(manager.reap, _STREAM_REAP_INTERVAL_SECONDS)

    def shutdown() -> None:
        stop_reaper()
        manager.stop_all()

    return shutdown


def _tidy_stream_orphans(tidy: Callable[[], int]) -> None:
    from mediaman.db import close_db

    # rationale: runs on a daemon thread; an exception here must be logged,
    # never allowed to propagate out of the thread.
    try:
        stopped = tidy()
        if stopped:
            logger.info("Stopped %d Plex stream(s) left by an earlier run", stopped)
    except Exception:
        logger.exception("could not tidy Plex streams at startup")
    finally:
        close_db()


def _start_stream_reaper(reap: Callable[[], int], interval: float) -> Callable[[], None]:
    """Call *reap* every *interval* seconds on a daemon thread; return the function that stops it."""
    from mediaman.db import close_db

    stop = threading.Event()

    def run() -> None:
        try:
            while not stop.wait(interval):
                # rationale: one failed round must not end the reaper.
                try:
                    reap()
                except Exception:
                    logger.exception("watch stream reaper failed; trying again next round")
        finally:
            close_db()

    thread = threading.Thread(target=run, name="watch-stream-reaper", daemon=True)
    thread.start()

    def shutdown() -> None:
        stop.set()
        # A round may be waiting on Plex; the thread is a daemon, so do not wait long.
        thread.join(timeout=5)

    return shutdown


def _shutdown(app: FastAPI, scheduler_started: bool) -> None:
    """Tear down in reverse dependency order: scheduler, then DB.

    The scheduler is stopped first (only if it actually started) so no
    background job is mid-flight against a connection that ``close()``
    is about to invalidate.
    """
    if scheduler_started:
        shutdown_scheduling()

    app.state.db.close()
    logger.info("Mediaman shutting down")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle.

    Orchestrates the bootstrap steps in dependency order:

    1. DB first — every later step needs an open connection.
    2. Crypto canary — must run before the scheduler so a key mismatch
       refuses to spawn background jobs that would silently fail.
    3. Scheduling — opt-in; failures here are logged but do NOT take
       the web UI down.
    4. Startup reconciliation — best-effort sweeps of crash-stranded
       state, after the scheduler is up.
    5. Watch streams — the stream manager and its reaper; the reaper is
       stopped first at shutdown.

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

    try:
        bootstrap_db(app, config)
    except DataDirNotWritableError as exc:
        logger.critical("%s", exc)
        sys.exit(1)
    except SchemaTooOldError as exc:
        logger.critical("Database bootstrap failed at startup: %s", exc)
        sys.exit(1)
    # rationale: any further bootstrap failure
    # must abort process; we cannot serve traffic without a DB.
    except Exception as exc:
        logger.critical("Database bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    try:
        bootstrap_crypto(app, config)
    # rationale: crypto absence means we cannot
    # decrypt secrets or sign tokens; abort process rather than serve broken
    # responses. bootstrap_crypto itself swallows recoverable errors.
    except Exception as exc:  # pragma: no cover — bootstrap_crypto already swallows
        logger.critical("Crypto bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    scheduler_started = bootstrap_scheduling(app, config)

    _run_startup_reconciliation(app)

    stop_stream_reaper = _start_watch_streams(config.secret_key)

    logger.info("Mediaman started on port %s", config.port)
    yield

    stop_stream_reaper()
    _shutdown(app, scheduler_started)


def _register_routers(app: FastAPI) -> None:
    """Mount every route module's router on *app*.

    The router imports are deferred into this function body (rather than
    placed at module top) so importing mediaman.app_factory does
    not transitively import every route module and its dependencies —
    keeps import cost low.
    """
    from mediaman.web.routes.account import router as account_router
    from mediaman.web.routes.auth import router as auth_router
    from mediaman.web.routes.avatars import router as avatars_router
    from mediaman.web.routes.cast import router as cast_router
    from mediaman.web.routes.dashboard import router as dashboard_router
    from mediaman.web.routes.download import router as download_router
    from mediaman.web.routes.downloads import router as downloads_router
    from mediaman.web.routes.force_password_change import router as force_pw_router
    from mediaman.web.routes.history import router as history_router
    from mediaman.web.routes.keep import router as keep_router
    from mediaman.web.routes.kept import router as kept_router
    from mediaman.web.routes.library import router as library_router
    from mediaman.web.routes.library_api import router as library_api_router
    from mediaman.web.routes.older_tvs import router as older_tvs_router
    from mediaman.web.routes.plex_link import router as plex_link_router
    from mediaman.web.routes.poster import router as poster_router
    from mediaman.web.routes.ratings import router as ratings_router
    from mediaman.web.routes.recommended import router as recommended_router
    from mediaman.web.routes.scan import router as scan_router
    from mediaman.web.routes.search import router as search_router
    from mediaman.web.routes.search.assist import router as search_assist_router
    from mediaman.web.routes.settings import router as settings_router
    from mediaman.web.routes.subscribers import router as subscribers_router
    from mediaman.web.routes.tv import router as tv_router
    from mediaman.web.routes.tv_page import router as tv_page_router
    from mediaman.web.routes.users import router as users_router
    from mediaman.web.routes.watch.browse_api import router as watch_browse_api_router
    from mediaman.web.routes.watch.catalog_api import router as watch_catalog_api_router
    from mediaman.web.routes.watch.explore_api import router as watch_explore_api_router
    from mediaman.web.routes.watch.image_api import router as watch_image_api_router
    from mediaman.web.routes.watch.library_row_api import router as watch_library_row_api_router
    from mediaman.web.routes.watch.list_page import router as watch_list_page_router
    from mediaman.web.routes.watch.marks_api import router as watch_marks_api_router
    from mediaman.web.routes.watch.pages import router as watch_pages_router
    from mediaman.web.routes.watch.preview_api import router as watch_preview_api_router
    from mediaman.web.routes.watch.recommended_api import router as watch_recommended_api_router
    from mediaman.web.routes.watch.stream_api import router as watch_stream_api_router
    from mediaman.web.routes.watch.title_preview_api import router as watch_title_preview_api_router

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
    app.include_router(search_assist_router)
    app.include_router(recommended_router)
    app.include_router(users_router)
    app.include_router(account_router)
    app.include_router(avatars_router)
    app.include_router(plex_link_router)
    app.include_router(watch_pages_router)
    app.include_router(watch_browse_api_router)
    app.include_router(watch_stream_api_router)
    app.include_router(cast_router)
    app.include_router(tv_router)
    app.include_router(tv_page_router)
    app.include_router(older_tvs_router)
    app.include_router(watch_preview_api_router)
    app.include_router(watch_marks_api_router)
    app.include_router(ratings_router)
    app.include_router(watch_library_row_api_router)
    app.include_router(watch_image_api_router)
    app.include_router(watch_catalog_api_router)
    app.include_router(watch_recommended_api_router)
    app.include_router(watch_list_page_router)
    app.include_router(watch_explore_api_router)
    app.include_router(watch_title_preview_api_router)


def _register_probes(app: FastAPI) -> None:
    """Register the ``/healthz`` and ``/readyz`` container probes on *app*.

    Both endpoints close over ``app.state`` (the readiness probe reads
    the scheduler/canary flags stashed there by the lifespan), so they
    must be defined where *app* is in scope — hence *app* is passed in
    rather than the probes living at module scope.
    """

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness probe for the container HEALTHCHECK.

        Returns 200 with a tiny JSON body whenever the ASGI event loop
        is responsive. Deliberately no DB or Plex round-trip — those are
        failure modes the healthcheck should not conflate with the
        process being alive. Runs on the loop, so it answers while stream
        starts hold the worker threads.
        """
        return {"status": "ok"}

    last_reason: str | None = None

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness probe — ``/healthz`` says "alive", this says "alive AND configured".

        The scheduler is the most consequential background service; if
        it did not come up the deletion executor and library sync will
        never run, so the app is technically responsive but
        operationally broken. Returning 503 here gives orchestrators a
        signal they can switch their healthcheck to without confusing
        liveness and readiness.

        ``/readyz`` is UNAUTHENTICATED and reachable on the public vhost, so the
        response body must not disclose internal state. The reason a probe is
        failing — scheduler/crypto-canary up-state and the raw bootstrap exception
        text (which can carry filesystem paths and exception class names, and
        whose crypto field reveals whether MEDIAMAN_SECRET_KEY matches the canary)
        — is written to the LOGS for operators, not returned to callers. The body
        is just the bare ready/not-ready status; the 200/503 code is the signal an
        orchestrator needs.
        """
        scheduler_healthy = bool(getattr(app.state, "scheduler_healthy", False))
        canary_ok = bool(getattr(app.state, "canary_ok", False))  # fail-closed: unset → not ready
        ready = scheduler_healthy and canary_ok
        # Log on a change of reason only, so a probe polling every few seconds
        # does not flood the log while the app stays unready.
        nonlocal last_reason
        reason = (
            None
            if ready
            else "scheduler={} crypto={} error={}".format(
                "ok" if scheduler_healthy else "down",
                "ok" if canary_ok else "down",
                getattr(app.state, "scheduler_error", None),
            )
        )
        if reason != last_reason:
            if reason is None:
                logger.info("readyz ready again")
            else:
                logger.warning("readyz not_ready: %s", reason)
            last_reason = reason
        return JSONResponse(
            {"status": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
        )


def create_app() -> FastAPI:
    # Set up the mediaman logger's scrubbing stderr handler before anything
    # logs, so the middleware warnings and startup errors go through it too.
    # Secrets (OMDB key, Plex token) are runtime-resolved from the DB after
    # bootstrap_db completes; callers register them via register_secret().
    install_root_filter()

    app = FastAPI(
        title="Mediaman",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    from mediaman.web import register_security_middleware
    from mediaman.web.error_pages import register_error_pages
    from mediaman.web.template_context import signed_in_avatar

    register_security_middleware(app)
    register_error_pages(app)

    version = _static_version(_STATIC_DIR) if _STATIC_DIR.is_dir() else None
    if version:
        app.mount(
            "/static",
            _RevalidatedStaticFiles(directory=str(_STATIC_DIR), version=version),
            name="static",
        )

    app.state.templates = Jinja2Templates(
        directory=str(_TEMPLATE_DIR), context_processors=[signed_in_avatar]
    )
    # Templates link assets as ``?v={{ static_v }}``; computed once, not per request.
    app.state.templates.env.globals["static_v"] = version.digest if version else ""

    _register_routers(app)
    _register_probes(app)

    return app
