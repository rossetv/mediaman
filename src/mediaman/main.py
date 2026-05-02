"""Mediaman application entry point."""

import ipaddress
import logging
import os
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
from mediaman.bootstrap.db import DataDirNotWritableError
from mediaman.config import ConfigError, load_config

logger = logging.getLogger("mediaman")

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"

# Wildcard tokens uvicorn happily accepts but mediaman refuses: both
# expand "trust this proxy" to "trust every peer", and uvicorn's
# proxy_headers handler rewrites ``request.client.host`` from the
# X-Forwarded-For header before our rate-limiter sees it. With either
# value set, an attacker controls the IP the rate-limiter buckets on.
_FORBIDDEN_TRUSTED_PROXY_TOKENS = frozenset({"*", "0.0.0.0/0", "::/0"})


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
    except Exception as exc:
        # Anything else from the DB layer is fatal — a corrupt schema,
        # a broken migration, an exhausted file descriptor. Logging the
        # exception preserves the traceback for debugging while the
        # process exits cleanly so the orchestrator restarts us.
        logger.critical("Database bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    try:
        bootstrap_crypto(app, config)
    except Exception as exc:  # pragma: no cover — bootstrap_crypto already swallows
        logger.critical("Crypto bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

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

    # Reconcile download_notifications rows stranded at notified=2 by a
    # crashed worker (H-5 — finding 22 follow-up).  The atomic claim flips
    # the status before the actual send, so a SIGKILL between claim and
    # send leaves the row pinned.  Idempotent and bounded by the in-flight
    # grace window.
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

    Unparseable values (``WORKERS=auto``, ``WORKERS=$()``, a stray
    comment) used to be silently treated as "unset"; they now log a
    WARNING so a typo never reaches a half-broken deployment unannounced.
    """
    candidates = ("MEDIAMAN_WORKERS", "UVICORN_WORKERS", "WORKERS")
    for name in candidates:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring %s=%r — value is not an integer. Set %s to 1 (or "
                "unset it) to silence this warning; mediaman requires a "
                "single worker.",
                name,
                raw,
                name,
            )
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


def _sanitise_trusted_proxies(raw: str) -> str:
    """Return a sanitised ``forwarded_allow_ips`` value or empty string.

    Uvicorn accepts ``"*"`` and ``"0.0.0.0/0"`` as "trust every peer".
    The internal IP-resolver tries to parse ``"*"``, fails, and returns
    an empty list — but uvicorn has ALREADY mutated
    ``request.client.host`` from the ``X-Forwarded-For`` header by then.
    The result is a per-IP rate-limit that buckets every request on an
    attacker-supplied address.

    Sanitisation rules:

    * Reject the literal wildcards in :data:`_FORBIDDEN_TRUSTED_PROXY_TOKENS`
      with a ``CRITICAL`` log line; return the empty string so
      :func:`cli_main` falls through to the proxy-headers-OFF branch.
    * Drop any entry that fails :class:`ipaddress.ip_network` parsing
      (single IPs are accepted because ``ip_network('10.0.0.1')`` is a
      valid /32 network); log a WARNING per dropped entry so a typo is
      visible.
    * Return the surviving entries comma-joined; uvicorn accepts that
      shape unchanged.

    Empty/whitespace input returns the empty string — caller treats that
    as "no proxy trust configured".
    """
    if not raw or not raw.strip():
        return ""

    cleaned: list[str] = []
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        if token in _FORBIDDEN_TRUSTED_PROXY_TOKENS:
            logger.critical(
                "Refusing wildcard MEDIAMAN_TRUSTED_PROXIES entry %r — "
                "this would let any peer set X-Forwarded-For and bypass "
                "per-IP rate limits. Drop it from the env var; only "
                "specific reverse-proxy IPs/CIDRs are accepted.",
                token,
            )
            continue
        try:
            ipaddress.ip_network(token, strict=False)
        except ValueError:
            logger.warning(
                "Ignoring invalid MEDIAMAN_TRUSTED_PROXIES entry %r — "
                "not a valid IP address or CIDR.",
                token,
            )
            continue
        cleaned.append(token)
    return ",".join(cleaned)


def cli_main() -> None:
    """CLI entry point — run the server or handle subcommands."""
    if len(sys.argv) > 1 and sys.argv[1] == "create-user":
        sys.argv = sys.argv[1:]
        from mediaman.auth.cli import create_user_cli

        create_user_cli()
        return

    _enforce_single_worker()

    import uvicorn

    try:
        config = load_config()
    except ConfigError as exc:
        # ``load_config`` is also called inside the lifespan, so the
        # error would surface there too — but if we wait, the operator
        # sees uvicorn's ASGI startup failure traceback rather than the
        # one-line config error. Catch here so the CLI exits cleanly
        # with the actionable message before uvicorn is even imported.
        print(f"Error: configuration is invalid: {exc}", file=sys.stderr)
        sys.exit(1)

    app = create_app()

    # If the operator has not set MEDIAMAN_BIND_HOST explicitly, defer to
    # the Docker-aware resolver so a containerised deployment binds to
    # 0.0.0.0 rather than the unreachable container loopback.
    bind_host = config.bind_host or _resolve_bind_host()
    trusted_proxies = _sanitise_trusted_proxies(config.trusted_proxies)

    # Uvicorn's ``proxy_headers`` machinery rewrites BOTH
    # ``request.url.scheme`` and ``request.client.host`` whenever the
    # direct peer is in ``forwarded_allow_ips``. The second rewrite is a
    # rate-limit-bypass footgun, so only enable proxy_headers when the
    # operator has EXPLICITLY set ``MEDIAMAN_TRUSTED_PROXIES`` to a
    # non-wildcard value (see :func:`_sanitise_trusted_proxies`).
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
if os.environ.get("MEDIAMAN_EAGER_APP", "").strip() == "1":
    app = create_app()
