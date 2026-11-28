"""What each source says about the server right now, read with GET requests only.

Plex gives the host's CPU and memory and who is watching, NZBGet the download speed and
queue, a read-only Docker socket proxy each container's CPU and memory, and mediaman reads
its own from its cgroup. Each reader returns plain values or raises the source's own error;
none of them keeps state.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

import requests

from mediaman.services.downloads.download_format import parse_clean_title
from mediaman.services.downloads.nzbget import NzbgetClient, NzbgetError
from mediaman.services.infra import SafeHTTPClient, SafeHTTPError
from mediaman.services.media_meta.plex_watch import PlexUnavailable, PlexWatch

# A live tab asks every two seconds: a source that does not answer quickly is down for now.
TIMEOUT: tuple[float, float] = (1.5, 2.5)
_DOCKER_MAX_BYTES = 1024 * 1024
_MB = 1024 * 1024
_TEXT_MAX = 120
# The apps told apart by name. Anything else running counts as everything else.
APPS = ("plex", "nzbget", "sonarr", "radarr", "mediaman")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}")
_MAX_CONTAINERS = 40


class DockerUnavailable(Exception):
    """The Docker proxy cannot be reached or sent something unreadable."""


def _num(value: object) -> float | None:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: str | None) -> str:
    return (value or "").strip()[:_TEXT_MAX]


# ── Plex ──


@dataclass(frozen=True, slots=True)
class PlexResources:
    host_cpu: float  # % of the whole host
    host_mem: float  # % of the host's memory
    plex_cpu: float
    plex_mem: float


@dataclass(frozen=True, slots=True)
class PlexSession:
    user: str
    device: str
    kind: str  # tv, phone, tablet, computer or web: the icon it is shown with
    title: str
    transcode: bool
    local: bool
    rate: float  # bytes a second


def _device_kind(player: Element | None) -> str:
    if player is None:
        return "tv"
    said = " ".join(
        (player.get(k) or "") for k in ("platform", "product", "device", "model")
    ).lower()
    if "tv" in said or any(
        w in said for w in ("roku", "chromecast", "shield", "xbox", "playstation")
    ):
        return "tv"
    if "ipad" in said or "tablet" in said:
        return "tablet"
    if any(w in said for w in ("iphone", "ios", "android")):
        return "phone"
    if any(w in said for w in ("chrome", "safari", "firefox", "edge", "web")):
        return "web"
    if any(w in said for w in ("mac", "windows", "linux")):
        return "computer"
    return "tv"


def _session_title(node: Element) -> str:
    title = _text(node.get("title"))
    show = _text(node.get("grandparentTitle"))
    if node.get("type") == "episode" and show:
        season, episode = node.get("parentIndex"), node.get("index")
        where = f"S{season} E{episode}" if season and episode else ""
        return " · ".join(p for p in (show, where) if p)
    return title


class PlexActivity(PlexWatch):
    """The two reads Activity makes of Plex. Raises PlexUnavailable when Plex does not answer."""

    def resources(self) -> PlexResources:
        """The newest reading of the host's and Plex's own CPU and memory, in percent."""
        rows = self._xml("/statistics/resources?timespan=6").findall("StatisticsResources")
        if not rows:
            raise PlexUnavailable("Plex sent no resource figures")
        newest = max(rows, key=lambda r: _num(r.get("at")) or 0)
        values = [
            _num(newest.get(k))
            for k in (
                "hostCpuUtilization",
                "hostMemoryUtilization",
                "processCpuUtilization",
                "processMemoryUtilization",
            )
        ]
        if any(v is None for v in values):
            raise PlexUnavailable("Plex sent unreadable resource figures")
        host_cpu, host_mem, plex_cpu, plex_mem = (min(100.0, max(0.0, v or 0)) for v in values)
        return PlexResources(host_cpu, host_mem, plex_cpu, plex_mem)

    def sessions(self) -> list[PlexSession]:
        """Who is watching now, on what, and how much of the line each takes."""
        out: list[PlexSession] = []
        for node in self._xml("/status/sessions"):
            if node.tag not in ("Video", "Track"):
                continue
            user = node.find("User")
            player = node.find("Player")
            session = node.find("Session")
            transcoding = node.find("TranscodeSession")
            decision = (transcoding.get("videoDecision") or "") if transcoding is not None else ""
            kbps = _num(session.get("bandwidth")) if session is not None else None
            if kbps is None:
                media = node.find("Media")
                kbps = _num(media.get("bitrate")) if media is not None else None
            local = (player is not None and player.get("local") == "1") or (
                session is not None and session.get("location") == "lan"
            )
            out.append(
                PlexSession(
                    user=_text(user.get("title") if user is not None else "") or "Someone",
                    device=_text(
                        (player.get("title") or player.get("product")) if player is not None else ""
                    )
                    or "A player",
                    kind=_device_kind(player),
                    title=_session_title(node),
                    transcode=transcoding is not None and decision == "transcode",
                    local=local,
                    rate=max(0.0, (kbps or 0) * 1000 / 8),
                )
            )
        return out


# ── NZBGet ──


@dataclass(frozen=True, slots=True)
class NzbItem:
    title: str
    state: str  # downloading, queued, paused, repairing, unpacking or finishing
    done: float  # bytes
    size: float


@dataclass(frozen=True, slots=True)
class NzbReading:
    rate: float  # bytes a second
    left: float  # bytes still to come, the whole queue
    items: list[NzbItem] = field(default_factory=list)


def _nzb_state(status: str) -> str:
    s = status.upper()
    if "UNPACK" in s:
        return "unpacking"
    if any(w in s for w in ("REPAIR", "VERIFY", "LOADING_PARS")):
        return "repairing"
    if s.startswith("PP_") or any(w in s for w in ("MOVING", "RENAMING", "EXECUTING")):
        return "finishing"
    if "PAUSED" in s:
        return "paused"
    if s in ("DOWNLOADING", "FETCHING"):
        return "downloading"
    return "queued"


_SEASON_RE = re.compile(r"[ ._-]S(\d{1,2})(?:E(\d{1,3}))?(?=[ ._-]|$)", re.IGNORECASE)


def _nzb_title(name: str) -> str:
    """A release's title as people say it, with its season or episode."""
    title = _text(parse_clean_title(name)) or "A download"
    found = _SEASON_RE.search(name)
    if not found:
        return title
    season, episode = int(found[1]), found[2]
    return f"{title} · " + (f"S{season} E{int(episode)}" if episode else f"Season {season}")


def read_nzbget(client: NzbgetClient) -> NzbReading:
    """NZBGet's speed and queue; raises what the client raises when NZBGet does not answer."""
    status = client.get_status()
    groups = client.get_queue()
    rate = _num(status.get("DownloadRate")) or 0.0
    items: list[NzbItem] = []
    left = 0.0
    for group in groups:
        size = (_num(group.get("FileSizeMB")) or 0) * _MB
        remain = max(0.0, (_num(group.get("RemainingSizeMB")) or 0) * _MB)
        left += remain
        name = group.get("NZBName")
        items.append(
            NzbItem(
                title=_nzb_title(name if isinstance(name, str) else ""),
                state=_nzb_state(str(group.get("Status") or "")),
                done=max(0.0, size - remain),
                size=size,
            )
        )
    return NzbReading(rate=max(0.0, rate), left=left, items=items)


# ── Docker, through a read-only proxy ──


@dataclass(frozen=True, slots=True)
class ContainerStats:
    app: str  # one of APPS, or "" for any other container
    cpu_total: float  # nanoseconds of CPU, ever
    system_total: float  # nanoseconds of the host's CPUs, ever
    mem: float  # bytes in use, page cache left out


def app_of(name: str) -> str:
    low = name.lower()
    return next((app for app in APPS if app in low), "")


class DockerProxy:
    """Reads the running containers and their figures through a socket proxy that allows only
    GET /containers/json and GET /containers/{id}/stats."""

    def __init__(self, url: str, *, allowed_hosts: frozenset[str] | None = None) -> None:
        self._http = SafeHTTPClient(
            url.rstrip("/"),
            default_timeout=TIMEOUT,
            default_max_bytes=_DOCKER_MAX_BYTES,
            allowed_hosts=allowed_hosts,
        )

    def _json(self, path: str) -> Any:
        try:
            resp = self._http.get(path, retry=False)
        except (SafeHTTPError, requests.RequestException) as exc:
            raise DockerUnavailable("The Docker proxy could not be reached") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise DockerUnavailable("The Docker proxy sent something unreadable") from exc

    def containers(self) -> list[tuple[str, str]]:
        """(id, app) of each running container, as far as the cap goes."""
        listed = self._json("/containers/json")
        if not isinstance(listed, list):
            raise DockerUnavailable("The Docker proxy sent no list of containers")
        out: list[tuple[str, str]] = []
        for c in listed[:_MAX_CONTAINERS]:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("Id") or "")
            if not _CONTAINER_ID_RE.fullmatch(cid):
                continue
            names = c.get("Names")
            names = names if isinstance(names, list) else []
            name = " ".join(str(n) for n in names) + " " + str(c.get("Image") or "")
            out.append((cid, app_of(name)))
        return out

    def stats(self, cid: str, app: str) -> ContainerStats:
        if not _CONTAINER_ID_RE.fullmatch(cid):
            raise DockerUnavailable("Not a container id")
        data = self._json(f"/containers/{cid}/stats?stream=false&one-shot=true")
        if not isinstance(data, dict):
            raise DockerUnavailable("The Docker proxy sent unreadable figures")
        cpu = _obj(data.get("cpu_stats"))
        usage = _obj(cpu.get("cpu_usage"))
        mem = _obj(data.get("memory_stats"))
        detail = _obj(mem.get("stats"))
        cache = _num(detail.get("inactive_file", detail.get("cache"))) or 0
        used = max(0.0, (_num(mem.get("usage")) or 0) - cache)
        return ContainerStats(
            app=app,
            cpu_total=_num(usage.get("total_usage")) or 0,
            system_total=_num(cpu.get("system_cpu_usage")) or 0,
            mem=used,
        )

    def check_reachable(self) -> None:
        """Raise DockerUnavailable unless the proxy lists the containers."""
        self.containers()


# ── mediaman itself ──


@dataclass(frozen=True, slots=True)
class SelfReading:
    cpu_seconds: float
    mem: float | None  # bytes
    rx: float | None  # bytes received, ever
    tx: float | None


_CGROUP = Path("/sys/fs/cgroup")
_NET_DEV = Path("/proc/net/dev")


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _cgroup_cpu(root: Path) -> float | None:
    stat = _read(root / "cpu.stat")
    if stat:
        for line in stat.splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec" and value.strip().isdigit():
                return int(value) / 1e6
    v1 = _read(root / "cpuacct" / "cpuacct.usage")
    return int(v1) / 1e9 if v1 and v1.strip().isdigit() else None


def _cgroup_mem(root: Path) -> float | None:
    for path in (root / "memory.current", root / "memory" / "memory.usage_in_bytes"):
        raw = _read(path)
        if raw and raw.strip().isdigit():
            return float(raw)
    return None


def _net(path: Path) -> tuple[float | None, float | None]:
    raw = _read(path)
    if not raw:
        return None, None
    rx = tx = 0.0
    for line in raw.splitlines()[2:]:
        name, _, rest = line.partition(":")
        cols = rest.split()
        if name.strip() == "lo" or len(cols) < 9:
            continue
        rx += _num(cols[0]) or 0
        tx += _num(cols[8]) or 0
    return rx, tx


def read_self(root: Path = _CGROUP, net_dev: Path = _NET_DEV) -> SelfReading:
    """mediaman's own CPU time, memory and traffic: its container's when it runs in one."""
    cpu = _cgroup_cpu(root)
    mem = _cgroup_mem(root)
    if cpu is None:
        cpu = time.process_time()
    if mem is None:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mem = float(peak if sys.platform == "darwin" else peak * 1024)
    rx, tx = _net(net_dev)
    return SelfReading(cpu_seconds=cpu, mem=mem, rx=rx, tx=tx)


def host_size() -> tuple[int, float | None]:
    """The host's cores and memory in bytes, as mediaman sees them."""
    try:
        mem = float(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        mem = None
    return os.cpu_count() or 1, mem


__all__ = [
    "APPS",
    "ContainerStats",
    "DockerProxy",
    "DockerUnavailable",
    "NzbItem",
    "NzbReading",
    "NzbgetError",
    "PlexActivity",
    "PlexResources",
    "PlexSession",
    "PlexUnavailable",
    "SelfReading",
    "app_of",
    "host_size",
    "read_nzbget",
    "read_self",
]
