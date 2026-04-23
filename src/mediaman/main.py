"""Mediaman application entry point."""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediaman.bootstrap import (
    bootstrap_crypto,
    bootstrap_db,
    bootstrap_scheduling,
    shutdown_scheduling,
)
from mediaman.bootstrap.scheduling import _validate_scan_time  # noqa: F401
from mediaman.config import load_config

# ``_validate_scan_time`` is re-exported above so the legacy
# ``from mediaman.main import _validate_scan_time`` import in the
# scheduler tests continues to resolve after the R23 bootstrap split.

logger = logging.getLogger("mediaman")

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
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
