"""Mediaman application entry point.

Thin CLI wrapper around :mod:`mediaman.app_factory`. All application
logic — ``create_app()``, ``lifespan()``, and the bootstrap helpers —
lives in ``app_factory`` so this module stays a slim entry point that
uvicorn and the ``mediaman`` console script target.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from mediaman.app_factory import create_app
from mediaman.config import ConfigError, load_config
from mediaman.validators import enforce_single_worker, sanitise_trusted_proxies

# Back-compat aliases — tests (and external callers) import these under
# their old underscore-prefixed names from mediaman.main.
_enforce_single_worker = enforce_single_worker
_sanitise_trusted_proxies = sanitise_trusted_proxies

logger = logging.getLogger(__name__)


def cli_main() -> None:
    """CLI entry point — run the server or handle subcommands."""
    if len(sys.argv) > 1 and sys.argv[1] == "create-user":
        sys.argv = sys.argv[1:]
        from mediaman.web.auth.cli import create_user_cli

        create_user_cli()
        return

    enforce_single_worker()

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
    trusted_proxies = sanitise_trusted_proxies(config.trusted_proxies)

    # Uvicorn's ``proxy_headers`` machinery rewrites BOTH
    # ``request.url.scheme`` and ``request.client.host`` whenever the
    # direct peer is in ``forwarded_allow_ips``. The second rewrite is a
    # rate-limit-bypass footgun, so only enable proxy_headers when the
    # operator has EXPLICITLY set ``MEDIAMAN_TRUSTED_PROXIES`` to a
    # non-wildcard value (see :func:`mediaman.validators.sanitise_trusted_proxies`).
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

    # /.dockerenv is the canonical Docker marker file. systemd/podman/lxc set
    # the lowercase `container` env var by convention; SIM112's "use uppercase"
    # advice doesn't apply since the variable name is dictated by the runtime.
    in_container = (
        Path("/.dockerenv").exists() or os.environ.get("container") == "docker"  # noqa: SIM112
    )
    # Inside a container the only route into the process is the published
    # port — binding to 0.0.0.0 is required, not an exposure bug.
    return "0.0.0.0" if in_container else "127.0.0.1"  # nosec B104


# Module-level instantiation for uvicorn targets such as
# ``uvicorn mediaman.main:app``. Gated behind ``MEDIAMAN_EAGER_APP=1`` to
# avoid import-time side effects for tests and CLI subcommands.
if os.environ.get("MEDIAMAN_EAGER_APP", "").strip() == "1":
    app = create_app()
