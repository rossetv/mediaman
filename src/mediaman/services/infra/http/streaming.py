"""Streaming response helpers — size-capping and content-type validation.

Responsibility
--------------
Read a :class:`requests.Response` body in chunks and enforce two safety
properties before returning the buffered bytes:

1. **Size cap** — abort if the accumulated body would exceed ``max_bytes``.
   A fast-fail on ``Content-Length`` avoids reading anything at all when
   the server advertises an oversize payload; the chunk loop catches servers
   that omit or lie about ``Content-Length``.

2. **Content-type validation** — when the caller supplies
   ``expected_content_type``, the response ``Content-Type`` header (stripped
   of parameters, lowercased) must start with that value.  A mismatch raises
   :class:`_ContentTypeMismatch` before any body is read, preventing a
   misconfigured upstream from smuggling HTML or binary data into a JSON
   path.

Both errors are signalled via private exception classes
(:class:`_SizeCapExceeded`, :class:`_ContentTypeMismatch`) so the caller
(the retry loop in :mod:`~mediaman.services.infra.http.retry`) can map them
to :class:`~mediaman.services.infra.http.client.SafeHTTPError` with the
appropriate status code and URL context.

Invariants
----------
- ``_read_capped`` never returns partial data — it raises or returns the
  complete buffered body.
- The cap applies to *decoded* bytes as seen by Python (i.e., after any
  transfer-encoding the transport handles).  When ``expected_content_type``
  is set, any ``Content-Encoding`` other than ``identity`` is rejected
  because the transport may already have decoded the body, making the raw
  byte count unreliable.
"""

from __future__ import annotations

import requests


class _SizeCapExceeded(Exception):
    """Internal signal that the streamed body breached the byte cap."""


class _ContentTypeMismatch(Exception):
    """Internal signal that the response ``Content-Type`` was unexpected."""


def _read_capped(
    response: requests.Response,
    max_bytes: int,
    *,
    expected_content_type: str | None = None,
) -> bytes:
    """Read *response* body up to *max_bytes*, raising if the cap is hit.

    Honours an advertised ``Content-Length`` as a fast-fail before reading
    anything, then streams chunks to catch servers that omit or lie about
    ``Content-Length``.

    When *expected_content_type* is non-``None``, the response's
    ``Content-Type`` header (case-insensitive, parameter-stripped) is
    matched against it before any body is read.  A mismatch raises
    :class:`_ContentTypeMismatch`.  The match is a prefix on the
    ``type/subtype`` portion so ``"application/json"`` matches both
    ``"application/json"`` and ``"application/json; charset=utf-8"``.
    A response advertising ``Content-Encoding`` other than ``identity``
    is also rejected when a content-type is pinned — see module docstring.
    """
    if expected_content_type:
        ctype_raw = response.headers.get("Content-Type", "") or ""
        # Strip parameters such as charset / boundary.
        ctype = ctype_raw.split(";", 1)[0].strip().lower()
        expected = expected_content_type.split(";", 1)[0].strip().lower()
        if not ctype.startswith(expected):
            raise _ContentTypeMismatch(
                f"unexpected Content-Type {ctype_raw!r}; expected {expected_content_type!r}"
            )
        # ``identity`` (or absent) is the only safe encoding when the caller
        # has pinned a specific content-type — any other encoding means
        # urllib3/requests will decode for us, which can defeat the byte cap.
        encoding = (response.headers.get("Content-Encoding", "") or "").strip().lower()
        if encoding and encoding not in ("identity", ""):
            raise _ContentTypeMismatch(
                f"unexpected Content-Encoding {encoding!r}; expected identity"
            )

    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise _SizeCapExceeded(
                    f"response body too large: declared {declared} > cap {max_bytes}"
                )
        except ValueError:
            # Malformed header — fall through to streaming enforcement.
            pass

    buf = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise _SizeCapExceeded(f"response body exceeded cap of {max_bytes} bytes")
    return bytes(buf)
