"""What each source says about the server right now. Plex and Docker are read with GET
requests only, NZBGet with its read-only JSON-RPC methods (POSTs); none of them is changed.

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
from urllib.parse import urlsplit
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
# The apps told apart. Anything else running counts as everything else.
APPS = ("plex", "nzbget", "sonarr", "radarr", "mediaman")
# A container is an app's when its image is named for it (the last part of the image's name,
# without its tag), or failing that when the container is named after the app.
_IMAGES = {
    "plex": "plex",
    "pms-docker": "plex",
    "plex-media-server": "plex",
    "nzbget": "nzbget",
    "sonarr": "sonarr",
    "radarr": "radarr",
    "mediaman": "mediaman",
}
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


_PLEX_FIGURES = (
    "hostCpuUtilization",
    "hostMemoryUtilization",
    "processCpuUtilization",
    "processMemoryUtilization",
)


@dataclass(frozen=True, slots=True)
class PlexResources:
    # Each in percent, None when Plex did not give it.
    host_cpu: float | None  # of the whole host
    host_mem: float | None  # of the host's memory
    plex_cpu: float | None
    plex_mem: float | None


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
        # Plex often leaves a figure out of its newest rows for a few seconds at a time: the
        # newest row that has them all is read, else the newest with what it lacks unknown.
        readings = [
            [_num(row.get(k)) for k in _PLEX_FIGURES]
            for row in sorted(rows, key=lambda r: _num(r.get("at")) or 0, reverse=True)
        ]
        values = next((v for v in readings if None not in v), readings[0])
        host_cpu, host_mem, plex_cpu, plex_mem = (
            None if v is None else min(100.0, max(0.0, v)) for v in values
        )
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
    by: str = ""  # the app that imports it once it is done: sonarr, radarr or "" if not known


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


def _importer(category: str, name: str) -> str:
    """Which app takes a download in: as its category says, else Sonarr for a season or an
    episode."""
    c = category.lower()
    if any(w in c for w in ("sonarr", "tv", "series", "show")):
        return "sonarr"
    if any(w in c for w in ("radarr", "movie", "film")):
        return "radarr"
    return "sonarr" if _SEASON_RE.search(name) else ""


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
        name = name if isinstance(name, str) else ""
        items.append(
            NzbItem(
                title=_nzb_title(name),
                state=_nzb_state(str(group.get("Status") or "")),
                done=max(0.0, size - remain),
                size=size,
                by=_importer(str(group.get("Category") or ""), name),
            )
        )
    return NzbReading(rate=max(0.0, rate), left=left, items=items)


# ── Docker, through a read-only proxy ──


@dataclass(frozen=True, slots=True)
class ContainerStats:
    cid: str
    app: str  # one of APPS, or "" for any other container
    cpu_total: float  # nanoseconds of CPU, ever
    system_total: float  # nanoseconds of the host's CPUs, ever
    mem: float  # bytes in use, page cache left out


def app_of(image: str, names: list[str]) -> str:
    """The app a container runs, from its image's name or its own; "" for any other."""
    repo = image.lower().split("@")[0].rsplit("/", 1)[-1].split(":")[0]
    if repo in _IMAGES:
        return _IMAGES[repo]
    return next((n.strip("/").lower() for n in names if n.strip("/").lower() in APPS), "")


def _base(url: str) -> str:
    """The proxy's address from its scheme, host, port and path alone, so nothing typed after
    them can move a request off the paths asked for."""
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{parts.scheme}://{host}{port}{parts.path.rstrip('/')}"


class DockerProxy:
    """Reads the running containers and their figures through a socket proxy that allows only
    GET /containers/json and GET /containers/{id}/stats."""

    def __init__(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._http = SafeHTTPClient(
            _base(url),
            session=session,
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
        except (ValueError, RecursionError) as exc:
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
            names = [str(n) for n in names] if isinstance(names, list) else []
            out.append((cid, app_of(str(c.get("Image") or ""), names)))
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
        # The page cache the kernel can drop at once, as `docker stats` leaves it out: cgroup v2
        # calls it inactive_file, v1 total_inactive_file.
        cache = _num(detail.get("inactive_file", detail.get("total_inactive_file"))) or 0
        used = max(0.0, (_num(mem.get("usage")) or 0) - cache)
        return ContainerStats(
            cid=cid,
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


def _inactive_file(stat: str | None, key: str) -> float:
    for line in (stat or "").splitlines():
        name, _, value = line.partition(" ")
        if name == key and value.strip().isdigit():
            return float(value)
    return 0.0


def _cgroup_mem(root: Path) -> float | None:
    """Memory in use, less the page cache the kernel can drop at once, as for a container."""
    for usage, stat, key in (
        (root / "memory.current", root / "memory.stat", "inactive_file"),
        (
            root / "memory" / "memory.usage_in_bytes",
            root / "memory" / "memory.stat",
            "total_inactive_file",
        ),
    ):
        raw = _read(usage)
        if raw and raw.strip().isdigit():
            return max(0.0, float(raw) - _inactive_file(_read(stat), key))
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


def own_cores() -> int:
    """The cores of the machine mediaman and the containers run on: a VM's, on a Mac."""
    return os.cpu_count() or 1


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
    "own_cores",
    "read_nzbget",
    "read_self",
]
