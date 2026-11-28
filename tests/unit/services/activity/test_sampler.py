"""Activity's readings: each source read from a fake, turned into rates and shares, and each
failure kept to its own source."""

from __future__ import annotations

import pytest

from mediaman.services.activity.sampler import RETRY_AFTER, Sampler
from mediaman.services.activity.sources import (
    DockerProxy,
    DockerUnavailable,
    SelfReading,
    read_self,
)
from tests.unit.services.activity.fakes import (
    DOCKER,
    NZB,
    SONARR_ID,
    Stack,
    configure,
)

GB = 2**30
MB = 2**20


class Own:
    """mediaman's own counters, stepped by the test."""

    def __init__(self) -> None:
        self.cpu = 10.0
        self.rx = 0.0
        self.tx = 0.0

    def __call__(self) -> SelfReading:
        return SelfReading(cpu_seconds=self.cpu, mem=144 * MB, rx=self.rx, tx=self.tx)


@pytest.fixture
def stack(fake_http, fake_response):
    s = Stack(fake_response)
    fake_http.handler(s)
    return s


@pytest.fixture
def own():
    return Own()


@pytest.fixture
def sampler(own):
    return Sampler(read_own=own, size=lambda: (10, 16 * GB))


def test_a_reading_has_the_host_from_plex_and_each_app_by_its_own_source(
    conn, secret_key, stack, sampler, own
):
    configure(conn, secret_key)
    sampler.read(conn, secret_key, now=1000.0)
    own.cpu += 2.0  # two CPU seconds in two seconds, of ten cores: 10%
    own.rx += 4_000_000
    own.tx += 2_000_000
    stack.cpu[SONARR_ID] += 3_000
    stack.system += 100_000
    r = sampler.read(conn, secret_key, now=1002.0)

    p = r.point
    assert r.sources == {"plex": "ok", "nzbget": "ok", "docker": "ok"}
    assert p["cpu"] == 79.5
    assert p["mem"] == pytest.approx(8 * GB)  # half of 16 GB
    apps = p["apps"]
    assert apps["plex"] == [17.5, pytest.approx(1.6 * GB)]  # from Plex itself, not its container
    assert apps["mediaman"][0] == pytest.approx(10.0)
    assert apps["sonarr"] == [pytest.approx(3.0), 600 * MB]  # page cache left out
    assert "" not in apps  # a container of no known app is everything else
    # Down: NZBGet's speed and mediaman's own; up: what Plex streams and mediaman sends.
    assert p["down"] == pytest.approx(200_000_000 + 2_000_000)
    assert p["up"] == pytest.approx((4000 + 12000) * 1000 / 8 + 1_000_000)


def test_who_is_watching_says_the_episode_the_device_and_how_it_plays(
    conn, secret_key, stack, sampler
):
    configure(conn, secret_key, docker=False, nzbget=False)
    theo, rosa = sampler.read(conn, secret_key, now=1000.0).sessions
    assert (theo.user, theo.device, theo.kind, theo.title) == (
        "theo",
        "iPhone",
        "phone",
        "Brass Sparrow · S1 E3",
    )
    assert (theo.transcode, theo.local, theo.rate) == (True, False, 500_000)
    assert (rosa.kind, rosa.title, rosa.transcode, rosa.local) == (
        "tv",
        "Kestrel Point",
        False,
        True,
    )


def test_the_downloads_are_named_cleanly_and_say_what_each_is_doing(
    conn, secret_key, stack, sampler
):
    configure(conn, secret_key, docker=False, plex=False)
    nzb = sampler.read(conn, secret_key, now=1000.0).nzb
    assert [(i.title, i.state) for i in nzb.items] == [
        ("Paper Cranes over Dover", "queued"),
        ("Iron Petal Academy · Season 1", "downloading"),
        ("The Long Coast · Season 3", "unpacking"),
    ]
    assert nzb.left == (9600 + 600) * MB


def test_a_source_that_does_not_answer_leaves_the_others_and_is_left_alone_a_while(
    conn, secret_key, stack, sampler
):
    configure(conn, secret_key)
    stack.down.add("nzbget")
    r = sampler.read(conn, secret_key, now=1000.0)
    assert r.sources == {"plex": "ok", "nzbget": "down", "docker": "ok"}
    assert r.point["dl"] is None and r.point["cpu"] == 79.5

    stack.down.clear()
    asked = len([u for u in stack.calls if u.startswith(NZB)])
    assert sampler.read(conn, secret_key, now=1000.0 + RETRY_AFTER - 1).sources["nzbget"] == "down"
    assert len([u for u in stack.calls if u.startswith(NZB)]) == asked
    assert sampler.read(conn, secret_key, now=1000.0 + RETRY_AFTER + 1).sources["nzbget"] == "ok"


def test_plex_down_leaves_the_host_unknown_and_docker_stands_in_for_plex(
    conn, secret_key, stack, sampler
):
    configure(conn, secret_key)
    stack.down.add("plex")
    r = sampler.read(conn, secret_key, now=1000.0)
    assert r.sources["plex"] == "down"
    assert r.point["cpu"] is None and r.point["mem"] is None
    assert r.point["apps"]["plex"][1] == 600 * MB  # its container's memory


def test_without_a_docker_proxy_set_nothing_is_asked_of_docker(conn, secret_key, stack, sampler):
    configure(conn, secret_key, docker=False)
    r = sampler.read(conn, secret_key, now=1000.0)
    assert r.sources["docker"] == "off"
    assert not [u for u in stack.calls if u.startswith(DOCKER)]
    assert set(r.point["apps"]) == {"plex", "mediaman"}


def test_tabs_asking_at_once_share_one_reading(conn, secret_key, stack, sampler):
    configure(conn, secret_key)
    first = sampler.read(conn, secret_key, now=1000.0)
    calls = len(stack.calls)
    assert sampler.read(conn, secret_key, now=1001.0) is first
    assert len(stack.calls) == calls
    assert len(sampler.live) == 1


def test_the_proxy_is_asked_only_for_container_ids(stack):
    proxy = DockerProxy(DOCKER)
    assert len(proxy.containers()) == 4  # "../../info" is no container id
    with pytest.raises(DockerUnavailable):
        proxy.stats("../../info", "")
    assert not [u for u in stack.calls if "info" in u]


def test_a_docker_proxy_that_does_not_answer_is_unavailable(stack):
    stack.down.add("docker")
    with pytest.raises(DockerUnavailable):
        DockerProxy(DOCKER).containers()


def test_mediaman_reads_its_own_cgroup_and_its_traffic_outside_loopback(tmp_path):
    (tmp_path / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 1\n")
    (tmp_path / "memory.current").write_text("151000000\n")
    net = tmp_path / "dev"
    net.write_text(
        "Inter-|   Receive\n face |bytes ...\n"
        "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
        "  eth0: 1200 5 0 0 0 0 0 0 340 4 0 0 0 0 0 0\n"
    )
    own = read_self(tmp_path, net)
    assert (own.cpu_seconds, own.mem, own.rx, own.tx) == (2.5, 151_000_000, 1200, 340)
