"""The history behind Activity's periods: points kept at four steps, each averaged from the
finer one, and dropped once older than its step is kept for. The table never holds more than
about 3,400 rows.
"""

from __future__ import annotations

import itertools
import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

Point = dict[str, object]

# (tier, seconds between points, seconds kept)
TIERS: tuple[tuple[int, int, int], ...] = (
    (0, 15, 3600),
    (1, 60, 24 * 3600),
    (2, 600, 7 * 24 * 3600),
    (3, 3600, 30 * 24 * 3600),
)
STEP = TIERS[0][1]
# The tier each period reads, and how far back it goes.
PERIODS: dict[str, tuple[int, int]] = {
    "1h": (0, 3600),
    "24h": (1, 24 * 3600),
    "7d": (2, 7 * 24 * 3600),
    "30d": (3, 30 * 24 * 3600),
}
# Points a period is drawn with at most: about one for every two pixels of the widest graph.
MAX_POINTS = 240
_FIGURES = ("down", "up", "dl", "watch", "mm", "cpu", "mem")


def _mean(values: Iterable[object]) -> float | None:
    known = [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    return sum(known) / len(known) if known else None


def average(points: Sequence[Point]) -> Point:
    """One point standing for *points*: each figure the mean of those that know it."""
    out: Point = {k: _mean(p.get(k) for p in points) for k in _FIGURES}
    out["at"] = _mean(p.get("at") for p in points)
    names: list[str] = []
    for p in points:
        apps = p.get("apps")
        if isinstance(apps, dict):
            names.extend(n for n in apps if n not in names)
    apps_out: dict[str, list[float | None]] = {}
    for name in names:
        pairs = []
        for p in points:
            apps = p.get("apps")
            pair = apps.get(name) if isinstance(apps, dict) else None
            if isinstance(pair, list) and len(pair) == 2:
                pairs.append(pair)
        apps_out[name] = [_mean(pair[0] for pair in pairs), _mean(pair[1] for pair in pairs)]
    out["apps"] = apps_out
    return out


def _encode(point: Point) -> str:
    return json.dumps({k: v for k, v in point.items() if k != "at"}, separators=(",", ":"))


def _decode(at: int, data: str) -> Point | None:
    try:
        point = json.loads(data)
    except ValueError:
        return None
    if not isinstance(point, dict):
        return None
    point["at"] = at
    return point


def rows(
    conn: sqlite3.Connection, tier: int, start: float, end: float | None = None
) -> list[Point]:
    sql = "SELECT at, data FROM activity_samples WHERE tier=? AND at>=?"
    args: list[object] = [tier, int(start)]
    if end is not None:
        sql += " AND at<?"
        args.append(int(end))
    out = []
    for at, data in conn.execute(sql + " ORDER BY at", args):
        point = _decode(at, data)
        if point is not None:
            out.append(point)
    return out


def store(conn: sqlite3.Connection, point: Point, at: float) -> bool:
    """Keep *point* as a point of the finest step, average each step's last whole period into
    the next, and drop what is older than each step keeps. Returns False when a point was kept
    less than a step ago, so no more than one is kept a step."""
    now = int(at)
    last = conn.execute("SELECT MAX(at) FROM activity_samples WHERE tier=0").fetchone()[0]
    if last is not None and 0 <= now - last < STEP * 2 / 3:
        return False
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO activity_samples (tier, at, data) VALUES (0, ?, ?)",
            (now, _encode(point)),
        )
        for (finer, _, _), (tier, step, _) in itertools.pairwise(TIERS):
            ended = now // step * step - step
            have = conn.execute(
                "SELECT 1 FROM activity_samples WHERE tier=? AND at=?", (tier, ended)
            ).fetchone()
            if have:
                continue
            parts = rows(conn, finer, ended, ended + step)
            if parts:
                merged = average(parts)
                conn.execute(
                    "INSERT INTO activity_samples (tier, at, data) VALUES (?, ?, ?)",
                    (tier, ended, _encode(merged)),
                )
        for tier, _, keep in TIERS:
            conn.execute("DELETE FROM activity_samples WHERE tier=? AND at<?", (tier, now - keep))
    return True


def downsample(
    points: Sequence[Point], start: float, end: float, most: int = MAX_POINTS
) -> list[Point]:
    """At most *most* points over [start, end]: those that share a slot are averaged."""
    if len(points) <= most:
        return list(points)
    width = (end - start) / most
    slots: dict[int, list[Point]] = {}
    for p in points:
        slots.setdefault(int((float(p["at"]) - start) // width), []).append(p)  # type: ignore[arg-type]
    return [average(slots[k]) for k in sorted(slots)]


def period(conn: sqlite3.Connection, name: str, now: float) -> list[Point]:
    """The points of a period, oldest first: those of its own step, then the finer ones that
    came after its last whole period, so the graph reaches the present."""
    tier, span = PERIODS[name]
    start = now - span
    points: list[Point] = []
    after = start
    for t in range(tier, -1, -1):
        found = rows(conn, t, after)
        if found:
            points += found
            after = float(found[-1]["at"]) + TIERS[t][1]  # type: ignore[arg-type]
    return downsample(points, start, now)


def since(conn: sqlite3.Connection) -> int | None:
    """When the oldest point kept was taken."""
    row = conn.execute("SELECT MIN(at) FROM activity_samples").fetchone()
    return row[0] if row and row[0] is not None else None
