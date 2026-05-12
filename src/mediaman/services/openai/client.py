"""Shared OpenAI HTTP client and low-level API call helper.

This module owns the singleton :data:`_OPENAI_CLIENT` and the
:func:`call_openai` function that sends prompts to the Responses API.
All security checks (web-search gating, title validation) live here so
they're applied consistently regardless of which prompt is being sent.

The higher-level prompt construction and result parsing live in
:mod:`mediaman.services.openai.recommendations.prompts`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

import requests

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

# Module-level client so the connection pool is shared across calls.
# Connect timeout: 5 s.  Read timeout: 30 s — the 90 s default was blocking
# the scan path for too long on a slow or unreachable OpenAI endpoint.
# Callers that are ``async def`` should wrap calls in ``asyncio.to_thread``
# so this synchronous HTTP call does not stall the event loop.
_OPENAI_CLIENT = SafeHTTPClient(
    "https://api.openai.com",
    default_timeout=(5.0, 30.0),
)

logger = logging.getLogger(__name__)

# Default OpenAI model for the /v1/responses API.
_DEFAULT_MODEL = "gpt-4.1"

# Regex for validating web-search-derived recommendation titles.
_SAFE_TITLE_RE = re.compile(r"^[\x20-\x7E]+$")
_MARKDOWN_LINK_RE = re.compile(r"\[.*?\]\(.*?\)")


def get_openai_key(conn: sqlite3.Connection | None, secret_key: str | None = None) -> str:
    """Read the OpenAI API key from settings.

    ``secret_key`` is passed to the DB reader to decrypt the stored key when
    it is encrypted. If ``None``, unencrypted keys are still returned but
    encrypted ones fall back silently.

    Logs (DEBUG) when the key is loaded. Raises ValueError if the key is not
    configured (neither in database settings nor MEDIAMAN_SECRET_KEY for
    encrypted values).

    §10.3 forbids plaintext credentials from environment variables other than
    MEDIAMAN_SECRET_KEY; the env-var fallback was removed to prevent
    unencrypted on-disk credentials.
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
    """Return the OpenAI model to use, honouring the ``openai_model`` setting."""
    if conn is None:
        return _DEFAULT_MODEL
    from mediaman.services.infra import get_string_setting

    return get_string_setting(conn, "openai_model", default=_DEFAULT_MODEL) or _DEFAULT_MODEL


def is_web_search_enabled(conn: sqlite3.Connection | None) -> bool:
    """Return whether ``openai_web_search_enabled`` is set to True in settings.

    Defaults to False so the indirect-prompt-injection surface (the model
    pulling arbitrary web content) is opt-in.
    """
    if conn is None:
        return False
    from mediaman.services.infra import get_bool_setting

    return get_bool_setting(conn, "openai_web_search_enabled", default=False)


def is_web_search_title_safe(title: str) -> bool:
    """Return True if *title* is safe to persist after a web-search response.

    Rejects the entire batch (caller must check the return value) if:
    - The title contains non-printable-ASCII characters.
    - The title contains markdown link syntax ``[text](url)``.
    - The title contains a URL scheme pattern (``http://``, ``https://``).
    """
    if not _SAFE_TITLE_RE.match(title):
        return False
    if _MARKDOWN_LINK_RE.search(title):
        return False
    return not re.search(r"https?://", title, re.IGNORECASE)


def call_openai(
    prompt: str,
    conn: sqlite3.Connection | None,
    use_web_search: bool = False,
    *,
    secret_key: str | None = None,
) -> list[dict[str, object]]:
    """Send a prompt to OpenAI Responses API and parse the JSON array response.

    Always uses the Responses API (``/v1/responses``). When both
    ``use_web_search`` is True *and* the ``openai_web_search_enabled``
    setting is enabled, the ``web_search_preview`` tool is included so
    GPT can look up real-time data.  The tool is gated behind the setting
    (default False) because it is an indirect-prompt-injection surface —
    the model can pull and execute instructions from arbitrary web pages.

    The default for ``use_web_search`` is False so the caller has to
    explicitly opt in alongside the operator-side setting; before this
    fix the caller default was True and the gate alone determined
    behaviour, which made it easy for a new code path to silently ask
    for web search even when the operator had disabled it.

    When web search is active, every returned recommendation title is
    validated against a strict safe-printable-ASCII check.  If any title
    looks adversarial (non-ASCII, markdown link syntax, embedded URL) the
    entire batch is rejected and an empty list is returned.
    """
    try:
        api_key = get_openai_key(conn, secret_key)
    except ValueError:
        logger.warning("Recommendations skipped — OpenAI API key not configured")
        return []

    web_search_active = use_web_search and is_web_search_enabled(conn)
    model = get_openai_model(conn)

    try:
        body: dict[str, object] = {
            "model": model,
            "input": prompt,
            "text": {"format": {"type": "json_object"}},
        }
        if web_search_active:
            # ``instructions`` only needs to push the model toward live
            # data when the web-search tool is actually wired up. Sending
            # the "ALWAYS search the web" line without the tool meant the
            # model was told to do something it had no way to do, which
            # both wastes prompt tokens and primes a higher
            # hallucination rate. Now both the directive and the tool
            # arrive together — or neither.
            body["instructions"] = (
                "You are a media recommendation engine. ALWAYS search the web to find "
                "current, real, accurate information. Do not rely on training data alone. "
                "Return only valid JSON."
            )
            body["tools"] = [{"type": "web_search_preview"}]
        else:
            body["instructions"] = "You are a media recommendation engine. Return only valid JSON."

        resp = _OPENAI_CLIENT.post(
            "/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        data = resp.json()

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

        raw_items: object = json.loads(content)
        if not isinstance(raw_items, list):
            return []

        items: list[dict[str, object]] = [i for i in raw_items if isinstance(i, dict)]

        if web_search_active:
            for item in items:
                title = str(item.get("title", ""))
                if not is_web_search_title_safe(title):
                    logger.warning(
                        "Rejecting web-search recommendation batch — title failed safety check: %r",
                        title,
                    )
                    return []

        return items

    except SafeHTTPError as exc:
        if exc.status_code == 401:
            logger.error("OpenAI API key rejected (401) — check settings")
        else:
            logger.exception("OpenAI API returned HTTP error: %s", exc)
        return []
    except requests.Timeout:
        logger.warning("OpenAI API call timed out after 30 s", exc_info=True)
        return []
    except requests.RequestException as exc:
        logger.exception("OpenAI API network error: %s", exc)
        return []
    except (ValueError, KeyError) as exc:
        logger.exception("Failed to parse OpenAI response: %s", exc)
        return []
