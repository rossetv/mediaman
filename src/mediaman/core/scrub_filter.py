"""Defence-in-depth logging redactor.

Scrubs token/key substrings from log records emitted by third-party
libraries that may otherwise leak credentials in DEBUG mode (urllib3,
requests).  Idempotent so module re-imports don't stack filters.

Stdlib only (logging, collections.abc), no I/O, no imports from other
mediaman modules.

Usage::

    from mediaman.core.scrub_filter import ScrubFilter, install_root_filter

    # Preferred: install once at startup.  It gives the root mediaman logger
    # a stderr handler at INFO and attaches the filter to its handlers.
    # Filters on a *handler* apply to all log records routed through that
    # handler regardless of which child logger (getLogger(__name__)) emits
    # them, because handler-level filters are evaluated after propagation.
    # This is propagation-safe: any future module using getLogger(__name__)
    # automatically inherits redaction without a per-module attach call.
    install_root_filter(secrets=[api_key])

    # Per-named-logger attach for third-party loggers outside the
    # mediaman tree (e.g. urllib3.connectionpool).
    ScrubFilter.attach("urllib3.connectionpool", secrets=[api_key])
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Iterable
from typing import TextIO
from urllib.parse import unquote_plus


class ScrubFilter(logging.Filter):
    """Logging filter that replaces sensitive substrings in log records.

    Walks ``record.msg``, ``record.args`` (including the ``str()`` of
    non-string args such as exceptions), the formatted traceback and the
    stack info, replacing each non-empty secret string with *replacement*.  Returns ``True`` unconditionally so the
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
        # Protects _secrets against concurrent register_secret calls from
        # background threads (docstring advertises thread-safety).
        self._lock = threading.Lock()
        # Deduplicate while preserving insertion order; filter empty strings.
        seen: set[str] = set()
        self._secrets: list[str] = []
        for s in secrets:
            if s and s not in seen:
                seen.add(s)
                self._secrets.append(s)
        self._replacement = replacement

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub secrets from *record* in place; always returns ``True``."""
        try:
            record.msg = self._scrub_value(record.msg)
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(self._scrub_value(a) for a in record.args)
                elif isinstance(record.args, dict):
                    record.args = {k: self._scrub_value(v) for k, v in record.args.items()}
            if record.exc_info and not record.exc_text:
                # Formatters reuse a cached exc_text, so rendering the
                # traceback here lets it be scrubbed before any handler prints it.
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self._scrub(record.exc_text)
            if record.stack_info:
                record.stack_info = self._scrub(record.stack_info)
        except Exception:  # rationale: logging filter cannot call logger.* in its own except handler (would recurse infinitely through the filter itself); redaction failure must be swallowed and the record emitted unscrubbed rather than crash logging.
            # A filter that raises silences the log record entirely. Returning True
            # lets the (possibly unscrubbed) record through rather than silencing
            # application logging.  This is the only safe option for a logging filter.
            return True
        return True

    def register_secret(self, secret: str) -> None:
        """Add *secret* to the set of strings that will be redacted.

        Safe to call at any time — including from background threads that
        resolve runtime secrets (e.g. a Plex token read from the DB after
        startup).  Empty strings are silently ignored.  Already-registered
        secrets are not duplicated.
        """
        if not secret:
            return
        with self._lock:
            if secret not in self._secrets:
                self._secrets.append(secret)

    def _scrub_value(self, value: object) -> object:
        """Return *value* scrubbed, keeping it as is unless it holds a secret.

        A non-string (an exception, a URL object) is replaced only when its
        ``str()`` or ``repr()`` carries a secret, by a stand-in that renders
        both scrubbed for ``%s`` and ``%r``; numeric args still work with
        ``%d`` and clean objects keep their own formatting.
        """
        if isinstance(value, str):
            return self._scrub(value)
        if value is None or isinstance(value, int | float):
            return value
        try:
            text = str(value)
        except (
            Exception
        ):  # rationale: a raising __str__ must not stop the other args being scrubbed
            return _Scrubbed(_UNPRINTABLE_ARG, _UNPRINTABLE_ARG)
        try:
            rep = repr(value)
        except (
            Exception
        ):  # rationale: a raising __repr__ must not stop the other args being scrubbed
            rep = text
        scrubbed_text, scrubbed_rep = self._scrub(text), self._scrub(rep)
        if scrubbed_text == text and scrubbed_rep == rep:
            return value
        return _Scrubbed(scrubbed_text, scrubbed_rep)

    def _scrub(self, text: str) -> str:
        """Return *text* with every secret replaced by the replacement string.

        Cost is O(secrets × len(text)) per record. The registered-secret
        set is expected to be tiny (a handful: the secret key, Plex/arr
        tokens, SMTP credentials) and is deduplicated on registration, so
        the per-record scan stays cheap; there is no need for a cap here.
        """
        with self._lock:
            secrets_snapshot = list(self._secrets)
        for secret in secrets_snapshot:
            text = text.replace(secret, self._replacement)
        return _CAST_TICKET_RE.sub(lambda m: m.group(1) + self._replacement, text)

    @classmethod
    def attach(
        cls,
        logger_name: str,
        secrets: Iterable[str],
        replacement: str = "***REDACTED***",
    ) -> ScrubFilter:
        """Add a ScrubFilter to the named logger, deduplicating on attach.

        If a ScrubFilter covering the same set of secrets and
        replacement string is already attached to the logger, the existing
        instance is returned and no duplicate is added.  This makes the call
        safe at module import time — repeated imports do not stack filters.

        Args:
            logger_name: The name passed to logging.getLogger.
            secrets: Iterable of secret strings to redact.
            replacement: Substitution string; defaults to ``"***REDACTED***"``.

        Returns:
            The ScrubFilter instance now attached to the logger.
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


_UNPRINTABLE_ARG = "<unprintable log argument>"


class _Scrubbed:
    """Stand-in log argument holding an object's scrubbed ``str()`` and ``repr()``."""

    __slots__ = ("_rep", "_text")

    def __init__(self, text: str, rep: str) -> None:
        self._text = text
        self._rep = rep

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._rep


_REDACTED_TOKEN = "***"
# Emailed links carry their credential in the path (/keep/<token>,
# /download/<token>, /api/keep/<token>/forever), as a TV's stream address does
# (/api/cast/<ticket>/...), or in a query value
# (/unsubscribe?token=..., /api/poster/<key>?sig=...).
# Slash runs and case are ignored because the credential is still usable
# through those spellings.
_TOKEN_PATH_RE = re.compile(
    r"^((?:/+api)?/+keep/+|/+download/+|/+api/+cast/+)[^/?#]+", re.IGNORECASE
)
# The same ticket anywhere in a line, such as an address an error quotes.
_CAST_TICKET_RE = re.compile(r"(/api/+cast/+)[A-Za-z0-9_-]{16,}", re.IGNORECASE)
_TOKEN_NAME_RE = re.compile(r"[\w.-]*token|sig|api_?key", re.IGNORECASE)
# What a person searched for, and the addresses of a page to go back to that may hold it.
_SEARCH_NAME = "q"
_ADDRESS_NAMES = frozenset({"from", "next"})
_SEARCH_PARAM_RE = re.compile(r"(?:^|[?&])q=")
# How deep an address may stand encoded in others.
_DECODES_MAX = 4


def _holds_search(address: str) -> bool:
    for _ in range(_DECODES_MAX):
        if _SEARCH_PARAM_RE.search(address):
            return True
        decoded = unquote_plus(address)
        if decoded == address:
            return False
        address = decoded
    return True


def _is_private(name: str, value: str) -> bool:
    name = unquote_plus(name)
    if name in _ADDRESS_NAMES:
        return _holds_search(value)
    return name == _SEARCH_NAME or _TOKEN_NAME_RE.fullmatch(name) is not None


def _redact_query(query: str) -> str:
    """Return *query* with the value of every token-like parameter masked, and what was
    searched for.

    It is split the way Starlette reads it: on ``&``, then each part on its
    first ``=``.  The name is matched decoded, because the app decodes it.
    """
    parts = query.split("&")
    for i, part in enumerate(parts):
        name, sep, value = part.partition("=")
        if sep and _is_private(name, value):
            parts[i] = name + sep + _REDACTED_TOKEN
    return "&".join(parts)


def _redact_request_target(target: str) -> str:
    """Return *target* (a path with optional query) with its credentials masked."""
    target = _TOKEN_PATH_RE.sub(lambda m: m.group(1) + _REDACTED_TOKEN, target)
    path, sep, query = target.partition("?")
    query, hash_sep, fragment = query.partition("#")
    return path + sep + _redact_query(query) + hash_sep + fragment


class AccessLogFilter(logging.Filter):
    """Mask credentials and searches in the request target of uvicorn access-log records.

    uvicorn logs ``client, method, target, http version, status`` as the
    record's args and its formatter unpacks them by position, so only the
    string args are rewritten and the tuple keeps its shape.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact_request_target(a) if isinstance(a, str) else a for a in record.args
            )
        return True


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Stream handler that writes to whatever ``sys.stderr`` is at emit time.

    Like logging's last-resort handler, it never holds on to a stream that
    was later replaced and closed (as pytest's capture does per test).
    """

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:  # type: ignore[override]
        return sys.stderr


#: Single shared ScrubFilter instance attached to every handler on
#: the ``"mediaman"`` root logger.  Use register_secret to add runtime
#: secrets (e.g. a Plex token resolved from the DB after startup) without
#: constructing a new filter instance.
_root_filter: ScrubFilter | None = None
# Protects _root_filter mutations in install_root_filter (module-level
# mutable singleton accessible from multiple threads).
_root_filter_lock = threading.Lock()


def install_root_filter(secrets: Iterable[str] = ()) -> ScrubFilter:
    """Give the ``"mediaman"`` logger a scrubbing stderr handler at INFO.

    It also scrubs uvicorn's error log and APScheduler's error output, and
    masks emailed tokens and searches in request targets in uvicorn's access log.

    The logger gets one stderr handler (added on the first call only) and,
    unless a level was already set on it, the INFO level; nothing else in
    the process configures it, so without this its records would reach only
    logging's last-resort handler, unscrubbed and WARNING and above.  The
    logger still propagates; the root logger has no handler of its own, so
    no line prints twice.

    Filters attached to a *handler* (not a logger) apply to every log record
    routed through that handler regardless of which child logger emitted it.
    Because child loggers propagate records up to the root mediaman logger,
    attaching to the handler is propagation-safe: any module that uses
    ``logging.getLogger(__name__)`` automatically inherits redaction without
    a per-module ScrubFilter.attach call.

    The function is idempotent — repeated calls extend the existing singleton's
    secret list rather than stacking duplicate filters.  Call
    register_secret afterwards to add runtime secrets one at a time.

    Args:
        secrets: Initial set of secret strings to redact.

    Returns:
        The ScrubFilter singleton now attached to every handler.
    """
    global _root_filter
    mediaman_logger = logging.getLogger("mediaman")

    with _root_filter_lock:
        if _root_filter is None:
            _root_filter = ScrubFilter(secrets=secrets)
        else:
            for s in secrets:
                _root_filter.register_secret(s)
        local_ref = _root_filter

        if not any(isinstance(h, _StderrHandler) for h in mediaman_logger.handlers):
            handler = _StderrHandler()
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            mediaman_logger.addHandler(handler)
        # Under uvicorn the effective level is root's WARNING, which would
        # drop every INFO line; an explicitly set level is left alone.
        if mediaman_logger.level == logging.NOTSET:
            mediaman_logger.setLevel(logging.INFO)

    for h in mediaman_logger.handlers:
        if local_ref not in h.filters:
            h.addFilter(local_ref)

    _install_server_filters(local_ref)
    return local_ref


def _install_server_filters(scrub: ScrubFilter) -> None:
    """Scrub what uvicorn and APScheduler print outside the mediaman logger.

    uvicorn applies its logging config after the app is created, replacing
    the handlers of its loggers but keeping their filters, so the uvicorn
    filters go on the loggers, where every uvicorn record is logged
    directly.  APScheduler logs from child loggers that nothing configures
    and that end at logging's last-resort handler (WARNING and above,
    unscrubbed); a scrubbing handler at the same level on ``apscheduler``
    takes its place, so the same lines print once each, scrubbed.
    """
    error_logger = logging.getLogger("uvicorn.error")
    if scrub not in error_logger.filters:
        error_logger.addFilter(scrub)

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessLogFilter())

    scheduler_logger = logging.getLogger("apscheduler")
    with _root_filter_lock:
        if not any(isinstance(h, _StderrHandler) for h in scheduler_logger.handlers):
            handler = _StderrHandler()
            handler.setLevel(logging.WARNING)
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            handler.addFilter(scrub)
            scheduler_logger.addHandler(handler)


def register_secret(secret: str) -> None:
    """Register *secret* with the root mediaman ScrubFilter.

    Convenience wrapper around ScrubFilter.register_secret on the
    singleton created by install_root_filter.  Safe to call from any
    thread at any time after install_root_filter has been called once
    at startup.  A no-op if install_root_filter has not yet been
    called (the filter does not yet exist).

    Args:
        secret: The secret string to redact in future log records.
    """
    if _root_filter is not None:
        _root_filter.register_secret(secret)
