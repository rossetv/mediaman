"""Tests for playing on an older TV: what its driver makes of a TV's answers, and does to it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mediaman.services.dlna.errors import TvFault, TvRefused, TvUnreachable
from mediaman.services.dlna.media import Media
from mediaman.services.dlna.renderer import Status
from mediaman.services.tv import older
from mediaman.services.tv.older import CannotPlay, Claim, Claims, Driver, Server
from mediaman.services.tv.players import HOLD_SECONDS, Choice, Left, Play, Players

LENGTH = 6000
SEGMENT = 8.0


class Renderer:
    """A TV that does what it is told, and counts time from what it was handed."""

    has_volume = True

    def __init__(self, clock) -> None:
        self.clock = clock
        self.calls: list[tuple] = []
        self.state = "STOPPED"
        self.count: int | None = 0
        self.trouble = False
        self.uri = ""
        self.says_uri = False
        self.level = 40
        self.fails: dict[str, Exception] = {}
        self.off = False

    def _call(self, name, *args):
        if self.off:
            raise TvUnreachable("gone")
        self.calls.append((name, *args))
        if name in self.fails:
            raise self.fails[name]

    def start(self, media):
        self._call("start", media.url)
        self.state, self.count = "PLAYING", 0
        if self.says_uri:
            self.uri = media.url

    def stop(self):
        self._call("stop")
        self.state = "STOPPED"

    def play(self):
        self._call("play")
        self.state = "PLAYING"

    def pause(self):
        self._call("pause")
        self.state = "PAUSED_PLAYBACK"

    def volume(self):
        return self.level

    def set_volume(self, level):
        self._call("volume", level)
        self.level = level

    def status(self):
        if self.off:
            raise TvUnreachable("gone")
        return Status(self.state, self.trouble, self.count, None, self.uri)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class Home:
    """One TV, its driver and its person, on one clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        clock = lambda: self.now  # noqa: E731
        self.tv = Renderer(clock)
        self.players = Players(clock=clock)
        self.claims = Claims()
        self.claim = Claim(7, "amy", 1, "session")
        self.claims.take(self.claim)
        self.over: str | None = None
        self.moved: Exception | None = None
        self.sent = (False, False)
        self.asked: list[tuple[str, bool]] = []
        self.stream_is_gone = False
        self.streams = 0
        self.alive = 0
        self.left: list[Left] = []
        self.driver = self.make(self.claim)
        self.players.poll(self.claim.key, ack=0, active=True, report=None)

    def make(self, claim: Claim, *, handed: bool = False) -> Driver:
        return Driver(
            claim,
            self.tv,
            Server(
                ends=lambda: self._ends(),
                reach=self._found,
                recognise=self._found,
                film=self._film,
                sent=lambda stream_id: self.sent,
                keep_alive=self._keep_alive,
                carry_on=lambda what, play_id, paused: self.asked.append((what, paused)),
                carry_out=lambda: self.left.extend(self.players.drain()),
            ),
            claims=self.claims,
            players=self.players,
            clock=lambda: self.now,
            work=self._work,
            handed=handed,
        )

    def _ends(self) -> str | None:
        return self.over

    def _work(self, do):
        do()

    def _found(self, known):
        if self.moved is not None:
            raise self.moved
        if self.tv.off:
            raise TvUnreachable("gone")
        return known

    def _film(self, url, position_s, about):
        at = position_s // SEGMENT * SEGMENT
        return Media(url=f"http://tv/{url}/{int(at)}", title=about["title"]), at

    def _keep_alive(self):
        self.alive += 1
        playing = self.players.stream(self.claim.key)
        if self.stream_is_gone and playing is not None:
            self.players.stream_gone(self.claim.key, playing[0], moved=False)

    def play(
        self, start_s=0.0, paused=False, duration=LENGTH, next_state="end", autoplay=True
    ) -> str:
        self.streams += 1
        play = Play(
            user="amy",
            user_id=1,
            rating_key="101",
            about={"title": "Film", "duration_s": duration},
            choice=Choice(autoplay=autoplay),
            start_s=start_s,
            played_s=0,
            asked=self.now,
            paused=paused,
            next_state=next_state,
        )
        key = self.claim.key
        self.players.begin(key, play, over=None)
        assert self.players.loaded(
            key, play.id, stream_id=f"s{self.streams}", url=f"film{self.streams}", tracks={}
        )
        return play.id

    def play_nothing(self) -> None:
        """Begin a play whose stream then fails to start."""
        play = Play("amy", 1, "101", {}, Choice(), start_s=0, played_s=0, asked=self.now)
        self.players.begin(self.claim.key, play, over=None)
        self.players.failed(self.claim.key, play.id, "unreachable")

    def step(self, seconds=1.0) -> bool:
        self.now += seconds
        return self.driver.step()

    def steps(self, count: int, *, playing=True) -> None:
        for _ in range(count):
            if playing and self.tv.count is not None:
                self.tv.count += 1
            assert self.step()

    def view(self) -> dict:
        return self.players.view(self.claim.key)

    def film(self) -> dict:
        return self.view()["play"]

    def command(self, kind, **args) -> int:
        return self.players.send(self.claim.key, kind, None, **args)


@pytest.fixture
def home() -> Home:
    return Home()


def test_a_film_is_handed_over_from_its_place_and_its_time_counted_from_there(home):
    home.play(start_s=1203)
    home.steps(3)
    assert home.tv.calls[-1] == ("start", "http://tv/film1/1200")
    home.tv.count = 30
    home.step()
    film = home.film()
    assert (film["state"], film["position_s"]) == ("playing", 1230)


def test_a_tv_that_counts_the_films_own_time_is_not_counted_twice(home):
    home.play(start_s=1200)
    home.step()
    home.tv.count = 1201
    home.step()
    assert home.film()["position_s"] == 1201
    home.tv.count = 1300
    home.step(99)
    assert home.film()["position_s"] == 1300


def test_a_tv_that_tells_no_time_is_counted_by_the_clock(home):
    home.play(start_s=800)
    home.step()
    home.tv.count = None
    home.steps(6)
    assert home.film()["position_s"] == 805


def test_only_time_that_played_counts_as_played(home):
    home.play()
    home.steps(11)
    home.tv.state = "PAUSED_PLAYBACK"
    home.step(60)
    home.step(60)
    assert home.film()["state"] == "paused"
    assert home.film()["played_s"] == 9


def test_a_seek_hands_the_film_over_anew_from_there_and_the_place_holds_meanwhile(home):
    home.play()
    home.steps(3)
    command = home.command("seek", position_s=4001)
    home.step()
    assert home.tv.names()[-2:] == ["stop", "start"]
    assert home.tv.calls[-1] == ("start", "http://tv/film1/4000")
    home.tv.state = "STOPPED"
    home.step()
    assert (home.film()["state"], home.film()["position_s"]) == ("buffering", 4001)
    assert home.view()["acked"] == command


def test_a_seek_while_paused_stays_paused(home):
    home.play()
    home.steps(3)
    home.command("pause")
    home.step()
    home.command("seek", position_s=80)
    home.steps(3, playing=False)
    assert home.tv.state == "PAUSED_PLAYBACK"
    assert home.film()["state"] == "paused"


def _paused_load(home):
    home.play(paused=True)
    home.step()


def test_a_film_loaded_paused_is_paused_once_it_plays(home):
    _paused_load(home)
    home.steps(2)
    assert home.tv.state == "PAUSED_PLAYBACK"
    assert home.tv.names().count("pause") == 1


@pytest.mark.parametrize("wrong", [TvUnreachable("gone"), TvFault("not now", 701)])
def test_a_pause_the_tv_did_not_take_is_not_lost(home, wrong):
    _paused_load(home)
    home.tv.fails["pause"] = wrong
    home.step()
    del home.tv.fails["pause"]
    home.steps(3)
    assert home.tv.state == "PAUSED_PLAYBACK"


def test_a_film_played_on_meanwhile_is_not_paused_late(home):
    _paused_load(home)
    home.tv.fails["pause"] = TvFault("not now", 701)
    home.step()
    home.command("play")
    home.step()
    del home.tv.fails["pause"]
    home.steps(2)
    assert home.tv.state == "PLAYING"


def test_a_tv_that_knows_no_pause_is_asked_for_it_once(home):
    _paused_load(home)
    home.tv.fails["pause"] = TvFault("no", 401)
    home.steps(3)
    assert home.tv.names().count("pause") == 1


def test_a_pause_does_not_land_on_the_film_of_whoever_took_the_tv(home):
    _paused_load(home)
    real = home.tv.status

    def taken_meanwhile():
        home.claims.take(Claim(7, "bob", 2, "other"))
        return real()

    home.tv.status = taken_meanwhile
    home.step()
    assert "pause" not in home.tv.names()


def test_a_tv_whose_controls_are_in_use_is_paused_only_after(home):
    _paused_load(home)
    held = []
    real = home.tv.pause
    home.tv.pause = lambda: (held.append(home.claims.steering(7).locked()), real())
    home.step()
    assert held == [True]


def test_the_remotes_commands_reach_the_tv(home):
    home.play()
    home.steps(2)
    home.command("pause")
    home.step()
    home.command("play")
    home.step()
    home.command("volume", level=0.25)
    home.step()
    home.command("mute", muted=True)
    home.steps(2)
    assert (home.tv.level, home.view()["muted"]) == (0, True)
    home.command("mute", muted=False)
    home.step()
    assert home.tv.calls[-5:] == [
        ("pause",),
        ("play",),
        ("volume", 25),
        ("volume", 0),
        ("volume", 25),
    ]


def test_a_tv_that_let_a_paused_film_go_is_handed_it_again(home):
    home.play()
    home.steps(20)
    home.command("pause")
    home.step()
    home.tv.fails["play"] = TvFault("no", 701)
    home.command("play")
    home.step()
    assert home.tv.calls[-1] == ("start", "http://tv/film1/16")


def test_a_command_the_tv_refuses_is_done_with(home):
    home.play()
    home.steps(2)
    home.tv.fails["pause"] = TvFault("no", 401)
    command = home.command("pause")
    home.steps(2)
    assert home.view()["acked"] == command
    assert home.tv.names().count("pause") == 1


def test_a_tv_whose_volume_mediaman_cannot_set_says_so(home):
    home.tv.has_volume = False
    home.play()
    home.steps(2)
    assert home.view()["can_volume"] is False


def test_a_command_for_a_film_the_tv_was_not_handed_yet_waits_for_it(home):
    home.play()
    home.steps(2)
    home.play()
    home.command("seek", position_s=4000)
    home.steps(2)
    assert [c[1] for c in home.tv.calls if c[0] == "start"] == [
        "http://tv/film1/0",
        "http://tv/film2/0",
        "http://tv/film2/4000",
    ]


def test_a_film_that_ran_to_its_end_has_ended(home):
    home.play(start_s=LENGTH - 2)
    home.steps(3)
    home.tv.state, home.sent = "STOPPED", (True, False)
    home.step()
    assert home.film()["state"] == "ended"


def test_a_film_whose_end_a_slow_tv_tells_late_has_ended(home):
    home.play(start_s=LENGTH - 48)
    home.steps(34)
    home.tv.state, home.sent = "STOPPED", (True, False)
    home.step(6)
    assert home.film()["state"] == "ended"


def _stopped_near_the_end(home, start_s, sent, paused=False):
    home.play(start_s=start_s, next_state="next")
    home.steps(3)
    if paused:
        home.tv.state = "PAUSED_PLAYBACK"
        home.step()
    home.tv.state, home.sent = "STOPPED", (sent, False)
    home.step()


@pytest.mark.parametrize(
    ("start_s", "sent", "paused"),
    [(LENGTH - 2, False, False), (LENGTH - 24, True, False), (LENGTH - 2, True, True)],
)
def test_a_persons_own_stop_near_the_end_starts_nothing(home, start_s, sent, paused):
    _stopped_near_the_end(home, start_s, sent, paused)
    assert home.view()["last"]["reason"] == "stopped"
    assert home.step() is False
    assert home.asked == []


def test_a_film_of_no_known_length_has_ended_once_all_of_it_was_sent(home):
    home.play(duration=None)
    home.steps(3)
    home.tv.state, home.sent = "STOPPED", (True, False)
    home.step()
    assert home.film()["state"] == "ended"


def test_a_stop_on_the_tvs_own_remote_ends_the_play_and_is_no_error(home):
    home.play()
    home.steps(30)
    home.tv.state = "STOPPED"
    home.step()
    assert home.view()["last"]["reason"] == "stopped"
    assert home.view()["last"]["position_s"] == 29
    assert home.step() is False
    assert home.tv.names().count("stop") == 1


THEIRS = "http://192.168.1.9:8200/MediaItems/22.mkv"


@pytest.mark.parametrize("state", ["PLAYING", "PAUSED_PLAYBACK", "STOPPED"])
def test_another_controllers_film_is_neither_followed_nor_stopped(home, state):
    home.tv.says_uri = True
    home.play()
    home.steps(30)
    home.tv.uri, home.tv.count, home.tv.state = THEIRS, 4000, state
    home.tv.calls.clear()
    home.step()
    assert home.step() is False
    home.driver._let_go()
    assert home.tv.calls == []
    assert home.view()["last"]["position_s"] == 29
    assert [left.position_s for left in home.left if left.end] == [29]


def test_another_controllers_film_is_told_at_once_where_the_tv_named_ours(home):
    home.tv.says_uri = True
    home.play()
    home.steps(3)
    home.tv.uri = THEIRS
    home.step()
    assert home.film() is None


def test_what_a_tv_named_before_is_not_taken_for_anothers_while_it_loads(home):
    home.tv.uri = THEIRS
    home.play()
    home.steps(3)
    assert home.film()["state"] == "loading"
    home.tv.uri = "http://tv/film1/0"
    home.steps(2)
    assert home.film()["state"] == "playing"


def test_a_tv_that_goes_on_naming_another_film_is_left_to_it(home):
    home.tv.uri = THEIRS
    home.play()
    assert not all(home.step() for _ in range(40))
    home.driver._let_go()
    assert home.tv.names().count("stop") == 1
    assert [left.position_s for left in home.left] == [None]


@pytest.mark.parametrize("uri", ["", "http://tv/film1/0"])
def test_a_tv_that_names_no_other_film_is_followed(home, uri):
    home.tv.uri = uri
    home.play()
    home.steps(3)
    assert home.film()["state"] == "playing"


def test_a_seek_is_no_other_film(home):
    home.tv.says_uri = True
    home.play()
    home.steps(3)
    home.command("seek", position_s=4000)
    home.steps(4)
    assert home.film()["state"] == "playing"


def test_a_stream_that_broke_off_is_an_error_and_no_stop(home):
    home.play()
    home.steps(30)
    home.tv.state, home.sent = "STOPPED", (False, True)
    home.step()
    assert (home.film()["state"], home.film()["error"]) == ("failed", "tv")


def test_a_tv_that_never_starts_the_film_has_failed(home):
    home.play()
    home.step()
    home.tv.state = "STOPPED"
    home.step(5)
    assert home.film()["state"] == "loading"
    home.step(older.STOPPED_SECONDS)
    assert home.film()["state"] == "failed"


def test_a_tv_that_says_it_has_trouble_has_failed(home):
    home.play()
    home.step()
    home.tv.state, home.tv.trouble = "STOPPED", True
    home.step()
    assert home.film()["state"] == "failed"


def test_a_tv_that_does_not_take_the_film_has_failed(home):
    home.tv.fails["start"] = TvFault("no", 714)
    home.play()
    home.steps(2)
    assert home.film()["state"] == "failed"


def test_a_film_there_is_nothing_of_to_hand_over_has_failed(home, monkeypatch):
    def no_film(url, position_s, about):
        raise CannotPlay("x")

    monkeypatch.setattr(home, "_film", no_film)
    home.driver = home.make(home.claim)
    home.play()
    home.steps(2)
    assert home.film()["state"] == "failed"


def test_a_stop_stops_the_tv_once_and_ends_the_driver(home):
    home.play()
    home.steps(3)
    home.players.stop(home.claim.key)
    assert home.step() is False
    assert home.tv.state == "STOPPED"
    assert home.tv.names().count("stop") == 2  # One before the film was handed over.


def test_a_tv_that_goes_away_is_lost_after_a_while_and_asked_for_nothing_more(home):
    home.play()
    home.steps(3)
    home.tv.off = True
    assert home.step()
    assert home.step(30)
    assert home.view()["lost_s"] > 10
    assert home.step(HOLD_SECONDS) is False
    assert home.view()["last"]["reason"] == "lost"


def test_a_tv_that_comes_back_carries_on(home):
    home.play()
    home.steps(3)
    home.tv.off = True
    home.step(30)
    home.tv.off = False
    played = home.film()["played_s"]
    home.steps(2)
    assert home.view()["on"] is True
    assert home.film()["played_s"] == played + 1


def test_another_device_at_the_tvs_address_is_sent_nothing(home):
    home.play()
    home.steps(3)
    home.tv.off = True
    home.step()
    home.tv.off, home.moved = False, TvRefused("another")
    home.command("pause")
    before = list(home.tv.calls)
    home.step()
    assert home.step(HOLD_SECONDS) is False
    home.driver._let_go()
    assert home.tv.calls == before


def test_no_film_is_handed_to_another_device(home):
    home.moved = TvRefused("another")
    home.play()
    home.steps(2)
    assert home.tv.calls == []
    assert home.film()["state"] == "failed"


def test_a_film_already_on_the_tv_is_not_stopped_on_another_device(home):
    home.play()
    home.steps(3)
    home.moved = TvRefused("another")
    home.play()
    before = list(home.tv.calls)
    home.steps(2)
    assert home.film()["state"] == "failed"
    assert home.tv.calls == before


def test_a_claim_that_is_no_longer_good_ends_the_play_and_stops_the_tv(home):
    home.play()
    home.steps(3)
    home.over = "signed_out"
    assert home.step() is False
    home.driver._let_go()
    assert home.tv.state == "STOPPED"
    assert not home.players.is_playing(home.claim.key)
    assert home.players.gone(1, 7)[0] == "signed_out"
    assert home.claims.of(7) is None


def test_who_lost_the_tv_to_another_does_not_stop_what_that_one_plays(home):
    home.play()
    home.steps(3)
    home.claims.take(Claim(7, "bob", 2, "other"))
    home.over = "taken"
    home.tv.calls.clear()
    assert home.step() is False
    home.driver._let_go()
    assert home.tv.calls == []


def test_a_taken_tv_is_stopped_where_nothing_starts_on_it(home):
    home.tv.state = "PLAYING"
    driver = home.make(home.claim, handed=True)
    home.play_nothing()
    assert driver.step()
    assert home.tv.names() == ["stop"]


def test_another_device_is_not_stopped_in_a_taken_tvs_place(home):
    home.moved = TvRefused("another")
    driver = home.make(home.claim, handed=True)
    home.play_nothing()
    driver.step()
    assert home.tv.calls == []


def test_a_tv_taken_over_is_handed_nothing_of_the_earlier_persons(home):
    home.claims.take(Claim(7, "bob", 2, "other"))
    home.play()
    home.step()
    assert home.tv.calls == []


def test_the_next_episode_is_asked_for_once_the_countdown_is_over(home):
    home.play(start_s=LENGTH - 2, next_state="next")
    home.steps(3)
    home.tv.state, home.sent = "STOPPED", (True, False)
    home.step()
    assert home.film()["state"] == "upnext"
    home.step(5)
    assert home.asked == []
    home.steps(8)
    assert home.asked == [("auto", False)]


def _up_next(home, autoplay=True):
    home.play(start_s=LENGTH - 2, next_state="next", autoplay=autoplay)
    home.steps(3)
    home.tv.state, home.sent = "STOPPED", (True, False)
    home.step()
    assert home.film()["state"] == "upnext"


def test_the_next_episode_is_asked_for_again_after_a_while_and_not_for_ever(home):
    _up_next(home)
    home.steps(12)
    first = len(home.asked)
    home.steps(2)
    assert len(home.asked) == first
    home.steps(older.AUTO_TRIES * 5 + 10)
    assert home.asked == [("auto", False)] * older.AUTO_TRIES
    assert home.film()["state"] == "ended"


def test_what_follows_only_at_a_press_is_put_away_after_a_while(home):
    _up_next(home, autoplay=False)
    for _ in range(int(older.OVER_SECONDS // 60)):
        assert home.step(60)
    home.step(60)
    assert home.step() is False
    assert home.asked == []


def test_a_tv_turned_off_between_two_episodes_is_lost(home):
    _up_next(home, autoplay=False)
    home.tv.off = True
    home.steps(25)
    assert home.view()["on"] is False
    assert home.step(HOLD_SECONDS) is False
    assert home.view()["last"]["reason"] == "lost"


def test_a_stream_that_ended_under_the_tv_is_asked_for_again_twice_at_most(home):
    home.play()
    home.steps(3)
    home.tv.state = "PAUSED_PLAYBACK"
    home.step()
    home.stream_is_gone = True
    home.steps(6)
    assert home.asked == [("again", True), ("again", False)]


def test_a_paused_tvs_stream_is_kept_alive(home):
    home.play()
    home.steps(3)
    home.tv.state = "PAUSED_PLAYBACK"
    before = home.alive
    home.steps(4, playing=False)
    assert home.alive == before + 4


def test_what_is_over_is_put_away_after_a_while(home):
    home.play(start_s=LENGTH - 2)
    home.steps(3)
    home.tv.state, home.sent = "STOPPED", (True, False)
    home.steps(2)
    for _ in range(int(older.OVER_SECONDS // 60)):
        assert home.step(60)
    home.step(60)
    assert home.view()["last"]["reason"] == "stopped"
    assert home.step() is False


def test_while_the_tv_takes_its_time_to_load_it_counts_as_answering(home):
    busy = SimpleNamespace(is_alive=lambda: True, join=lambda timeout: None)
    home.driver._start_work = lambda do: busy
    home.play()
    for _ in range(40):
        assert home.step()
    assert home.view()["on"] is True
    assert home.film()["state"] == "loading"


def test_a_driver_that_fails_ends_what_plays_and_stops_the_tv(home, monkeypatch):
    home.play()
    home.steps(3)

    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(home, "_ends", broken)
    home.driver.run()
    (left,) = home.left
    assert (left.stream_id, left.end) == ("s1", True)
    assert home.tv.state == "STOPPED"
    assert home.players.gone(1, 7)[0] == "lost"


def test_a_driver_nobody_plays_on_ends(home):
    assert home.step()
    assert home.step(200) is False
