"""A later scan re-sends a newsletter only to the subscribers it has not reached."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.infra import SafeHTTPError
from mediaman.services.mail.newsletter import send_newsletter
from tests.helpers.factories import (
    insert_media_item,
    insert_scheduled_action,
    insert_subscriber,
    insert_suggestion,
)

_SECRET_KEY = "0123456789abcdef" * 4
_DISK = {"total_bytes": 10 << 40, "used_bytes": 5 << 40, "free_bytes": 5 << 40}


@pytest.fixture
def conn(db_path):
    conn = init_db(str(db_path))
    now = datetime.now(UTC).isoformat()
    for key, value in (
        ("mailgun_domain", "mg.example.com"),
        ("mailgun_api_key", "key-testvalue"),
        ("mailgun_from_address", "media@example.com"),
        ("base_url", "http://mediaman.local"),
    ):
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            (key, value, now),
        )
    conn.commit()
    for email in ("alice@example.com", "bob@example.com", "carol@example.com"):
        insert_subscriber(conn, email=email)
    yield conn
    conn.close()


class _Outbox:
    """Fake Mailgun: counts successful sends per address; *failing* addresses raise a 503."""

    def __init__(self) -> None:
        self.sent: Counter[str] = Counter()
        self.html: dict[str, str] = {}
        self.subject: dict[str, str] = {}
        self.failing: set[str] = set()

    def send(self, *, to: str, subject: str, html: str, unsubscribe_url: str | None = None) -> None:
        if to in self.failing:
            raise SafeHTTPError(status_code=503, body_snippet="down", url="u")
        self.sent[to] += 1
        self.html[to] = html
        self.subject[to] = subject

    def scan(self, conn) -> None:
        mailgun = MagicMock()
        mailgun.send.side_effect = self.send
        with (
            patch("mediaman.services.mail.mailgun.MailgunClient", return_value=mailgun),
            patch("mediaman.services.infra.get_aggregate_disk_usage", return_value=_DISK),
            patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
            patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
        ):
            send_newsletter(conn, _SECRET_KEY)


def _queue(conn, media_id: str, size_bytes: int = 1 << 20) -> int:
    insert_media_item(
        conn, id=media_id, title=media_id, plex_rating_key=media_id, file_size_bytes=size_bytes
    )
    return insert_scheduled_action(conn, media_item_id=media_id, token=f"tok-{media_id}")


def _notified(conn, action_id: int) -> int:
    return conn.execute(
        "SELECT notified FROM scheduled_actions WHERE id = ?", (action_id,)
    ).fetchone()["notified"]


def test_a_later_scan_sends_only_to_the_subscriber_whose_send_failed(conn):
    action_id = _queue(conn, "m1")
    outbox = _Outbox()
    outbox.failing = {"bob@example.com"}

    outbox.scan(conn)
    outbox.failing = set()
    outbox.scan(conn)

    assert outbox.sent == {"alice@example.com": 1, "carol@example.com": 1, "bob@example.com": 1}
    assert _notified(conn, action_id) == 1


def test_a_failed_send_does_not_erase_an_earlier_success_for_the_same_item(conn):
    first = _queue(conn, "m1")
    outbox = _Outbox()
    outbox.failing = {"bob@example.com"}
    outbox.scan(conn)  # alice and carol are served the first item; bob is not

    second = _queue(conn, "m2")
    outbox.failing = {"alice@example.com", "bob@example.com"}
    outbox.scan(conn)  # the new item makes alice due again, and her send fails

    row = conn.execute(
        "SELECT sent_at FROM newsletter_deliveries WHERE scheduled_action_id = ? AND recipient = ?",
        (first, "alice@example.com"),
    ).fetchone()
    assert row["sent_at"] is not None

    outbox.failing = set()
    outbox.scan(conn)
    assert _notified(conn, first) == 1
    assert _notified(conn, second) == 1


def test_a_subscriber_already_served_still_gets_new_recommendations_without_the_old_item(conn):
    _queue(conn, "OldMovie")
    outbox = _Outbox()
    outbox.failing = {"bob@example.com"}
    outbox.scan(conn)  # alice and carol get OldMovie; bob keeps failing

    insert_suggestion(conn, title="FreshPick", batch_id="b1")
    outbox.scan(conn)

    assert outbox.sent["alice@example.com"] == 2
    assert "FreshPick" in outbox.html["alice@example.com"]
    assert "OldMovie" not in outbox.html["alice@example.com"]


def test_a_subscriber_already_served_gets_nothing_when_there_is_nothing_new(conn):
    _queue(conn, "OldMovie")
    outbox = _Outbox()
    outbox.failing = {"bob@example.com"}
    outbox.scan(conn)

    outbox.scan(conn)

    assert outbox.sent["alice@example.com"] == 1
    assert outbox.sent["carol@example.com"] == 1


def test_items_are_marked_notified_once_the_last_subscriber_is_reached(conn):
    action_id = _queue(conn, "OldMovie")
    outbox = _Outbox()
    outbox.failing = {"bob@example.com"}
    outbox.scan(conn)
    insert_suggestion(conn, title="FreshPick", batch_id="b1")

    outbox.failing = set()
    outbox.scan(conn)

    assert _notified(conn, action_id) == 1
    assert outbox.sent["bob@example.com"] == 1
    assert "OldMovie" in outbox.html["bob@example.com"]


def test_a_partial_copys_subject_counts_only_the_items_left_for_that_subscriber(conn):
    _queue(conn, "FirstMovie", size_bytes=1 << 30)
    _queue(conn, "SecondMovie", size_bytes=1 << 30)
    outbox = _Outbox()
    outbox.failing = {"bob@example.com"}
    outbox.scan(conn)  # alice and carol are served both; bob is not
    _queue(conn, "ThirdMovie", size_bytes=3 << 30)

    outbox.failing = set()
    outbox.scan(conn)

    assert "1 item scheduled" in outbox.subject["alice@example.com"]
    assert "3.0 GB" in outbox.subject["alice@example.com"]
    assert "3 items scheduled" in outbox.subject["bob@example.com"]
    assert "5.0 GB" in outbox.subject["bob@example.com"]


def test_a_recommendations_only_newsletter_has_a_plain_subject(conn):
    insert_suggestion(conn, title="FreshPick", batch_id="b1")
    outbox = _Outbox()

    outbox.scan(conn)

    assert outbox.subject["alice@example.com"] == "Mediaman Weekly Report"
