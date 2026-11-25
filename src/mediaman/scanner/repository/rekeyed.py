"""SQL that carries keeps and Watch progress to the new key Plex gives a title.

Plex gives a title it removed and found again a new key. The same title is one
of the same season with a file of its own, whatever else differs; or one at the
same path, of the same season, that is not surely another. An episode also has
the same number. One with the same id is the same title too: its TVDB id, or
its TMDB id where one of the two has no TVDB id. Its TMDB id is the one it last
had while Plex has none for it; so is its TVDB id, if it never had a TMDB id.

Two with the same id in one library are one title. In two libraries they are
two only while both are there: Plex has listed the one since the other came,
lists it still, and one of its files is on the disk. Only a library that was
read, from a list long enough to trust, tells that it lists a title no more.
Until then a keep or a watch of the one covers the other, and stays where it is.

Nothing here commits: each caller runs it in the transaction that adds or
removes the title.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import timedelta
from typing import Any

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso, now_utc, parse_iso_utc
from mediaman.scanner._eligibility import is_inactive
from mediaman.scanner.repository._protection import (
    is_protected,
    is_show_kept_pure,
    restore_keeps,
)
from mediaman.scanner.repository.scheduled_actions import (
    audit_lost_removal,
    book_removed_files_where,
    delete_scheduled_action,
    delete_show_scheduled_deletions,
    fetch_deletion,
)
from mediaman.scanner.repository.settings import read_setting

# How long the progress of a title Plex stopped listing waits for it to come back.
_PROGRESS_KEPT_FOR = timedelta(days=90)

_PROGRESS_COLUMNS = (
    "username, rating_key, media_item_id, show_rating_key, position_s, duration_s, "
    "watched, credited_at, updated_at, seen_at"
)

# media_items.unlisted: Plex lists the title; lists it no more; lists it with none of its files there.
_THERE, _UNLISTED, _WITHOUT_FILES = 0, 1, 2

# A file of a title: its path, and the episode's number when it is an episode.
Place = tuple[str, int | None]

# The last watch of a title ``o``, with the plays made in mediaman a scan may not have stored yet.
LATEST_WATCH = (
    "MAX(COALESCE(o.last_watched_at, ''), COALESCE("
    " (SELECT MAX(wp.credited_at) FROM watch_progress wp WHERE wp.media_item_id = o.id), ''))"
)


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
    "tracked_files, episode_keys, plex_library_id, last_tmdb_id, last_tvdb_id, media_type, "
    "last_scanned_at, first_listed_at, unlisted"
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


def files_json(title: sqlite3.Row | Mapping[str, Any] | None) -> str | None:
    """Return the files of a title as a row set aside for it stores them, None when not known."""
    files = sorted(title_files(title)) if title is not None else []
    return json.dumps([{"path": path} for path in files]) if files else None


def pruned_files(pruned: sqlite3.Row) -> set[str]:
    """Return the paths of the files a row set aside for a title holds."""
    return title_files({"tracked_files": pruned["tracked_files"], "episode_keys": None})


def file_places(files: Collection[str]) -> list[str]:
    """Return what the path of a title with one of *files* may be: a file, or the folder of one."""
    return sorted({place for file in files for place in (file, file.rsplit("/", 1)[0]) if place})


def titles_with_files(
    conn: sqlite3.Connection, files: Collection[str], season_number: object
) -> list[sqlite3.Row]:
    """Return the library's films, or seasons of the number, that have one of *files*.

    Whatever their ids and libraries: a file is one title's. They are found
    where their path is one of the files, or the folder of one.
    """
    places = file_places(files)
    rows: list[sqlite3.Row] = []
    for start in range(0, len(places), 500):
        chunk = places[start : start + 500]
        # rationale: only the column list and "?" placeholders are interpolated; every value is bound
        rows += conn.execute(  # nosec B608
            f"SELECT {_TITLE} FROM media_items "
            f"WHERE season_number IS ? AND file_path IN ({','.join('?' * len(chunk))})",
            (season_number, *chunk),
        ).fetchall()
    return [row for row in rows if not title_files(row).isdisjoint(files)]


def ids_conflict(
    old: sqlite3.Row | Mapping[str, Any], new: sqlite3.Row | Mapping[str, Any]
) -> bool | None:
    """Return True if the ids of two titles differ, None if they have none to compare."""
    for column in ("tmdb_id", "tvdb_id"):
        if old[column] is not None and new[column] is not None:
            return bool(old[column] != new[column])
    return None


# The TVDB and TMDB id of a title, None where it has none.
Ids = tuple[int | None, int | None]


def title_ids(title: sqlite3.Row | Mapping[str, Any]) -> Ids:
    """Return the ids of a title, or those it last had.

    A TVDB id it last had may be of a match before the TMDB id it has.
    """
    tvdb_id = title["tvdb_id"]
    tmdb_id = title["last_tmdb_id"] if title["tmdb_id"] is None else title["tmdb_id"]
    if tvdb_id is None and tmdb_id is None:
        tvdb_id = title["last_tvdb_id"]
    return _known_id(tvdb_id), _known_id(tmdb_id)


def _known_id(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def ids_match(one: Ids, other: Ids) -> bool:
    """Return True if the ids are one title's: the TVDB ids, else the TMDB ids."""
    if one[0] is not None and other[0] is not None:
        return one[0] == other[0]
    return one[1] is not None and one[1] == other[1]


def _titles_with_ids(
    conn: sqlite3.Connection, ids: Ids, where: str, params: tuple[object, ...]
) -> list[sqlite3.Row]:
    """Return the library's titles of the ids that meet the fixed condition *where*."""
    # rationale: the interpolated columns and condition are fixed strings; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_TITLE} FROM media_items "
        "WHERE (COALESCE(tvdb_id, last_tvdb_id) = ? OR COALESCE(tmdb_id, last_tmdb_id) = ?) "
        f"  AND {where}",
        (*ids, *params),
    )
    return [row for row in rows if ids_match(ids, title_ids(row))]


def _films_or_seasons_with_ids(
    conn: sqlite3.Connection, ids: Ids, season_number: int | None
) -> list[sqlite3.Row]:
    """Return the library's films of the ids, or its seasons of that number."""
    return _titles_with_ids(
        conn,
        ids,
        "season_number IS ? AND (media_type = 'movie') = (season_number IS NULL)",
        (season_number,),
    )


def is_film_or_numbered(title: sqlite3.Row) -> bool:
    """Return False for a season with no number, which its show's ids do not tell."""
    return bool((title["media_type"] == "movie") == (title["season_number"] is None))


def copies(
    conn: sqlite3.Connection, title: sqlite3.Row, *, every_library_read: bool = True
) -> list[sqlite3.Row]:
    """Return the library's other films or seasons with the ids of *title* that are one with it.

    Those of its library are. Of those in other libraries, only the ones that
    may be it: until every library of a run is read nothing tells which they are.
    """
    if not is_film_or_numbered(title):
        return []
    return [
        other
        for other in _films_or_seasons_with_ids(conn, title_ids(title), title["season_number"])
        if other["id"] != title["id"]
        and (
            other["plex_library_id"] == title["plex_library_id"]
            or (
                every_library_read
                and _may_be_moved(
                    other["unlisted"], other["last_scanned_at"], title["first_listed_at"]
                )
            )
        )
    ]


def _may_be_moved(unlisted: int, last_listed_at: str | None, first_listed_at: str | None) -> bool:
    """Return True if a title of another library may be the one first listed at *first_listed_at*.

    It is *unlisted*, or listed with none of its files there, or nobody has
    seen it listed since the other is.
    """
    return unlisted != _THERE or (last_listed_at or "") < (first_listed_at or "")


def _in_other_libraries(conn: sqlite3.Connection, title: sqlite3.Row) -> list[sqlite3.Row]:
    """Return the films with the ids of *title*, or the seasons of its show, of other libraries."""
    return _titles_with_ids(
        conn,
        title_ids(title),
        "(media_type = 'movie') = (? = 'movie') AND plex_library_id != ?",
        (title["media_type"], title["plex_library_id"]),
    )


def mark_listed_copies(conn: sqlite3.Connection, media_id: str, listed: Collection[str]) -> None:
    """Note that Plex lists each title of other libraries with the ids of *media_id* that it does.

    Run it once the title is stored: it was seen after those read before it.
    """
    title = fetch_title(conn, media_id)
    if title is None:
        return
    conn.executemany(
        "UPDATE media_items SET last_scanned_at = ? WHERE id = ? AND last_scanned_at < ?",
        [
            (title["last_scanned_at"], other["id"], title["last_scanned_at"])
            for other in _in_other_libraries(conn, title)
            if other["id"] in listed
        ],
    )


def fetch_titles_in_two_libraries(
    conn: sqlite3.Connection, library_ids: Collection[int]
) -> list[sqlite3.Row]:
    """Return the titles of the libraries that have an id of a title of another library."""
    other = "SELECT 1 FROM media_items o WHERE o.plex_library_id != mi.plex_library_id AND "
    rows: list[sqlite3.Row] = []
    for library_id in library_ids:
        # rationale: the interpolated fragments are fixed strings; every value is bound
        rows += conn.execute(  # nosec B608
            "SELECT mi.id, mi.tracked_files, mi.episode_keys FROM media_items mi "
            "WHERE mi.plex_library_id = ? AND ("
            f"EXISTS ({other} COALESCE(o.tvdb_id, o.last_tvdb_id) = "
            "  COALESCE(mi.tvdb_id, mi.last_tvdb_id)) "
            f"OR EXISTS ({other} COALESCE(o.tmdb_id, o.last_tmdb_id) = "
            "  COALESCE(mi.tmdb_id, mi.last_tmdb_id)))",
            (library_id,),
        ).fetchall()
    return rows


def note_listing(
    conn: sqlite3.Connection,
    library_id: int,
    listed: Collection[str],
    touched: Collection[str],
    since: str,
    *,
    trusted: bool = True,
    without_files: Collection[str] = (),
    inactivity_days: int = 30,
) -> None:
    """Note which titles of a library are there, as it was read in the run begun at *since*.

    One Plex lists is, unless it is in *without_files*; one whose history failed
    is stored by no run, but is listed. Of one Plex does not list, nothing is
    known if it is in *touched*, or if the list is not *trusted*: one too short
    to remove anything by. What is true of the copies of one found gone changes
    with it: see _unschedule_copies.
    """
    for row in conn.execute(
        "SELECT id, unlisted, last_scanned_at FROM media_items WHERE plex_library_id = ?",
        (library_id,),
    ).fetchall():
        if row["id"] in listed:
            state = _WITHOUT_FILES if row["id"] in without_files else _THERE
        elif trusted and row["id"] not in touched:
            state = _UNLISTED
        else:
            continue
        if state == _THERE:
            if row["unlisted"] or (row["last_scanned_at"] or "") < since:
                conn.execute(
                    "UPDATE media_items SET unlisted = 0, last_scanned_at = ? WHERE id = ?",
                    (now_iso(), row["id"]),
                )
        elif row["unlisted"] != state:
            conn.execute("UPDATE media_items SET unlisted = ? WHERE id = ?", (state, row["id"]))
            if not row["unlisted"]:
                _unschedule_copies(conn, row["id"], since, inactivity_days)


def _unschedule_copies(
    conn: sqlite3.Connection, media_id: str, since: str, inactivity_days: int
) -> None:
    """Cancel the deletions the copies of a title found gone were given as if it were there.

    Those of this run, which the next decides. An older one too, if the title
    was watched before it, lately enough to keep its copy today; a watch after
    it cancels nothing, as none does of the title itself.
    """
    title = fetch_title(conn, media_id)
    if title is None:
        return
    whole_show = is_show_kept_pure(conn, title["show_rating_key"])
    # rationale: the interpolated expression is a fixed string; every value is bound
    watched = conn.execute(  # nosec B608
        f"SELECT {LATEST_WATCH} AS at FROM media_items o WHERE o.id = ?", (media_id,)
    ).fetchone()["at"]
    lately = _is_lately(watched, inactivity_days)
    for other in _in_other_libraries(conn, title):
        same_season = other["season_number"] == title["season_number"]
        if not whole_show and not same_season:
            continue
        made = "scheduled_at >= ?"
        params: tuple[object, ...] = (other["id"], since)
        if lately and same_season and is_film_or_numbered(title):
            made, params = f"({made} OR scheduled_at > ?)", (*params, watched)
        if _delete_unclaimed(
            conn,
            f"media_item_id = ? AND action = 'scheduled_deletion' AND token_used = 0 AND {made}",
            params,
        ):
            log_audit(
                conn,
                other["id"],
                "delete_refused",
                "Not deleted: it may be a title that is gone from another library, "
                "so the next scan decides",
            )


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
    """Return the library's other keys of the title *media_id* that are not in *removed*.

    They are at its path and season and not surely another title, or have a file of its own.
    """
    title = fetch_title(conn, media_id)
    if title is None:
        return []
    there = [
        other
        for other in _others_at(conn, title, removed)
        if other["id"] != media_id and not _is_another_title(title, other)
    ]
    found = {media_id, *removed, *(other["id"] for other in there)}
    return there + [
        other
        for other in titles_with_files(conn, title_files(title), title["season_number"])
        if other["id"] not in found
    ]


def held_beside_another_title(
    conn: sqlite3.Connection, media_ids: Collection[str], inactivity_days: int
) -> set[str]:
    """Return those of *media_ids*, about to be removed, that must stay: kept, or watched lately.

    Such a one has a file that a title not in *media_ids* tracks too and that is
    not it: of another season, or a season where it is a film. That title takes
    no keep and no watch, so the file would go with it once this one is forgotten.
    """
    now, staying = now_iso(), set()
    tracking: dict[str, set[str]] | None = None
    for media_id in media_ids:
        title = fetch_title(conn, media_id)
        if title is None or not (
            is_protected(conn, media_id)
            or is_show_kept_pure(conn, title["show_rating_key"], now_iso_str=now)
            or is_watched_lately(conn, media_id, inactivity_days)
        ):
            continue
        if tracking is None:
            tracking = _ids_by_tracked_file(conn)
        others = set().union(*(tracking.get(file, ()) for file in title_files(title)))
        others.difference_update(media_ids)
        if others and others - {other["id"] for other in other_keys(conn, media_id, media_ids)}:
            staying.add(media_id)
    return staying


def _ids_by_tracked_file(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Return the ids of the library's titles that track each file."""
    tracking: dict[str, set[str]] = {}
    for row in conn.execute("SELECT id, tracked_files FROM media_items WHERE tracked_files != ''"):
        for file in title_files({"tracked_files": row["tracked_files"], "episode_keys": None}):
            tracking.setdefault(file, set()).add(row["id"])
    return tracking


def is_watched_lately(conn: sqlite3.Connection, media_id: str, inactivity_days: int) -> bool:
    """Return True if the last watch of the title would keep it from being scheduled today."""
    # rationale: the interpolated expression is a fixed string; every value is bound
    watched = conn.execute(  # nosec B608
        f"SELECT {LATEST_WATCH} AS at FROM media_items o WHERE o.id = ?", (media_id,)
    ).fetchone()
    return watched is not None and _is_lately(watched["at"], inactivity_days)


def _is_lately(watched: str | None, inactivity_days: int) -> bool:
    """Return True if a title last watched at *watched* would not be scheduled today."""
    watched_at = parse_iso_utc(watched)
    return watched_at is not None and not is_inactive([{"viewed_at": watched_at}], inactivity_days)


# --- Keeps of one film or season ----------------------------------------------

# Settings key holding the last of pruned_keeps looked at for a key that should have had it.
_CARRIED_UP_TO_KEY = "pruned_keeps_carried_up_to"

_LIVE_ITEM_KEEP = (
    "(sa.action = 'protected_forever' OR (sa.action = 'snoozed' AND sa.execute_at >= ?))"
)


def files_held_for_gone_titles(conn: sqlite3.Connection, inactivity_days: int) -> dict[str, bool]:
    """Return the files of titles removed from the library that a keep or a watch still holds.

    True for one of a live keep set aside, a title's own or its show's, that no
    title has taken; False for one held only by a watch that would keep its
    title from being scheduled today. A title listed with such a file may be
    one the keep or the watch is never given to: of another season, or a film
    at another path.
    """
    now = now_iso()
    # rationale: the interpolated conditions are fixed strings; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT sa.tracked_files, NULL AS watched FROM pruned_keeps sa "
        f"WHERE {_LIVE_ITEM_KEEP} AND sa.tracked_files IS NOT NULL "
        "UNION ALL "
        "SELECT ps.tracked_files, NULL FROM pruned_show_seasons ps "
        f"JOIN kept_shows ks USING (show_rating_key) WHERE {_LIVE_KEEP} "
        "  AND ps.tracked_files IS NOT NULL "
        "UNION ALL "
        "SELECT pw.tracked_files, pw.last_watched_at FROM pruned_watches pw "
        "WHERE pw.tracked_files IS NOT NULL",
        (now, now),
    ).fetchall()
    held: dict[str, bool] = {}
    for row in rows:
        kept = row["watched"] is None
        if kept or _is_lately(row["watched"], inactivity_days):
            for file in pruned_files(row):
                held[file] = kept or held.get(file, False)
    return held


def is_kept_under_another_key(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if a kept film, season or show of the library is the title *media_id*.

    A film or season with its files keeps it. So does another copy of it, or
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


def _copy_keeps(conn: sqlite3.Connection, title: sqlite3.Row) -> list[sqlite3.Row]:
    """Return the live keeps of the copies of *title*."""
    return [keep for other in copies(conn, title) for keep in _live_item_keeps(conn, other["id"])]


def _live_item_keeps(conn: sqlite3.Connection, media_id: str) -> list[sqlite3.Row]:
    # rationale: the interpolated condition is a fixed string; every value is bound
    return conn.execute(  # nosec B608
        f"SELECT * FROM scheduled_actions sa WHERE sa.media_item_id = ? AND {_LIVE_ITEM_KEEP}",
        (media_id, now_iso()),
    ).fetchall()


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
        # A Keep turns the row into the keep itself.
        if _delete_unclaimed(
            conn, "id = ? AND action = 'scheduled_deletion' AND token_used = 0", (row["id"],)
        ):
            log_audit(
                conn,
                row["media_item_id"],
                "delete_refused",
                f"Not deleted: {row['title']} — it is kept, so its scheduled deletion is cancelled",
            )


def _delete_unclaimed(conn: sqlite3.Connection, where: str, params: tuple[object, ...]) -> int:
    """Delete the rows matching the fixed condition *where*, but for one being carried out.

    What a row records as removed is booked first, and a removal it began with
    no outcome is audited. Return how many were deleted.
    """
    where = f"{where} AND delete_status IS NOT 'deleting'"
    book_removed_files_where(conn, where, params)
    # rationale: the interpolated condition is a fixed string; every value is bound
    rows = conn.execute(f"SELECT id FROM scheduled_actions WHERE {where}", params)  # nosec B608
    for row in rows.fetchall():
        dropped = fetch_deletion(conn, row["id"])
        if dropped is not None:
            audit_lost_removal(conn, dropped)
    # rationale: the interpolated condition is a fixed string; every value is bound
    deleted = conn.execute(f"DELETE FROM scheduled_actions WHERE {where}", params)  # nosec B608
    return deleted.rowcount


def _keep_removed_since(conn: sqlite3.Connection, keep: sqlite3.Row, media_id: str) -> bool:
    """Return True if someone removed a keep from *media_id* since *keep* was made."""
    # Undoing a Keep is audited as 'unprotected' too, but only puts back the deletion it replaced.
    removed_since = conn.execute(
        "SELECT 1 FROM audit_log WHERE media_item_id = ? AND action = 'unprotected' "
        "AND detail LIKE 'Protection removed %' AND created_at > ? LIMIT 1",
        (media_id, keep["snoozed_at"] or keep["scheduled_at"]),
    ).fetchone()
    return removed_since is not None


def _give_keep(conn: sqlite3.Connection, keep: sqlite3.Row, media_id: str) -> bool:
    """Keep *media_id* for as long as *keep* of pruned_keeps says, unless it already is for longer.

    Either way its scheduled deletion is cancelled, but for one being carried out.
    Return False if it was refused: a keep someone removed from it since *keep* was
    made is not given again.
    """
    if _keep_removed_since(conn, keep, media_id):
        return False
    forever = keep["action"] == "protected_forever"
    if not any(
        own["action"] == "protected_forever"
        or (not forever and own["execute_at"] >= keep["execute_at"])
        for own in _live_item_keeps(conn, media_id)
    ):
        _delete_unclaimed(
            conn,
            "media_item_id = ? AND action = 'snoozed' AND execute_at >= ?",
            (media_id, now_iso()),
        )
        restore_keeps(conn, media_id, [keep])
    _delete_unclaimed(
        conn,
        "media_item_id = ? AND action = 'scheduled_deletion' AND token_used = 0",
        (media_id,),
    )
    return True


def carry_pruned_keeps(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Give the keeps save_pruned_keeps set aside to the other keys of their titles.

    Such a key is in the library already, so it would never take them as a new
    one does. A title's own key still takes its keep back; by path no other
    does, or the next new key would take one removed from this one meanwhile.
    The files of a title no other key has are noted, for carry_film_keeps.
    """
    for keep in conn.execute(
        "SELECT pk.* FROM pruned_keeps pk JOIN media_items mi ON mi.id = pk.media_item_id"
    ).fetchall():
        if keep["media_item_id"] not in media_ids:
            continue
        others = other_keys(conn, keep["media_item_id"], media_ids)
        for other in others:
            _give_keep(conn, keep, other["id"])
        if others:
            conn.execute("UPDATE pruned_keeps SET file_path = '' WHERE id = ?", (keep["id"],))
        else:
            conn.execute(
                "UPDATE pruned_keeps SET tracked_files = ? WHERE id = ?",
                (files_json(fetch_title(conn, keep["media_item_id"])), keep["id"]),
            )
    _set_carried_up_to(conn)


def carry_film_keeps(conn: sqlite3.Connection) -> None:
    """Give the keeps of removed films, seasons and shows to the library's titles that are they.

    A title replaced by other files, renamed or moved to another library has a
    new path and a new key, and Plex may match it only after it lists it. One
    with a file of the removed title is it, and takes the keep for good. Else
    those with its ids take it: its own key and one at its path still do after
    them; no other does once one of its own library has it, nor one of another
    library once one there has it, or once a person removed it from one there:
    that one was a copy, and the title may still come back to its own library.
    Run it once the items set aside are removed, and once a run has stored all
    Plex lists.
    """
    # rationale: the interpolated condition is a fixed string; every value is bound
    keeps = conn.execute(  # nosec B608
        f"SELECT sa.* FROM pruned_keeps sa WHERE {_LIVE_ITEM_KEEP} AND "
        "(sa.tmdb_id IS NOT NULL OR sa.tvdb_id IS NOT NULL OR sa.tracked_files IS NOT NULL)",
        (now_iso(),),
    ).fetchall()
    for keep in keeps:

        def give(title: sqlite3.Row, keep: sqlite3.Row = keep) -> bool:
            return _give_keep(conn, keep, title["id"])

        claim = None
        if [
            title
            for title in titles_with_files(conn, pruned_files(keep), keep["season_number"])
            if give(title)
        ]:
            conn.execute("UPDATE pruned_keeps SET file_path = '' WHERE id = ?", (keep["id"],))
        else:
            titles = _films_or_seasons_with_ids(conn, _pruned_ids(keep), keep["season_number"])
            if any(_keep_removed_since(conn, keep, str(key)) for key in _entries(keep["given_to"])):
                titles = _of_own_library(keep, titles)
            claim = _give_nearest(keep, titles, give, "id")
        _store_claim(conn, "pruned_keeps", keep["id"], claim)
    _carry_pruned_show_keeps(conn)


def _pruned_ids(pruned: sqlite3.Row) -> Ids:
    return title_ids({**dict(pruned), "last_tmdb_id": None, "last_tvdb_id": None})


# The other libraries where a set-aside keep was given, and the keys given it there.
_Claim = tuple[set[object], set[object]]


def _give_nearest(
    pruned: sqlite3.Row,
    titles: Collection[sqlite3.Row],
    give: Callable[[sqlite3.Row], bool],
    key: str,
) -> _Claim | None:
    """Give the keep of *pruned* to those of *titles* in its own library, else to those elsewhere.

    Return where else it is given, under which *key* of the titles, None once
    its own library has it. One elsewhere may be a copy that was always there,
    so the title may still come back to its own library, or to a third.
    """
    own_library = pruned["plex_library_id"]
    if [t for t in titles if t["plex_library_id"] == own_library and give(t)]:
        return None
    had = set(_entries(pruned["given_libraries"]))
    given = [t for t in titles if t["plex_library_id"] not in {own_library, *had} and give(t)]
    return (
        had.union(title["plex_library_id"] for title in given),
        {*_entries(pruned["given_to"]), *(title[key] for title in given)},
    )


def _of_own_library(pruned: sqlite3.Row, titles: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    """Return those of *titles* in the library the title of *pruned* was in."""
    return [title for title in titles if title["plex_library_id"] == pruned["plex_library_id"]]


def _store_claim(
    conn: sqlite3.Connection, table: str, pruned_id: int, claim: _Claim | None
) -> None:
    """Store what became of the keep of the row of the fixed *table*: None uses its ids and files up."""
    params: list[object] = []
    if claim is None:
        change = "tvdb_id = NULL, tmdb_id = NULL, tracked_files = NULL"
    elif claim[0]:
        change = "given_libraries = ?, given_to = ?"
        params += [json.dumps(sorted(entries, key=str)) for entries in claim]
    else:
        return
    # rationale: the interpolated table and columns are fixed strings; every value is bound
    conn.execute(f"UPDATE {table} SET {change} WHERE id = ?", (*params, pruned_id))  # nosec B608


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
    """Return the live keeps of the other keys of the show of *season*, and of the season itself.

    A key of its show has a season where this one has it, or its ids: in another
    library, only if it may be the same show. The season alone may have come from
    a show whose other seasons stay.
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
    return keeps + _other_show_keeps_by_ids(conn, season) + _season_show_keeps(conn, season)


def _season_show_keeps(conn: sqlite3.Connection, season: sqlite3.Row) -> list[sqlite3.Row]:
    """Return the live keeps of the other shows that have *season* too, or had it last.

    One has it with its files, or as a copy. A show had it last while none with
    the season's ids and number is in the library the show's own was in; for a
    season of another library, until a person removed the keep from a show
    there that was given it.
    """
    same = titles_with_files(conn, title_files(season), season["season_number"])
    show_keys = {other["show_rating_key"] for other in [*same, *copies(conn, season)]}
    if season["show_rating_key"] and is_film_or_numbered(season):
        ids = title_ids(season)
        for gone in conn.execute(
            "SELECT * FROM pruned_show_seasons WHERE season_number IS ? "
            "AND (tvdb_id IS NOT NULL OR tmdb_id IS NOT NULL)",
            (season["season_number"],),
        ).fetchall():
            if ids_match(_pruned_ids(gone), ids) and (
                gone["plex_library_id"] == season["plex_library_id"]
                or not (
                    _of_own_library(
                        gone, _films_or_seasons_with_ids(conn, ids, season["season_number"])
                    )
                    or _show_keep_removed_where_given(conn, gone)
                )
            ):
                show_keys.add(gone["show_rating_key"])
    keeps = []
    for show_key in show_keys - {None, "", season["show_rating_key"]}:
        # rationale: the interpolated condition is a fixed string; every value is bound
        keep = conn.execute(  # nosec B608
            f"SELECT ks.* FROM kept_shows ks WHERE ks.show_rating_key = ? AND {_LIVE_KEEP}",
            (show_key, now_iso()),
        ).fetchone()
        if keep is not None:
            keeps.append(keep)
    return keeps


def _other_show_keeps_by_ids(conn: sqlite3.Connection, season: sqlite3.Row) -> list[sqlite3.Row]:
    if not season["show_rating_key"]:
        return []
    others = {
        (other["show_rating_key"], other["plex_library_id"])
        for other in _titles_with_ids(conn, title_ids(season), "media_type != 'movie'", ())
    }
    first_listed_at = conn.execute(
        "SELECT MIN(COALESCE(first_listed_at, '')) AS at FROM media_items "
        "WHERE show_rating_key = ?",
        (season["show_rating_key"],),
    ).fetchone()["at"]
    keeps = []
    for show_key, library_id in others:
        # rationale: the interpolated condition is a fixed string; every value is bound
        keep = conn.execute(  # nosec B608
            "SELECT ks.*, MAX(mi.last_scanned_at) AS last_scanned_at, "
            "MIN(mi.unlisted) AS unlisted FROM kept_shows ks "
            "JOIN media_items mi ON mi.show_rating_key = ks.show_rating_key "
            f"WHERE ks.show_rating_key = ? AND {_LIVE_KEEP} GROUP BY ks.id",
            (show_key, now_iso()),
        ).fetchone()
        if keep is None or show_key == season["show_rating_key"]:
            continue
        if library_id == season["plex_library_id"] or _may_be_moved(
            keep["unlisted"], keep["last_scanned_at"], first_listed_at
        ):
            keeps.append(keep)
    return keeps


def _carry_pruned_show_keeps(conn: sqlite3.Connection) -> None:
    """Give the keep of each show none of whose seasons is left to the shows with its ids.

    Once a person removed it from one of another library, only to those of its own.
    """
    # rationale: the interpolated condition is a fixed string; every value is bound
    seasons = conn.execute(  # nosec B608
        "SELECT ps.*, ks.show_title, ks.action, ks.execute_at, ks.snooze_duration, ks.created_at "
        "FROM pruned_show_seasons ps JOIN kept_shows ks USING (show_rating_key) "
        f"WHERE {_LIVE_KEEP} AND (ps.tvdb_id IS NOT NULL OR ps.tmdb_id IS NOT NULL) "
        "  AND NOT EXISTS (SELECT 1 FROM media_items mi "
        "    WHERE mi.show_rating_key = ps.show_rating_key) ORDER BY ps.id",
        (now_iso(),),
    ).fetchall()
    by_show: dict[str, list[sqlite3.Row]] = {}
    for season in seasons:
        by_show.setdefault(season["show_rating_key"], []).append(season)
    for gone in by_show.values():
        shows = {
            other["show_rating_key"]: other
            for season in gone
            for other in _titles_with_ids(conn, _pruned_ids(season), "media_type != 'movie'", ())
            if other["show_rating_key"]
        }

        def give(show: sqlite3.Row, keep: sqlite3.Row = gone[0]) -> bool:
            return _give_show_keep(conn, keep, show["show_rating_key"])

        titles = list(shows.values())
        if any(_show_keep_removed_where_given(conn, season) for season in gone):
            titles = _of_own_library(gone[0], titles)
        claim = _give_nearest(gone[0], titles, give, "show_rating_key")
        for season in gone:
            _store_claim(conn, "pruned_show_seasons", season["id"], claim)


def _show_keep_removed_where_given(conn: sqlite3.Connection, gone: sqlite3.Row) -> bool:
    """Return True if someone removed the keep of the show of *gone* from a show it was given to."""
    keep = conn.execute(
        "SELECT * FROM kept_shows WHERE show_rating_key = ?", (gone["show_rating_key"],)
    ).fetchone()
    return keep is not None and any(
        _show_keep_removed_since(conn, keep, str(key)) for key in _entries(gone["given_to"])
    )


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


def _give_show_keep(conn: sqlite3.Connection, keep: sqlite3.Row, show_key: str) -> bool:
    """Keep *show_key* for as long as *keep* says, unless it is already kept for longer.

    Either way its scheduled deletions are cancelled, but for one being carried out.
    Return False if it was refused: a keep the new key was removed from after the old
    one got it is not given again.
    """
    if _show_keep_removed_since(conn, keep, show_key):
        return False
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
    return True


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
    of the same path and season and not surely of another show, or of the same
    season with a file of its own; and not in *listed*, the keys Plex lists now.
    A season that is, belongs to the show it was stored under. The keep stays on
    the old key too. Run it before the seasons are stored or evaluated.
    """
    now = now_iso()
    # rationale: the interpolated condition is a fixed string; every value is bound
    kept = conn.execute(  # nosec B608
        "SELECT ks.*, mi.id AS season_id, mi.file_path, mi.season_number, "
        "mi.tracked_files, mi.episode_keys "
        "FROM kept_shows ks JOIN media_items mi ON mi.show_rating_key = ks.show_rating_key "
        f"WHERE {_LIVE_KEEP} "
        "UNION ALL "
        "SELECT ks.*, ps.media_item_id, ps.file_path, ps.season_number, ps.tracked_files, NULL "
        "FROM kept_shows ks JOIN pruned_show_seasons ps USING (show_rating_key) "
        f"WHERE {_LIVE_KEEP}",
        (now, now),
    ).fetchall()
    if not kept:
        return
    by_id: dict[str, list[sqlite3.Row]] = {}
    by_place: dict[tuple[object, object], list[sqlite3.Row]] = {}
    by_file: dict[tuple[object, object], list[sqlite3.Row]] = {}
    for keep in kept:
        by_id.setdefault(keep["season_id"], []).append(keep)
        if keep["season_id"] in listed:
            continue
        if keep["file_path"]:
            by_place.setdefault((keep["file_path"], keep["season_number"]), []).append(keep)
        for file in title_files(keep):
            by_file.setdefault((file, keep["season_number"]), []).append(keep)
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
            for file in title_files(_listed_title(season)):
                old = [*old, *by_file.get((file, season.get("season_number")), [])]
        for keep in old:
            if keep["show_rating_key"] != show_key:
                _give_show_keep(conn, keep, show_key)


def save_pruned_show_seasons(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Carry the keeps of the kept shows whose seasons are about to be removed.

    A season still in the library under another key gives its show the keep;
    without one, where the season was and its ids are set aside for carry_show_keeps
    and carry_film_keeps.
    """
    now = now_iso()
    # rationale: the interpolated condition is a fixed string; every value is bound
    conn.execute(  # nosec B608
        "DELETE FROM pruned_show_seasons WHERE show_rating_key NOT IN "
        f"(SELECT ks.show_rating_key FROM kept_shows ks WHERE {_LIVE_KEEP})",
        (now,),
    )
    seasons = conn.execute(  # nosec B608
        "SELECT ks.*, mi.id AS season_id, mi.file_path, mi.season_number, mi.tvdb_id, "
        "mi.tmdb_id, mi.last_tvdb_id, mi.last_tmdb_id, mi.plex_library_id "
        "FROM kept_shows ks JOIN media_items mi ON mi.show_rating_key = ks.show_rating_key "
        f"WHERE {_LIVE_KEEP}",
        (now,),
    ).fetchall()
    for season in seasons:
        if season["season_id"] not in media_ids:
            continue
        shows = {
            other["show_rating_key"] for other in other_keys(conn, season["season_id"], media_ids)
        }
        for show_key in shows - {None, "", season["show_rating_key"]}:
            _give_show_keep(conn, season, show_key)
        if not shows - {None, ""}:
            conn.execute(
                "INSERT INTO pruned_show_seasons (show_rating_key, media_item_id, file_path, "
                "season_number, tvdb_id, tmdb_id, plex_library_id, tracked_files) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    season["show_rating_key"],
                    season["season_id"],
                    season["file_path"],
                    season["season_number"],
                    *title_ids(season),
                    season["plex_library_id"],
                    files_json(fetch_title(conn, season["season_id"])),
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
        "DELETE FROM pruned_show_seasons "
        "WHERE file_path = ? AND file_path != '' AND season_number IS ? "
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
            "SELECT file_path, season_number, media_type, episode_keys, tvdb_id, "
            "tmdb_id, last_tvdb_id, last_tmdb_id FROM media_items WHERE id = ?",
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
                "file_path, season_number, episode_path, episode_number, pruned_at, "
                f"tvdb_id, tmdb_id) VALUES ({', '.join('?' * 17)})",
                (
                    *(row[column] for column in _PROGRESS_COLUMNS.split(", ")),
                    title["file_path"],
                    title["season_number"],
                    *place,
                    now.isoformat(),
                    *title_ids(title),
                ),
            )


def restore_progress(conn: sqlite3.Connection) -> None:
    """Give back the progress set aside for titles now in the library again.

    A title's own comes back by its key. That of a key no longer in the library
    goes to the title at its path, and the episode of its key or its path and
    number, if there is just one; where there is none, to the title with its ids,
    and the episode of its key or number. Run it once a run has stored all Plex lists, so every copy is known.
    """
    for row in conn.execute("SELECT * FROM pruned_progress").fetchall():
        own = conn.execute(
            "SELECT id, show_rating_key, episode_keys FROM media_items WHERE id = ?",
            (row["media_item_id"],),
        ).fetchone()
        titles = [own] if own else _others_at(conn, row, ())
        found = [(key, title) for title in titles if (key := _restored_key(row, title))]
        if not found and not own:
            found = [
                (key, title)
                for title in _films_or_seasons_with_ids(
                    conn, _pruned_ids(row), row["season_number"]
                )
                if (key := _restored_key_by_number(row, title))
            ]
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


def _restored_key_by_number(row: sqlite3.Row, title: sqlite3.Row) -> str | None:
    """Return the key *row*'s progress has in *title*, whose files are elsewhere."""
    if row["episode_path"] is None:
        return str(title["id"])
    if row["rating_key"] in _episode_places(title["episode_keys"]):
        return str(row["rating_key"])
    keys = [
        key
        for key, (_, number) in _episode_places(title["episode_keys"]).items()
        if number is not None and number == row["episode_number"]
    ]
    return keys[0] if len(keys) == 1 else None


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
