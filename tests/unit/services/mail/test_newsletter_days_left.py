"""Each scheduled item in the newsletter shows the days left until its own deadline."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.mail.newsletter import send_newsletter
from tests.helpers.factories import insert_media_item, insert_scheduled_action, insert_subscriber

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
    insert_subscriber(conn, email="alice@example.com")
    yield conn
    conn.close()


def _queue(conn, title: str, *, days_into_grace: float, grace_days: int = 7) -> None:
    """Queue *title* for deletion *days_into_grace* days ago, with a *grace_days* grace period."""
    insert_media_item(conn, id=title, title=title, plex_rating_key=title)
    scheduled_at = datetime.now(UTC) - timedelta(days=days_into_grace)
    insert_scheduled_action(
        conn,
        media_item_id=title,
        scheduled_at=scheduled_at.isoformat(),
        execute_at=(scheduled_at + timedelta(days=grace_days)).isoformat(),
        token=f"tok-{title}",
    )


def _cards(conn, *, manual: bool) -> dict[str, str]:
    """Send the newsletter and return each item's card text, keyed by title."""
    mailgun = MagicMock()
    with (
        patch("mediaman.services.mail.mailgun.MailgunClient", return_value=mailgun),
        patch("mediaman.services.infra.get_aggregate_disk_usage", return_value=_DISK),
        patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
        patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
    ):
        if manual:
            send_newsletter(
                conn, _SECRET_KEY, recipients=["alice@example.com"], mark_notified=False
            )
        else:
            send_newsletter(conn, _SECRET_KEY)
    html = mailgun.send.call_args.kwargs["html"]
    cards = {}
    for chunk in html.split('class="sched-card"')[1:]:
        title = re.search(r'class="item-title">([^<]*)<', chunk).group(1)
        cards[title] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk))
    return cards


@pytest.mark.parametrize("manual", [False, True], ids=["scan_send", "manual_send"])
def test_each_item_shows_the_days_left_of_its_own_deadline(conn, manual):
    _queue(conn, "Five Days In", days_into_grace=5)
    _queue(conn, "One Day In", days_into_grace=1)
    _queue(conn, "Long Grace", days_into_grace=5, grace_days=30)

    cards = _cards(conn, manual=manual)

    assert "2 days left" in cards["Five Days In"]
    assert "6 days left" in cards["One Day In"]
    assert "25 days left" in cards["Long Grace"]
    assert "14 days left" not in "".join(cards.values())


def test_the_last_day_reads_singular(conn):
    _queue(conn, "Last Day", days_into_grace=6)

    assert "1 day left" in _cards(conn, manual=False)["Last Day"]


def test_a_deadline_already_passed_shows_due_today_not_negative_days(conn):
    _queue(conn, "Overdue", days_into_grace=10)

    card = _cards(conn, manual=False)["Overdue"]

    assert "Due today" in card
    assert "days left" not in card
    assert "-3" not in card


@pytest.mark.parametrize(
    ("hours_away", "expected", "not_expected"),
    [(4, "Due today", "1 day left"), (26, "1 day left", "2 days left")],
    ids=["same_utc_date", "next_utc_date"],
)
def test_days_left_counts_utc_calendar_days_not_hours(conn, hours_away, expected, not_expected):
    now = datetime(2030, 6, 10, 10, 0, tzinfo=UTC)
    insert_media_item(conn, id="Soon", title="Soon", plex_rating_key="Soon")
    insert_scheduled_action(
        conn,
        media_item_id="Soon",
        scheduled_at=(now - timedelta(days=3)).isoformat(),
        execute_at=(now + timedelta(hours=hours_away)).isoformat(),
        token="tok-Soon",
    )

    with patch("mediaman.services.mail.newsletter.now_utc", return_value=now):
        card = _cards(conn, manual=False)["Soon"]

    assert expected in card
    assert not_expected not in card
