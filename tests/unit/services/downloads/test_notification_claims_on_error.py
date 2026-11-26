"""What happens to claimed download-ready emails when an error escapes a notification tick."""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.helpers.factories import insert_download_notification, insert_settings


@pytest.fixture
def conn(tmp_path, monkeypatch):
    from mediaman.db import init_db
    from mediaman.services.downloads import _notification_claims

    monkeypatch.setattr(_notification_claims, "_batch_stamps", set())
    monkeypatch.setattr(_notification_claims, "_sent_unrecorded", {})
    conn = init_db(str(tmp_path / "mm.db"))
    insert_settings(
        conn,
        mailgun_domain="test.example.com",
        mailgun_api_key="dummy-key",
        mailgun_from_address="noreply@test.example.com",
    )
    for tmdb_id in (1, 2):
        insert_download_notification(
            conn, email="user@example.com", title=f"Film {tmdb_id}", tmdb_id=tmdb_id
        )
    yield conn
    conn.close()


@pytest.fixture
def mailgun(monkeypatch):
    """Radarr says both films are there; the fake Mailgun records each email it is given."""
    radarr = MagicMock()
    radarr.get_movies.return_value = [
        {"tmdbId": 1, "hasFile": True},
        {"tmdbId": 2, "hasFile": True},
    ]
    client = MagicMock()
    client.sent = []
    client.send.side_effect = lambda to, subject, html: client.sent.append(subject)
    monkeypatch.setattr("mediaman.services.mail.mailgun.MailgunClient", lambda *a, **kw: client)
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: radarr)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: None)
    return client


def _tick(conn: sqlite3.Connection) -> None:
    from mediaman.services.downloads.notifications import check_download_notifications

    check_download_notifications(conn, secret_key="x" * 64)


def _notified(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT notified FROM download_notifications ORDER BY id")]


def test_a_batch_an_error_cut_short_before_any_send_is_freed_and_sent_on_the_next_tick(
    conn, mailgun, monkeypatch
):
    from mediaman.services.downloads import notifications

    real = notifications._get_notification_template
    monkeypatch.setattr(
        notifications, "_get_notification_template", MagicMock(side_effect=OSError("gone"))
    )
    with pytest.raises(OSError):
        _tick(conn)
    assert _notified(conn) == [0, 0]

    monkeypatch.setattr(notifications, "_get_notification_template", real)
    _tick(conn)
    assert len(mailgun.sent) == 2
    assert _notified(conn) == [1, 1]


def test_an_error_after_one_email_went_out_frees_only_the_rest_and_sends_none_twice(
    conn, mailgun, monkeypatch
):
    from mediaman.services.downloads import notifications

    real = notifications._build_email_payload
    calls = []

    def payload(row, *args):
        calls.append(row.id)
        if len(calls) == 2:
            raise KeyError("poster")
        return real(row, *args)

    monkeypatch.setattr(notifications, "_build_email_payload", payload)
    with pytest.raises(KeyError):
        _tick(conn)
    assert _notified(conn) == [1, 0]

    monkeypatch.setattr(notifications, "_build_email_payload", real)
    _tick(conn)
    assert len(mailgun.sent) == 2
    assert _notified(conn) == [1, 1]


def test_an_email_an_unknown_error_cut_short_mid_send_keeps_its_claim(conn, mailgun):
    """Whether it went out is not known, so it is left to the grace rather than sent again."""
    mailgun.send.side_effect = RuntimeError("cut off")
    with pytest.raises(RuntimeError):
        _tick(conn)
    assert _notified(conn) == [2, 0]


def test_being_told_to_end_frees_the_emails_a_tick_claimed_and_none_is_sent_twice(
    conn, mailgun, monkeypatch
):
    """A film on its way to a TV can hold off the lifespan's end until the process is killed."""
    from mediaman import app_factory
    from mediaman.services.media_meta import plex_stream
    from mediaman.web.routes import older_stream, tv

    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    gate = threading.Event()

    def movies():
        gate.wait(10)
        return [{"tmdbId": 1, "hasFile": True}, {"tmdbId": 2, "hasFile": True}]

    radarr = MagicMock()
    radarr.get_movies.side_effect = movies
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: radarr)
    manager = SimpleNamespace(reap=lambda: 0, tidy_orphans=lambda: 0, stop_all=lambda: None)
    monkeypatch.setattr(plex_stream, "init_stream_manager", lambda *a, **k: manager)
    monkeypatch.setattr(app_factory, "_tidy_stream_orphans", lambda tidy: None)
    monkeypatch.setattr(app_factory, "_start_stream_reaper", lambda reap, interval: lambda: None)
    monkeypatch.setattr(tv, "save_places", lambda: None)
    acts: list = []
    monkeypatch.setattr(app_factory, "_when_told_to_end", lambda act: acts.append(act))
    app_factory._start_watch_streams("k")

    def tick() -> None:
        other = sqlite3.connect(db_path, check_same_thread=False)
        other.row_factory = sqlite3.Row
        try:
            _tick(other)
        finally:
            other.close()

    ticking = threading.Thread(target=tick)
    ticking.start()
    try:
        for _ in range(500):
            if _notified(conn) == [2, 2]:
                break
            threading.Event().wait(0.01)
        told = threading.Thread(target=acts[0])
        told.start()
        told.join(10)
        assert _notified(conn) == [0, 0]
    finally:
        gate.set()
        ticking.join(10)
        older_stream.ending.clear()
    assert mailgun.sent == []

    _tick(conn)
    assert len(mailgun.sent) == 2
    assert _notified(conn) == [1, 1]
