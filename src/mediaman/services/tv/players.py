"""What plays on each paired TV, in memory: the TV's reports, and the commands it has yet to take.

Nothing here waits for anything. A change that leaves a place to save or a stream to end is put
aside as a `Left`, which the caller carries out with ``drain``.
"""

from __future__ import annotations

import itertools
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

#: A TV that polls this rarely has stopped answering.
ANSWER_SECONDS = 10.0
#: A play goes on if its TV answers again within this.
HOLD_SECONDS = 120.0
COUNTDOWN_SECONDS = 10.0
#: "Are you still watching?" after so many episodes that began by themselves, or so long without a
#: sign of anybody, and how long it waits for the answer.
ASK_AFTER_EPISODES = 3
ASK_AFTER_SECONDS = 2 * 3600.0
STILL_SECONDS = 600.0
SAVE_SECONDS = 10.0
#: How long someone whose TV was taken over or unpaired while it played is told so.
GONE_KEEP_SECONDS = 600.0
_FORGET_SECONDS = 3600.0

NextState = Literal["next", "end", "unknown"]
#: The TV's own word on what it does.
Reported = Literal["loading", "playing", "paused", "buffering", "ended", "error"]
Kind = Literal["play", "pause", "seek", "volume", "mute", "stop", "load"]

#: A newer command takes the place of these, where the TV has not taken them yet.
_REPLACES: dict[str, frozenset[str]] = {
    "play": frozenset({"play", "pause"}),
    "pause": frozenset({"play", "pause"}),
    "seek": frozenset({"seek"}),
    "volume": frozenset({"volume"}),
    "mute": frozenset({"mute"}),
}


class NotPlaying(Exception):
    """Nothing plays on the TV that the command could be for."""


class Stale(Exception):
    """What plays on the TV is no longer what was asked about."""


class TvOff(Exception):
    """The TV does not answer."""


@dataclass(frozen=True)
class Left:
    """A place to save and, with *end*, a stream to end.

    Someone made since may have the name of a person who was removed: the id tells them apart.
    """

    user: str
    stream_id: str
    #: None until the TV has said where it is.
    position_s: float | None
    played_s: float
    end: bool
    user_id: int


@dataclass(frozen=True)
class Report:
    play_id: str
    state: Reported
    position_s: float
    played_s: float
    volume: float
    muted: bool
    can_volume: bool


@dataclass(frozen=True)
class Choice:
    """How a title was asked to be played."""

    quality: str = "auto"
    #: As ``POST /api/cast/stream`` takes it.
    subtitle: str | None = None
    autoplay: bool = True


@dataclass
class Play:
    user: str
    user_id: int
    rating_key: str
    #: The title's own fields, as both screens show them.
    about: Mapping[str, Any]
    choice: Choice
    start_s: float
    played_s: float
    #: When its person asked for it.
    asked: float
    id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    #: Whether the TV is to load it paused.
    paused: bool = False
    #: Whether a paused TV took this stream by itself, the last having ended, and has not played
    #: since.
    renewed: bool = False
    state: str = "starting"
    position_s: float = 0.0
    reported_at: float = 0.0
    #: Whether the TV has said where it is.
    placed: bool = False
    saved_at: float = 0.0
    #: The ticket is in it.
    url: str | None = field(default=None, repr=False)
    stream_id: str | None = None
    tracks: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    next_state: NextState = "unknown"
    next_item: Mapping[str, Any] | None = None
    upnext_at: float | None = None
    still_at: float | None = None


@dataclass
class _Tv:
    seen: float | None = None
    stamped: float | None = None
    play: Play | None = None
    last: dict[str, Any] | None = None
    commands: list[dict[str, Any]] = field(default_factory=list)
    acked: int = 0
    volume: float = 1.0
    muted: bool = False
    can_volume: bool = False
    #: Episodes that began by themselves, and the last sign of anybody.
    sitting: int = 0
    active_at: float = 0.0


class Players:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        #: Another after a restart, which ends all that plays.
        self.boot = secrets.token_urlsafe(6)
        self._lock = threading.Lock()
        # By the hash of the TV's token, which changes when the TV changes hands, so that nothing
        # begun for one owner reaches the next.
        self._tvs: dict[str, _Tv] = {}
        self._left: list[Left] = []
        # (person's id, TV) that was theirs -> (since when not, why, what had played).
        self._gone: dict[tuple[int, int], tuple[float, str, dict[str, Any]]] = {}
        # A TV skips ids it has had, and it outlives this process.
        self._ids = itertools.count(int(time.time() * 1000))

    def now(self) -> float:
        return self._clock()

    def drain(self) -> list[Left]:
        with self._lock:
            left, self._left = self._left, []
        return left

    # The methods below that take a _Tv need the lock.

    def _tv(self, token_hash: str) -> _Tv:
        now = self._clock()
        for other in self._tvs.values():
            self._settle(other)
        self._tvs = {
            key: tv
            for key, tv in self._tvs.items()
            if tv.play is not None or (tv.seen is not None and now - tv.seen < _FORGET_SECONDS)
        }
        return self._tvs.setdefault(token_hash, _Tv())

    def _settle(self, tv: _Tv) -> None:
        """Do what the time that has passed alone decides."""
        now = self._clock()
        if tv.play is None:
            return
        if tv.seen is None or now - tv.seen > HOLD_SECONDS:
            self._close(tv, "lost")
        elif tv.play.still_at is not None and now >= tv.play.still_at + STILL_SECONDS:
            self._close(tv, "still")

    def _put_aside(self, play: Play, *, end: bool) -> None:
        if play.stream_id is None:
            return
        place = play.position_s if play.placed else None
        self._left.append(Left(play.user, play.stream_id, place, play.played_s, end, play.user_id))
        play.saved_at = self._clock()
        if end:
            play.stream_id = play.url = None

    def _close(self, tv: _Tv, reason: str) -> None:
        if tv.play is None:
            return
        self._put_aside(tv.play, end=True)
        tv.last = {"reason": reason, **_summary(tv.play)}
        tv.play = None
        tv.commands = []

    def _command(self, tv: _Tv, kind: Kind, **args: Any) -> int:
        replaced = _REPLACES.get(kind)
        tv.commands = (
            [c for c in tv.commands if c["kind"] not in replaced] if replaced is not None else []
        )
        command_id = next(self._ids)
        tv.commands.append({"id": command_id, "kind": kind, **args})
        return command_id

    def _answers(self, tv: _Tv) -> bool:
        return tv.seen is not None and self._clock() - tv.seen <= ANSWER_SECONDS

    def _current(self, tv: _Tv, play_id: str | None) -> Play:
        """What plays; raises unless it is *play_id*, where that is given."""
        if tv.play is None:
            raise NotPlaying("nothing plays")
        if play_id is not None and tv.play.id != play_id:
            raise Stale("something else plays")
        return tv.play

    # ── the TV ──

    def poll(
        self, token_hash: str, *, ack: int, active: bool, report: Report | None
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Take in what the TV says; return what it is to play, and the commands it has not had."""
        with self._lock:
            tv = self._tv(token_hash)
            now = tv.seen = self._clock()
            tv.acked = max(tv.acked, ack)
            tv.commands = [c for c in tv.commands if c["id"] > ack]
            if active:
                tv.sitting, tv.active_at = 0, now
            if report is not None:
                self._report(tv, report)
            if tv.play is None:
                return None, list(tv.commands)
            play = tv.play
            art = f"/api/tv/device/art/{play.id}"
            shown = self._shown(play) | {
                "url": play.url,
                "art_url": art,
                "start_s": play.start_s,
                "paused": play.paused,
            }
            return shown, list(tv.commands)

    def _report(self, tv: _Tv, report: Report) -> None:
        tv.volume, tv.muted, tv.can_volume = report.volume, report.muted, report.can_volume
        play, now = tv.play, self._clock()
        if play is None or play.id != report.play_id or play.url is None:
            return
        if report.state == "error":
            self._put_aside(play, end=True)
            play.state, play.error = "failed", "tv"
            return
        was = play.state
        if report.state != "loading":
            play.position_s, play.placed, play.reported_at = report.position_s, True, now
            play.played_s = max(play.played_s, report.played_s)
        if report.state == "ended":
            self._at_end(tv, play)
            return
        play.state = report.state
        if play.state == "playing":
            play.paused = play.renewed = False
        paused = report.state == "paused" and was != "paused"
        if paused or (report.state == "playing" and now - play.saved_at >= SAVE_SECONDS):
            self._put_aside(play, end=False)

    def _at_end(self, tv: _Tv, play: Play) -> None:
        self._put_aside(play, end=True)
        now = self._clock()
        if play.next_state == "end":
            play.state = "ended"
        elif tv.sitting >= ASK_AFTER_EPISODES or now - tv.active_at >= ASK_AFTER_SECONDS:
            play.state, play.still_at = "still", now
        else:
            play.state = "upnext"
            play.upnext_at = now + COUNTDOWN_SECONDS if play.choice.autoplay else None

    def stream_gone(self, token_hash: str, play_id: str, *, moved: bool) -> None:
        """The play's stream ended under the TV; with *moved*, its person plays the title elsewhere."""
        with self._lock:
            tv = self._tv(token_hash)
            play = tv.play
            if play is None or play.id != play_id or play.stream_id is None:
                return
            if moved:
                self._close(tv, "moved")
            elif play.renewed:
                # Paused and alone for as long as two streams last.
                self._close(tv, "still")
            else:
                self._put_aside(play, end=True)
                play.state, play.error = "failed", "gone"

    def stream(self, token_hash: str) -> tuple[str, str, str] | None:
        """The play, stream and person of what plays on the TV."""
        with self._lock:
            play = self._tv(token_hash).play
            if play is None or play.stream_id is None:
                return None
            return play.id, play.stream_id, play.user

    def due_stamp(self, token_hash: str) -> bool:
        """Whether the TV was not marked as seen in the database for a minute; counts as marked."""
        with self._lock:
            tv, now = self._tv(token_hash), self._clock()
            if tv.stamped is not None and now - tv.stamped < 60:
                return False
            tv.stamped = now
            return True

    def holds(self, token_hash: str, play_id: str) -> bool:
        """Whether the play goes on, as far as its TV is concerned."""
        with self._lock:
            play = self._tv(token_hash).play
            return play is not None and play.id == play_id

    def art(self, play_id: str) -> tuple[str, str] | None:
        """The person and the title of a play."""
        with self._lock:
            plays = [tv.play for tv in self._tvs.values()]
        found = next((p for p in plays if p is not None and p.id == play_id), None)
        return (found.user, found.rating_key) if found is not None else None

    # ── what plays ──

    def current(self, token_hash: str, play_id: str | None = None) -> Play:
        """What plays on the TV; raises ``NotPlaying`` or, unless it is *play_id*, ``Stale``."""
        with self._lock:
            return self._current(self._tv(token_hash), play_id)

    def begin(
        self,
        token_hash: str,
        play: Play,
        *,
        over: str | None,
        by_itself: bool = False,
        pressed: bool = True,
    ) -> int:
        """Have *play* take the place of what plays, and return the command's id.

        Raises ``TvOff`` where the TV does not answer, and ``Stale`` where *over* is named and no
        longer plays.
        """
        with self._lock:
            tv = self._tv(token_hash)
            if not self._answers(tv):
                raise TvOff("the TV does not answer")
            if over is not None and (tv.play is None or tv.play.id != over):
                raise Stale("something else plays")
            if tv.play is not None:
                if tv.play.rating_key == play.rating_key:
                    play.played_s = max(play.played_s, tv.play.played_s)
                self._put_aside(tv.play, end=True)
            if by_itself:
                tv.sitting += 1
            elif pressed:
                tv.sitting, tv.active_at = 0, self._clock()
            play.position_s = play.start_s
            play.saved_at = self._clock()
            tv.play, tv.last = play, None
            return self._command(tv, "load")

    def loaded(
        self, token_hash: str, play_id: str, *, stream_id: str, url: str, tracks: Mapping[str, Any]
    ) -> bool:
        """Give the play its stream; False where it was stopped or replaced meanwhile."""
        with self._lock:
            play = self._tv(token_hash).play
            if play is None or play.id != play_id:
                return False
            play.state, play.stream_id, play.url, play.tracks = "loading", stream_id, url, tracks
            return True

    def failed(self, token_hash: str, play_id: str, error: str) -> None:
        with self._lock:
            tv = self._tv(token_hash)
            if tv.play is None or tv.play.id != play_id:
                return
            if error == "replaced":
                self._close(tv, "moved")
            else:
                tv.play.state, tv.play.error = "failed", error

    def set_next(
        self, token_hash: str, play_id: str, state: NextState, item: Mapping[str, Any] | None
    ) -> None:
        with self._lock:
            play = self._tv(token_hash).play
            if play is not None and play.id == play_id:
                play.next_state, play.next_item = state, item
                if state == "end" and play.state in ("upnext", "still"):
                    play.state, play.upnext_at, play.still_at = "ended", None, None

    def due_next(self, token_hash: str, play_id: str) -> Play:
        """What plays, where its countdown has run out; raises ``Stale`` where it has not."""
        with self._lock:
            play = self._current(self._tv(token_hash), play_id)
            # The TV counts on a clock of its own.
            if play.upnext_at is None or self._clock() < play.upnext_at - 1:
                raise Stale("no countdown has run out")
            return play

    def cancel(self, token_hash: str, play_id: str | None) -> None:
        """Let nothing follow by itself."""
        with self._lock:
            tv = self._tv(token_hash)
            play = self._current(tv, play_id)
            if play.state == "upnext":
                play.state, play.upnext_at = "ended", None
            tv.sitting, tv.active_at = 0, self._clock()

    def send(self, token_hash: str, kind: Kind, play_id: str | None, **args: Any) -> int:
        """Put a command in the TV's queue and return its id."""
        with self._lock:
            tv = self._tv(token_hash)
            if self._current(tv, play_id).url is None:
                raise NotPlaying("no stream plays")
            if not self._answers(tv):
                raise TvOff("the TV does not answer")
            tv.sitting, tv.active_at = 0, self._clock()
            return self._command(tv, kind, **args)

    def stop(self, token_hash: str, play_id: str | None = None) -> int:
        with self._lock:
            tv = self._tv(token_hash)
            if play_id is not None:
                self._current(tv, play_id)
            self._close(tv, "stopped")
            return self._command(tv, "stop")

    def leave_places(self) -> None:
        """Have the place of everything that plays saved, and go on."""
        with self._lock:
            for tv in self._tvs.values():
                if tv.play is not None:
                    self._put_aside(tv.play, end=False)

    def end_plays_of(self, user: str) -> None:
        with self._lock:
            for tv in self._tvs.values():
                if tv.play is not None and tv.play.user == user:
                    self._close(tv, "signed_out")

    def forget(self, token_hash: str, reason: str, tv_id: int | None = None) -> None:
        """End what plays on a TV whose token is no longer good; with *tv_id*, its person is told."""
        with self._lock:
            tv = self._tvs.pop(token_hash, None)
            if tv is None or tv.play is None:
                return
            now, user_id = self._clock(), tv.play.user_id
            self._close(tv, reason)
            self._gone = {k: g for k, g in self._gone.items() if now - g[0] <= GONE_KEEP_SECONDS}
            if tv.last is not None and tv_id is not None:
                self._gone[(user_id, tv_id)] = (now, reason, tv.last)

    # ── the person ──

    def gone(self, user_id: int, tv_id: int) -> tuple[str, dict[str, Any]] | None:
        """Why the person no longer has the TV that played for them, and what it played."""
        with self._lock:
            at, reason, last = self._gone.get((user_id, tv_id), (0.0, "", {}))
            if not reason or self._clock() - at > GONE_KEEP_SECONDS:
                return None
            return reason, last

    def is_on(self, token_hash: str) -> bool:
        with self._lock:
            return self._answers(self._tv(token_hash))

    def is_playing(self, token_hash: str) -> bool:
        with self._lock:
            return self._tv(token_hash).play is not None

    def plays(self, token_hash: str, rating_key: str) -> bool:
        with self._lock:
            play = self._tv(token_hash).play
            return play is not None and play.rating_key == rating_key

    def view(self, token_hash: str) -> dict[str, Any]:
        """The TV as its owner's devices see it; the ticket is not in it."""
        with self._lock:
            tv, now = self._tv(token_hash), self._clock()
            play = tv.play
            on = self._answers(tv)
            shown: dict[str, Any] | None = None
            if play is not None:
                still = play.still_at
                shown = self._shown(play) | {
                    "rating_key": play.rating_key,
                    "position_s": self._position(play),
                    "quality": play.choice.quality,
                    "still_since_s": None if still is None else now - still,
                    **play.tracks,
                }
            return {
                "boot": self.boot,
                "on": on,
                "lost_s": None if on or play is None or tv.seen is None else now - tv.seen,
                "acked": tv.acked,
                "volume": tv.volume,
                "muted": tv.muted,
                "can_volume": tv.can_volume,
                "play": shown,
                "last": tv.last,
            }

    def _position(self, play: Play) -> float:
        """Where the play is by now, its last report being some seconds old."""
        if play.state != "playing":
            return play.position_s
        since = min(self._clock() - play.reported_at, ANSWER_SECONDS)
        duration = play.about.get("duration_s") or 0
        place = play.position_s + since
        return min(place, duration) if duration else place

    def _shown(self, play: Play) -> dict[str, Any]:
        now = self._clock()
        upnext, still = play.upnext_at, play.still_at
        return {
            **play.about,
            "play_id": play.id,
            "state": play.state,
            "played_s": play.played_s,
            "next": play.next_item
            if play.next_item is not None or play.next_state == "end"
            else {"season": None, "episode": None, "title": None},
            "upnext_in_s": None if upnext is None else max(0.0, upnext - now),
            "still_in_s": None if still is None else max(0.0, still + STILL_SECONDS - now),
            "error": play.error,
        }


_SUMMARY = ("title", "year", "season", "episode", "episode_title", "duration_s")


def _summary(play: Play) -> dict[str, Any]:
    return {
        "rating_key": play.rating_key,
        "position_s": play.position_s,
        **{key: play.about.get(key) for key in _SUMMARY},
    }


_players = Players()


def get_players() -> Players:
    return _players
