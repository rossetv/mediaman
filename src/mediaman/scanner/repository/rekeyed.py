"""SQL that carries keeps and Watch progress to the new key Plex gives a title.

Plex gives a title it removed and found again a new key. The same title is one
at the same path, of the same season; an episode also has the same number.
Nothing here commits: each caller runs it in the transaction that adds or
removes the title.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping
from datetime import timedelta

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso, now_utc
from mediaman.scanner.repository._protection import restore_keeps
from mediaman.scanner.repository.scheduled_actions import (
    book_removed_files_where,
    delete_show_scheduled_deletions,
)
from mediaman.scanner.repository.settings import read_setting

# How long the progress of a title Plex stopped listing waits for it to come back.
_PROGRESS_KEPT_FOR = timedelta(days=90)

_PROGRESS_COLUMNS = (
    "username, rating_key, media_item_id, show_rating_key, position_s, duration_s, "
    "watched, credited_at, updated_at, seen_at"
)

# A file of a title: its path, and the episode's number when it is an episode.
Place = tuple[str, int | None]


def episode_keys_json(item: Mapping[str, object]) -> str | None:
    """Return the season's episodes as stored in ``media_items``, None when not known."""
    episodes = item.get("episodes")
    return json.dumps(episodes) if isinstance(episodes, list) and episodes else None


def _episode_places(stored: str | None) -> dict[str, Place]:
    """Return the place of each episode of a stored ``episode_keys``, by episode key."""
    return {e["key"]: (e["path"], e["number"]) for e in json.loads(stored or "[]")}


def _keys_by_place(stored: str | None) -> dict[Place, str]:
    """Return the key of each episode of a stored ``episode_keys``, by place.

    A place two episodes share, or one with no path or number, names no one episode: it is left out.
    """
    by_place: dict[Place, str] = {}
    shared: set[Place] = set()
    for key, place in _episode_places(stored).items():
        if place in by_place:
            shared.add(place)
        by_place[place] = key
    return {p: k for p, k in by_place.items() if p[0] and p[1] is not None and p not in shared}


# --- Keeps of one film or season ----------------------------------------------

# Settings key holding the last of pruned_keeps looked at for a key that should have had it.
_CARRIED_UP_TO_KEY = "pruned_keeps_carried_up_to"

_LIVE_ITEM_KEEP = (
    "(sa.action = 'protected_forever' OR (sa.action = 'snoozed' AND sa.execute_at >= ?))"
)

# Another key ``o`` of the title ``mi``, unless their ids say they are two titles in one place.
_ANOTHER_KEY = (
    "o.file_path = mi.file_path AND mi.file_path != '' AND o.season_number IS mi.season_number"
    " AND o.id != mi.id"
    " AND CASE WHEN o.tmdb_id IS NOT NULL AND mi.tmdb_id IS NOT NULL THEN o.tmdb_id = mi.tmdb_id"
    " WHEN o.tvdb_id IS NOT NULL AND mi.tvdb_id IS NOT NULL THEN o.tvdb_id = mi.tvdb_id"
    " ELSE 1 END"
)


def is_kept_under_another_key(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if a kept film or season of the library has the files of *media_id*."""
    # rationale: the interpolated conditions are fixed strings; every value is bound
    row = conn.execute(  # nosec B608
        f"SELECT 1 FROM media_items mi JOIN media_items o ON {_ANOTHER_KEY} "
        "JOIN scheduled_actions sa ON sa.media_item_id = o.id "
        f"WHERE mi.id = ? AND {_LIVE_ITEM_KEEP} LIMIT 1",
        (media_id, now_iso()),
    ).fetchone()
    return row is not None


def _delete_unclaimed(conn: sqlite3.Connection, where: str, params: tuple[object, ...]) -> None:
    """Delete the rows matching the fixed condition *where*, but for one being carried out."""
    where = f"{where} AND delete_status IS NOT 'deleting'"
    book_removed_files_where(conn, where, params)
    # rationale: the interpolated condition is a fixed string; every value is bound
    conn.execute(f"DELETE FROM scheduled_actions WHERE {where}", params)  # nosec B608


def _give_keep(conn: sqlite3.Connection, keep: sqlite3.Row, media_id: str) -> None:
    """Keep *media_id* for as long as *keep* of pruned_keeps says, unless it already is for longer.

    Either way its scheduled deletion is cancelled, but for one being carried out.
    A keep someone removed from it since *keep* was made is not given again.
    """
    # Undoing a Keep is audited as 'unprotected' too, but only puts back the deletion it replaced.
    removed_since = conn.execute(
        "SELECT 1 FROM audit_log WHERE media_item_id = ? AND action = 'unprotected' "
        "AND detail LIKE 'Protection removed %' AND created_at > ? LIMIT 1",
        (media_id, keep["snoozed_at"] or keep["scheduled_at"]),
    ).fetchone()
    if removed_since:
        return
    now = now_iso()
    forever = keep["action"] == "protected_forever"
    # rationale: the interpolated condition is a fixed string; every value is bound
    live = conn.execute(  # nosec B608
        f"SELECT * FROM scheduled_actions sa WHERE sa.media_item_id = ? AND {_LIVE_ITEM_KEEP}",
        (media_id, now),
    ).fetchall()
    if not any(
        own["action"] == "protected_forever"
        or (not forever and own["execute_at"] >= keep["execute_at"])
        for own in live
    ):
        _delete_unclaimed(
            conn,
            "media_item_id = ? AND action = 'snoozed' AND execute_at >= ?",
            (media_id, now),
        )
        restore_keeps(conn, media_id, [keep])
    _delete_unclaimed(
        conn,
        "media_item_id = ? AND action = 'scheduled_deletion' AND token_used = 0",
        (media_id,),
    )


def carry_pruned_keeps(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Give the keeps save_pruned_keeps set aside to the other keys of their titles.

    Such a key is in the library already, so it would never take them as a new
    one does. A title's own key still takes its keep back; by path no other
    does, or the next new key would take one removed from this one meanwhile.
    """
    for keep in conn.execute(
        "SELECT pk.* FROM pruned_keeps pk JOIN media_items mi ON mi.id = pk.media_item_id"
    ).fetchall():
        if keep["media_item_id"] not in media_ids:
            continue
        # rationale: the interpolated condition is a fixed string; every value is bound
        others = conn.execute(  # nosec B608
            f"SELECT o.id FROM media_items mi JOIN media_items o ON {_ANOTHER_KEY} WHERE mi.id = ?",
            (keep["media_item_id"],),
        ).fetchall()
        staying = [row["id"] for row in others if row["id"] not in media_ids]
        for other in staying:
            _give_keep(conn, keep, other)
        if staying:
            conn.execute("UPDATE pruned_keeps SET file_path = '' WHERE id = ?", (keep["id"],))
    _set_carried_up_to(conn)


def carry_stranded_keeps(conn: sqlite3.Connection) -> None:
    """Give the keeps an older mediaman set aside to the keys left at their path and season.

    It left them where no key in the library could take them. Nothing tells
    which ids their titles had, so each also stays to be taken by a new key.
    """
    up_to = _carried_up_to(conn)
    keeps = conn.execute(
        "SELECT * FROM pruned_keeps WHERE id > ? AND file_path != '' "
        "AND (action = 'protected_forever' OR execute_at >= ?)",
        (up_to, now_iso()),
    ).fetchall()
    for keep in keeps:
        for row in conn.execute(
            "SELECT id FROM media_items WHERE file_path = ? AND season_number IS ?",
            (keep["file_path"], keep["season_number"]),
        ).fetchall():
            _give_keep(conn, keep, row["id"])
    if keeps:
        _set_carried_up_to(conn)


def _carried_up_to(conn: sqlite3.Connection) -> int:
    raw = read_setting(conn, _CARRIED_UP_TO_KEY)
    return int(raw) if raw and raw.isdecimal() else 0


def _set_carried_up_to(conn: sqlite3.Connection) -> None:
    last = conn.execute("SELECT MAX(id) AS id FROM pruned_keeps").fetchone()["id"]
    if last is None or last <= _carried_up_to(conn):
        return
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (_CARRIED_UP_TO_KEY, str(last), now_iso()),
    )


# --- Whole-show keeps ---------------------------------------------------------

_LIVE_KEEP = "(ks.action = 'protected_forever' OR ks.execute_at > ?)"


def _give_show_keep(conn: sqlite3.Connection, keep: sqlite3.Row, show_key: str) -> None:
    """Keep *show_key* for as long as *keep* says, unless it is already kept for longer.

    Either way its scheduled deletions are cancelled, but for one being carried out.
    A keep the new key was removed from after the old one got it is not given again.
    """
    removed_since = conn.execute(
        "SELECT 1 FROM audit_log WHERE media_item_id = ? AND action = 'removed_show_keep' "
        "AND created_at > MAX(?, COALESCE((SELECT MAX(created_at) FROM audit_log "
        "  WHERE media_item_id = ? AND action = 'kept_show'), '')) LIMIT 1",
        (show_key, keep["created_at"], keep["show_rating_key"]),
    ).fetchone()
    if removed_since:
        return
    given = conn.execute(
        "INSERT INTO kept_shows "
        "(show_rating_key, show_title, action, execute_at, snooze_duration, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(show_rating_key) DO UPDATE SET action = excluded.action, "
        "  execute_at = excluded.execute_at, snooze_duration = excluded.snooze_duration "
        "WHERE kept_shows.action != 'protected_forever' "
        "  AND (excluded.action = 'protected_forever' "
        "    OR excluded.execute_at > COALESCE(kept_shows.execute_at, ''))",
        (
            show_key,
            keep["show_title"],
            keep["action"],
            keep["execute_at"],
            keep["snooze_duration"],
            keep["created_at"],
        ),
    )
    if given.rowcount:
        log_audit(
            conn,
            show_key,
            "kept_show",
            f"Show '{keep['show_title']}' kept: Plex lists it under a new key",
        )
    # The scan skips a season still scheduled, so one left here would go under the keep.
    delete_show_scheduled_deletions(conn, show_key)


def carry_show_keeps(
    conn: sqlite3.Connection, seasons: Iterable[Mapping[str, object]], listed: Collection[str]
) -> None:
    """Keep each show that *seasons*, as Plex lists them now, show to be a kept one under a new key.

    A season not in the library yet belongs to the show of the one it replaces:
    of the same path and season, and not in *listed*, the keys Plex lists now. A
    season that is, belongs to the show it was stored under. The keep stays on
    the old key too. Run it before the seasons are stored or evaluated.
    """
    now = now_iso()
    # rationale: the interpolated condition is a fixed string; every value is bound
    kept = conn.execute(  # nosec B608
        "SELECT ks.*, mi.id AS season_id, mi.file_path, mi.season_number "
        "FROM kept_shows ks JOIN media_items mi ON mi.show_rating_key = ks.show_rating_key "
        f"WHERE {_LIVE_KEEP} "
        "UNION ALL "
        "SELECT ks.*, ps.media_item_id, ps.file_path, ps.season_number "
        "FROM kept_shows ks JOIN pruned_show_seasons ps USING (show_rating_key) "
        f"WHERE {_LIVE_KEEP}",
        (now, now),
    ).fetchall()
    if not kept:
        return
    by_id: dict[str, list[sqlite3.Row]] = {}
    by_place: dict[tuple[object, object], list[sqlite3.Row]] = {}
    for keep in kept:
        by_id.setdefault(keep["season_id"], []).append(keep)
        if keep["file_path"] and keep["season_id"] not in listed:
            by_place.setdefault((keep["file_path"], keep["season_number"]), []).append(keep)
    for season in seasons:
        season_id, show_key = season["plex_rating_key"], season.get("show_rating_key")
        if not isinstance(season_id, str) or not isinstance(show_key, str) or not show_key:
            continue
        old = by_id.get(season_id, [])
        if not conn.execute("SELECT 1 FROM media_items WHERE id = ?", (season_id,)).fetchone():
            old = old + by_place.get((season.get("file_path"), season.get("season_number")), [])
        for keep in old:
            if keep["show_rating_key"] != show_key:
                _give_show_keep(conn, keep, show_key)


def save_pruned_show_seasons(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Carry the keeps of the kept shows whose seasons are about to be removed.

    A season still in the library under another key gives its show the keep;
    without one, where the season was is set aside for carry_show_keeps.
    """
    now = now_iso()
    # rationale: the interpolated condition is a fixed string; every value is bound
    conn.execute(  # nosec B608
        "DELETE FROM pruned_show_seasons WHERE show_rating_key NOT IN "
        f"(SELECT ks.show_rating_key FROM kept_shows ks WHERE {_LIVE_KEEP})",
        (now,),
    )
    seasons = conn.execute(  # nosec B608
        "SELECT ks.*, mi.id AS season_id, mi.file_path, mi.season_number "
        "FROM kept_shows ks JOIN media_items mi ON mi.show_rating_key = ks.show_rating_key "
        f"WHERE {_LIVE_KEEP}",
        (now,),
    ).fetchall()
    for season in seasons:
        if season["season_id"] not in media_ids or not season["file_path"]:
            continue
        shows = {other["show_rating_key"] for other in _others_at(conn, season, media_ids)}
        for show_key in shows - {None, "", season["show_rating_key"]}:
            _give_show_keep(conn, season, show_key)
        if not shows - {None, ""}:
            conn.execute(
                "INSERT INTO pruned_show_seasons "
                "(show_rating_key, media_item_id, file_path, season_number) VALUES (?, ?, ?, ?)",
                (
                    season["show_rating_key"],
                    season["season_id"],
                    season["file_path"],
                    season["season_number"],
                ),
            )


def forget_pruned_show_season(
    conn: sqlite3.Connection, file_path: str, season_number: object
) -> None:
    """Forget where a kept show's season was, now that it is in the library again."""
    conn.execute(
        "DELETE FROM pruned_show_seasons WHERE file_path = ? AND season_number IS ?",
        (file_path, season_number),
    )


def _others_at(
    conn: sqlite3.Connection, title: sqlite3.Row, removed: Collection[str]
) -> list[sqlite3.Row]:
    """Return the library's titles at the path and season of *title* that are not in *removed*."""
    rows = conn.execute(
        "SELECT id, show_rating_key, episode_keys FROM media_items "
        "WHERE file_path = ? AND file_path != '' AND season_number IS ?",
        (title["file_path"], title["season_number"]),
    )
    return [row for row in rows if row["id"] not in removed]


# --- Watch progress -----------------------------------------------------------


def _move_progress(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    rating_key: str,
    media_item_id: str,
    show_rating_key: str | None,
) -> None:
    """Store *row* of a person's progress, and what they dismissed, under the new keys.

    Where the person has progress there already, the later place stays, and
    neither row's mark nor its play counted for cleanup is lost.
    """
    later = "excluded.updated_at > watch_progress.updated_at"
    # rationale: the interpolated columns and condition are fixed strings; every value is bound
    conn.execute(  # nosec B608
        f"INSERT INTO watch_progress ({_PROGRESS_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(username, rating_key) DO UPDATE SET "
        "media_item_id = excluded.media_item_id, "
        "show_rating_key = excluded.show_rating_key, "
        f"position_s = CASE WHEN {later} THEN excluded.position_s ELSE position_s END, "
        f"duration_s = CASE WHEN {later} THEN excluded.duration_s ELSE duration_s END, "
        f"watched = CASE WHEN {later} THEN excluded.watched ELSE watched END, "
        "credited_at = NULLIF("
        "  MAX(COALESCE(credited_at, ''), COALESCE(excluded.credited_at, '')), ''), "
        "updated_at = MAX(updated_at, excluded.updated_at), "
        "seen_at = NULLIF(MAX(COALESCE(seen_at, ''), COALESCE(excluded.seen_at, '')), '')",
        (
            row["username"],
            rating_key,
            media_item_id,
            show_rating_key,
            row["position_s"],
            row["duration_s"],
            row["watched"],
            row["credited_at"],
            row["updated_at"],
            row["seen_at"],
        ),
    )
    old_title = row["show_rating_key"] or row["rating_key"]
    new_title = show_rating_key or rating_key
    if old_title == new_title:
        return
    conn.execute(
        "INSERT INTO watch_dismissed (username, title_key, dismissed_at) "
        "SELECT username, ?, dismissed_at FROM watch_dismissed "
        "WHERE username = ? AND title_key = ? "
        "ON CONFLICT(username, title_key) DO UPDATE SET "
        "dismissed_at = MAX(dismissed_at, excluded.dismissed_at)",
        (new_title, row["username"], old_title),
    )
    conn.execute(
        "DELETE FROM watch_dismissed WHERE username = ? AND title_key = ?",
        (row["username"], old_title),
    )


def save_pruned_progress(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Set aside the progress of titles about to be removed, for restore_progress.

    It waits for a while only. That of a title mediaman has deleted since, with
    nothing else at its path, is not set aside.
    """
    now = now_utc()
    conn.execute(
        "DELETE FROM pruned_progress WHERE pruned_at < ?",
        ((now - _PROGRESS_KEPT_FOR).isoformat(),),
    )
    for media_id in media_ids:
        rows = conn.execute(
            "SELECT wp.*, EXISTS (SELECT 1 FROM audit_log a "
            "  WHERE a.media_item_id = wp.media_item_id AND a.action = 'deleted' "
            "    AND a.created_at >= MAX(wp.updated_at, COALESCE(wp.seen_at, ''))) AS deleted "
            "FROM watch_progress wp WHERE wp.media_item_id = ?",
            (media_id,),
        ).fetchall()
        if not rows:
            continue
        title = conn.execute(
            "SELECT file_path, season_number, media_type, episode_keys "
            "FROM media_items WHERE id = ?",
            (media_id,),
        ).fetchone()
        places = {key: place for place, key in _keys_by_place(title["episode_keys"]).items()}
        replaced = bool(_others_at(conn, title, media_ids))
        for row in rows:
            if row["deleted"] and not replaced:
                continue
            # A film has no episode path; an episode nothing is known of has an empty one.
            place: tuple[str | None, int | None] = (None, None)
            if title["media_type"] != "movie":
                place = places.get(row["rating_key"], ("", None))
            # rationale: the interpolated column list is a fixed string; every value is bound
            conn.execute(  # nosec B608
                f"INSERT OR REPLACE INTO pruned_progress ({_PROGRESS_COLUMNS}, "
                "file_path, season_number, episode_path, episode_number, pruned_at) "
                f"VALUES ({', '.join('?' * 15)})",
                (
                    *(row[column] for column in _PROGRESS_COLUMNS.split(", ")),
                    title["file_path"],
                    title["season_number"],
                    *place,
                    now.isoformat(),
                ),
            )


def restore_progress(conn: sqlite3.Connection) -> None:
    """Give back the progress set aside for titles now in the library again.

    A title's own comes back by its key. That of a key no longer in the library
    goes to the title at its path, and the episode of its key or its path and
    number, if there is just one. Run it once a run has stored all Plex lists, so every copy is known.
    """
    for row in conn.execute("SELECT * FROM pruned_progress").fetchall():
        own = conn.execute(
            "SELECT id, show_rating_key, episode_keys FROM media_items WHERE id = ?",
            (row["media_item_id"],),
        ).fetchone()
        titles = [own] if own else _others_at(conn, row, ())
        found = [(key, title) for title in titles if (key := _restored_key(row, title))]
        if len(found) != 1:
            continue
        [(key, title)] = found
        _move_progress(conn, row, key, title["id"], title["show_rating_key"])
        conn.execute(
            "DELETE FROM pruned_progress WHERE username = ? AND rating_key = ?",
            (row["username"], row["rating_key"]),
        )


def _restored_key(row: sqlite3.Row, title: sqlite3.Row) -> str | None:
    """Return the key *row*'s progress has in *title*, None if it is not known."""
    if row["episode_path"] is None:
        return str(title["id"])
    if row["rating_key"] in _episode_places(title["episode_keys"]):
        return str(row["rating_key"])
    return _keys_by_place(title["episode_keys"]).get((row["episode_path"], row["episode_number"]))


def refresh_progress_keys(
    conn: sqlite3.Connection, media_id: str, item: Mapping[str, object]
) -> None:
    """Move the progress in a season to the new keys *item* has for its episodes or show.

    Run it before *item* is stored over what the library has.
    """
    stored = conn.execute(
        "SELECT show_rating_key, episode_keys FROM media_items WHERE id = ?", (media_id,)
    ).fetchone()
    show_key, episode_keys = item.get("show_rating_key"), episode_keys_json(item)
    if stored is None or not isinstance(show_key, str):
        return
    if (stored["show_rating_key"], stored["episode_keys"]) == (show_key, episode_keys):
        return
    places = {key: place for place, key in _keys_by_place(stored["episode_keys"]).items()}
    keys = _keys_by_place(episode_keys)
    moves = []
    for row in conn.execute("SELECT * FROM watch_progress WHERE media_item_id = ?", (media_id,)):
        key = row["rating_key"]
        if key in places:
            key = keys.get(places[key], key)
        if (key, show_key) != (row["rating_key"], row["show_rating_key"]):
            moves.append((row, key))
    # All go before any is stored: one may take the key another leaves.
    conn.executemany(
        "DELETE FROM watch_progress WHERE username = ? AND rating_key = ?",
        [(row["username"], row["rating_key"]) for row, _ in moves],
    )
    for row, key in moves:
        _move_progress(conn, row, key, media_id, show_key)
