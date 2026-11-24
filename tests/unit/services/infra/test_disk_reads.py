"""A disk read for a request thread ends in time, whatever the mount does."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mediaman.services.infra.storage import disk_reads
from mediaman.services.infra.storage.disk_reads import DiskReads


@pytest.fixture
def release():
    """What a hung read waits for; set at the end so that no thread outlives the test."""
    event = threading.Event()
    yield event
    event.set()
    for thread in threading.enumerate():
        if thread.name == "disk-read":
            thread.join(5)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(disk_reads.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def short(monkeypatch):
    monkeypatch.setattr(disk_reads, "_DEADLINE_SECONDS", 0.1)


def _hang(release, calls):
    def read(path):
        calls.append(path)
        release.wait(30)
        return 1

    return read


def test_calls_for_one_hung_folder_share_one_read_and_one_deadline(release, short):
    reads, calls = DiskReads(4), []

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: reads.read("a", _hang(release, calls)), range(8)))

    assert results == [None] * 8
    assert calls == ["a"]


def test_several_hung_folders_are_waited_for_together(release, monkeypatch):
    monkeypatch.setattr(disk_reads, "_DEADLINE_SECONDS", 0.3)
    paths = [str(n) for n in range(6)]

    start = time.monotonic()
    assert set(DiskReads(8).read_all(paths, _hang(release, [])).values()) == {None}

    assert time.monotonic() - start < 1.2


def test_a_hung_folder_is_not_waited_for_again(release, monkeypatch):
    monkeypatch.setattr(disk_reads, "_DEADLINE_SECONDS", 0.3)
    reads = DiskReads(4)
    reads.read("a", _hang(release, []))

    start = time.monotonic()
    for _ in range(5):
        reads.read("a", _hang(release, []))

    assert time.monotonic() - start < 0.3


def test_a_healthy_folder_is_read_beside_a_hung_one(release, short):
    reads = DiskReads(4)
    reads.read("a", _hang(release, []))

    assert reads.read("b", lambda _: 5) == 5


def test_no_more_reads_hang_than_there_are_slots_and_the_rest_is_not_waited_for(
    release, monkeypatch
):
    monkeypatch.setattr(disk_reads, "_DEADLINE_SECONDS", 0.2)
    reads, calls = DiskReads(2), []
    reads.read_all(["a", "b"], _hang(release, calls))

    start = time.monotonic()
    assert reads.read("c", _hang(release, calls)) is None

    assert time.monotonic() - start < 0.2
    assert sorted(calls) == ["a", "b"]


def test_more_healthy_folders_than_slots_are_all_read():
    paths = [str(n) for n in range(9)]

    assert DiskReads(2).read_all(paths, int) == {path: int(path) for path in paths}


def test_a_failed_folder_is_left_alone_for_a_few_seconds(clock):
    reads, calls = DiskReads(2), []

    def fail(path):
        calls.append(path)
        raise OSError("stale mount")

    reads.read("a", fail)
    clock[0] += 2.9
    assert reads.read("a", fail) is None
    assert len(calls) == 1

    clock[0] += 0.2
    assert reads.read("a", lambda _: 5) == 5


def test_a_read_that_raises_anything_else_gives_its_slot_back(caplog):
    reads = DiskReads(1)

    def fail(_path):
        raise ValueError("embedded null byte")

    assert reads.read("a", fail) is None
    assert reads.read("b", lambda _: 5) == 5


def test_a_read_that_ends_too_late_counts_as_failed_then_is_tried_again(release, clock):
    reads, calls = DiskReads(2), []
    started = threading.Event()

    def slow(path):
        calls.append(path)
        started.set()
        release.wait(30)
        clock[0] += 5
        return 1

    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(reads.read, "a", slow)
        started.wait(5)
        release.set()
        assert result.result(5) is None

    assert reads.read("a", slow) is None
    assert calls == ["a"]

    clock[0] += 4
    assert reads.read("a", lambda _: 5) == 5


def test_a_hung_read_does_not_hold_the_program_open(release, short):
    DiskReads(1).read("a", _hang(release, []))

    assert [t.daemon for t in threading.enumerate() if t.name == "disk-read"] == [True]
