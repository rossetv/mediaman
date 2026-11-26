"""Shared OpenAI HTTP client and low-level API call helper.

This module owns the singleton _OPENAI_CLIENT and the
call_openai function that sends prompts to the Responses API.
All security checks (web-search gating, title validation) live here so
they're applied consistently regardless of which prompt is being sent.

The higher-level prompt construction and result parsing live in
mediaman.services.openai.recommendations.prompts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from typing import Any

import requests

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

# Module-level client so the connection pool is shared across calls.
# Connect timeout: 5 s.  Read timeout: 30 s unless a call sets its own.
# Callers that are ``async def`` should wrap calls in ``asyncio.to_thread``
# so this synchronous HTTP call does not stall the event loop.
# A global is required: per-request instantiation would rebuild
# the TCP/TLS connection pool on every call. Single-worker invariant:
# mediaman runs under a single Uvicorn worker; ``SafeHTTPClient`` wraps a
# ``requests.Session`` that is never mutated after construction (no header
# mutation, no auth state, no cookies), so no external lock is needed
# beyond what ``urllib3``'s internal connection pool already provides.
_OPENAI_CLIENT = SafeHTTPClient(
    "https://api.openai.com",
    default_timeout=(5.0, 30.0),
)

# Twenty reasoned picks, some after a web search, take a model longer than a plain answer.
# A call that times out may be billed, so it is never sent again (POST does not retry).
_CALL_TIMEOUT = (5.0, 120.0)
# Twenty picks are about 3,000 tokens; the rest is for what a model thinks before them.
_MAX_OUTPUT_TOKENS = 16_000

logger = logging.getLogger(__name__)

#: Models the settings page offers and the settings API accepts.
OPENAI_MODELS: tuple[str, ...] = ("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra", "gpt-6-luna")

# Also used when a stored choice is no longer offered, so an install that saved a
# retired model keeps working instead of sending OpenAI a model it has dropped.
_DEFAULT_MODEL = "gpt-6.2-sol"

# The digest of the key OpenAI last refused, in memory only: a changed key is not refused.
_refused_key: str | None = None

_MARKDOWN_LINK_RE = re.compile(r"\[.*?\]\(.*?\)")

#: A domain of any ending, as mail programs and pages make links of ("S.W.A.T." is none).
DOMAIN_RE = re.compile(r"\w\.(?:[^\W\d_]{2,}|xn--)")


def get_openai_key(conn: sqlite3.Connection | None, secret_key: str | None = None) -> str:
    """Read the OpenAI API key from settings, decrypting with *secret_key* (if None,
    encrypted values fall back silently).

    Raises ValueError when no key is configured. Plaintext credentials from the
    environment are refused; only MEDIAMAN_SECRET_KEY is read from it.
    """
    if conn is not None:
        # Import via settings_reader so tests that patch
        # ``mediaman.services.infra.settings_reader.get_string_setting`` intercept
        # this call correctly; patching the re-export on the infra package would
        # require a different patch target.
        from mediaman.services.infra.settings_reader import get_string_setting

        val = get_string_setting(conn, "openai_api_key", secret_key=secret_key)
        if val:
            logger.debug("OpenAI API key loaded from database settings")
            return val
    raise ValueError("OpenAI API key is not configured (set it in Settings)")


def get_openai_model(conn: sqlite3.Connection | None) -> str:
    if conn is None:
        return _DEFAULT_MODEL
    from mediaman.services.infra import get_string_setting

    model = get_string_setting(conn, "openai_model", default=_DEFAULT_MODEL)
    return model if model in OPENAI_MODELS else _DEFAULT_MODEL


def is_web_search_enabled(conn: sqlite3.Connection | None) -> bool:
    """Return whether openai_web_search_enabled is True. Defaults to False so the
    prompt-injection surface of web search is opt-in.
    """
    if conn is None:
        return False
    from mediaman.services.infra import get_bool_setting

    return get_bool_setting(conn, "openai_web_search_enabled", default=False)


def _title_hostile(title: str) -> bool:
    """Whether *title* holds a character that is neither printable ASCII nor a letter, mark,
    number or punctuation of another script, markdown link syntax ``[text](url)``, or a URL
    scheme (``http://``, ``https://``): what a manipulated web search leaves.
    """
    if not title or not all(
        " " <= c <= "~" or (ord(c) > 0x7F and unicodedata.category(c)[0] in "LMNP") for c in title
    ):
        return True
    return bool(_MARKDOWN_LINK_RE.search(title) or re.search(r"https?://", title, re.IGNORECASE))


def holds_domain(title: str) -> bool:
    """Whether *title* holds an address ("evil.xyz"), as "Mr.Robot" does by its shape."""
    return DOMAIN_RE.search(title) is not None


def _build_request(
    prompt: str,
    model: str,
    api_key: str,
    web_search_active: bool,
) -> tuple[dict[str, object], dict[str, str]]:
    """Build the Responses-API request body and auth headers.

    ``web_search_active`` controls whether the ``web_search_preview`` tool
    and the instruction to search the web are included. Both the
    directive and the tool must arrive together — or neither — so the model
    is never told to do something it has no way to do.
    """
    body: dict[str, object] = {
        "model": model,
        "input": prompt,
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
    }
    if web_search_active:
        body["instructions"] = (
            "You are a media recommendation engine. Use web search for current facts. "
            "Return only valid JSON."
        )
        body["tools"] = [{"type": "web_search_preview"}]
    else:
        body["instructions"] = "You are a media recommendation engine. Return only valid JSON."
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return body, headers


def _parse_response(data: Any) -> list[dict[str, object]] | None:
    """Extract the list of recommendation dicts from a Responses-API payload.

    The ``/v1/responses`` API rejects a top-level JSON array, so the model
    wraps our requested array inside an object. Our prompt asks for the
    wrapper key ``"items"``, but earlier model revisions chose ``"results"``
    or ``"recommendations"`` — fall back to "first list-valued field" so a
    single rename does not silently nuke every recommendation.

    Returns the list of dict items, or ``None`` if no extractable list was
    found (caller logs and returns ``[]``).
    """
    content = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    content = part.get("text", "")
                    break

    content = content.strip()
    # Defensive fallback: strip markdown code fences if the model ignored
    # the json_object format request and wrapped the output anyway.
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```\s*$", "", content)

    raw: object = json.loads(content)
    raw_items: object
    if isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, dict):
        raw_items = raw.get("items")
        if not isinstance(raw_items, list):
            raw_items = next(
                (v for v in raw.values() if isinstance(v, list)),
                None,
            )
    else:
        raw_items = None

    if not isinstance(raw_items, list):
        logger.warning(
            "OpenAI response had no extractable list — top-level type was %s, "
            "keys=%r. Returning empty batch.",
            type(raw).__name__,
            list(raw.keys()) if isinstance(raw, dict) else None,
        )
        return None

    items = [i for i in raw_items if isinstance(i, dict)]
    if not items:
        logger.info("OpenAI's answer held nothing usable in %d items", len(raw_items))
    return items


class OpenAIUnavailable(Exception):
    """No answer came from OpenAI; *refused* when it refused the API key, or none is set."""

    def __init__(self, *, refused: bool = False) -> None:
        super().__init__("OpenAI refused the API key" if refused else "OpenAI gave no answer")
        self.refused = refused


def _digest(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def key_refused(conn: sqlite3.Connection | None, secret_key: str | None = None) -> bool:
    """Whether OpenAI refused the API key now set when it was last sent."""
    try:
        api_key = get_openai_key(conn, secret_key)
    except ValueError:
        return False
    return _refused_key is not None and _refused_key == _digest(api_key)


def _log_error(exc: Exception) -> None:
    """One plain line for what OpenAI or the network did; a traceback only for the rest."""
    if isinstance(exc, SafeHTTPError):
        if exc.status_code == 401:
            logger.warning("OpenAI API key rejected (401) — check settings")
        else:
            logger.warning("OpenAI API returned HTTP error: %s", exc)
    elif isinstance(exc, requests.Timeout):
        logger.warning("OpenAI API call timed out")
    elif isinstance(exc, requests.RequestException):
        logger.warning("OpenAI API network error: %s", exc)
    else:
        logger.exception("Failed to parse OpenAI response: %s", exc)


def ask_openai(
    prompt: str,
    conn: sqlite3.Connection | None,
    use_web_search: bool = False,
    *,
    secret_key: str | None = None,
) -> list[dict[str, object]]:
    """Send a prompt to the Responses API and parse the JSON array response.

    The web_search_preview tool is included only when *use_web_search* is True and the
    openai_web_search_enabled setting is on; it is gated because web pages are an
    indirect-prompt-injection surface. *use_web_search* defaults to False so callers
    must opt in alongside the operator setting.

    An empty list is an answer that held nothing. Raises OpenAIUnavailable when no answer
    came: the key was refused or is not set, OpenAI could not be reached or answered in a
    shape that cannot be read, or, with web search active, a title looked adversarial (the
    whole batch is then rejected; a title that holds a domain is left out alone).
    """
    global _refused_key
    try:
        api_key = get_openai_key(conn, secret_key)
    except ValueError:
        logger.info("OpenAI skipped — no API key is set")
        raise OpenAIUnavailable(refused=True) from None

    web_search_active = use_web_search and is_web_search_enabled(conn)
    model = get_openai_model(conn)

    try:
        body, headers = _build_request(prompt, model, api_key, web_search_active)
        resp = _OPENAI_CLIENT.post(
            "/v1/responses", headers=headers, json=body, timeout=_CALL_TIMEOUT
        )
        _refused_key = None
        items = _parse_response(resp.json())
    except (
        SafeHTTPError,
        requests.Timeout,
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
    ) as exc:
        _log_error(exc)
        refused = isinstance(exc, SafeHTTPError) and exc.status_code == 401
        if refused:
            _refused_key = _digest(api_key)
        raise OpenAIUnavailable(refused=refused) from exc
    if items is None:
        raise OpenAIUnavailable

    if web_search_active:
        # Conservative security choice: reject the entire batch on any hostile
        # title rather than filtering per-item. A single adversarial title
        # indicates the web-search path may have been manipulated, so the entire
        # result set is untrusted. A title that only has the shape of a domain
        # ("Mr.Robot") is the one that is dropped.
        for item in items:
            title = str(item.get("title", ""))
            if _title_hostile(title):
                logger.error(
                    "openai.web_search.batch_rejected title=%r — "
                    "title failed safety check; entire batch discarded",
                    title,
                )
                raise OpenAIUnavailable
        items = [i for i in items if not holds_domain(str(i.get("title", "")))]

    return items


def call_openai(
    prompt: str,
    conn: sqlite3.Connection | None,
    use_web_search: bool = False,
    *,
    secret_key: str | None = None,
) -> list[dict[str, object]]:
    """ask_openai, with [] when no answer came."""
    try:
        return ask_openai(prompt, conn, use_web_search, secret_key=secret_key)
    except OpenAIUnavailable:
        return []
