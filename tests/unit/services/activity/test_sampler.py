"""Activity's readings: each source read from a fake, turned into rates and shares, and each
failure kept to its own source."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from mediaman.services.activity.sampler import RETRY_AFTER, Sampler
from mediaman.services.activity.sources import (
    DockerProxy,
    DockerUnavailable,
    SelfReading,
    app_of,
    read_self,
)
from mediaman.web.repository.settings import write_settings
from tests.unit.services.activity.fakes import (
    DOCKER,
    NZB,
    PLEX,
    PLEX_ID,
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
    yield s
    s.released.set()


@pytest.fixture
def own():
    return Own()


@pytest.fixture
def sampler(own):
    return Sampler(read_own=own, cores=lambda: 10, budget=0.3)


def server(conn, secret_key, **size: str) -> None:
    write_settings(
        conn, body_dict=size, allowed_keys=list(size), secret_key=secret_key, now="2026-11-28"
    )


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
    # The host and Plex as Plex gives them: shares of the server, whatever mediaman runs on.
    assert (p["cpu"], p["mem_pct"], p["plex_mem_pct"]) == (79.5, 50.0, 10.0)
    assert (r.cores, r.mem_total) == (None, None)
    apps = p["apps"]
    assert apps["plex"] == [17.5, None]  # from Plex itself, not its container
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
    assert r.point["cpu"] is None and r.point["mem_pct"] is None
    assert r.point["apps"]["plex"][1] == 600 * MB  # its container's memory


def _resources(*rows: str) -> str:
    return (
        "<MediaContainer>"
        + "".join(f"<StatisticsResources {r}/>" for r in rows)
        + "</MediaContainer>"
    )


FULL = 'at="100" hostCpuUtilization="10.0" hostMemoryUtilization="20.0" processCpuUtilization="1.0" processMemoryUtilization="2.0"'
NO_PLEX_CPU = 'at="106" hostCpuUtilization="79.5" hostMemoryUtilization="50.0" processMemoryUtilization="10.0"'


def test_when_plexs_newest_figures_lack_one_the_newest_full_row_is_read(
    conn, secret_key, stack, sampler
):
    configure(conn, secret_key, docker=False)
    stack.resources = _resources(NO_PLEX_CPU, FULL)
    r = sampler.read(conn, secret_key, now=1000.0)
    assert r.sources["plex"] == "ok"
    assert r.point["cpu"] == 10.0
    assert r.point["apps"]["plex"][0] == 1.0


def test_a_figure_plex_never_gives_is_unknown_and_plex_and_who_watches_stay(
    conn, secret_key, stack, sampler
):
    configure(conn, secret_key, docker=False)
    stack.resources = _resources(NO_PLEX_CPU, NO_PLEX_CPU.replace('at="106"', 'at="100"'))
    r = sampler.read(conn, secret_key, now=1000.0)
    assert r.sources["plex"] == "ok"
    assert len(r.sessions) == 2
    assert (r.point["cpu"], r.point["plex_mem_pct"]) == (79.5, 10.0)
    assert r.point["apps"]["plex"][0] is None
    assert not sampler._resting_now("plex", 1001.0)


def test_when_plex_leaves_out_its_own_cpu_its_container_stands_in(conn, secret_key, stack, sampler):
    configure(conn, secret_key)
    stack.resources = _resources(NO_PLEX_CPU)
    sampler.read(conn, secret_key, now=1000.0)
    stack.cpu[PLEX_ID] += 20_000
    stack.system += 100_000
    r = sampler.read(conn, secret_key, now=1002.0)
    assert r.point["apps"]["plex"][0] == pytest.approx(20.0)


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


def test_a_source_that_hangs_is_down_once_the_budget_is_spent_and_then_left_alone(
    conn, secret_key, stack, sampler
):
    configure(conn, secret_key)
    stack.hang.add("nzbget")
    started = time.monotonic()
    r = sampler.read(conn, secret_key, now=1000.0)
    assert time.monotonic() - started < 1.0  # the budget is 0.3 s; NZBGet would take 10
    assert r.sources == {"plex": "ok", "nzbget": "down", "docker": "ok"}
    asked = len([u for u in stack.calls if u.startswith(NZB)])
    sampler.read(conn, secret_key, now=1000.0 + RETRY_AFTER - 1)
    assert len([u for u in stack.calls if u.startswith(NZB)]) == asked


def test_whoever_asks_while_a_reading_is_taken_gets_the_one_before_at_once(
    conn, db_path, secret_key, stack, own
):
    configure(conn, secret_key)
    sampler = Sampler(read_own=own, cores=lambda: 10, budget=5.0)
    first = sampler.read(conn, secret_key, now=1000.0)
    stack.hang.add("plex")
    asked = len([u for u in stack.calls if u.startswith(PLEX)])
    other = sqlite3.connect(db_path, check_same_thread=False)
    other.row_factory = sqlite3.Row
    taking = threading.Thread(
        target=lambda: sampler.read(other, secret_key, now=1010.0, max_age=1), daemon=True
    )
    taking.start()
    for _ in range(300):
        if len([u for u in stack.calls if u.startswith(PLEX)]) > asked:
            break
        time.sleep(0.01)
    started = time.monotonic()
    assert sampler.read(conn, secret_key, now=1010.0, max_age=1) is first
    assert time.monotonic() - started < 0.1
    stack.released.set()
    taking.join(5)


@pytest.mark.parametrize(
    ("source", "reply"),
    [
        ("docker", "[" * 200_000),  # nested too deep for the JSON reader
        ("plex", 404),  # a server without /statistics/resources
        ("plex", "<MediaContainer/>"),  # no resource figures at all
        ("plex", '<!DOCTYPE x [<!ENTITY a "b">]><MediaContainer>&a;</MediaContainer>'),
    ],
)
def test_an_odd_reply_is_that_source_down_and_rested_not_an_error(
    conn, secret_key, stack, sampler, fake_response, source, reply
):
    configure(conn, secret_key)
    stack.odd[source] = (
        fake_response(status=reply) if isinstance(reply, int) else fake_response(text=reply)
    )
    r = sampler.read(conn, secret_key, now=1000.0)
    assert r.sources[source] == "down"
    assert sampler._resting_now(source, 1000.0 + RETRY_AFTER - 1)


def test_with_the_servers_size_set_its_cores_scale_each_apps_share(conn, secret_key, stack, own):
    # mediaman's machine (a VM) has 4 cores; the server it runs on has 8.
    sampler = Sampler(read_own=own, cores=lambda: 4)
    configure(conn, secret_key, docker=False)
    server(conn, secret_key, server_cores="8", server_memory_gb="16")
    sampler.read(conn, secret_key, now=1000.0)
    own.cpu += 2.0  # two seconds in two seconds: all of one core of the VM's four
    r = sampler.read(conn, secret_key, now=1002.0)
    assert r.point["apps"]["mediaman"][0] == pytest.approx(12.5)  # of the server's eight
    assert (r.cores, r.mem_total) == (8, 16 * GB)


def test_a_container_is_an_apps_by_its_image_or_its_own_name_not_a_part_of_either():
    assert app_of("lscr.io/linuxserver/sonarr:4.0", ["/tv"]) == "sonarr"
    assert app_of("plexinc/pms-docker@sha256:ab", ["/media"]) == "plex"
    assert app_of("ghcr.io/x/custom", ["/radarr"]) == "radarr"
    assert app_of("onedr0p/exportarr", ["/sonarr-exporter"]) == ""
    assert app_of("kometateam/plex-meta-manager", ["/pmm"]) == ""


def test_the_docker_proxys_address_cannot_move_a_request_off_its_paths(stack):
    DockerProxy(DOCKER + "/?x=#y").containers()
    assert stack.calls[-1] == DOCKER + "/containers/json"


def test_on_cgroup_v1_memory_leaves_out_the_inactive_page_cache(stack, fake_response):
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 1}, "system_cpu_usage": 1},
        "memory_stats": {
            "usage": 700 * MB,
            "stats": {"total_inactive_file": 100 * MB, "cache": 300 * MB},
        },
    }
    stack.odd["docker"] = fake_response(json_data=stats)
    assert DockerProxy(DOCKER).stats(SONARR_ID, "sonarr").mem == 600 * MB


def test_mediamans_own_memory_leaves_out_the_inactive_page_cache(tmp_path):
    (tmp_path / "memory.current").write_text("151000000\n")
    (tmp_path / "memory.stat").write_text("anon 1\ninactive_file 51000000\n")
    assert read_self(tmp_path, tmp_path / "none").mem == 100_000_000


def test_a_docker_address_that_cannot_be_read_is_docker_down(conn, secret_key, stack, sampler):
    configure(conn, secret_key, docker=False)
    body = {"docker_proxy_url": "http://docker.test:99999"}  # no such port
    write_settings(conn, body_dict=body, allowed_keys=list(body), secret_key=secret_key, now="x")
    r = sampler.read(conn, secret_key, now=1000.0)
    assert r.sources == {"plex": "ok", "nzbget": "ok", "docker": "down"}
