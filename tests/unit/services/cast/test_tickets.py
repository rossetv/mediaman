"""Tests for the tickets a TV reads its stream with."""

from __future__ import annotations

import pytest

from mediaman.services.cast.tickets import TICKET_SECONDS, TicketStore


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock) -> TicketStore:
    return TicketStore(clock=clock)


def _issue(store: TicketStore, stream: str = "s1", live=lambda t: True) -> str:
    return store.issue(stream, "alice", "session", live=live)


def test_a_ticket_reads_its_own_stream_only(store):
    first, second = _issue(store, "s1"), _issue(store, "s2")
    assert store.read(first).stream_id == "s1"
    assert store.read(second).stream_id == "s2"


def test_a_ticket_runs_out(store, clock):
    token = _issue(store)
    clock.now += TICKET_SECONDS - 1
    assert store.read(token) is not None
    clock.now += 1
    assert store.read(token) is None


@pytest.mark.parametrize("token", ["", "x" * 43, "short", "é" * 43, "a/b"])
def test_a_made_up_ticket_reads_nothing(store, token):
    _issue(store)
    assert store.read(token) is None


def test_the_store_never_holds_the_ticket_itself(store):
    token = _issue(store)
    assert token not in repr(vars(store))


def test_tickets_of_ended_streams_are_forgotten_at_the_next_issue(store):
    _issue(store, "ended")
    _issue(store, "new", live=lambda t: t.stream_id != "ended")
    assert "ended" not in repr(vars(store))
