"""One reading of every source, turned into rates and shares, and the last minute of them.

A rate needs two readings: the CPU of mediaman and of each container is worked out from the
one before. One sampler serves the whole process; a reading younger than *max_age* is handed
out again, so any number of open tabs cost one reading every two seconds.

The sources are read at once, and a reading takes no longer than ``BUDGET``: a source that has
not answered by then is down, and its calls are cut. Whoever asks while a reading is being
taken gets the one before at once. A source that fails, or is too slow, is left alone for a
while, so a dead one cannot slow every reading down.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

import requests

from mediaman.services.activity import cutoff
from mediaman.services.activity.cutoff import Cutoff
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
    SelfReading,
    own_cores,
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
from mediaman.services.media_meta.plex_watch import PlexError, PlexNotConfigured

logger = logging.getLogger(__name__)

LIVE_SECONDS = 60
LIVE_EVERY = 2
# Seconds a reading may take, all sources at once.
BUDGET = 2.2
# How long a source that failed is left alone before it is asked again.
RETRY_AFTER = 10.0
# Sessions of a busy server, and the resource figures, are small; anything bigger is not Plex.
PLEX_MAX_BYTES = 2 * 1024 * 1024
# Containers whose figures are asked for at once.
DOCKER_AT_ONCE = 4
_GIB = 1024**3


class _TooSlow(Exception):
    pass


# What a source may raise when it is down, slow, or answers with something it should not.
# ValueError covers unreadable JSON and XML, defusedxml's refusals among them; RecursionError,
# JSON nested too deep.
_DOWN = (
    _TooSlow,
    PlexError,
    NzbgetError,
    DockerUnavailable,
    SafeHTTPError,
    requests.RequestException,
    ConfigDecryptError,
    ValueError,
    RecursionError,
)

# A point: its time, the line down and up in bytes a second, the host's CPU and memory in
# percent of the server, Plex's memory in percent, and for each app [CPU % of the server,
# memory bytes]. Any figure may be None: not known.
Point = dict[str, object]


@dataclass(slots=True)
class Reading:
    at: float
    point: Point
    sources: dict[str, str]  # plex, nzbget, docker: "ok", "down" or "off"
    sessions: list[PlexSession] = field(default_factory=list)
    nzb: NzbReading | None = None
    cores: int | None = None  # the server's, as set in Settings
    mem_total: float | None = None  # bytes, as set in Settings


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


def _setting_number(conn: sqlite3.Connection, key: str) -> float | None:
    try:
        n = float(get_string_setting(conn, key))
    except ValueError:
        return None
    return n if n > 0 else None


def _read_plex(
    url: str, token: str, hosts: frozenset[str], session: requests.Session
) -> tuple[PlexResources, list[PlexSession]]:
    plex = PlexActivity(
        url,
        token,
        http=SafeHTTPClient(
            session=session,
            default_timeout=TIMEOUT,
            default_max_bytes=PLEX_MAX_BYTES,
            allowed_hosts=hosts,
        ),
    )
    return plex.resources(), plex.sessions()


def _read_docker(proxy: DockerProxy, cut: Cutoff, deadline: float) -> list[ContainerStats]:
    """The figures of every container but mediaman's, read a few at once; raises _TooSlow when
    they are not all in by *deadline*."""
    wanted = [(cid, app) for cid, app in proxy.containers() if app != "mediaman"]
    pool = ThreadPoolExecutor(max_workers=DOCKER_AT_ONCE, thread_name_prefix="activity-docker")
    try:
        futures = [pool.submit(cut.run, lambda c=c, a=a: proxy.stats(c, a)) for c, a in wanted]
        done, late = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if late:
        raise _TooSlow(f"{len(late)} of {len(futures)} containers")
    return [f.result() for f in futures]


class Sampler:
    def __init__(
        self,
        *,
        read_own: Callable[[], SelfReading] = read_self,
        cores: Callable[[], int] = own_cores,
        budget: float = BUDGET,
    ) -> None:
        self._lock = threading.Lock()  # held while a reading is taken
        self._state = threading.Lock()  # held only to hand over a reading
        self._read_own = read_own
        self._cores = cores
        self._budget = budget
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
        """The newest reading, taken now unless one younger than *max_age* seconds exists or
        one is being taken; only the very first reading is waited for."""
        at = time.time() if now is None else now
        last = self._last
        if last is not None and 0 <= at - last.at < max_age:
            return last
        if not self._lock.acquire(blocking=last is None):
            return last  # type: ignore[return-value]  # blocking when there is none
        try:
            last = self._last
            if last is not None and 0 <= at - last.at < max_age:
                return last
            reading = self._take(conn, secret_key, at)
            with self._state:
                self._last = reading
                self.live.append(reading.point)
                while self.live and at - float(self.live[0]["at"]) > LIVE_SECONDS + LIVE_EVERY:  # type: ignore[arg-type]
                    self.live.popleft()
            return reading
        finally:
            self._lock.release()

    def recent(self) -> list[Point]:
        """The last minute of readings, oldest first."""
        with self._state:
            return list(self.live)

    def _resting_now(self, source: str, at: float) -> bool:
        return at < self._resting.get(source, 0)

    def _failed(self, source: str, at: float, why: BaseException) -> str:
        self._resting[source] = at + RETRY_AFTER
        logger.warning("activity.source_down source=%s why=%s", source, type(why).__name__)
        return "down"

    def _take(self, conn: sqlite3.Connection, secret_key: str, at: float) -> Reading:
        deadline = time.monotonic() + self._budget
        hosts = allowed_outbound_hosts(conn)
        server_cores = _setting_number(conn, "server_cores")
        server_mem = _setting_number(conn, "server_memory_gb")
        sources: dict[str, str] = {}
        jobs: dict[str, Callable[[], object]] = {}
        cut = Cutoff()

        plex_url = get_string_setting(conn, "plex_url")
        if not plex_url:
            sources["plex"] = "off"
        elif self._resting_now("plex", at):
            sources["plex"] = "down"
        else:
            try:
                token = get_string_setting(conn, "plex_token", secret_key=secret_key)
            except ConfigDecryptError as exc:
                sources["plex"] = self._failed("plex", at, exc)
            else:
                jobs["plex"] = lambda: _read_plex(plex_url, token, hosts, cutoff.session())

        from mediaman.services.arr.build import build_nzbget_from_db

        try:
            client = build_nzbget_from_db(
                conn, secret_key, timeout=TIMEOUT, session=cutoff.session()
            )
        except ConfigDecryptError:
            client = None
        if client is None:
            sources["nzbget"] = "off"
        elif self._resting_now("nzbget", at):
            sources["nzbget"] = "down"
        else:
            jobs["nzbget"] = lambda: read_nzbget(client)

        docker_url = get_string_setting(conn, "docker_proxy_url")
        if not docker_url:
            sources["docker"] = "off"
            self._containers = {}
        elif self._resting_now("docker", at):
            sources["docker"] = "down"
        else:
            proxy = DockerProxy(docker_url, allowed_hosts=hosts, session=cutoff.session())
            jobs["docker"] = lambda: _read_docker(proxy, cut, deadline)

        got = self._gather(jobs, cut, deadline, sources, at)

        resources: PlexResources | None = None
        sessions: list[PlexSession] = []
        if "plex" in got:
            resources, sessions = got["plex"]  # type: ignore[misc]
        nzb: NzbReading | None = got.get("nzbget")  # type: ignore[assignment]

        cores = self._cores()
        # Shares of mediaman's own machine are shares of the server when it is set.
        scale = cores / server_cores if server_cores else 1.0
        apps: dict[str, list[float | None]] = {}
        own = self._read_own()
        own_cpu = None
        rx = tx = None
        if self._own is not None:
            was_at, was = self._own
            span = at - was_at
            if span > 0:
                own_cpu = max(0.0, (own.cpu_seconds - was.cpu_seconds) / (span * cores) * 100)
                own_cpu *= scale
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
            # Plex gives its memory only as a share of the server: it is kept as one.
            _add(apps, "plex", resources.plex_cpu, None)

        if "docker" in got:
            seen: dict[str, ContainerStats] = {}
            for stats in got["docker"]:  # type: ignore[attr-defined]
                seen[stats.cid] = stats
                was_stats = self._containers.get(stats.cid)
                cpu = None
                if was_stats is not None:
                    system = stats.system_total - was_stats.system_total
                    if system > 0:
                        cpu = max(0.0, (stats.cpu_total - was_stats.cpu_total) / system * 100)
                        cpu *= scale
                # Plex is read from Plex itself while it answers.
                if stats.app and not (stats.app == "plex" and resources is not None):
                    _add(apps, stats.app, cpu, stats.mem)
            self._containers = seen
        elif docker_url:
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
            "mem_pct": _pct(resources.host_mem if resources else None),
            "plex_mem_pct": _pct(resources.plex_mem if resources else None),
            "apps": {k: [_pct(cpu), _whole(mem)] for k, (cpu, mem) in apps.items()},
        }
        return Reading(
            at=at,
            point=point,
            sources=sources,
            sessions=sessions,
            nzb=nzb,
            cores=int(server_cores) if server_cores else None,
            mem_total=server_mem * _GIB if server_mem else None,
        )

    def _gather(
        self,
        jobs: dict[str, Callable[[], object]],
        cut: Cutoff,
        deadline: float,
        sources: dict[str, str],
        at: float,
    ) -> dict[str, object]:
        """Run *jobs* at once until *deadline*; what each answered, by source. Each source's
        state goes into *sources*: ok, off, or down when it failed or was not done in time."""
        if not jobs:
            return {}
        pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="activity")
        futures: dict[str, Future[object]] = {k: pool.submit(cut.run, fn) for k, fn in jobs.items()}
        try:
            wait(futures.values(), timeout=max(0.0, deadline - time.monotonic()))
        finally:
            # Calls still waiting are cut, so their threads end now rather than when the
            # source is done trickling.
            cut.cut()
            pool.shutdown(wait=False, cancel_futures=True)
        got: dict[str, object] = {}
        for source, future in futures.items():
            if not future.done():
                sources[source] = self._failed(source, at, _TooSlow())
                continue
            exc = future.exception()
            if exc is None:
                got[source] = future.result()
                sources[source] = "ok"
            elif isinstance(exc, PlexNotConfigured):
                sources[source] = "off"
            elif isinstance(exc, _DOWN):
                sources[source] = self._failed(source, at, exc)
            else:
                raise exc
        return got


SAMPLER = Sampler()
