"""Tests for what plays on the TVs."""

from __future__ import annotations

import pytest

from mediaman.services.tv.players import (
    ASK_AFTER_EPISODES,
    ASK_AFTER_SECONDS,
    COUNTDOWN_SECONDS,
    GONE_KEEP_SECONDS,
    HOLD_SECONDS,
    STILL_SECONDS,
    Choice,
    Left,
    NotPlaying,
    Play,
    Players,
    Report,
    Stale,
    TvOff,
)

TV = "token-hash"


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def players(clock) -> Players:
    return Players(clock=clock)


def _play(next_state="next", autoplay=True, key="101") -> Play:
    following = {"rating_key": "102", "season": 1, "episode": 5, "title": "Next"}
    return Play(
        user="amy",
        rating_key=key,
        about={"title": "Film", "duration_s": 3000},
        choice=Choice(autoplay=autoplay),
        start_s=0,
        played_s=0,
        asked=0,
        next_state=next_state,
        next_item=following if next_state == "next" else None,
    )


def _poll(players, report=None, ack=0, active=False):
    return players.poll(TV, ack=ack, active=active, report=report)


def _start(players, play=None, by_itself=False, stream="stream") -> Play:
    play = play or _play()
    _poll(players)
    players.begin(TV, play, over=None, by_itself=by_itself)
    assert players.loaded(TV, play.id, stream_id=stream, url="/ticket", tracks={})
    players.drain()
    return play


def _report(play, state="playing", position=100.0, played=50.0) -> Report:
    return Report(play.id, state, position, played, 0.5, False, True)


def _wait(players, clock, seconds):
    """Let *seconds* pass with the TV polling; what it is told to play."""
    for _ in range(int(seconds / 100)):
        clock.now += 100
        shown, _ = _poll(players)
    return shown


def _ids(commands) -> list[str]:
    return [c["kind"] for c in commands]


def test_a_command_comes_again_until_the_tv_has_acknowledged_it(players):
    play = _start(players)
    _, commands = _poll(players)
    command = players.send(TV, "pause", play.id)
    assert _ids(_poll(players, ack=commands[-1]["id"])[1]) == ["pause"]
    assert _ids(_poll(players, ack=commands[-1]["id"])[1]) == ["pause"]
    assert _poll(players, ack=command)[1] == []
    assert players.view(TV)["acked"] == command


def test_command_ids_only_grow(players):
    play = _start(players)
    ids = [players.send(TV, kind, play.id) for kind in ("play", "pause", "play")]
    assert ids == sorted(set(ids))
    assert Players().stop(TV) > 10**12, "a TV outlives the process that counts"


def test_a_newer_command_takes_the_place_of_one_not_taken(players):
    play = _start(players)
    _, commands = _poll(players)
    seen = commands[-1]["id"]
    for place in (10, 20, 30):
        players.send(TV, "seek", play.id, position_s=place)
    players.send(TV, "pause", play.id)
    players.send(TV, "play", play.id)
    _, commands = _poll(players, ack=seen)
    assert [(c["kind"], c.get("position_s")) for c in commands] == [("seek", 30), ("play", None)]


def test_a_stop_leaves_no_earlier_command(players):
    play = _start(players)
    players.send(TV, "pause", play.id)
    players.stop(TV)
    shown, commands = _poll(players)
    assert shown is None
    assert _ids(commands) == ["stop"]


def test_nothing_is_sent_to_a_tv_that_does_not_answer(players, clock):
    play = _start(players)
    clock.now += 11
    with pytest.raises(TvOff):
        players.send(TV, "pause", play.id)
    with pytest.raises(TvOff):
        players.begin(TV, _play(), over=None)


def test_a_command_for_what_no_longer_plays_is_refused(players):
    first = _start(players)
    _start(players)
    with pytest.raises(Stale):
        players.send(TV, "pause", first.id)
    with pytest.raises(Stale):
        players.stop(TV, first.id)
    with pytest.raises(Stale):
        players.begin(TV, _play(), over=first.id)
    players.stop(TV)
    with pytest.raises(NotPlaying):
        players.send(TV, "pause", None)


def test_a_report_of_what_no_longer_plays_changes_nothing(players):
    first = _start(players)
    second = _start(players)
    _poll(players, _report(first, position=999))
    assert players.view(TV)["play"]["position_s"] == 0
    assert players.drain() == []
    assert second.state == "loading"


def test_the_place_is_kept_every_ten_seconds_and_at_a_pause(players, clock):
    play = _start(players)
    _poll(players, _report(play, position=5))
    assert players.drain() == []
    clock.now += 10
    _poll(players, _report(play, position=15))
    assert players.drain() == [Left("amy", "stream", 15, 50, False)]
    clock.now += 1
    _poll(players, _report(play, "paused", position=16))
    assert players.drain() == [Left("amy", "stream", 16, 50, False)]
    clock.now += 60
    _poll(players, _report(play, "paused", position=16))
    assert players.drain() == []


def test_a_tv_still_loading_moves_no_place(players):
    play = _start(players)
    _poll(players, _report(play, "loading", position=0))
    players.stop(TV)
    assert players.drain() == [Left("amy", "stream", None, 0, True)]


def test_a_tv_away_for_a_while_goes_on_and_one_away_too_long_is_lost(players, clock):
    play = _start(players)
    _poll(players, _report(play, position=70))
    clock.now += HOLD_SECONDS
    assert players.view(TV)["lost_s"] == HOLD_SECONDS
    shown, _ = _poll(players)
    assert shown is not None
    clock.now += HOLD_SECONDS + 1
    players.drain()
    assert players.holds(TV, play.id) is False
    assert players.drain() == [Left("amy", "stream", 70, 50, True)]
    last = players.view(TV)["last"]
    assert (last["reason"], last["position_s"], last["title"]) == ("lost", 70, "Film")


def test_the_end_counts_down_to_the_next_episode(players, clock):
    play = _start(players)
    shown, _ = _poll(players, _report(play, "ended", position=3000))
    assert (shown["state"], shown["upnext_in_s"]) == ("upnext", COUNTDOWN_SECONDS)
    assert players.drain() == [Left("amy", "stream", 3000, 50, True)]
    clock.now += 5
    with pytest.raises(Stale):
        players.due_next(TV, play.id)
    clock.now += 5
    assert players.due_next(TV, play.id) is play


def test_nothing_counts_down_once_cancelled_or_with_autoplay_off(players, clock):
    play = _start(players)
    _poll(players, _report(play, "ended"))
    players.cancel(TV, play.id)
    off = _start(players, _play(autoplay=False))
    shown, _ = _poll(players, _report(off, "ended"))
    assert (shown["state"], shown["upnext_in_s"]) == ("upnext", None)
    clock.now += COUNTDOWN_SECONDS
    for each in (play, off):
        with pytest.raises(Stale):
            players.due_next(TV, each.id)


def test_the_last_episode_just_ends(players):
    play = _start(players, _play(next_state="end"))
    shown, _ = _poll(players, _report(play, "ended"))
    assert (shown["state"], shown["next"]) == ("ended", None)


def test_learning_late_that_nothing_follows_ends_the_countdown(players, clock):
    play = _start(players, _play(next_state="unknown"))
    assert _poll(players, _report(play, "ended"))[0]["state"] == "upnext"
    players.set_next(TV, play.id, "end", None)
    shown, _ = _poll(players)
    assert (shown["state"], shown["upnext_in_s"], shown["next"]) == ("ended", None, None)


def _to_the_end(players, by_itself):
    play = _start(players, by_itself=by_itself)
    return _poll(players, _report(play, "ended"))[0]["state"]


def test_after_three_episodes_by_themselves_it_asks(players):
    assert _to_the_end(players, False) == "upnext"
    for _ in range(ASK_AFTER_EPISODES - 1):
        assert _to_the_end(players, True) == "upnext"
    assert _to_the_end(players, True) == "still"
    assert _to_the_end(players, False) == "upnext"


@pytest.mark.parametrize("sign", ["key", "command", "none"])
def test_after_two_hours_without_a_sign_of_anybody_it_asks(players, clock, sign):
    play = _start(players)
    _wait(players, clock, ASK_AFTER_SECONDS / 2)
    _poll(players, _report(play))
    if sign == "key":
        _poll(players, active=True)
    elif sign == "command":
        players.send(TV, "pause", play.id)
    _wait(players, clock, ASK_AFTER_SECONDS / 2)
    shown, _ = _poll(players, _report(play, "ended"))
    assert shown["state"] == ("still" if sign == "none" else "upnext")


def test_a_question_nobody_answers_ends_by_itself(players, clock):
    play = _start(players)
    for _ in range(ASK_AFTER_EPISODES):
        play = _start(players, by_itself=True)
    _poll(players, _report(play, "ended", position=3000))
    players.drain()
    assert _wait(players, clock, STILL_SECONDS - 100) is not None
    assert players.view(TV)["play"]["still_since_s"] == STILL_SECONDS - 100
    assert _wait(players, clock, 100) is None
    assert players.view(TV)["last"]["reason"] == "still"


def test_a_start_that_was_stopped_meanwhile_is_not_handed_to_the_tv(players):
    play = _play()
    _poll(players)
    players.begin(TV, play, over=None)
    players.stop(TV)
    assert not players.loaded(TV, play.id, stream_id="s", url="/ticket", tracks={})
    assert _poll(players)[0] is None


def test_a_restart_of_the_title_counts_the_played_time_on(players):
    play = _start(players)
    _poll(players, _report(play, played=240))
    assert _start(players).played_s == 240
    assert _start(players, _play(key="202")).played_s == 0


def test_starting_over_what_plays_leaves_its_place_and_ends_its_stream(players):
    play = _start(players)
    _poll(players, _report(play, position=70))
    players.begin(TV, _play(key="202"), over=None)
    assert players.drain() == [Left("amy", "stream", 70, 50, True)]


def test_a_stream_that_ended_under_the_tv_is_told_apart_from_one_moved_elsewhere(players):
    play = _start(players)
    players.stream_gone(TV, play.id, moved=False)
    shown, _ = _poll(players)
    assert (shown["state"], shown["error"], shown["url"]) == ("failed", "gone", None)
    play = _start(players)
    players.stream_gone(TV, play.id, moved=True)
    assert _poll(players)[0] is None
    assert players.view(TV)["last"]["reason"] == "moved"


def test_someone_whose_tv_was_taken_while_it_played_is_told_for_a_while(players, clock):
    play = _start(players)
    _poll(players, _report(play, position=70))
    players.forget(TV, "taken", 7)
    assert players.drain() == [Left("amy", "stream", 70, 50, True)]
    reason, last = players.gone("amy", 7)
    assert (reason, last["position_s"]) == ("taken", 70)
    assert players.gone("bob", 7) is None
    clock.now += GONE_KEEP_SECONDS + 1
    assert players.gone("amy", 7) is None


def test_the_ticket_is_in_neither_the_remotes_view_nor_a_log_of_the_play(players):
    play = _start(players)
    assert "/ticket" not in repr(players.view(TV))
    assert "/ticket" not in repr(play)


def test_signing_out_everywhere_ends_the_persons_plays_alone(players):
    _start(players)
    players.end_plays_of("bob")
    assert players.drain() == []
    players.end_plays_of("amy")
    assert players.drain() == [Left("amy", "stream", None, 0, True)]
