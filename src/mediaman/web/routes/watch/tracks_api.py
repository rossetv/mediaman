"""Audio and subtitles: a title's tracks with those that play for the signed-in person, their
choice for the title, the languages they want by default, and whether their TV skips intros
and credits.

Each acts on the signed-in person alone. A choice holds for the whole show, and for the item it
was made on, its exact tracks; it plays from the next start of a stream.
"""

from __future__ import annotations

import sqlite3
from typing import Literal, Self

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.cast.tracks import (
    LANGUAGES,
    NATIVE_NAMES,
    Remembered,
    Track,
    normalise_language,
)
from mediaman.services.media_meta.plex_watch import PlexError, PlexItem, is_in_libraries
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import track_choices
from mediaman.web.repository.track_choices import Defaults
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.stream_api import (
    _open_plex,
    _scanned_library_ids,
    pick_tracks,
    plex_failure,
    title_key,
    title_tracks,
)

router = APIRouter()

# Per person, over these routes together: each but the defaults' asks Plex for the title.
_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)

_KEY = r"^[0-9]{1,18}$"
_LANGUAGE = r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{1,8})?$"


class _KeptSubtitle(BaseModel):
    """The subtitle part of a kept choice, as an answer gave it, to be kept again as it was."""

    model_config = ConfigDict(extra="forbid")

    rating_key: str = Field(pattern=_KEY)
    subtitle: Literal["on", "off"] | None
    subtitle_id: str | None = Field(pattern=_KEY)
    subtitle_lang: str | None = Field(pattern=_LANGUAGE)
    subtitle_forced: StrictBool
    subtitle_hearing_impaired: StrictBool

    @model_validator(mode="after")
    def _as_kept(self) -> Self:
        described = (self.subtitle_id, self.subtitle_lang, self.subtitle_forced)
        if self.subtitle != "on" and (any(described) or self.subtitle_hearing_impaired):
            raise ValueError("only subtitles kept on describe a track")
        return self


class _Choice(BaseModel):
    """A field left out keeps what was chosen; null goes back to the defaults for it.

    ``subtitle_choice`` gives back the subtitle part of an earlier answer's
    ``remembered_subtitle_choice``, in place of ``subtitle``.
    """

    model_config = ConfigDict(extra="forbid")

    audio: str | None = Field(default=None, pattern=_KEY)
    subtitle: str | None = Field(default=None, pattern=r"^(off|[0-9]{1,18})$")
    subtitle_choice: _KeptSubtitle | None = None

    @model_validator(mode="after")
    def _one_subtitle(self) -> Self:
        if "subtitle_choice" in self.model_fields_set and (
            self.subtitle_choice is None or "subtitle" in self.model_fields_set
        ):
            raise ValueError("subtitle_choice is a kept choice, given in place of subtitle")
        return self


class _Defaults(BaseModel):
    """A field left out keeps what is set; null clears it."""

    model_config = ConfigDict(extra="forbid")

    audio_lang: str | None = Field(default=None, pattern=_LANGUAGE)
    subtitle_lang: str | None = Field(default=None, pattern=rf"(^off$)|{_LANGUAGE}")
    #: Whether a paired TV skips intros and credits by itself; null is refused.
    auto_skip: StrictBool = True


def _title(
    request: Request, conn: sqlite3.Connection, rating_key: str
) -> tuple[PlexItem, list[Track], list[Track]] | Response:
    try:
        plex = _open_plex(request, conn)
        item = plex.get_item(rating_key)
        if item.type not in ("movie", "episode") or not is_in_libraries(
            item, _scanned_library_ids(conn)
        ):
            return respond_err("not_found", status=404)
        _, audio, subtitles = title_tracks(conn, plex, rating_key)
    except PlexError as exc:
        return plex_failure(exc)
    return item, audio, subtitles


def _answer(
    conn: sqlite3.Connection,
    username: str,
    item: PlexItem,
    audio: list[Track],
    subtitles: list[Track],
) -> Response:
    pick = pick_tracks(conn, username, item, audio, subtitles)
    defaults = track_choices.get_defaults(conn, username)
    kept = track_choices.get_choice(conn, username, title_key(item))
    return respond_ok(
        {
            "audio": pick.audio,
            "subtitle": pick.subtitle,
            "audio_tracks": audio,
            "subtitle_tracks": subtitles,
            "remembered": kept is not None,
            # What is kept for the subtitles, to be given back as it is where a change of them
            # does not take: what plays tells neither Off from none needed nor the kept track.
            "remembered_subtitle_choice": None
            if kept is None
            else {
                "rating_key": kept.rating_key,
                "subtitle": kept.subtitle,
                "subtitle_id": kept.subtitle_id,
                "subtitle_lang": kept.subtitle_lang,
                "subtitle_forced": kept.subtitle_forced,
                "subtitle_hearing_impaired": kept.subtitle_hearing_impaired,
            },
            "defaults": {
                "audio_lang": defaults.audio_lang,
                "subtitle_lang": defaults.subtitle_lang,
            },
        }
    )


@router.get("/api/watch/tracks/{rating_key}")
def get_tracks(
    request: Request, rating_key: str, username: str = Depends(get_current_user)
) -> Response:
    if not _LIMITER.check(username):
        return _rate_limited()
    if not _valid_key(rating_key):
        return respond_err("not_found", status=404)
    conn = get_db()
    title = _title(request, conn, rating_key)
    if isinstance(title, Response):
        return title
    return _answer(conn, username, *title)


@router.put("/api/watch/tracks/{rating_key}")
def set_tracks(
    request: Request,
    rating_key: str,
    body: _Choice,
    username: str = Depends(get_current_user),
) -> Response:
    if not _LIMITER.check(username):
        return _rate_limited()
    if not _valid_key(rating_key):
        return respond_err("not_found", status=404)
    conn = get_db()
    title = _title(request, conn, rating_key)
    if isinstance(title, Response):
        return title
    item, audio, subtitles = title
    audio_track = next((t for t in audio if t["id"] == body.audio), None)
    subtitle_track = next((t for t in subtitles if t["id"] == body.subtitle), None)
    if (body.audio is not None and audio_track is None) or (
        body.subtitle not in (None, "off") and subtitle_track is None
    ):
        return respond_err("track_unavailable", status=409)
    held = track_choices.get_choice(conn, username, title_key(item))
    kept = held if held is not None else Remembered(item.rating_key)
    same = kept.rating_key == item.rating_key
    sent = body.model_fields_set
    if "audio" in sent:
        audio_id = body.audio
        audio_lang = audio_track["language"] if audio_track is not None else None
    else:
        # Stream ids are of the item they were chosen on.
        audio_id, audio_lang = kept.audio_id if same else None, kept.audio_lang
    given = body.subtitle_choice
    subtitle: str | None
    if given is not None:
        subtitle = given.subtitle
        subtitle_lang = normalise_language(given.subtitle_lang)
        if given.subtitle_lang is not None and subtitle_lang is None:
            return respond_err("unknown_language", status=422)
        # Stream ids are of the item they were chosen on.
        ids = {t["id"] for t in subtitles} if given.rating_key == item.rating_key else set()
        subtitle_id = given.subtitle_id if given.subtitle_id in ids else None
        forced, hearing_impaired = given.subtitle_forced, given.subtitle_hearing_impaired
    elif "subtitle" not in sent:
        subtitle = kept.subtitle
        subtitle_id = kept.subtitle_id if same else None
        subtitle_lang = kept.subtitle_lang
        forced, hearing_impaired = kept.subtitle_forced, kept.subtitle_hearing_impaired
    elif subtitle_track is not None:
        subtitle, subtitle_id = "on", subtitle_track["id"]
        subtitle_lang = subtitle_track["language"]
        forced, hearing_impaired = subtitle_track["forced"], subtitle_track["hearing_impaired"]
    else:
        subtitle, subtitle_id, subtitle_lang = body.subtitle, None, None
        forced = hearing_impaired = False
    track_choices.save_choice(
        conn,
        username,
        title_key(item),
        Remembered(
            rating_key=item.rating_key,
            audio_id=audio_id,
            audio_lang=audio_lang,
            subtitle=subtitle,
            subtitle_id=subtitle_id,
            subtitle_lang=subtitle_lang,
            subtitle_forced=forced,
            subtitle_hearing_impaired=hearing_impaired,
        ),
    )
    return _answer(conn, username, item, audio, subtitles)


@router.delete("/api/watch/tracks/{rating_key}")
def clear_tracks(
    request: Request, rating_key: str, username: str = Depends(get_current_user)
) -> Response:
    """Go back to the person's defaults for the whole title."""
    if not _LIMITER.check(username):
        return _rate_limited()
    if not _valid_key(rating_key):
        return respond_err("not_found", status=404)
    conn = get_db()
    title = _title(request, conn, rating_key)
    if isinstance(title, Response):
        return title
    item, audio, subtitles = title
    track_choices.clear_choice(conn, username, title_key(item))
    return _answer(conn, username, item, audio, subtitles)


def _rate_limited() -> Response:
    return respond_err("too_many_requests", status=429)


def _valid_key(rating_key: str) -> bool:
    return rating_key.isascii() and rating_key.isdigit() and len(rating_key) <= 18


def _defaults_answer(conn: sqlite3.Connection, username: str, defaults: Defaults) -> Response:
    return respond_ok(
        {
            "audio_lang": defaults.audio_lang,
            "subtitle_lang": defaults.subtitle_lang,
            "auto_skip": track_choices.skips_on_tv(conn, username),
            "languages": [
                {"code": code, "name": name, "native": NATIVE_NAMES[code]}
                for code, name in LANGUAGES.items()
            ],
            "library_languages": track_choices.library_languages(conn, username),
        }
    )


@router.get("/api/account/playback")
def get_playback(username: str = Depends(get_current_user)) -> Response:
    conn = get_db()
    return _defaults_answer(conn, username, track_choices.get_defaults(conn, username))


@router.put("/api/account/playback")
def set_playback(body: _Defaults, username: str = Depends(get_current_user)) -> Response:
    if not _LIMITER.check(username):
        return _rate_limited()
    conn = get_db()
    held = track_choices.get_defaults(conn, username)
    sent = body.model_fields_set
    audio_lang, subtitle_lang = held.audio_lang, held.subtitle_lang
    if "audio_lang" in sent:
        audio_lang = normalise_language(body.audio_lang)
        if body.audio_lang is not None and audio_lang is None:
            return respond_err("unknown_language", status=422)
    if "subtitle_lang" in sent:
        subtitle_lang = body.subtitle_lang
        if subtitle_lang not in (None, "off"):
            subtitle_lang = normalise_language(subtitle_lang)
            if subtitle_lang is None:
                return respond_err("unknown_language", status=422)
    defaults = Defaults(audio_lang, subtitle_lang)
    if {"audio_lang", "subtitle_lang"} & sent:
        track_choices.set_defaults(conn, username, defaults)
    if "auto_skip" in sent:
        track_choices.set_skips_on_tv(conn, username, body.auto_skip)
    return _defaults_answer(conn, username, defaults)
