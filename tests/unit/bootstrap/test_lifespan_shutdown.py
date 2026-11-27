"""The lifespan stops the Watch stream reaper before it closes the database."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from mediaman import app_factory
from mediaman.web.routes import tv


def test_the_stream_reaper_is_stopped_before_the_database_is_closed(monkeypatch):
    order: list[str] = []
    config = SimpleNamespace(port=8282, secret_key="k")
    monkeypatch.setattr(app_factory, "load_config", lambda: config)
    monkeypatch.setattr(app_factory, "bootstrap_db", lambda app, cfg: None)
    monkeypatch.setattr(app_factory, "bootstrap_crypto", lambda app, cfg: None)
    monkeypatch.setattr(app_factory, "bootstrap_scheduling", lambda app, cfg: False)
    monkeypatch.setattr(app_factory, "_run_startup_reconciliation", lambda app: None)
    monkeypatch.setattr(
        app_factory, "_start_watch_streams", lambda key: lambda: order.append("reaper stopped")
    )
    monkeypatch.setattr(app_factory, "_shutdown", lambda app, started: order.append("db closed"))

    async def run() -> None:
        async with app_factory.lifespan(FastAPI()):
            order.append("serving")

    asyncio.run(run())

    assert order == ["serving", "reaper stopped", "db closed"]


def test_shutdown_saves_the_places_then_stops_the_reaper_then_every_live_stream(monkeypatch):
    from mediaman.services.media_meta import plex_stream

    order: list[str] = []
    manager = SimpleNamespace(
        reap=lambda: 0,
        tidy_orphans=lambda: 0,
        stop_all=lambda: order.append("streams stopped"),
    )
    monkeypatch.setattr(plex_stream, "init_stream_manager", lambda *a, **k: manager)
    monkeypatch.setattr(app_factory, "_tidy_stream_orphans", lambda tidy: None)
    monkeypatch.setattr(
        app_factory, "_start_stream_reaper", lambda reap, interval: lambda: order.append("reaper")
    )

    monkeypatch.setattr(tv, "save_places", lambda: order.append("places"))

    app_factory._start_watch_streams("k")()

    assert order == ["places", "reaper", "streams stopped"]


def _told_to_end(monkeypatch, order: list[str]):
    """What is done at the signal, with each step noted in *order*."""
    from mediaman import db
    from mediaman.services.media_meta import plex_stream

    manager = SimpleNamespace(reap=lambda: 0, tidy_orphans=lambda: 0, stop_all=lambda: None)
    monkeypatch.setattr(plex_stream, "init_stream_manager", lambda *a, **k: manager)
    monkeypatch.setattr(app_factory, "_tidy_stream_orphans", lambda tidy: None)
    monkeypatch.setattr(app_factory, "_start_stream_reaper", lambda reap, interval: lambda: None)
    acts: list = []
    monkeypatch.setattr(app_factory, "_when_told_to_end", lambda act: acts.append(act))
    monkeypatch.setattr(db, "get_db", lambda: None)
    monkeypatch.setattr(db, "close_db", lambda: order.append("closed"))
    app_factory._start_watch_streams("k")
    return acts[0]


def test_at_the_signal_the_claims_are_given_back_after_the_places_even_where_saving_fails(
    monkeypatch,
):
    from mediaman.services.downloads import _notification_claims

    order: list[str] = []

    def save() -> None:
        order.append("places")
        raise RuntimeError("no database")

    monkeypatch.setattr(tv, "save_places", save)
    monkeypatch.setattr(
        _notification_claims, "release_claims_on_shutdown", lambda db: order.append("claims")
    )
    act = _told_to_end(monkeypatch, order)
    with pytest.raises(RuntimeError):
        act()
    assert order == ["places", "claims", "closed"]


def test_a_start_after_a_release_lets_a_tick_claim_again(monkeypatch):
    from mediaman.services.downloads import _notification_claims
    from mediaman.services.media_meta import plex_stream

    manager = SimpleNamespace(reap=lambda: 0, tidy_orphans=lambda: 0, stop_all=lambda: None)
    monkeypatch.setattr(plex_stream, "init_stream_manager", lambda *a, **k: manager)
    monkeypatch.setattr(app_factory, "_tidy_stream_orphans", lambda tidy: None)
    monkeypatch.setattr(app_factory, "_start_stream_reaper", lambda reap, interval: lambda: None)
    monkeypatch.setattr(app_factory, "_when_told_to_end", lambda act: None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE download_notifications (notified INT, claimed_at TEXT)")
    _notification_claims.release_claims_on_shutdown(conn)
    assert _notification_claims._ending.is_set()

    app_factory._start_watch_streams("k")

    assert not _notification_claims._ending.is_set()


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_what_is_done_at_the_signal_waits_for_no_open_response(number):
    """The lifespan's end comes after the last of them, and one may never end."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("only the main thread takes signals")
    heard: list[int] = []
    done = threading.Event()
    before = signal.signal(number, lambda received, frame: heard.append(received))
    try:
        mine = signal.getsignal(number)
        undo = app_factory._when_told_to_end(done.set)
        os.kill(os.getpid(), number)
        assert done.wait(5)
        assert heard == [number], "what was to hear it hears it"
        undo()
        assert signal.getsignal(number) is mine
    finally:
        signal.signal(number, before)


def test_elsewhere_than_the_main_thread_no_signal_is_taken():
    undone: list[bool] = []

    def start() -> None:
        app_factory._when_told_to_end(lambda: None)()
        undone.append(True)

    thread = threading.Thread(target=start)
    thread.start()
    thread.join(5)
    assert undone == [True]


def test_at_the_signal_the_database_is_closed_even_where_the_claims_cannot_be_given_back(
    monkeypatch,
):
    from mediaman.services.downloads import _notification_claims

    order: list[str] = []
    monkeypatch.setattr(tv, "save_places", lambda: None)

    def release(db) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(_notification_claims, "release_claims_on_shutdown", release)
    act = _told_to_end(monkeypatch, order)
    with pytest.raises(RuntimeError):
        act()
    assert order == ["closed"]
