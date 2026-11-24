"""SQL that carries keeps and Watch progress to the new key Plex gives a title.

Plex gives a title it removed and found again a new key. The same title is one
at the same path, of the same season; an episode also has the same number. For
keeps, films of one library with the same TMDB id are the same title too.
Nothing here commits: each caller runs it in the transaction that adds or
removes the title.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping
from datetime import timedelta
from typing import Any

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso, now_utc
from mediaman.scanner.repository._protection import (
    is_protected,
    is_show_kept_pure,
    restore_keeps,
)
from mediaman.scanner.repository.scheduled_actions import (
    book_removed_files_where,
    delete_scheduled_action,
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


# --- Titles -------------------------------------------------------------------

_TITLE = (
    "id, file_path, season_number, show_title, show_rating_key, tmdb_id, tvdb_id, "
    "tracked_files, episode_keys, media_type, plex_library_id"
)


def fetch_title(conn: sqlite3.Connection, media_id: str) -> sqlite3.Row | None:
    # rationale: the interpolated column list is a fixed string; every value is bound
    row: sqlite3.Row | None = conn.execute(  # nosec B608
        f"SELECT {_TITLE} FROM media_items WHERE id = ?", (media_id,)
    ).fetchone()
    return row


def _entries(stored: str | None) -> list[object]:
    """Return the entries of a stored JSON list, none when it is missing or unreadable."""
    try:
        entries = json.loads(stored or "[]")
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


def title_files(title: sqlite3.Row | Mapping[str, Any]) -> set[str]:
    """Return the paths of the files a title is known to have."""
    entries = _entries(title["tracked_files"]) + _entries(title["episode_keys"])
    return {str(e["path"]) for e in entries if isinstance(e, dict) and e.get("path")}


def ids_conflict(
    old: sqlite3.Row | Mapping[str, Any], new: sqlite3.Row | Mapping[str, Any]
) -> bool | None:
    """Return True if the ids of two titles differ, None if they have none to compare."""
    for column in ("tmdb_id", "tvdb_id"):
        if old[column] is not None and new[column] is not None:
            return bool(old[column] != new[column])
    return None


def _is_another_title(old: sqlite3.Row, new: sqlite3.Row | Mapping[str, Any]) -> bool:
    """Return True if *new*, at the path and season of *old*, is surely another title.

    A folder may hold the seasons of two shows. With a file in common they are
    one, whatever their ids say, as a match corrected in Plex changes those;
    where the files of one are not known, nothing is sure.
    """
    old_files, new_files = title_files(old), title_files(new)
    if not old_files or not new_files or old_files & new_files:
        return False
    conflict = ids_conflict(old, new)
    if conflict is not None:
        return conflict
    titles = old["show_title"], new["show_title"]
    return all(titles) and titles[0] != titles[1]


def _others_at(
    conn: sqlite3.Connection, title: sqlite3.Row, removed: Collection[str]
) -> list[sqlite3.Row]:
    """Return the library's titles at the path and season of *title* that are not in *removed*."""
    # rationale: the interpolated column list is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_TITLE} FROM media_items "
        "WHERE file_path = ? AND file_path != '' AND season_number IS ?",
        (title["file_path"], title["season_number"]),
    )
    return [row for row in rows if row["id"] not in removed]


def other_keys(
    conn: sqlite3.Connection, media_id: str, removed: Collection[str]
) -> list[sqlite3.Row]:
    """Return the library's other keys of the title *media_id* that are not in *removed*."""
    title = fetch_title(conn, media_id)
    if title is None:
        return []
    return [
        other
        for other in _others_at(conn, title, removed)
        if other["id"] != media_id and not _is_another_title(title, other)
    ]


# --- Keeps of one film or season ----------------------------------------------

# Settings key holding the last of pruned_keeps looked at for a key that should have had it.
_CARRIED_UP_TO_KEY = "pruned_keeps_carried_up_to"

_LIVE_ITEM_KEEP = (
    "(sa.action = 'protected_forever' OR (sa.action = 'snoozed' AND sa.execute_at >= ?))"
)


def is_kept_under_another_key(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if a kept film, season or show of the library is the title *media_id*.

    A film or season with its files keeps it. So does another copy of the film, or
    another key of the season's show, unless someone removed its own keep since.
    """
    title = fetch_title(conn, media_id)
    if title is None:
        return False
    return (
        any(is_protected(conn, other["id"]) for other in other_keys(conn, media_id, ()))
        or any(not _keep_removed_since(conn, keep, media_id) for keep in _copy_keeps(conn, title))
        or any(
            not _show_keep_removed_since(conn, keep, title["show_rating_key"])
            for keep in _other_show_keeps(conn, title)
        )
    )


def _copy_keeps(conn: sqlite3.Connection, film: sqlite3.Row) -> list[sqlite3.Row]:
    """Return the live keeps of the library's other films with the TMDB id of *film*."""
    if film["media_type"] != "movie" or film["tmdb_id"] is None:
        return []
    # CROSS JOIN reads the few keeps first, as no index finds a film by its TMDB id.
    # rationale: the interpolated condition is a fixed string; every value is bound
    return conn.execute(  # nosec B608
        "SELECT sa.* FROM scheduled_actions sa "
        "CROSS JOIN media_items mi ON mi.id = sa.media_item_id "
        f"WHERE {_LIVE_ITEM_KEEP} AND mi.media_type = 'movie' "
        "  AND mi.tmdb_id = ? AND mi.plex_library_id = ? AND mi.id != ?",
        (now_iso(), film["tmdb_id"], film["plex_library_id"], film["id"]),
    ).fetchall()


def _other_copies(
    conn: sqlite3.Connection, media_id: str, removed: Collection[str]
) -> list[sqlite3.Row]:
    """Return the library's other films with the TMDB id of *media_id* that are not in *removed*."""
    rows = conn.execute(
        "SELECT mi.id FROM media_items film JOIN media_items mi "
        "ON mi.plex_library_id = film.plex_library_id AND mi.tmdb_id = film.tmdb_id "
        "WHERE film.id = ? AND film.media_type = 'movie' AND mi.media_type = 'movie' "
        "  AND mi.id != film.id",
        (media_id,),
    )
    return [row for row in rows if row["id"] not in removed]


def is_kept_deletion(conn: sqlite3.Connection, action_id: int, now_iso: str) -> bool:
    """Return True if a keep the scan honours covers the item of the deletion row.

    The scan's own rule decides (is_protected, is_show_kept_pure,
    is_kept_under_another_key), so the pass never removes what a scan would
    keep. A pure read.
    """
    row = conn.execute(
        "SELECT sa.media_item_id, mi.show_rating_key FROM scheduled_actions sa "
        "LEFT JOIN media_items mi ON sa.media_item_id = mi.id WHERE sa.id = ?",
        (action_id,),
    ).fetchone()
    if row is None:
        return False
    return (
        is_protected(conn, row["media_item_id"])
        or is_show_kept_pure(conn, row["show_rating_key"], now_iso_str=now_iso)
        or is_kept_under_another_key(conn, row["media_item_id"])
    )


def cancel_kept_deletion(conn: sqlite3.Connection, action_id: int, now_iso: str) -> bool:
    """Delete the deletion row if is_kept_deletion says its item is kept; return True if it did.

    Run it after the claim, inside the claim's transaction, so a keep
    committed before the claim wins.
    """
    if not is_kept_deletion(conn, action_id, now_iso):
        return False
    delete_scheduled_action(conn, action_id)
    return True


def cancel_deletions_kept_under_another_key(conn: sqlite3.Connection) -> None:
    """Cancel each scheduled deletion of a title another key keeps, but for one being carried out.

    The deletion pass would when it is due; until then the title would read as leaving.
    """
    rows = conn.execute(
        "SELECT sa.id, sa.media_item_id, mi.title FROM scheduled_actions sa "
        "JOIN media_items mi ON mi.id = sa.media_item_id "
        "WHERE sa.action = 'scheduled_deletion' AND sa.token_used = 0 "
        "  AND sa.delete_status IS NOT 'deleting'"
    ).fetchall()
    for row in rows:
        if not is_kept_under_another_key(conn, row["media_item_id"]):
            continue
        _delete_unclaimed(conn, "id = ?", (row["id"],))
        log_audit(
            conn,
            row["media_item_id"],
            "delete_refused",
            f"Not deleted: {row['title']} — it is kept, so its scheduled deletion is cancelled",
        )


def _delete_unclaimed(conn: sqlite3.Connection, where: str, params: tuple[object, ...]) -> None:
    """Delete the rows matching the fixed condition *where*, but for one being carried out."""
    where = f"{where} AND delete_status IS NOT 'deleting'"
    book_removed_files_where(conn, where, params)
    # rationale: the interpolated condition is a fixed string; every value is bound
    conn.execute(f"DELETE FROM scheduled_actions WHERE {where}", params)  # nosec B608


def _keep_removed_since(conn: sqlite3.Connection, keep: sqlite3.Row, media_id: str) -> bool:
    """Return True if someone removed a keep from *media_id* since *keep* was made."""
    # Undoing a Keep is audited as 'unprotected' too, but only puts back the deletion it replaced.
    removed_since = conn.execute(
        "SELECT 1 FROM audit_log WHERE media_item_id = ? AND action = 'unprotected' "
        "AND detail LIKE 'Protection removed %' AND created_at > ? LIMIT 1",
        (media_id, keep["snoozed_at"] or keep["scheduled_at"]),
    ).fetchone()
    return removed_since is not None


def _give_keep(conn: sqlite3.Connection, keep: sqlite3.Row, media_id: str) -> None:
    """Keep *media_id* for as long as *keep* of pruned_keeps says, unless it already is for longer.

    Either way its scheduled deletion is cancelled, but for one being carried out.
    A keep someone removed from it since *keep* was made is not given again.
    """
    if _keep_removed_since(conn, keep, media_id):
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
    does, or the next new key would take one removed from this one meanwhile;
    nor does one by its TMDB id.
    """
    for keep in conn.execute(
        "SELECT pk.* FROM pruned_keeps pk JOIN media_items mi ON mi.id = pk.media_item_id"
    ).fetchall():
        if keep["media_item_id"] not in media_ids:
            continue
        others = {row["id"] for row in other_keys(conn, keep["media_item_id"], media_ids)}
        copies = {row["id"] for row in _other_copies(conn, keep["media_item_id"], media_ids)}
        for other in others | copies:
            _give_keep(conn, keep, other)
        if others:
            conn.execute("UPDATE pruned_keeps SET file_path = '' WHERE id = ?", (keep["id"],))
        if others or copies:
            conn.execute("UPDATE pruned_keeps SET tmdb_id = NULL WHERE id = ?", (keep["id"],))
    _set_carried_up_to(conn)


def carry_film_keeps(conn: sqlite3.Connection) -> None:
    """Give the keeps save_pruned_keeps set aside to the films listed since with their TMDB ids.

    A film replaced by another file has a new path and a new key, and Plex may
    match it only after it lists it. Its own key and one at its path still take
    its keep. Run it once a run has stored all Plex lists.
    """
    # rationale: the interpolated condition is a fixed string; every value is bound
    keeps = conn.execute(  # nosec B608
        "SELECT sa.*, mi.id AS film_id FROM pruned_keeps sa CROSS JOIN media_items mi "
        "ON mi.plex_library_id = sa.plex_library_id AND mi.tmdb_id = sa.tmdb_id "
        f"WHERE sa.tmdb_id IS NOT NULL AND {_LIVE_ITEM_KEEP} "
        "  AND mi.media_type = 'movie' AND mi.id != sa.media_item_id",
        (now_iso(),),
    ).fetchall()
    for keep in keeps:
        _give_keep(conn, keep, keep["film_id"])
    conn.executemany(
        "UPDATE pruned_keeps SET tmdb_id = NULL WHERE id = ?",
        [(keep["id"],) for keep in keeps],
    )


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


def _other_show_keeps(conn: sqlite3.Connection, season: sqlite3.Row) -> list[sqlite3.Row]:
    """Return the live keeps of the other keys of the show of *season*.

    Such a key has a season where this one has it.
    """
    # rationale: the interpolated condition is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT ks.*, own.id AS own_id, other.id AS other_id FROM media_items own "
        "JOIN media_items other ON other.file_path = own.file_path AND own.file_path != '' "
        "  AND other.season_number IS own.season_number "
        "JOIN kept_shows ks ON ks.show_rating_key = other.show_rating_key "
        "WHERE (own.id = ? OR own.show_rating_key = ?) "
        f"  AND ks.show_rating_key IS NOT ? AND {_LIVE_KEEP}",
        (season["id"], season["show_rating_key"], season["show_rating_key"], now_iso()),
    ).fetchall()
    keeps = []
    for row in rows:
        own, other = fetch_title(conn, row["own_id"]), fetch_title(conn, row["other_id"])
        if own is not None and other is not None and not _is_another_title(other, own):
            keeps.append(row)
    return keeps


def _show_keep_removed_since(
    conn: sqlite3.Connection, keep: sqlite3.Row, show_key: str | None
) -> bool:
    """Return True if someone removed the keep of *show_key* after the show of *keep* got it."""
    removed_since = conn.execute(
        "SELECT 1 FROM audit_log WHERE media_item_id = ? AND action = 'removed_show_keep' "
        "AND created_at > MAX(?, COALESCE((SELECT MAX(created_at) FROM audit_log "
        "  WHERE media_item_id = ? AND action = 'kept_show'), '')) LIMIT 1",
        (show_key, keep["created_at"], keep["show_rating_key"]),
    ).fetchone()
    return removed_since is not None


def _give_show_keep(conn: sqlite3.Connection, keep: sqlite3.Row, show_key: str) -> None:
    """Keep *show_key* for as long as *keep* says, unless it is already kept for longer.

    Either way its scheduled deletions are cancelled, but for one being carried out.
    A keep the new key was removed from after the old one got it is not given again.
    """
    if _show_keep_removed_since(conn, keep, show_key):
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
    # The deletion pass cancels these when they are due; until then a kept show reads as leaving.
    delete_show_scheduled_deletions(conn, show_key)


def _listed_title(season: Mapping[str, object]) -> dict[str, Any]:
    """Return *season* as Plex lists it in the columns _is_another_title reads."""
    return {
        **{key: season.get(key) for key in _TITLE.split(", ")},
        "tracked_files": json.dumps(season.get("tracked_files")),
        "episode_keys": episode_keys_json(season),
    }


def carry_show_keeps(
    conn: sqlite3.Connection, seasons: Iterable[Mapping[str, object]], listed: Collection[str]
) -> None:
    """Keep each show that *seasons*, as Plex lists them now, show to be a kept one under a new key.

    A season not in the library yet belongs to the show of the one it replaces:
    of the same path and season, not surely of another show, and not in *listed*,
    the keys Plex lists now. A season that is, belongs to the show it was stored
    under. The keep stays on the old key too. Run it before the seasons are
    stored or evaluated.
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
            there = by_place.get((season.get("file_path"), season.get("season_number")), [])
            for keep in there:
                # Of a season no longer in the library, nothing but its place is known.
                title = fetch_title(conn, keep["season_id"])
                if title is None or not _is_another_title(title, _listed_title(season)):
                    old = [*old, keep]
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
        shows = {
            other["show_rating_key"] for other in other_keys(conn, season["season_id"], media_ids)
        }
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
    conn: sqlite3.Connection, media_id: str, file_path: str, season_number: object
) -> None:
    """Forget where a kept show's season was, now that *media_id* is it in the library again.

    It is when its show has the keep, or lost it since; else it may be of
    another show in the same folder.
    """
    conn.execute(
        "DELETE FROM pruned_show_seasons WHERE file_path = ? AND season_number IS ? "
        "AND EXISTS (SELECT 1 FROM media_items mi WHERE mi.id = ? AND ("
        "  EXISTS (SELECT 1 FROM kept_shows ks WHERE ks.show_rating_key = mi.show_rating_key) "
        "  OR EXISTS (SELECT 1 FROM audit_log a WHERE a.media_item_id = mi.show_rating_key "
        "    AND a.action = 'removed_show_keep')))",
        (file_path, season_number, media_id),
    )


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
