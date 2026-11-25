"""Tests for the codes TVs show to be paired."""

from __future__ import annotations

import pytest

from mediaman.services.tv import codes
from mediaman.services.tv.codes import CODE_SECONDS, PairingCodes

HOME = "192.0.2.7"


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock) -> PairingCodes:
    return PairingCodes(clock=clock)


def test_a_code_is_used_up_by_the_first_who_takes_it(store):
    code = store.issue("hash", None, HOME)
    assert store.look(code) is not None
    assert store.claim(code) is not None
    assert store.claim(code) is None
    store.end(code)
    assert store.claim(code) is None
    assert store.seconds_left("hash") is None


def test_a_code_being_paired_is_nobody_elses_and_neither_runs_out_nor_gives_way(
    store, clock, monkeypatch
):
    monkeypatch.setattr(codes, "MAX_OPEN", 2)
    code = store.issue("hash", 7, HOME)
    store.claim(code)
    assert store.look(code) is None
    store.issue("again", 7, HOME)
    for n in range(3):
        store.issue(f"a{n}", None, HOME)
    clock.now += CODE_SECONDS + 1
    assert store.seconds_left("hash") == 1
    store.release(code)
    assert store.seconds_left("hash") is None


def test_a_code_given_back_can_be_taken_again(store):
    code = store.issue("hash", None, HOME)
    store.claim(code)
    store.release(code)
    assert store.claim(code) is not None


def test_a_code_runs_out(store, clock):
    code = store.issue("hash", None, HOME)
    clock.now += CODE_SECONDS - 1
    assert store.seconds_left("hash") == 1
    clock.now += 1
    assert store.look(code) is None
    assert store.claim(code) is None
    assert store.seconds_left("hash") is None


def test_a_new_code_for_a_paired_tv_ends_its_last_one(store):
    first, other = store.issue("a", 7, HOME), store.issue("b", None, HOME)
    second = store.issue("c", 7, HOME)
    assert store.look(first) is None
    assert store.look(other) is not None
    assert store.look(second) is not None


def test_whoever_asks_for_many_codes_ends_their_own_and_nobody_elses(store, monkeypatch):
    monkeypatch.setattr(codes, "MAX_OPEN", 5)
    mine = store.issue("mine", None, HOME)
    first = store.issue("a", None, "198.51.100.1")
    others = [store.issue(f"b{n}", None, f"203.{n}.0.1") for n in range(3)]
    theirs = [store.issue(f"c{n}", None, f"198.51.100.{n}") for n in range(2)]
    assert store.look(first) is None
    assert store.look(theirs[0]) is None, "its second, of a block that has the most"
    for code in (mine, *others, theirs[1]):
        assert store.look(code) is not None


@pytest.mark.parametrize(
    "line",
    [
        [f"2001:db8:7:{n:x}::1" for n in range(8)],
        [f"198.51.{n}.1" for n in range(8)],
        [f"::ffff:198.51.{n}.1" for n in range(8)],
    ],
    ids=["IPv6", "IPv4", "IPv4 in IPv6"],
)
def test_one_lines_many_blocks_end_their_own_codes_first(store, monkeypatch, line):
    monkeypatch.setattr(codes, "MAX_OPEN", 4)
    mine = store.issue("mine", None, HOME)
    other = store.issue("other", None, "2001:db8:8::1")
    theirs = [store.issue(f"c{n}", None, address) for n, address in enumerate(line)]
    for code in (mine, other, *theirs[-2:]):
        assert store.look(code) is not None
    assert [c for c in theirs if store.look(c) is not None] == theirs[-2:]


def test_homes_behind_a_listener_of_both_kinds_are_not_taken_for_one_line(store, monkeypatch):
    monkeypatch.setattr(codes, "MAX_OPEN", 3)
    mine = store.issue("mine", None, f"::ffff:{HOME}")
    for n in range(4):
        store.issue(f"c{n}", None, f"::ffff:198.51.100.{n}")
    assert store.look(mine) is not None


def test_within_a_line_the_block_with_the_most_open_gives_way(store, monkeypatch):
    monkeypatch.setattr(codes, "MAX_OPEN", 4)
    neighbour = store.issue("n", None, "2001:db8:7:1::1")
    many = [store.issue(f"c{n}", None, f"2001:db8:7:2::{n}") for n in range(3)]
    store.issue("new", None, "2001:db8:7:2::9")
    assert store.look(neighbour) is not None
    assert store.look(many[0]) is None


def test_with_every_place_taken_by_as_many_addresses_a_new_code_is_still_given(store, monkeypatch):
    monkeypatch.setattr(codes, "MAX_OPEN", 3)
    first, *rest = [store.issue(f"a{n}", None, f"203.{n}.0.1") for n in range(3)]
    new = store.issue("new", None, HOME)
    assert store.look(first) is None
    for code in (*rest, new):
        assert store.look(code) is not None


def test_the_code_itself_is_not_kept(store):
    code = store.issue("hash", None, HOME)
    assert code not in repr(vars(store))
