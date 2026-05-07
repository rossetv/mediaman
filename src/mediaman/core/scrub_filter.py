"""Ring 0: defence-in-depth logging redactor.

Scrubs token/key substrings from log records emitted by third-party
libraries that may otherwise leak credentials in DEBUG mode (urllib3,
requests).  Idempotent so module re-imports don't stack filters.

Ring 0 contract: stdlib only (logging, collections.abc), no I/O, no
imports from other mediaman modules.

Canonical home: ``mediaman.core.scrub_filter``.
Back-compat shim: ``mediaman.services.infra.scrub_filter``.

Usage::

    from mediaman.core.scrub_filter import ScrubFilter, install_root_filter

    # Preferred: attach once to every handler on the root mediaman logger.
    # Filters on a *handler* apply to all log records routed through that
    # handler regardless of which child logger (getLogger(__name__)) emits
    # them, because handler-level filters are evaluated after propagation.
    # This is propagation-safe: any future module using getLogger(__name__)
    # automatically inherits redaction without a per-module attach call.
    install_root_filter(secrets=[api_key])

    # Legacy: attach directly to a named logger (still supported).
    ScrubFilter.attach("urllib3.connectionpool", secrets=[api_key])
    ScrubFilter.attach("mediaman", secrets=[api_key])
"""

from __future__ import annotations

import logging
from collections.abc import Iterable


class ScrubFilter(logging.Filter):
    """Logging filter that replaces sensitive substrings in log records.

    Walks both ``record.msg`` and ``record.args``, replacing each non-empty
    secret string with *replacement*.  Returns ``True`` unconditionally so the
    (scrubbed) record is always emitted — this filter redacts, it does not gate.

    The filter must not log anything itself; it runs deep inside the logging
    machinery and any attempt to emit a record here would recurse infinitely.

    Args:
        secrets: Iterable of secret strings to redact.  Empty strings are
            silently ignored.
        replacement: The string substituted for each found secret.
            Defaults to ``"***REDACTED***"``.
    """

    def __init__(
        self,
        secrets: Iterable[str],
        replacement: str = "***REDACTED***",
    ) -> None:
        super().__init__()
        # Deduplicate while preserving insertion order; filter empty strings.
        seen: set[str] = set()
        self._secrets: list[str] = []
        for s in secrets:
            if s and s not in seen:
                seen.add(s)
                self._secrets.append(s)
        self._replacement = replacement

    # ------------------------------------------------------------------
    # logging.Filter interface
    # ------------------------------------------------------------------

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub secrets from *record* in place; always returns ``True``."""
        try:
            if isinstance(record.msg, str):
                record.msg = self._scrub(record.msg)
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        self._scrub(a) if isinstance(a, str) else a for a in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: self._scrub(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
        except Exception:
            # A filter that raises silences the log record entirely.
            # Swallow all errors and let the (possibly unscrubbed) record
            # through rather than breaking application logging.
            pass
        return True

    # ------------------------------------------------------------------
    # Runtime secret registration
    # ------------------------------------------------------------------

    def register_secret(self, secret: str) -> None:
        """Add *secret* to the set of strings that will be redacted.

        Safe to call at any time — including from background threads that
        resolve runtime secrets (e.g. a Plex token read from the DB after
        startup).  Empty strings are silently ignored.  Already-registered
        secrets are not duplicated.
        """
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Return *text* with every secret replaced by the replacement string."""
        for secret in self._secrets:
            text = text.replace(secret, self._replacement)
        return text

    # ------------------------------------------------------------------
    # Idempotent attach helper
    # ------------------------------------------------------------------

    @classmethod
    def attach(
        cls,
        logger_name: str,
        secrets: Iterable[str],
        replacement: str = "***REDACTED***",
    ) -> ScrubFilter:
        """Add a :class:`ScrubFilter` to the named logger, deduplicating on attach.

        If a :class:`ScrubFilter` covering the same set of secrets and
        replacement string is already attached to the logger, the existing
        instance is returned and no duplicate is added.  This makes the call
        safe at module import time — repeated imports do not stack filters.

        Args:
            logger_name: The name passed to :func:`logging.getLogger`.
            secrets: Iterable of secret strings to redact.
            replacement: Substitution string; defaults to ``"***REDACTED***"``.

        Returns:
            The :class:`ScrubFilter` instance now attached to the logger.
        """
        target = logging.getLogger(logger_name)
        # Normalise secrets to a sorted tuple for equality checks.
        secret_list = sorted(s for s in secrets if s)
        for f in target.filters:
            if (
                isinstance(f, cls)
                and sorted(f._secrets) == secret_list
                and f._replacement == replacement
            ):
                return f
        new_filter = cls(secrets=secret_list, replacement=replacement)
        target.addFilter(new_filter)
        return new_filter


# ---------------------------------------------------------------------------
# Module-level singleton and root-handler install
# ---------------------------------------------------------------------------

#: Single shared :class:`ScrubFilter` instance attached to every handler on
#: the ``"mediaman"`` root logger.  Use :func:`register_secret` to add runtime
#: secrets (e.g. a Plex token resolved from the DB after startup) without
#: constructing a new filter instance.
_root_filter: ScrubFilter | None = None


def install_root_filter(secrets: Iterable[str] = ()) -> ScrubFilter:
    """Attach a single :class:`ScrubFilter` to every handler on the ``"mediaman"`` logger.

    Filters attached to a *handler* (not a logger) apply to every log record
    routed through that handler regardless of which child logger emitted it.
    Because child loggers propagate records up to the root mediaman logger,
    attaching to the handler is propagation-safe: any module that uses
    ``logging.getLogger(__name__)`` automatically inherits redaction without
    a per-module :meth:`ScrubFilter.attach` call.

    The function is idempotent — repeated calls extend the existing singleton's
    secret list rather than stacking duplicate filters.  Call
    :func:`register_secret` afterwards to add runtime secrets one at a time.

    Args:
        secrets: Initial set of secret strings to redact.

    Returns:
        The :class:`ScrubFilter` singleton now attached to every handler.
    """
    global _root_filter
    mediaman_logger = logging.getLogger("mediaman")

    if _root_filter is None:
        _root_filter = ScrubFilter(secrets=secrets)
    else:
        for s in secrets:
            _root_filter.register_secret(s)

    for handler in mediaman_logger.handlers:
        if _root_filter not in handler.filters:
            handler.addFilter(_root_filter)

    return _root_filter


def register_secret(secret: str) -> None:
    """Register *secret* with the root mediaman :class:`ScrubFilter`.

    Convenience wrapper around :meth:`ScrubFilter.register_secret` on the
    singleton created by :func:`install_root_filter`.  Safe to call from any
    thread at any time after :func:`install_root_filter` has been called once
    at startup.  A no-op if :func:`install_root_filter` has not yet been
    called (the filter does not yet exist).

    Args:
        secret: The secret string to redact in future log records.
    """
    if _root_filter is not None:
        _root_filter.register_secret(secret)
