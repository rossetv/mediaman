"""What happens to claimed download-ready emails when mediaman shuts down mid-tick."""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.helpers.factories import insert_download_notification, insert_settings


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    from mediaman.db import init_db
    from mediaman.services.downloads import _notification_claims
    from mediaman.services.downloads._notification_backoff import _backoff_state

    monkeypatch.setattr(_notification_claims, "_batch_stamps", set(), raising=False)
    monkeypatch.setattr(_notification_claims, "_sent_unrecorded", {})
    _backoff_state.clear()
    path = str(tmp_path / "mm.db")
    conn = init_db(path)
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
    conn.close()
    return path


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _notified(path: str) -> list[int]:
    conn = _connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT notified FROM download_notifications")]
    finally:
        conn.close()


def _wire(monkeypatch, *, movies_gate=None, send_gate=None):
    """Radarr says both films are there; return the list each sent email lands in."""
    sent: list[str] = []
    radarr = MagicMock()

    def movies():
        if movies_gate is not None:
            movies_gate.wait(10)
        return [{"tmdbId": 1, "hasFile": True}, {"tmdbId": 2, "hasFile": True}]

    radarr.get_movies.side_effect = movies
    mailgun = MagicMock()

    def send(to, subject, html):
        if send_gate is not None:
            send_gate.wait(10)
        sent.append(subject)

    mailgun.send.side_effect = send
    monkeypatch.setattr("mediaman.services.mail.mailgun.MailgunClient", lambda *a, **kw: mailgun)
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: radarr)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: None)
    return sent


def _tick(path: str) -> None:
    from mediaman.services.downloads.notifications import check_download_notifications

    conn = _connect(path)
    try:
        check_download_notifications(conn, secret_key="x" * 64)
    finally:
        conn.close()


def _shut_down(path: str) -> None:
    from mediaman.app_factory import _shutdown

    _shutdown(SimpleNamespace(state=SimpleNamespace(db=_connect(path))), False)


def _until(check) -> None:
    for _ in range(500):
        if check():
            return
        threading.Event().wait(0.01)
    raise AssertionError("condition never came true")


def test_emails_claimed_but_not_yet_sent_are_freed_at_shutdown_and_sent_once(db_path, monkeypatch):
    gate = threading.Event()
    sent = _wire(monkeypatch, movies_gate=gate)
    tick = threading.Thread(target=_tick, args=(db_path,))
    tick.start()
    _until(lambda: _notified(db_path) == [2, 2])

    _shut_down(db_path)
    assert _notified(db_path) == [0, 0]

    gate.set()
    tick.join(10)
    assert sent == []

    _tick(db_path)
    assert len(sent) == 2
    assert _notified(db_path) == [1, 1]


def test_an_email_on_its_way_out_at_shutdown_is_not_sent_again(db_path, monkeypatch):
    gate = threading.Event()
    sent = _wire(monkeypatch, send_gate=gate)
    tick = threading.Thread(target=_tick, args=(db_path,))
    tick.start()
    from mediaman.services.mail.mailgun import MailgunClient

    _until(lambda: MailgunClient().send.call_count == 1)

    _shut_down(db_path)
    gate.set()
    tick.join(10)
    _tick(db_path)

    assert sorted(sent) == sorted(set(sent))
    assert len(sent) == 2
    assert _notified(db_path) == [1, 1]


def test_an_email_sent_but_not_written_down_is_written_at_shutdown(db_path, monkeypatch):
    sent = _wire(monkeypatch)
    conn = _connect(db_path)
    conn.execute(
        "CREATE TRIGGER no_write BEFORE UPDATE OF notified ON download_notifications "
        "WHEN NEW.notified = 1 BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
    )
    conn.commit()
    _tick(db_path)
    conn.execute("DROP TRIGGER no_write")
    conn.commit()
    conn.close()

    _shut_down(db_path)

    assert _notified(db_path) == [1, 1]
    assert len(sent) == 2
