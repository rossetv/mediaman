# rationale: not split further. This is one concept — the POST /download/{token} flow —
# and it is already decomposed into small private helpers (token validation,
# the two-phase claim, the Arr submit branches, the shared success epilogue,
# the already-exists handler). The Radarr and Sonarr submit helpers share
# _record_and_respond and the _unmark_token_used release path and both build
# their Arr client through ``build_*_from_db`` patched at this module path by
# the test suite; carving them into siblings would fragment the single
# token-flow and scatter that monkeypatch surface for no navigability gain.

from __future__ import annotations

import logging
import math
import sqlite3
from typing import TypedDict

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mediaman.core.audit import log_audit
from mediaman.core.deleted_media import DeletedMedia, fill_show_tvdb_id
from mediaman.crypto import generate_poll_token, validate_download_token
from mediaman.crypto.tokens import DownloadTokenPayload
from mediaman.db import get_db
from mediaman.services.arr.add_outcome import movie_add_landed, series_add_landed
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    ACTION_QUEUED,
    ExistingMovieError,
    add_or_remonitor_movie,
    is_series_already_tracked,
    radarr_fetches_movie,
    radarr_holds_movie,
    season_on_disk,
    sonarr_fetches_series,
    sonarr_tracks_series,
)
from mediaman.services.downloads.notifications import (
    record_download_notification,
    record_notification_on_its_way,
    remember_tvdb_id,
)
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import RateLimiter, get_client_ip
from mediaman.web.auth.middleware import get_optional_admin_from_token
from mediaman.web.responses import respond_failure
from mediaman.web.routes.library_api._redownload_match import _redownload_audit_id

from ._season import (
    already_there,
    deleted_season,
    season_name,
    sonarr_media_id,
)
from ._tokens import (
    _mark_token_used,
    _unmark_token_used,
    add_went_unanswered,
    note_unanswered_add,
)
from .confirm import drop_arr_cache, read_download_state

logger = logging.getLogger(__name__)

router = APIRouter()

# Rate-limiter is process-scoped: per-IP attempt counters must survive across
# requests in the same worker process to be effective.
_DOWNLOAD_LIMITER_POST = RateLimiter(max_attempts=10, window_seconds=60)


class DownloadPayload(TypedDict):
    """Validated parameters for one download submission. conn is passed separately: TypedDicts
    model serialisable data, not execution contexts.
    """

    token: str
    exp: int
    title: str
    tmdb_id: int | None
    email: str | None
    audit_action: str
    audit_detail: str
    actor: str | None
    secret_key: str
    season: DeletedMedia | None


def _record_and_respond(
    *,
    conn: sqlite3.Connection,
    email: str | None,
    title: str,
    media_type: str,
    tmdb_id: int | None,
    service: str,
    audit_action: str,
    audit_detail: str,
    secret_key: str,
    actor: str | None = None,
    tvdb_id: int | None = None,
    season_number: int | None = None,
) -> JSONResponse:
    """Audit, notify, commit, mint a poll token and return the success response; the epilogue
    shared by both submitters.

    email is None when an admin minted the token with no notification email, or when the
    email was asked for before: no notification row is recorded then.
    """
    audit_id = title
    if season_number is not None:
        audit_id = _redownload_audit_id(
            media_type=media_type, tmdb_id=None, tvdb_id=tvdb_id, season_number=season_number
        )
    with conn:
        log_audit(conn, audit_id, audit_action, audit_detail, actor=actor)
        if season_number is not None and tmdb_id is not None and tvdb_id is not None:
            fill_show_tvdb_id(conn, tmdb_id=tmdb_id, tvdb_id=tvdb_id)
        if email:
            record_download_notification(
                conn,
                email=email,
                title=title,
                media_type=media_type,
                tmdb_id=tmdb_id,
                tvdb_id=tvdb_id,
                service=service,
                season_number=season_number,
            )
        if tmdb_id is not None and tvdb_id is not None:
            remember_tvdb_id(conn, tmdb_id=tmdb_id, tvdb_id=tvdb_id)

    poll_token = generate_poll_token(
        media_item_id=(
            sonarr_media_id(title, season_number) if service == "sonarr" else f"radarr:{title}"
        ),
        service=service,
        tmdb_id=tmdb_id if tmdb_id is not None else 0,
        secret_key=secret_key,
    )
    service_label = "Radarr" if service == "radarr" else "Sonarr"
    what = f"'{title}'"
    if season_number is not None:
        what = f"{season_name(season_number).lower()} of '{title}'"
    return JSONResponse(
        {
            "ok": True,
            "message": f"Added {what} to {service_label} — download starting shortly",
            "service": service,
            "tmdb_id": tmdb_id,
            "poll_token": poll_token,
        }
    )


class _HeldNotFetched(SafeHTTPError):
    """A 409 for a title the Arr holds but will not fetch: nothing was started, so the
    link is released rather than burned."""


class _SeasonOnDisk(SafeHTTPError):
    """The 409 for a season Sonarr has complete."""


class _AlreadyThere(SafeHTTPError):
    """A refused add of a title the Arr holds, with the state the emailed page names it by and
    whether an email about it is to come."""

    def __init__(self, exc: SafeHTTPError, state: str | None, *, mail_to_come: bool) -> None:
        super().__init__(exc.status_code, exc.body_snippet, exc.url)
        self.state = state
        self.mail_to_come = mail_to_come
        self.releases_link = isinstance(exc, _HeldNotFetched)


def _held_error(exc: SafeHTTPError, fetched: bool) -> SafeHTTPError:
    """The 409 for a refused add of a title the Arr holds."""
    cls = SafeHTTPError if fetched else _HeldNotFetched
    return cls(409, exc.body_snippet, exc.url)


def _note_if_unanswered(
    payload: DownloadPayload,
    conn: sqlite3.Connection,
    exc: SafeHTTPError,
    *,
    media_type: str,
    service: str,
    tvdb_id: int | None = None,
) -> None:
    """Ask for the email of an add that got no answer, and note the add for a retry.

    The add may land after it was looked for, and then the page no longer offers the button.
    """
    if exc.status_code != 0 and exc.status_code < 500:
        return
    season = payload["season"]
    with conn:
        if payload["email"]:
            record_download_notification(
                conn,
                email=payload["email"],
                title=payload["title"],
                media_type=media_type,
                tmdb_id=payload["tmdb_id"],
                tvdb_id=tvdb_id,
                service=service,
                season_number=season.season_number if season else None,
            )
        if payload["tmdb_id"] is not None and tvdb_id is not None:
            remember_tvdb_id(conn, tmdb_id=payload["tmdb_id"], tvdb_id=tvdb_id)
        # Commits both: a retry finding the note asks for no second email.
        note_unanswered_add(conn, payload["token"], payload["exp"])


def _already_there_error(
    payload: DownloadPayload,
    conn: sqlite3.Connection,
    exc: SafeHTTPError,
    *,
    media_type: str,
    service: str,
    tvdb_id: int | None = None,
) -> SafeHTTPError:
    """*exc*, or what the title it was refused for is doing: one on its way is mailed to the
    person when it is ready, as if their request had gone through. Raises ArrError when that
    cannot be read, so the link can be tried again.
    """
    tmdb_id = payload["tmdb_id"]
    if exc.status_code not in (409, 422) or not tmdb_id:
        return exc
    season = payload["season"]
    season_number = season.season_number if season else None
    drop_arr_cache()
    try:
        state, held = read_download_state(
            conn,
            payload["secret_key"],
            media_type,
            tmdb_id=tmdb_id,
            season_number=season_number,
            tvdb_id=tvdb_id,
        )
    except SafeHTTPError as unread:
        raise ArrError(f"{service} state read failed: HTTP {unread.status_code}") from unread
    mail_to_come = False
    if state == ACTION_QUEUED and held is not None and payload["email"]:
        with conn:
            mail_to_come = record_notification_on_its_way(
                conn,
                held,
                email=payload["email"],
                title=payload["title"],
                media_type=media_type,
                service=service,
                tmdb_id=tmdb_id,
                tvdb_id=tvdb_id,
                season_number=season_number,
            )
    if held is not None and tvdb_id is not None and not held.get("tmdbId"):
        with conn:
            remember_tvdb_id(conn, tmdb_id=tmdb_id, tvdb_id=tvdb_id)
    return _AlreadyThere(exc, state, mail_to_come=mail_to_come)


def _landed_late(payload: DownloadPayload, conn: sqlite3.Connection, exc: SafeHTTPError) -> bool:
    """Whether *exc* refuses as already there what this link's own earlier add put there."""
    if exc.status_code not in (409, 422) or isinstance(exc, _HeldNotFetched):
        return False
    return add_went_unanswered(conn, payload["token"])


def _submit_to_radarr(payload: DownloadPayload, conn: sqlite3.Connection) -> JSONResponse:
    """Add a movie to Radarr and return the poll-token response; raises SafeHTTPError on
    Arr-level failures for the caller to handle. conn is passed explicitly so
    DownloadPayload stays serialisable.
    """
    token = payload["token"]
    title = payload["title"]
    tmdb_id = payload["tmdb_id"]
    email = payload["email"]
    secret_key = payload["secret_key"]

    if not tmdb_id:
        # Never resolve a public download link by title alone — an ambiguous or
        # remade title can enqueue the wrong film.  Require a stable TMDB
        # identifier; the token is released so a corrected link (with an
        # identifier) can be issued.
        _unmark_token_used(token)
        return respond_failure(
            "Missing stable media identifier — this download link cannot "
            "be used.  Ask the admin to re-issue it with a TMDB ID.",
            status=422,
        )

    client = build_radarr_from_db(conn, secret_key)
    if not client:
        _unmark_token_used(token)
        return respond_failure("Radarr not configured", status=503)

    try:
        outcome = add_or_remonitor_movie(client, tmdb_id, title, keep_filed=True)
        if outcome == "already_tracked":
            raise SafeHTTPError(409, "already tracked", "radarr/api/v3/movie")
    except SafeHTTPError as exc:
        error = exc
        # Radarr answers 400, not 409, for a movie it holds: take the already-exists path.
        if exc.status_code == 400 and radarr_holds_movie(client, tmdb_id):
            error = _held_error(exc, radarr_fetches_movie(client, tmdb_id))
        if not movie_add_landed(error, client, tmdb_id):
            if not _landed_late(payload, conn, error):
                _note_if_unanswered(payload, conn, error, media_type="movie", service="radarr")
                error = _already_there_error(
                    payload, conn, error, media_type="movie", service="radarr"
                )
                if error is exc:
                    raise
                raise error from exc
            email = None
        outcome = "added"
    if outcome == "bad_entry":
        raise ExistingMovieError("Radarr holds the movie with no usable id")
    logger.info(
        "Download token: added movie '%s' (tmdb:%s) to Radarr for %s",
        title,
        tmdb_id,
        payload["email"],
    )

    return _record_and_respond(
        conn=conn,
        email=email,
        title=title,
        media_type="movie",
        tmdb_id=tmdb_id,
        service="radarr",
        audit_action=payload["audit_action"],
        audit_detail=payload["audit_detail"],
        actor=payload["actor"],
        secret_key=secret_key,
    )


def _remonitor_held_season(client: ArrClient, tvdb_id: int, season_number: int) -> bool:
    """Re-monitor and search the season of a series Sonarr holds; False when it holds none.

    A season Sonarr has complete is a 409. Only that and an add may answer "already there",
    so an HTTP error here is raised as ArrError.
    """
    try:
        series = client.get_series_by_tvdb(tvdb_id)
        if series is None:
            return False
        series_id = series.get("id")
        if series_id is None:
            raise ArrError("Sonarr holds the series with no usable id")
        season = next(
            (s for s in series.get("seasons") or [] if s.get("seasonNumber") == season_number),
            None,
        )
        if season is not None and season_on_disk(season):
            raise _SeasonOnDisk(409, "already on disk", "sonarr/api/v3/series")
        client.remonitor_season(series_id, season_number)
    except _SeasonOnDisk:
        raise
    except SafeHTTPError as exc:
        raise ArrError(f"Sonarr season re-monitor failed: HTTP {exc.status_code}") from exc
    return True


def _series_held(client: ArrClient, tvdb_id: int) -> bool:
    """Whether Sonarr holds the series; an unreadable answer is raised as ArrError.

    Only an add may answer "already there" or have landed unanswered.
    """
    try:
        return is_series_already_tracked(client, tvdb_id)
    except SafeHTTPError as exc:
        raise ArrError(f"Sonarr series lookup failed: HTTP {exc.status_code}") from exc


def _submit_to_sonarr(payload: DownloadPayload, conn: sqlite3.Connection) -> JSONResponse:
    """Add a series, or the one season asked for, to Sonarr and return the poll-token response;
    raises SafeHTTPError on Arr-level failures for the caller to handle. conn is passed explicitly so
    DownloadPayload stays serialisable.
    """
    token = payload["token"]
    title = payload["title"]
    tmdb_id = payload["tmdb_id"]
    email = payload["email"]
    secret_key = payload["secret_key"]

    if not tmdb_id:
        # Refuse public Sonarr submissions without a stable TMDB identifier —
        # title-only lookup_by_term can enqueue the wrong show.
        _unmark_token_used(token)
        return respond_failure(
            "Missing stable media identifier — this download link cannot "
            "be used.  Ask the admin to re-issue it with a TMDB ID.",
            status=422,
        )

    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        _unmark_token_used(token)
        return respond_failure("Sonarr not configured", status=503)

    season = payload["season"]
    season_number = season.season_number if season else None
    tvdb_id = season.tvdb_id if season else None
    if not tvdb_id:
        results = client.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
        if not results:
            _unmark_token_used(token)
            return respond_failure("Series not found in Sonarr lookup", status=404)
        tvdb_id = results[0].get("tvdbId")
        if not tvdb_id:
            _unmark_token_used(token)
            return respond_failure("No TVDB ID found for this series", status=422)

    try:
        if season_number is None:
            # Asked before the add, so a 5xx or timeout for a show held all along is not
            # taken for an add that landed.
            if _series_held(client, tvdb_id):
                raise _held_error(
                    SafeHTTPError(409, "already tracked", "sonarr/api/v3/series"),
                    sonarr_fetches_series(client, tvdb_id),
                )
            client.add_series(tvdb_id, title)
        elif _remonitor_held_season(client, tvdb_id, season_number):
            # The email was asked for with the add that got no answer, and may have gone out.
            if add_went_unanswered(conn, token):
                email = None
        else:
            client.add_series_with_seasons(tvdb_id, title, [season_number])
    except SafeHTTPError as exc:
        error = exc
        # Sonarr answers 400, not 409, for a series it tracks: same path.
        if exc.status_code == 400 and sonarr_tracks_series(client, tvdb_id):
            error = _held_error(exc, sonarr_fetches_series(client, tvdb_id))
        if not series_add_landed(error, client, tvdb_id):
            if not _landed_late(payload, conn, error):
                _note_if_unanswered(
                    payload, conn, error, media_type="tv", service="sonarr", tvdb_id=tvdb_id
                )
                error = _already_there_error(
                    payload, conn, error, media_type="tv", service="sonarr", tvdb_id=tvdb_id
                )
                if error is exc:
                    raise
                raise error from exc
            email = None
    logger.info(
        "Download token: added series '%s' (tvdb:%s season:%s) to Sonarr for %s",
        title,
        tvdb_id,
        "all" if season_number is None else season_number,
        payload["email"],
    )

    return _record_and_respond(
        conn=conn,
        email=email,
        title=title,
        media_type="tv",
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        service="sonarr",
        audit_action=payload["audit_action"],
        audit_detail=payload["audit_detail"],
        actor=payload["actor"],
        secret_key=secret_key,
        season_number=season_number,
    )


def _validate_token_request(
    request: Request, token: str, secret_key: str
) -> tuple[DownloadTokenPayload | None, JSONResponse | None]:
    """Pre-flight checks (rate limit, token length, HMAC, exp). Returns (payload, None) on
    success or (None, error_response).
    """
    if not _DOWNLOAD_LIMITER_POST.check(get_client_ip(request)):
        limited = respond_failure("Too many requests", status=429)
        limited.headers["Retry-After"] = "60"
        return None, limited
    if len(token) > 4096:
        return None, respond_failure("Token expired or invalid", status=410)
    payload = validate_download_token(token, secret_key)
    if payload is None:
        return None, respond_failure("Token expired or invalid", status=410)
    exp_value = payload.get("exp", 0)
    # ``int(float('inf'))`` raises OverflowError. Guard against a
    # signer-controlled non-finite ``exp`` so a malformed token can never
    # crash the handler — treat it as expired/invalid.
    if not isinstance(exp_value, (int, float)) or not math.isfinite(exp_value):
        return None, respond_failure("Token expired or invalid", status=410)
    return payload, None


def _claim_token(token: str, exp_value: float) -> JSONResponse | None:
    """Mark the token used in the DB (phase 1 reservation). Returns None on success, or an
    error response for a DB failure (503) or replay (409).
    """
    try:
        claimed = _mark_token_used(token, int(exp_value))
    except sqlite3.Error:
        # The token persistence layer already logs CRITICAL with a
        # traceback; here we only need to translate the failure into a
        # retryable response to the client.
        return respond_failure("Service temporarily unavailable, please retry", status=503)
    if not claimed:
        return respond_failure("This download link has already been used", status=409)
    return None


def _build_dl_payload(
    conn: sqlite3.Connection,
    token: str,
    payload: DownloadTokenPayload,
    secret_key: str,
    actor: str | None,
) -> tuple[DownloadPayload, str, bool]:
    """Build a DownloadPayload from the token payload. Returns (dl_payload, media_type,
    is_redownload); the connection is not in it, so the payload stays purely data.
    """
    title = payload.get("title", "")
    media_type = payload.get("mt", "")
    tmdb_id = payload.get("tmdb")
    # ``email`` may be absent, empty, or explicitly None (token minted by an
    # admin with no notification email) — normalise all three to None.
    email = payload.get("email") or None
    action = payload.get("act", "download")
    is_redownload = action == "redownload"
    audit_action = "re_downloaded" if is_redownload else "downloaded"
    by_clause = f" by {email}" if email else ""
    season = deleted_season(conn, payload)
    season_clause = ""
    if season and season.season_number is not None:
        season_clause = f" {season_name(season.season_number).lower()} of '{title}'"
    audit_detail = (
        f"Re-downloaded{season_clause}{by_clause}"
        if is_redownload
        else f"Downloaded '{title}'{by_clause}"
    )
    dl_payload: DownloadPayload = {
        "token": token,
        "exp": int(payload.get("exp", 0)),
        "title": title,
        "tmdb_id": tmdb_id,
        "email": email,
        "audit_action": audit_action,
        "audit_detail": audit_detail,
        "actor": actor,
        "secret_key": secret_key,
        "season": season,
    }
    return dl_payload, media_type, is_redownload


def _handle_already_exists_error(
    exc: SafeHTTPError,
    token: str,
    title: str,
    media_type: str,
    tmdb_id: int | None,
    is_redownload: bool,
    secret_key: str,
    season_number: int | None,
) -> JSONResponse:
    """Handle Arr 409/422 (item already exists) and non-recoverable transport errors.

    For 409/422: release the token reservation for re-download links, mint
    a poll token, and return a 409 naming the title's state. For other HTTP errors: release the
    reservation and return a 502.
    """
    status = exc.status_code
    if status in (409, 422):
        # The Arr service reports the item already exists. For a
        # *re-download* link this is the expected hot path — the
        # user explicitly wants to re-grab a title that's already
        # in the library, so the token must be released so the
        # page can immediately re-issue the request via the usual
        # flow rather than leaving the user stranded with a "link
        # already used" error on the next click.
        #
        # For a fresh download link, the same response means the
        # admin already added it elsewhere; the click was
        # effectively idempotent, so we keep the token consumed
        # (preserves replay protection) and surface a poll_token
        # for the page to display the existing library state.
        if is_redownload:
            _unmark_token_used(token)
        service_name = "radarr" if media_type == "movie" else "sonarr"
        svc_label = "Radarr" if media_type == "movie" else "Sonarr"
        poll_token = None
        if tmdb_id:
            poll_token = generate_poll_token(
                media_item_id=(
                    sonarr_media_id(title, season_number)
                    if service_name == "sonarr"
                    else f"radarr:{title}"
                ),
                service=service_name,
                tmdb_id=tmdb_id,
                secret_key=secret_key,
            )
        extra: dict[str, object] = {"poll_token": poll_token} if poll_token else {}
        if isinstance(exc, _AlreadyThere) and exc.state:
            extra["download_state"] = exc.state
            extra["notify_by_email"] = exc.mail_to_come
        said = f"'{title}' already exists"
        if season_number is not None:
            said = already_there(season_number, title)
        return respond_failure(f"{said} in your {svc_label} library", status=409, **extra)
    # Phase 2 (failure): release the reservation so the user can
    # retry once the upstream recovers.
    _unmark_token_used(token)
    # Demote to DEBUG: every transient Arr blip would otherwise
    # spam the WARNING log with a full traceback. The audit trail
    # captures the user-facing failure separately, so operators
    # who need the stack can dial up the log level.
    logger.debug("Download token submit failed for '%s': %s", title, exc, exc_info=True)
    return respond_failure("Download request failed — check service connectivity", status=502)


@router.post("/download/{token}")
def download_submit(request: Request, token: str) -> JSONResponse:
    """Trigger a download via Radarr or Sonarr.

    CSRF-exempt: the route is HMAC-token-authenticated and clicked from email clients, where
    the browser's Origin is whichever webmail host the recipient uses. The exemption is
    opt-in via _CSRF_EXEMPT_ROUTES in mediaman.web, so a sibling POST /download/... route
    does not inherit it.
    """
    config = request.app.state.config
    conn = get_db()

    payload, pre_err = _validate_token_request(request, token, config.secret_key)
    if pre_err is not None:
        return pre_err

    if payload is None:  # _validate_token_request guarantees one or the other
        raise RuntimeError("payload is None after validate_token_request — invariant violated")
    exp_value = payload.get("exp", 0)
    # Before the claim: a failed read must not leave the link used.
    actor = get_optional_admin_from_token(request.cookies.get("session_token"), request=request)
    # Phase 1 of the 2-phase reservation: claim the token in the DB
    # *before* doing any Arr work.
    claim_err = _claim_token(token, exp_value)
    if claim_err is not None:
        return claim_err

    dl_payload, media_type, is_redownload = _build_dl_payload(
        conn, token, payload, config.secret_key, actor
    )
    title = dl_payload["title"]
    tmdb_id = dl_payload["tmdb_id"]

    try:
        if media_type == "movie":
            result = _submit_to_radarr(dl_payload, conn)
        else:
            result = _submit_to_sonarr(dl_payload, conn)
        # Phase 2 (success): the Arr submission completed — leave the
        # reservation row in place so the token cannot be replayed.
        return result

    except SafeHTTPError as exc:
        return _handle_already_exists_error(
            exc,
            token,
            title,
            media_type,
            tmdb_id,
            is_redownload or (isinstance(exc, _AlreadyThere) and exc.releases_link),
            config.secret_key,
            dl_payload["season"].season_number if dl_payload["season"] else None,
        )
    except (requests.RequestException, ArrError, ValueError, sqlite3.Error) as exc:
        # ``ArrError`` covers the Arr-domain failures (``ArrConfigError`` from
        # missing root folder / quality profile, ``ArrUpstreamError`` from a
        # malformed upstream response). ``ValueError`` covers the
        # ``add_movie(tmdb_id <= 0)`` / ``add_series(tvdb_id <= 0)`` argument
        # guards. Both must be caught here: letting them propagate would
        # leave the consumed token stranded, permanently burning the
        # user's one-shot link with no recovery path. ``KeyboardInterrupt``
        # and ``SystemExit`` derive from ``BaseException`` and are not
        # caught here, which is the intended control-flow behaviour.
        _unmark_token_used(token)
        logger.debug("Download token submit failed for '%s': %s", title, exc, exc_info=True)
        return respond_failure("Download request failed — check service connectivity", status=502)
    finally:
        # A failed request may have added the title too.
        drop_arr_cache()
