"""Tests for the codes TVs show to be paired."""

from __future__ import annotations

import pytest

from mediaman.services.tv import codes
from mediaman.services.tv.codes import CODE_SECONDS, PairingCodes, TooManyCodes


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
    code = store.issue("hash", None)
    assert store.look(code) is not None
    assert store.take(code) is not None
    assert store.take(code) is None
    assert store.seconds_left("hash") is None


def test_a_code_runs_out(store, clock):
    code = store.issue("hash", None)
    clock.now += CODE_SECONDS - 1
    assert store.seconds_left("hash") == 1
    clock.now += 1
    assert store.look(code) is None
    assert store.take(code) is None
    assert store.seconds_left("hash") is None


def test_a_new_code_for_a_paired_tv_ends_its_last_one(store):
    first, other = store.issue("a", 7), store.issue("b", None)
    second = store.issue("c", 7)
    assert store.look(first) is None
    assert store.look(other) is not None
    assert store.look(second) is not None


def test_only_so_many_codes_are_open_at_once(store, clock, monkeypatch):
    monkeypatch.setattr(codes, "MAX_OPEN", 2)
    store.issue("a", None)
    store.issue("b", None)
    with pytest.raises(TooManyCodes):
        store.issue("c", None)
    clock.now += CODE_SECONDS
    store.issue("c", None)


def test_the_code_itself_is_not_kept(store):
    code = store.issue("hash", None)
    assert code not in repr(vars(store))
