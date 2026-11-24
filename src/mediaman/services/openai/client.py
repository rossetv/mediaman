"""Shared OpenAI HTTP client and low-level API call helper.

This module owns the singleton _OPENAI_CLIENT and the
call_openai function that sends prompts to the Responses API.
All security checks (web-search gating, title validation) live here so
they're applied consistently regardless of which prompt is being sent.

The higher-level prompt construction and result parsing live in
mediaman.services.openai.recommendations.prompts.
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)

#: Models the settings page offers and the settings API accepts.
OPENAI_MODELS: tuple[str, ...] = ("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra", "gpt-6-luna")

# Also used when a stored choice is no longer offered, so an install that saved a
# retired model keeps working instead of sending OpenAI a model it has dropped.
_DEFAULT_MODEL = "gpt-6.2-sol"

_MARKDOWN_LINK_RE = re.compile(r"\[.*?\]\(.*?\)")


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


def is_web_search_title_safe(title: str) -> bool:
    """Return True if *title* is safe to persist after a web-search response.

    Rejects the entire batch (caller must check the return value) if:
    - The title contains a character that is neither printable ASCII nor a letter, mark,
      number or punctuation of another script (so no control or invisible character).
    - The title contains markdown link syntax ``[text](url)``.
    - The title contains a URL scheme pattern (``http://``, ``https://``).
    """
    if not title or not all(
        " " <= c <= "~" or (ord(c) > 0x7F and unicodedata.category(c)[0] in "LMNP") for c in title
    ):
        return False
    if _MARKDOWN_LINK_RE.search(title):
        return False
    return not re.search(r"https?://", title, re.IGNORECASE)


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

    return [i for i in raw_items if isinstance(i, dict)]


def _handle_error(exc: Exception) -> list[dict[str, object]]:
    """Log *exc* and return the empty-batch sentinel [], keeping call_openai's error
    handling in one place.
    """
    if isinstance(exc, SafeHTTPError):
        if exc.status_code == 401:
            logger.exception("OpenAI API key rejected (401) — check settings")
        else:
            logger.exception("OpenAI API returned HTTP error: %s", exc)
    elif isinstance(exc, requests.Timeout):
        logger.warning("OpenAI API call timed out", exc_info=True)
    elif isinstance(exc, requests.RequestException):
        logger.exception("OpenAI API network error: %s", exc)
    else:
        logger.exception("Failed to parse OpenAI response: %s", exc)
    return []


def call_openai(
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

    When web search is active every returned title must pass is_web_search_title_safe;
    if any looks adversarial the whole batch is rejected and [] is returned.
    """
    try:
        api_key = get_openai_key(conn, secret_key)
    except ValueError:
        logger.warning("Recommendations skipped — OpenAI API key not configured")
        return []

    web_search_active = use_web_search and is_web_search_enabled(conn)
    model = get_openai_model(conn)

    try:
        body, headers = _build_request(prompt, model, api_key, web_search_active)
        resp = _OPENAI_CLIENT.post(
            "/v1/responses", headers=headers, json=body, timeout=_CALL_TIMEOUT
        )
        items = _parse_response(resp.json())
        if items is None:
            return []

        if web_search_active:
            # Conservative security choice: reject the entire batch on any
            # unsafe title rather than filtering per-item. A single adversarial
            # title indicates the web-search path may have been manipulated, so
            # the entire result set is untrusted. The per-item filtering path
            # (used for non-web-search results via _validate_llm_string) is
            # deliberately more lenient because it operates on model-only
            # output without the indirect-prompt-injection surface.
            for item in items:
                title = str(item.get("title", ""))
                if not is_web_search_title_safe(title):
                    logger.error(
                        "openai.web_search.batch_rejected title=%r — "
                        "title failed safety check; entire batch discarded",
                        title,
                    )
                    return []

        return items

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
        return _handle_error(exc)
