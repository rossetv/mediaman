"""One reading of every source, turned into rates and shares, and the last minute of them.

A rate needs two readings: the CPU of mediaman and of each container is worked out from the
one before. One sampler serves the whole process; a reading younger than *max_age* is handed
out again, so any number of open tabs cost one reading every two seconds. A source that fails
is left alone for a while, so a dead one cannot slow every reading down.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

from mediaman.services.activity.sources import (
    TIMEOUT,
    ContainerStats,
    DockerProxy,
    DockerUnavailable,
    NzbgetError,
    NzbReading,
    PlexActivity,
    PlexResources,
    PlexSession,
    PlexUnavailable,
    SelfReading,
    host_size,
    read_nzbget,
    read_self,
)
from mediaman.services.infra import (
    ConfigDecryptError,
    SafeHTTPClient,
    SafeHTTPError,
    allowed_outbound_hosts,
    get_string_setting,
)
from mediaman.services.media_meta.plex_watch import PlexNotConfigured

logger = logging.getLogger(__name__)

LIVE_SECONDS = 60
LIVE_EVERY = 2
# How long a source that failed is left alone before it is asked again.
RETRY_AFTER = 10.0
# Sessions of a busy server, and the resource figures, are small; anything bigger is not Plex.
PLEX_MAX_BYTES = 2 * 1024 * 1024

# A point: its time, the line down and up in bytes a second, the host's CPU in percent and
# memory in bytes, and for each app [CPU %, memory bytes]. Any figure may be None: not known.
Point = dict[str, object]


@dataclass(slots=True)
class Reading:
    at: float
    point: Point
    sources: dict[str, str]  # plex, nzbget, docker: "ok", "down" or "off"
    sessions: list[PlexSession] = field(default_factory=list)
    nzb: NzbReading | None = None
    cores: int = 1
    mem_total: float | None = None


def _whole(n: float | None) -> float | None:
    return None if n is None else float(round(n))


def _pct(n: float | None) -> float | None:
    return None if n is None else round(n, 2)


def _add(
    apps: dict[str, list[float | None]], app: str, cpu: float | None, mem: float | None
) -> None:
    have = apps.setdefault(app, [None, None])
    if cpu is not None:
        have[0] = (have[0] or 0) + cpu
    if mem is not None:
        have[1] = (have[1] or 0) + mem


class Sampler:
    def __init__(
        self,
        *,
        read_own: Callable[[], SelfReading] = read_self,
        size: Callable[[], tuple[int, float | None]] = host_size,
    ) -> None:
        self._lock = threading.Lock()
        self._read_own = read_own
        self._size = size
        self._last: Reading | None = None
        self._own: tuple[float, SelfReading] | None = None
        self._containers: dict[str, ContainerStats] = {}
        self._resting: dict[str, float] = {}  # source -> when it may be asked again
        self.live: deque[Point] = deque(maxlen=LIVE_SECONDS // LIVE_EVERY + 2)

    def read(
        self,
        conn: sqlite3.Connection,
        secret_key: str,
        *,
        max_age: float = 1.5,
        now: float | None = None,
    ) -> Reading:
        """The newest reading, taken now unless one younger than *max_age* seconds exists."""
        with self._lock:
            at = time.time() if now is None else now
            if self._last is not None and 0 <= at - self._last.at < max_age:
                return self._last
            reading = self._take(conn, secret_key, at)
            self._last = reading
            self.live.append(reading.point)
            while self.live and at - float(self.live[0]["at"]) > LIVE_SECONDS + LIVE_EVERY:  # type: ignore[arg-type]
                self.live.popleft()
            return reading

    def recent(self) -> list[Point]:
        """The last minute of readings, oldest first."""
        with self._lock:
            return list(self.live)

    def _resting_now(self, source: str, at: float) -> bool:
        return at < self._resting.get(source, 0)

    def _failed(self, source: str, at: float) -> str:
        self._resting[source] = at + RETRY_AFTER
        return "down"

    def _take(self, conn: sqlite3.Connection, secret_key: str, at: float) -> Reading:
        cores, mem_total = self._size()
        hosts = allowed_outbound_hosts(conn)
        sources: dict[str, str] = {}
        apps: dict[str, list[float | None]] = {}

        resources: PlexResources | None = None
        sessions: list[PlexSession] = []
        plex_url = get_string_setting(conn, "plex_url")
        if not plex_url:
            sources["plex"] = "off"
        elif self._resting_now("plex", at):
            sources["plex"] = "down"
        else:
            try:
                plex = PlexActivity(
                    plex_url,
                    get_string_setting(conn, "plex_token", secret_key=secret_key),
                    http=SafeHTTPClient(
                        default_timeout=TIMEOUT,
                        default_max_bytes=PLEX_MAX_BYTES,
                        allowed_hosts=hosts,
                    ),
                )
                resources = plex.resources()
                sessions = plex.sessions()
                sources["plex"] = "ok"
            except PlexNotConfigured:
                sources["plex"] = "off"
            except (PlexUnavailable, ConfigDecryptError):
                resources, sessions = None, []
                sources["plex"] = self._failed("plex", at)

        nzb: NzbReading | None = None
        from mediaman.services.arr.build import build_nzbget_from_db

        try:
            client = build_nzbget_from_db(conn, secret_key)
        except ConfigDecryptError:
            client = None
        if client is None:
            sources["nzbget"] = "off"
        elif self._resting_now("nzbget", at):
            sources["nzbget"] = "down"
        else:
            try:
                nzb = read_nzbget(client)
                sources["nzbget"] = "ok"
            except (SafeHTTPError, requests.RequestException, NzbgetError):
                sources["nzbget"] = self._failed("nzbget", at)

        own = self._read_own()
        own_cpu = None
        rx = tx = None
        if self._own is not None:
            was_at, was = self._own
            span = at - was_at
            if span > 0:
                own_cpu = max(0.0, (own.cpu_seconds - was.cpu_seconds) / (span * cores) * 100)
                if (
                    own.rx is not None
                    and was.rx is not None
                    and own.tx is not None
                    and was.tx is not None
                ):
                    rx = max(0.0, (own.rx - was.rx) / span)
                    tx = max(0.0, (own.tx - was.tx) / span)
        self._own = (at, own)
        _add(apps, "mediaman", own_cpu, own.mem)

        if resources is not None:
            _add(
                apps,
                "plex",
                resources.plex_cpu,
                resources.plex_mem / 100 * mem_total if mem_total else None,
            )

        docker_url = get_string_setting(conn, "docker_proxy_url")
        if not docker_url:
            sources["docker"] = "off"
            self._containers = {}
        elif self._resting_now("docker", at):
            sources["docker"] = "down"
        else:
            try:
                proxy = DockerProxy(docker_url, allowed_hosts=hosts)
                seen: dict[str, ContainerStats] = {}
                for cid, app in proxy.containers():
                    if app == "mediaman" or (app == "plex" and resources is not None):
                        continue  # read from their own source
                    stats = proxy.stats(cid, app)
                    seen[cid] = stats
                    was_stats = self._containers.get(cid)
                    cpu = None
                    if was_stats is not None:
                        system = stats.system_total - was_stats.system_total
                        if system > 0:
                            cpu = max(0.0, (stats.cpu_total - was_stats.cpu_total) / system * 100)
                    if app:
                        _add(apps, app, cpu, stats.mem)
                self._containers = seen
                sources["docker"] = "ok"
            except DockerUnavailable:
                logger.info("activity.docker_unavailable")
                sources["docker"] = self._failed("docker", at)
                self._containers = {}

        watching = sum(s.rate for s in sessions) if sources["plex"] == "ok" else None
        downloading = nzb.rate if nzb is not None else None
        parts_down = [v for v in (downloading, rx) if v is not None]
        parts_up = [v for v in (watching, tx) if v is not None]
        point: Point = {
            "at": round(at, 1),
            "down": _whole(sum(parts_down) if parts_down else None),
            "up": _whole(sum(parts_up) if parts_up else None),
            "dl": _whole(downloading),
            "watch": _whole(watching),
            "mm": _whole((rx or 0) + (tx or 0) if rx is not None else None),
            "cpu": _pct(resources.host_cpu if resources else None),
            "mem": _whole(
                resources.host_mem / 100 * mem_total if resources and mem_total else None
            ),
            "apps": {k: [_pct(cpu), _whole(mem)] for k, (cpu, mem) in apps.items()},
        }
        return Reading(
            at=at,
            point=point,
            sources=sources,
            sessions=sessions,
            nzb=nzb,
            cores=cores,
            mem_total=mem_total,
        )


SAMPLER = Sampler()
