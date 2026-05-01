"""Mediaman application entry point."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediaman.bootstrap import (
    bootstrap_crypto,
    bootstrap_db,
    bootstrap_scheduling,
    shutdown_scheduling,
)
from mediaman.config import load_config

logger = logging.getLogger("mediaman")

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # type: ignore[return]  # FastAPI's lifespan protocol requires an async generator; mypy can't infer the implicit return from yield
    """Startup and shutdown lifecycle.

    Delegates the actual work to the :mod:`mediaman.bootstrap` package
    so this function stays a slim orchestrator. Order matters:

    1. DB first — every later step needs an open connection.
    2. Crypto canary — must run before the scheduler so a key mismatch
       refuses to spawn background jobs that would silently fail.
    3. Scheduling — opt-in; failures here are logged but do NOT take
       the web UI down.
    """
    config = load_config()
    bootstrap_db(app, config)
    bootstrap_crypto(app, config)
    scheduler_started = bootstrap_scheduling(app, config)

    # Reconcile any in-flight manual delete operations that crashed
    # between the external Radarr/Sonarr call and the local DB cleanup
    # (finding 24).  Idempotent — safe to run on every cold start.
    try:
        from mediaman.web.routes.library.api import reconcile_pending_delete_intents

        reconciled = reconcile_pending_delete_intents()
        if reconciled:
            logger.info("Reconciled %d pending delete intent(s) at startup", reconciled)
    except Exception:
        logger.exception("delete-intent reconciliation failed at startup; continuing")

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
        """
        scheduler_healthy = bool(getattr(app.state, "scheduler_healthy", False))
        canary_ok = bool(getattr(app.state, "canary_ok", True))
        ready = scheduler_healthy and canary_ok
        body = {
            "status": "ready" if ready else "not_ready",
            "scheduler": "ok" if scheduler_healthy else "down",
            "crypto": "ok" if canary_ok else "down",
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    return app


def _resolve_bind_host() -> str:
    """Return the host address uvicorn should bind to.

    Reads ``MEDIAMAN_BIND_HOST`` from the environment. On bare metal the
    default is ``127.0.0.1`` (localhost-only) so a fresh deployment is
    not accidentally exposed to the network. When running inside a
    Docker container the default becomes ``0.0.0.0`` — the container's
    own loopback is not reachable from the host port forward, and the
    Docker network already isolates the service from the outside world.
    Operators can override either default by setting ``MEDIAMAN_BIND_HOST``
    explicitly.
    """
    import os
    from pathlib import Path

    explicit = os.environ.get("MEDIAMAN_BIND_HOST", "").strip()
    if explicit:
        return explicit

    # /.dockerenv is the canonical Docker marker file.
    in_container = Path("/.dockerenv").exists() or os.environ.get("container") == "docker"
    # Inside a container the only route into the process is the published
    # port — binding to 0.0.0.0 is required, not an exposure bug.
    return "0.0.0.0" if in_container else "127.0.0.1"  # nosec B104


def _enforce_single_worker() -> None:
    """Refuse to start under multi-worker uvicorn (finding 3).

    Several invariants in mediaman assume a single process holds the
    SQLite connection: the APScheduler instance, the in-memory rate
    limits, and the search-trigger throttle would all duplicate or race
    if a second worker booted up. Token replay (finding 2) is now backed
    by SQLite and would survive, but the rest is not yet ready for
    horizontal scale, so we fail loudly instead of degrading silently.

    Reads ``MEDIAMAN_WORKERS`` and the legacy ``WORKERS`` env var so an
    operator who exports either by accident sees an immediate error
    rather than a half-broken deployment. ``UVICORN_WORKERS`` is also
    inspected because uvicorn itself respects it.
    """
    import os

    candidates = ("MEDIAMAN_WORKERS", "UVICORN_WORKERS", "WORKERS")
    for name in candidates:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 1:
            logger.error(
                "Refusing to start: %s=%d but mediaman requires WORKERS=1. "
                "Several invariants (scheduler, rate-limits, in-process "
                "throttles) assume a single process — multi-worker support "
                "would silently corrupt them. Run multiple replicas behind "
                "your reverse proxy instead, or unset %s.",
                name,
                value,
                name,
            )
            raise RuntimeError(
                f"mediaman requires a single worker; {name}={value} is not supported"
            )


def cli_main() -> None:
    """CLI entry point — run the server or handle subcommands."""
    if len(sys.argv) > 1 and sys.argv[1] == "create-user":
        sys.argv = sys.argv[1:]
        from mediaman.auth.cli import create_user_cli

        create_user_cli()
        return

    _enforce_single_worker()

    import uvicorn

    config = load_config()
    app = create_app()

    # If the operator has not set MEDIAMAN_BIND_HOST explicitly, defer to
    # the Docker-aware resolver so a containerised deployment binds to
    # 0.0.0.0 rather than the unreachable container loopback.
    bind_host = config.bind_host or _resolve_bind_host()
    trusted_proxies = config.trusted_proxies

    # Uvicorn's ``proxy_headers`` machinery rewrites BOTH
    # ``request.url.scheme`` and ``request.client.host`` whenever the
    # direct peer is in ``forwarded_allow_ips``. The second rewrite is a
    # rate-limit-bypass footgun, so only enable proxy_headers when the
    # operator has EXPLICITLY set ``MEDIAMAN_TRUSTED_PROXIES``.
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
        uvicorn.run(
            app,
            host=bind_host,
            port=config.port,
            proxy_headers=False,
            server_header=False,
            date_header=False,
        )


# Module-level instantiation for uvicorn targets such as
# ``uvicorn mediaman.main:app``. Gated behind ``MEDIAMAN_EAGER_APP=1`` to
# avoid import-time side effects for tests and CLI subcommands.
import os as _os  # noqa: E402 — gated import to avoid side-effects on module load

if _os.environ.get("MEDIAMAN_EAGER_APP", "").strip() == "1":
    app = create_app()
