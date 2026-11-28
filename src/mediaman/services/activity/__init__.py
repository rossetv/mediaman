"""Activity: what the server is busy with, for admins: the line, CPU and memory, by app, now
and over time. Plex and Docker are read with GET requests, NZBGet with its read-only JSON-RPC
methods; nothing is written anywhere but mediaman's own history table.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.core.time import parse_iso_utc
from mediaman.db import is_scan_running
from mediaman.services.activity import history
from mediaman.services.activity.sampler import LIVE_EVERY, SAMPLER, Reading, Sampler

logger = logging.getLogger(__name__)

# The downloads listed under the line's Downloads row; the rest are counted.
_LISTED = 3


def _work(conn: sqlite3.Connection) -> dict[str, object]:
    """What mediaman itself is busy with, from its own tables."""
    work: dict[str, object] = {"searching": {}, "fixing": 0, "scanning": False, "ai": None}
    try:
        searching: dict[str, set[str]] = {}
        for (key,) in conn.execute("SELECT key FROM arr_searching_since"):
            service, _, rest = str(key).partition(":")
            searching.setdefault(service, set()).add(rest.partition(":")[0])
        work["searching"] = {k: len(v) for k, v in sorted(searching.items())}
        work["fixing"] = conn.execute(
            "SELECT COUNT(*) FROM arr_import_retries WHERE stuck_since IS NOT NULL"
        ).fetchone()[0]
        work["scanning"] = is_scan_running(conn)
        row = conn.execute(
            "SELECT status, started_at FROM user_ai_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            started = parse_iso_utc(row[1])
            work["ai"] = {
                "running": row[0] == "running",
                "at": started.timestamp() if started else None,
            }
    except sqlite3.Error:
        logger.warning("activity.work_unreadable", exc_info=True)
    return work


def live_payload(
    conn: sqlite3.Connection, reading: Reading, live: list[dict[str, object]]
) -> dict[str, object]:
    """Everything the Activity tab shows now: the newest point, the last minute, the
    downloads, who is watching and what mediaman is doing."""
    nzb = reading.nzb
    downloads = None
    if nzb is not None:
        busy = sorted(
            nzb.items,
            key=lambda i: {"downloading": 0, "unpacking": 1, "repairing": 1, "finishing": 1}.get(
                i.state, 2
            ),
        )
        downloads = {
            "rate": nzb.rate,
            "left": nzb.left,
            "count": len(nzb.items),
            "items": [
                {"title": i.title, "state": i.state, "done": i.done, "size": i.size, "by": i.by}
                for i in busy[:_LISTED]
            ],
        }
    return {
        "at": reading.at,
        "every": LIVE_EVERY,
        "sources": reading.sources,
        "cores": reading.cores,
        "mem_total": reading.mem_total,
        "point": reading.point,
        "live": live,
        "since": history.since(conn),
        "downloads": downloads,
        "sessions": [
            {
                "user": s.user,
                "device": s.device,
                "kind": s.kind,
                "title": s.title,
                "transcode": s.transcode,
                "local": s.local,
                "rate": s.rate,
            }
            for s in reading.sessions
        ],
        "work": _work(conn),
    }


def sample_and_store(conn: sqlite3.Connection, secret_key: str, sampler: Sampler = SAMPLER) -> None:
    """The scheduler's job: a reading every step, kept in the history."""
    reading = sampler.read(conn, secret_key, max_age=history.STEP / 3)
    history.store(conn, reading.point, reading.at)


__all__ = ["SAMPLER", "Sampler", "history", "live_payload", "sample_and_store"]
