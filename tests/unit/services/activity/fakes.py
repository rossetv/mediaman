"""A made-up Plex, NZBGet and Docker proxy behind the fake HTTP transport, for Activity's tests."""

from __future__ import annotations

from mediaman.web.repository.settings import write_settings

PLEX = "http://plex.test:32400"
NZB = "http://nzbget.test:6789"
DOCKER = "http://docker.test:2375"
SONARR_ID = "a" * 64
RADARR_ID = "b" * 64
PLEX_ID = "c" * 64
OTHER_ID = "d" * 64

RESOURCES = """<MediaContainer size="2">
<StatisticsResources at="100" hostCpuUtilization="10.0" processCpuUtilization="1.0"
  hostMemoryUtilization="20.0" processMemoryUtilization="2.0" timespan="6"/>
<StatisticsResources at="106" hostCpuUtilization="79.5" processCpuUtilization="17.5"
  hostMemoryUtilization="50.0" processMemoryUtilization="10.0" timespan="6"/>
</MediaContainer>"""

SESSIONS = """<MediaContainer size="2">
<Video type="episode" title="The Long Night" grandparentTitle="Brass Sparrow" parentIndex="1" index="3">
  <User title="theo"/><Player title="iPhone" platform="iOS" product="Plex for iOS" local="0"/>
  <Session bandwidth="4000" location="wan"/><TranscodeSession videoDecision="transcode"/>
</Video>
<Video type="movie" title="Kestrel Point">
  <User title="rosa"/><Player title="Living room TV" platform="tvOS" local="1"/>
  <Session bandwidth="12000" location="lan"/>
</Video>
</MediaContainer>"""


def nzb_reply(method: str) -> dict[str, object]:
    if method == "status":
        return {"result": {"DownloadRate": 200_000_000}}
    return {
        "result": [
            {
                "NZBName": "Paper.Cranes.over.Dover.2024.1080p.WEB",
                "Status": "QUEUED",
                "FileSizeMB": 9600,
                "RemainingSizeMB": 9600,
            },
            {
                "NZBName": "Iron.Petal.Academy.S01.1080p.WEB-DL",
                "Status": "DOWNLOADING",
                "FileSizeMB": 1000,
                "RemainingSizeMB": 600,
            },
            {
                "NZBName": "The.Long.Coast.S03.1080p",
                "Status": "PP_QUEUED/UNPACKING",
                "FileSizeMB": 2000,
                "RemainingSizeMB": 0,
            },
        ]
    }


class Stack:
    """Answers as the three services would; a name in *down* does not answer, one in *hang*
    waits until the test ends, and one in *odd* answers with what that maps it to."""

    def __init__(self, fake_response) -> None:
        import threading

        self.fake_response = fake_response
        self.down: set[str] = set()
        self.hang: set[str] = set()
        self.odd: dict[str, object] = {}
        self.resources = RESOURCES
        self.released = threading.Event()
        self.calls: list[str] = []
        # Each container's CPU and the host's, in nanoseconds, as Docker counts them ever.
        self.cpu = {SONARR_ID: 1_000, RADARR_ID: 5_000, PLEX_ID: 0, OTHER_ID: 0}
        self.system = 100_000

    def __call__(self, method, url, **kwargs):
        import requests

        self.calls.append(url)
        service = next(
            k
            for k, base in (("plex", PLEX), ("nzbget", NZB), ("docker", DOCKER))
            if url.startswith(base)
        )
        if service in self.down:
            raise requests.ConnectionError("down")
        if service in self.hang:
            self.released.wait(10)
            raise requests.ConnectionError("cut")
        r = self.fake_response
        if service in self.odd:
            return self.odd[service]
        if service == "plex":
            if "/statistics/resources" in url:
                return r(content=self.resources.encode())
            return r(content=SESSIONS.encode())
        if service == "nzbget":
            return r(json_data=nzb_reply(kwargs["json"]["method"]))
        if url.endswith("/containers/json"):
            return r(
                json_data=[
                    {"Id": SONARR_ID, "Names": ["/sonarr"], "Image": "lscr.io/linuxserver/sonarr"},
                    {"Id": RADARR_ID, "Names": ["/radarr"], "Image": "lscr.io/linuxserver/radarr"},
                    {"Id": PLEX_ID, "Names": ["/plex"], "Image": "plexinc/pms-docker"},
                    {"Id": OTHER_ID, "Names": ["/homepage"], "Image": "ghcr.io/homepage"},
                    {"Id": "../../info", "Names": ["/sneaky"]},
                ]
            )
        cid = url.split("/containers/")[1].split("/")[0]
        return r(
            json_data={
                "cpu_stats": {
                    "cpu_usage": {"total_usage": self.cpu[cid]},
                    "system_cpu_usage": self.system,
                },
                "memory_stats": {"usage": 700 * 2**20, "stats": {"inactive_file": 100 * 2**20}},
            }
        )


def configure(
    conn, secret_key: str, *, docker: bool = True, nzbget: bool = True, plex: bool = True
) -> None:
    body: dict[str, object] = {}
    if plex:
        body |= {"plex_url": PLEX, "plex_token": "tok"}
    if nzbget:
        body |= {"nzbget_url": NZB, "nzbget_username": "u", "nzbget_password": "p"}
    if docker:
        body["docker_proxy_url"] = DOCKER
    write_settings(
        conn,
        body_dict=body,
        allowed_keys=list(body),
        secret_key=secret_key,
        now="2026-11-28T12:00:00+00:00",
    )
