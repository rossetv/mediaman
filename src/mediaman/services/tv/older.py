"""Who plays on each older TV, and the thread that does for it what a paired TV's page does itself.

Such a TV holds no token and asks mediaman for nothing but the film. A :class:`Driver` polls it,
says what it does to :class:`~mediaman.services.tv.players.Players` as the TV's own report, and
carries out on it the commands found there. All calls to one TV's controls are made one at a time.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mediaman.db import close_db
from mediaman.services.dlna.errors import TvError, TvFault
from mediaman.services.dlna.media import Media
from mediaman.services.dlna.renderer import Renderer, Status
from mediaman.services.tv.players import NotPlaying, Players, Report, Reported, Stale

logger = logging.getLogger(__name__)

STEP_SECONDS = 1.0
#: How long a TV may take to start a film, and how long one told to play may still say it has none.
LOAD_SECONDS = 90.0
STOPPED_SECONDS = 20.0
#: A TV that stops this close to the end has come to it.
NEAR_END_SECONDS = 30.0
#: How long a film that is over or failed is left for its person to look at.
OVER_SECONDS = 600.0
#: How often a TV with no film on is asked whether it is still there.
IDLE_ASK_SECONDS = 10.0
#: How often the next episode is asked for, and how long after a try that came to nothing.
AUTO_TRIES = 6
AUTO_WAIT_SECONDS = 5.0
_START_WAIT_SECONDS = 120.0
_LAST_WORK_SECONDS = 60.0
_VOLUME_STEPS = 5
_AGAIN_TRIES = 2
# UPnP's code for what a TV does not know how to do.
_NOT_KNOWN = 401
# A TV's clock and mediaman's are read apart.
_CLOCK_SLACK_SECONDS = 5.0


class CannotPlay(Exception):
    """mediaman has nothing to hand the TV."""


@dataclass(frozen=True)
class Claim:
    """One person's use of an older TV, until another's takes its place."""

    tv_id: int
    user: str
    user_id: int
    #: The session that asked, as the sessions table keys it.
    session_hash: str
    #: What stands in ``Players`` for the token of a paired TV.
    key: str = field(default_factory=lambda: f"older-{secrets.token_urlsafe(24)}")


class Claims:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_tv: dict[int, Claim] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._answered: dict[int, bool] = {}

    def take(self, claim: Claim) -> Claim | None:
        """Let *claim* have its TV, and return the one it takes it from."""
        with self._lock:
            before = self._by_tv.get(claim.tv_id)
            self._by_tv[claim.tv_id] = claim
        return before

    def of(self, tv_id: int) -> Claim | None:
        with self._lock:
            return self._by_tv.get(tv_id)

    def with_key(self, key: str) -> Claim | None:
        with self._lock:
            return next((c for c in self._by_tv.values() if c.key == key), None)

    def drop(self, claim: Claim) -> None:
        with self._lock:
            if self._by_tv.get(claim.tv_id) == claim:
                del self._by_tv[claim.tv_id]

    def every(self) -> list[Claim]:
        with self._lock:
            return list(self._by_tv.values())

    def steering(self, tv_id: int) -> threading.Lock:
        """The lock of the TV's controls."""
        with self._lock:
            return self._locks.setdefault(tv_id, threading.Lock())

    def note(self, tv_id: int, *, answered: bool) -> None:
        with self._lock:
            self._answered[tv_id] = answered

    def answered(self, tv_id: int) -> bool:
        """Whether the TV answered when it was last asked; one never asked counts as if."""
        with self._lock:
            return self._answered.get(tv_id, True)


@dataclass(frozen=True)
class Server:
    """What a driver needs of the rest of mediaman."""

    #: Why the claim is no longer good, or None.
    ends: Callable[[], str | None]
    #: The same TV again, wherever it is now; raises ``TvError`` where it is not found.
    reach: Callable[[Renderer], Renderer]
    #: The same TV at the same address, asked once.
    recognise: Callable[[Renderer], Renderer]
    #: What to hand the TV for the stream at an address, from a place on, and where that begins;
    #: raises ``CannotPlay``.
    film: Callable[[str, float, dict[str, Any]], tuple[Media, float]]
    #: Whether the TV was sent the stream's last part, and whether sending broke off.
    sent: Callable[[str], tuple[bool, bool]]
    keep_alive: Callable[[], None]
    #: As the TV's own keys ask: what, of which play, and whether the TV is paused.
    carry_on: Callable[[str, str, bool], None]
    carry_out: Callable[[], None]


@dataclass
class _Film:
    """What the TV was handed."""

    play_id: str
    stream_id: str
    url: str
    about: dict[str, Any]
    #: Where the TV's own count of time begins.
    base_s: float
    #: When it was handed over.
    at: float
    position_s: float
    played_s: float
    #: Whether it is to be paused as soon as it plays.
    pause: bool = False
    begun: bool = False
    #: Whether it has played since the TV was handed it last.
    on: bool = False
    paused: bool = False
    #: The TV's count is of the film's time, not of what it was handed. None: not known yet.
    absolute: bool | None = None
    seen_playing: float | None = None

    def take_place(self, count: int | None, now: float) -> None:
        if count is None:
            if self.seen_playing is not None:
                self.position_s += now - self.seen_playing
            return
        since = now - self.at
        if self.absolute is None:
            # No more can have been played than time has passed.
            self.absolute = (
                count > since + _CLOCK_SLACK_SECONDS
                and -_CLOCK_SLACK_SECONDS <= count - self.base_s <= since + _CLOCK_SLACK_SECONDS
            )
        self.position_s = count if self.absolute else self.base_s + count

    def near_end(self) -> bool:
        duration = self.about.get("duration_s") or 0
        return bool(duration) and self.position_s >= duration - NEAR_END_SECONDS


class Driver:
    def __init__(
        self,
        claim: Claim,
        renderer: Renderer,
        server: Server,
        *,
        claims: Claims,
        players: Players,
        clock: Callable[[], float] = time.monotonic,
        work: Callable[[Callable[[], None]], threading.Thread | None] | None = None,
        handed: bool = False,
    ) -> None:
        """With *handed* the TV may be playing what the claim before this one's handed it."""
        self._claim = claim
        self._renderer = renderer
        self._server = server
        self._claims = claims
        self._players = players
        self._clock = clock
        self._start_work = work or _on_a_thread
        self._worker: threading.Thread | None = None
        self._began = clock()
        self._film: _Film | None = None
        self._failed = False
        #: Whether the TV may be playing what it was handed.
        self._handed = handed
        self._had_play = False
        # It may be a while since it was looked for.
        self._away = handed
        self._asked_idle = clock()
        self._ack = 0
        self._steps = 0
        self._again = 0
        self._auto_for = ""
        self._auto_tries = 0
        self._auto_at = 0.0
        self._over: tuple[str, float] | None = None
        self._volume = 1.0
        self._unmuted = 1.0
        self._muted = False

    # ── the loop ──

    def run(self) -> None:
        try:
            while self.step():
                time.sleep(STEP_SECONDS)
        except Exception:
            logger.exception("older tv: the thread that plays on it failed")
        finally:
            # Nobody else would end what still plays.
            if self._players.is_playing(self._claim.key):
                self._players.forget(self._claim.key, "lost", self._claim.tv_id)
                self._claims.drop(self._claim)
            self._server.carry_out()
            self._let_go()

    def step(self) -> bool:
        """Do what is due; False once nothing is left to do."""
        why = self._server.ends()
        if why is not None:
            self._players.forget(self._claim.key, why, self._claim.tv_id)
            self._claims.drop(self._claim)
            self._server.carry_out()
            return False
        if self._busy():
            play = self._tell(self._loading())
            return play is not None or not self._had_play or self._busy()
        if self._away and not self._back():
            # What plays is told as lost once its time is up.
            playing = self._players.is_playing(self._claim.key)
            self._server.carry_out()
            return playing
        try:
            report = self._look()
        except TvError:
            self._away = True
            if self._film is not None:
                self._film.seen_playing = None
            return True
        if self._film is not None:
            self._server.keep_alive()
        play = self._tell(report)
        if play is None:
            self._silence()
            waits = self._clock() - self._began < _START_WAIT_SECONDS
            return not self._had_play and waits
        self._follow(play)
        return True

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _work(self, do: Callable[[], None]) -> None:
        """Do what may take long apart, so that the TV counts as answering meanwhile."""
        self._worker = self._start_work(do)

    def _back(self) -> bool:
        """Whether the TV that went away answers again, and is the same one."""
        try:
            self._renderer = self._server.recognise(self._renderer)
        except TvError:
            return False
        self._away = False
        return True

    def _ours(self) -> bool:
        return self._claims.of(self._claim.tv_id) == self._claim

    def _silence(self) -> None:
        """Stop what the TV was handed."""
        self._film = None
        if not self._handed:
            return
        self._handed = False
        with self._claims.steering(self._claim.tv_id):
            held = self._claims.of(self._claim.tv_id)
            # Whoever took the TV over stops it.
            if held is None or held == self._claim:
                with contextlib.suppress(TvError):
                    self._renderer.stop()

    def _let_go(self) -> None:
        if self._worker is not None:
            self._worker.join(_LAST_WORK_SECONDS)
        if not self._away:
            self._silence()
        close_db()

    # ── what the TV says ──

    def _tell(self, report: Report | None) -> dict[str, Any] | None:
        """Give the report, take the commands; what is to play."""
        play, commands = self._players.poll(
            self._claim.key, ack=self._ack, active=False, report=report
        )
        self._server.carry_out()
        self._had_play = self._had_play or play is not None
        # Commands are for what plays, which the TV may not have been handed yet.
        if not self._busy() and not self._is_owed(play):
            self._obey(commands)
        return play

    def _is_owed(self, play: dict[str, Any] | None) -> bool:
        """Whether the TV has yet to be handed the play's film."""
        if play is None or play["url"] is None:
            return False
        return self._film is None or self._film.play_id != play["play_id"]

    def _report(self, film: _Film, state: Reported) -> Report:
        return Report(
            play_id=film.play_id,
            state=state,
            position_s=max(0.0, film.position_s),
            played_s=film.played_s,
            volume=self._volume,
            muted=self._muted,
            can_volume=self._renderer.has_volume,
        )

    def _loading(self) -> Report | None:
        film = self._film
        if film is None:
            return None
        return self._report(film, "buffering" if film.begun else "loading")

    def _look(self) -> Report | None:
        """Ask the TV what it does; raises ``TvError`` where it does not say."""
        film = self._film
        if film is None:
            self._ask_idle()
            return None
        if self._failed:
            return self._report(film, "error")
        status = self._renderer.status()
        self._claims.note(self._claim.tv_id, answered=True)
        if self._steps % _VOLUME_STEPS == 0:
            self._ask_volume()
        self._steps += 1
        state = self._judge(film, status)
        return None if state is None else self._report(film, state)

    def _ask_idle(self) -> None:
        now = self._clock()
        if now - self._asked_idle >= IDLE_ASK_SECONDS:
            self._asked_idle = now
            self._renderer.status()

    def _ask_volume(self) -> None:
        if not self._renderer.has_volume:
            return
        with contextlib.suppress(TvError):
            level = self._renderer.volume() / 100
            if level > 0:
                self._muted = False
            if not self._muted:
                self._volume = level

    def _judge(self, film: _Film, status: Status) -> Reported | None:
        """What the TV's status means for the film; None where it stopped it itself."""
        now = self._clock()
        if status.state == "PLAYING":
            film.take_place(status.position, now)
            if film.seen_playing is not None:
                film.played_s += now - film.seen_playing
            film.seen_playing, film.begun, film.on, film.paused = now, True, True, False
            self._again = 0
            if film.pause:
                self._pause_late(film)
            return "playing"
        film.seen_playing = None
        if status.state == "PAUSED_PLAYBACK":
            film.begun = film.on = film.paused = True
            film.take_place(status.position, now)
            return "paused"
        if status.state in ("STOPPED", "NO_MEDIA_PRESENT"):
            return self._stopped(film, status, now)
        if not film.on and now - film.at > LOAD_SECONDS:
            return "error"
        return "buffering" if film.begun else "loading"

    def _pause_late(self, film: _Film) -> None:
        """Pause what was to be paused once it plays; tried again where the TV does not yet."""
        with self._claims.steering(self._claim.tv_id):
            if not self._ours():
                return
            try:
                self._renderer.pause()
            except TvFault as exc:
                film.pause = exc.code != _NOT_KNOWN
            else:
                film.pause = False

    def _stopped(self, film: _Film, status: Status, now: float) -> Reported | None:
        all_sent, broke_off = self._server.sent(film.stream_id)
        if film.on and (film.near_end() or (all_sent and not film.about.get("duration_s"))):
            self._handed = False
            return "ended"
        if status.trouble or broke_off:
            return "error"
        if not film.on:
            if now - film.at > STOPPED_SECONDS:
                return "error"
            return "buffering" if film.begun else "loading"
        # Its own remote, or another device.
        self._handed = False
        self._players.stop(self._claim.key)
        return None

    # ── what is asked of it ──

    def _follow(self, play: dict[str, Any]) -> None:
        state, play_id = play["state"], play["play_id"]
        if play["url"] is not None:
            self._over = None
            if self._is_owed(play):
                self._hand(play)
            return
        paused = self._film is not None and self._film.paused
        self._silence()
        if state == "upnext" and play["upnext_in_s"] == 0:
            self._ask_next(play_id)
        elif state == "failed" and play["error"] == "gone" and self._again < _AGAIN_TRIES:
            self._again += 1
            self._work(lambda: self._server.carry_on("again", play_id, paused))
        elif state in ("ended", "failed") or (state == "upnext" and play["upnext_in_s"] is None):
            if self._over is None or self._over[0] != play_id:
                self._over = (play_id, self._clock())
            elif self._clock() - self._over[1] > OVER_SECONDS:
                self._players.stop(self._claim.key)
                self._server.carry_out()

    def _ask_next(self, play_id: str) -> None:
        if self._auto_for != play_id:
            self._auto_for, self._auto_tries, self._auto_at = play_id, 0, 0.0
        if self._clock() < self._auto_at:
            return
        if self._auto_tries >= AUTO_TRIES:
            with contextlib.suppress(NotPlaying, Stale):
                self._players.cancel(self._claim.key, play_id)
            return
        self._auto_tries += 1

        def ask() -> None:
            try:
                self._server.carry_on("auto", play_id, False)
            finally:
                self._auto_at = self._clock() + AUTO_WAIT_SECONDS

        self._work(ask)

    def _hand(self, play: dict[str, Any]) -> None:
        playing = self._players.stream(self._claim.key)
        if playing is None or playing[0] != play["play_id"]:
            return
        self._film = _Film(
            play_id=play["play_id"],
            stream_id=playing[1],
            url=play["url"],
            about=play,
            base_s=play["start_s"],
            at=self._clock(),
            position_s=play["start_s"],
            played_s=play["played_s"],
            pause=play["paused"],
        )
        self._load()

    def _load(self) -> None:
        """Hand the TV the film from its place on."""
        film = self._film
        if film is None:
            return
        self._failed = False
        place = film.position_s

        def load() -> None:
            # Whatever goes wrong, the film has failed rather than the thread.
            failed = True
            try:
                with self._claims.steering(self._claim.tv_id):
                    if not self._ours():
                        return
                    # Until it is known to be the same TV, it is left alone.
                    handed, self._handed = self._handed, False
                    self._renderer = self._server.reach(self._renderer)
                    self._handed = handed
                    media, base = self._server.film(film.url, place, film.about)
                    # Some refuse a film while anything plays.
                    with contextlib.suppress(TvError):
                        self._renderer.stop()
                    self._handed = True
                    self._renderer.start(media)
                film.base_s, film.position_s = base, max(place, base)
                film.at, film.on = self._clock(), False
                film.absolute = film.seen_playing = None
                failed = False
            except (TvError, CannotPlay) as exc:
                logger.warning("older tv: could not start the film (%s)", type(exc).__name__)
            finally:
                self._failed = failed

        self._work(load)

    def _obey(self, commands: list[dict[str, Any]]) -> None:
        for command in commands:
            film = self._film
            try:
                if film is not None:
                    with self._claims.steering(self._claim.tv_id):
                        if not self._ours():
                            return
                        reload = self._do(film, command)
                    if reload:
                        self._load()
            except TvFault as exc:
                logger.info("older tv: it refused to %s (%s)", command["kind"], exc.code)
            except TvError:
                self._away = True
                return
            self._ack = command["id"]
            if self._busy():
                return

    def _do(self, film: _Film, command: dict[str, Any]) -> bool:
        """Carry out a command; whether the film must be handed over anew for it."""
        kind = command["kind"]
        if kind == "play":
            film.pause = False
            try:
                self._renderer.play()
            except TvFault:
                # Some let go of a film that was paused for long.
                return True
        elif kind == "pause":
            self._renderer.pause()
        elif kind == "seek":
            film.position_s = command["position_s"]
            film.pause = film.paused
            return True
        elif kind == "volume":
            self._volume, self._muted = command["level"], False
            self._renderer.set_volume(round(self._volume * 100))
        elif kind == "mute" and command["muted"] != self._muted:
            if command["muted"]:
                self._unmuted = self._volume
            else:
                self._volume = self._unmuted
            self._renderer.set_volume(0 if command["muted"] else round(self._volume * 100))
            self._muted = command["muted"]
        return False


def _on_a_thread(do: Callable[[], None]) -> threading.Thread:
    def run() -> None:
        try:
            do()
        finally:
            close_db()

    thread = threading.Thread(target=run, name="older-tv-work", daemon=True)
    thread.start()
    return thread


_claims = Claims()


def get_claims() -> Claims:
    return _claims
