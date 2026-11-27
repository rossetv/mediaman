"""Each person's audio and subtitle languages, and what they chose for a show or movie."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from mediaman.core.time import now_iso
from mediaman.services.cast.tracks import Remembered


@dataclass(frozen=True)
class Defaults:
    #: An ISO 639 code, or None for the track Plex has selected.
    audio_lang: str | None = None
    #: An ISO 639 code, "off", or None: neither shows subtitles but forced ones.
    subtitle_lang: str | None = None


def get_defaults(conn: sqlite3.Connection, username: str) -> Defaults:
    row = conn.execute(
        "SELECT audio_lang, subtitle_lang FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    return Defaults() if row is None else Defaults(row["audio_lang"], row["subtitle_lang"])


def set_defaults(conn: sqlite3.Connection, username: str, defaults: Defaults) -> None:
    with conn:
        conn.execute(
            "UPDATE admin_users SET audio_lang = ?, subtitle_lang = ? WHERE username = ?",
            (defaults.audio_lang, defaults.subtitle_lang, username),
        )


def get_choice(conn: sqlite3.Connection, username: str, title_key: str) -> Remembered | None:
    row = conn.execute(
        "SELECT c.* FROM user_track_choices c JOIN admin_users u ON u.id = c.user_id"
        " WHERE u.username = ? AND c.title_key = ?",
        (username, title_key),
    ).fetchone()
    if row is None:
        return None
    return Remembered(
        rating_key=row["rating_key"],
        audio_id=row["audio_stream_id"],
        audio_lang=row["audio_lang"],
        subtitle=row["subtitle"],
        subtitle_id=row["subtitle_stream_id"],
        subtitle_lang=row["subtitle_lang"],
        subtitle_forced=bool(row["subtitle_forced"]),
        subtitle_hearing_impaired=bool(row["subtitle_hearing_impaired"]),
    )


def save_choice(
    conn: sqlite3.Connection, username: str, title_key: str, choice: Remembered
) -> None:
    """Keep *choice* for the title; one with nothing chosen goes back to the defaults."""
    if choice.audio_id is None and choice.audio_lang is None and choice.subtitle is None:
        clear_choice(conn, username, title_key)
        return
    with conn:
        conn.execute(
            "INSERT INTO user_track_choices (user_id, title_key, rating_key, audio_stream_id,"
            " audio_lang, subtitle, subtitle_stream_id, subtitle_lang, subtitle_forced,"
            " subtitle_hearing_impaired, updated_at)"
            " SELECT id, :title, :key, :audio_id, :audio_lang, :subtitle, :subtitle_id,"
            " :subtitle_lang, :forced, :hi, :at FROM admin_users WHERE username = :user"
            " ON CONFLICT (user_id, title_key) DO UPDATE SET rating_key = excluded.rating_key,"
            " audio_stream_id = excluded.audio_stream_id, audio_lang = excluded.audio_lang,"
            " subtitle = excluded.subtitle, subtitle_stream_id = excluded.subtitle_stream_id,"
            " subtitle_lang = excluded.subtitle_lang, subtitle_forced = excluded.subtitle_forced,"
            " subtitle_hearing_impaired = excluded.subtitle_hearing_impaired,"
            " updated_at = excluded.updated_at",
            {
                "user": username,
                "title": title_key,
                "key": choice.rating_key,
                "audio_id": choice.audio_id,
                "audio_lang": choice.audio_lang,
                "subtitle": choice.subtitle,
                "subtitle_id": choice.subtitle_id,
                "subtitle_lang": choice.subtitle_lang,
                "forced": int(choice.subtitle_forced),
                "hi": int(choice.subtitle_hearing_impaired),
                "at": now_iso(),
            },
        )


def clear_choice(conn: sqlite3.Connection, username: str, title_key: str) -> None:
    with conn:
        conn.execute(
            "DELETE FROM user_track_choices WHERE title_key = ?"
            " AND user_id = (SELECT id FROM admin_users WHERE username = ?)",
            (title_key, username),
        )
