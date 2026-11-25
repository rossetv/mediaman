"""A later scan re-sends a newsletter only to the subscribers it has not reached."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.crypto import generate_keep_token, validate_download_token
from mediaman.crypto.tokens import keep_token_expiry
from mediaman.db import init_db
from mediaman.services.infra import SafeHTTPError
from mediaman.services.mail.newsletter import render, send_newsletter
from tests.helpers.factories import (
    insert_media_item,
    insert_scheduled_action,
    insert_subscriber,
    insert_suggestion,
    insert_user_pick,
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

    insert_suggestion(conn, title="FreshPick", batch_id="b1", tmdb_id=101)
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
    insert_suggestion(conn, title="FreshPick", batch_id="b1", tmdb_id=102)

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
    insert_suggestion(conn, title="FreshPick", batch_id="b1", tmdb_id=103)
    outbox = _Outbox()

    outbox.scan(conn)

    assert outbox.subject["alice@example.com"] == "Mediaman Weekly Report"


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


def _person(conn, username: str, email: str | None) -> None:
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, email)"
        " VALUES (?, 'x', '2020', ?)",
        (username, email),
    )
    conn.commit()


def test_each_subscriber_gets_their_own_picks_and_the_shared_ones_and_no_one_elses(conn):
    _person(conn, "al", "Alice@Example.com")
    _person(conn, "bo", "bob@example.com")
    insert_suggestion(conn, title="SharedPick", batch_id="b1", tmdb_id=104)
    insert_suggestion(conn, title="HouseholdPick", category="personal", batch_id="b1", tmdb_id=105)
    insert_user_pick(
        conn, "al", title="AlsPick", batch_id=_days_ago(3), tmdb_id=5, because_of='["AlsFilm"]'
    )
    insert_user_pick(conn, "al", title="AlsOldPick", batch_id=_days_ago(9), tmdb_id=106)
    insert_user_pick(conn, "bo", title="BosPick", batch_id=_days_ago(1), tmdb_id=107)
    outbox = _Outbox()

    outbox.scan(conn)

    shown = {
        to.split("@")[0]: {
            title
            for title in ("SharedPick", "HouseholdPick", "AlsPick", "AlsOldPick", "BosPick")
            if title in html
        }
        for to, html in outbox.html.items()
    }
    assert shown == {
        "alice": {"SharedPick", "AlsPick"},
        "bob": {"SharedPick", "BosPick"},
        "carol": {"SharedPick"},
    }
    assert all("AlsFilm" not in html for html in outbox.html.values())
    assert "AI picks" in outbox.html["alice@example.com"]


def test_an_address_two_people_share_gets_neithers_picks(conn):
    _person(conn, "al", "alice@example.com")
    _person(conn, "ally", "ALICE@example.com")
    insert_suggestion(conn, title="SharedPick", batch_id="b1", tmdb_id=108)
    insert_user_pick(conn, "al", title="AlsPick", tmdb_id=109)
    insert_user_pick(conn, "ally", title="AllysPick", tmdb_id=110)
    outbox = _Outbox()

    outbox.scan(conn)

    html = outbox.html["alice@example.com"]
    assert "SharedPick" in html
    assert "AlsPick" not in html and "AllysPick" not in html


def test_picks_of_one_person_send_no_empty_email_to_the_others(conn):
    _person(conn, "al", "alice@example.com")
    insert_user_pick(conn, "al", title="AlsPick", tmdb_id=111)
    outbox = _Outbox()

    outbox.scan(conn)

    assert outbox.sent == {"alice@example.com": 1}


def test_picks_tmdb_does_not_vouch_for_are_neither_mailed_nor_worth_an_email(conn):
    _person(conn, "al", "alice@example.com")
    insert_suggestion(conn, title="SharedPick", batch_id="b1", tmdb_id=None)
    insert_user_pick(conn, "al", title="AlsPick", tmdb_id=None)
    outbox = _Outbox()

    outbox.scan(conn)
    assert outbox.sent == {}

    insert_suggestion(conn, title="KnownPick", batch_id="b1", tmdb_id=7)
    outbox.scan(conn)
    html = outbox.html["alice@example.com"]
    assert "KnownPick" in html
    assert "SharedPick" not in html and "AlsPick" not in html


def test_own_picks_over_28_days_old_are_neither_mailed_nor_worth_an_email(conn):
    _person(conn, "al", "alice@example.com")
    _person(conn, "bo", "bob@example.com")
    insert_user_pick(conn, "al", title="AlsPick", batch_id=_days_ago(29), tmdb_id=112)
    insert_user_pick(conn, "bo", title="BosPick", batch_id=_days_ago(27), tmdb_id=113)
    outbox = _Outbox()

    outbox.scan(conn)

    assert outbox.sent == {"bob@example.com": 1}
    assert "BosPick" in outbox.html["bob@example.com"]


def test_an_own_pick_rated_a_strong_no_is_not_mailed_to_that_person_and_one_rated_down_is_last(
    conn,
):
    _person(conn, "al", "alice@example.com")
    _person(conn, "bo", "bob@example.com")
    for user, n in (("al", 0), ("bo", 10)):
        for i, title in enumerate(("DownPick", "NoPick", "PlainPick")):
            insert_user_pick(conn, user, title=title, media_type="movie", tmdb_id=200 + n + i)
    insert_suggestion(conn, title="SharedPick", media_type="movie", batch_id="b1", tmdb_id=250)
    conn.executemany(
        "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, updated_at)"
        " SELECT id, 'movie', ?, ?, '2026' FROM admin_users WHERE username = ?",
        # bo's ratings are of titles only al has picked
        [(200, -1, "al"), (201, -2, "al"), (250, -2, "al"), (202, -2, "bo")],
    )
    conn.commit()
    outbox = _Outbox()

    outbox.scan(conn)

    alice, bob = outbox.html["alice@example.com"], outbox.html["bob@example.com"]
    assert "NoPick" not in alice and "NoPick" in bob
    assert alice.index("PlainPick") < alice.index("DownPick")
    assert "SharedPick" in alice


def test_the_link_of_an_own_pick_names_the_title_and_no_stored_pick(conn):
    _person(conn, "al", "alice@example.com")
    insert_user_pick(conn, "al", title="AlsPick", tmdb_id=5)
    outbox = _Outbox()

    outbox.scan(conn)

    [token] = re.findall(r"/download/([^\"?]+)", outbox.html["alice@example.com"])
    payload = validate_download_token(token, _SECRET_KEY)
    assert (payload["title"], payload["tmdb"], payload["sid"]) == ("AlsPick", 5, None)


@pytest.mark.parametrize("tmdb_id", [5, None])
def test_a_title_among_an_own_batch_and_a_later_shared_one_is_shown_once(conn, tmdb_id):
    _person(conn, "al", "alice@example.com")
    insert_suggestion(conn, title="BothPick", tmdb_id=5, batch_id=_days_ago(0))
    insert_user_pick(conn, "al", title="BothPick", tmdb_id=tmdb_id, batch_id=_days_ago(8))
    outbox = _Outbox()

    outbox.scan(conn)

    counts = {to: html.count(">BothPick</div>") for to, html in outbox.html.items()}
    assert counts["alice@example.com"] == counts["bob@example.com"] == 1


def _queue_many(conn, count: int) -> dict[int, str]:
    """Queue *count* titles leaving a day apart, the latest first, each with a working Keep link;
    their names by action."""
    now = datetime.now(UTC)
    titles = {}
    for day in range(count, 0, -1):
        name = f"Title{day:03}x"
        insert_media_item(conn, id=name, title=name, plex_rating_key=name)
        execute_at = now + timedelta(days=day, hours=1)
        action_id = insert_scheduled_action(
            conn,
            media_item_id=name,
            token=f"tok-{name}",
            scheduled_at=now.isoformat(),
            execute_at=execute_at.isoformat(),
        )
        token = generate_keep_token(
            media_item_id=name,
            action_id=action_id,
            expires_at=keep_token_expiry(now, execute_at),
            secret_key=_SECRET_KEY,
        )
        conn.execute(
            "UPDATE scheduled_actions SET token_hash = ? WHERE id = ?",
            (hashlib.sha256(token.encode()).hexdigest(), action_id),
        )
        titles[action_id] = name
    conn.commit()
    return titles


def _names(html: str) -> list[str]:
    """The titles a mail names, in order, each as often as it names it."""
    return re.findall(r">(Title\d{3}x)<", html)


class _Log(_Outbox):
    """Keeps every mail, not only the latest of each address."""

    def __init__(self) -> None:
        super().__init__()
        self.mails: dict[str, list[str]] = {}

    def send(self, *, to: str, subject: str, html: str, unsubscribe_url: str | None = None) -> None:
        super().send(to=to, subject=subject, html=html, unsubscribe_url=unsubscribe_url)
        self.mails.setdefault(to, []).append(html)

    def named(self, to: str) -> list[str]:
        return [name for html in self.mails.get(to, []) for name in _names(html)]


def test_titles_past_what_one_mail_holds_are_named_in_the_next_ones_soonest_first(conn):
    titles = _queue_many(conn, 300)
    outbox = _Log()

    outbox.scan(conn)
    first = _names(outbox.html["alice@example.com"])
    assert 80 < len(first) < 300
    assert outbox.html["alice@example.com"].count("/keep/") == len(first)
    assert {titles[a] for a in titles if _notified(conn, a)} == set(first)
    for _ in range(5):
        outbox.scan(conn)

    for to in ("alice@example.com", "bob@example.com", "carol@example.com"):
        in_turn = [name for html in outbox.mails[to] for name in sorted(_names(html))]
        assert in_turn == sorted(titles.values())
    assert all(_notified(conn, a) for a in titles)
    sent = dict(outbox.sent)
    outbox.scan(conn)
    assert outbox.sent == sent


def test_a_title_is_marked_only_once_each_person_was_shown_it(conn):
    titles = _queue_many(conn, 300)
    outbox = _Log()
    outbox.failing = {"bob@example.com"}

    outbox.scan(conn)  # alice and carol are shown the first ones, bob nothing
    outbox.scan(conn)  # they are shown the next ones

    assert not any(_notified(conn, a) for a in titles)
    outbox.failing = set()
    outbox.scan(conn)
    shown_to_all = set(outbox.named("bob@example.com")) & set(outbox.named("alice@example.com"))
    assert {titles[a] for a in titles if _notified(conn, a)} == shown_to_all
    assert len(shown_to_all) < 300

    for _ in range(5):
        outbox.scan(conn)
    for to in ("alice@example.com", "bob@example.com", "carol@example.com"):
        assert sorted(outbox.named(to)) == sorted(titles.values())
    assert all(_notified(conn, a) for a in titles)


def test_a_title_one_persons_mail_had_no_room_for_waits_for_that_person_alone(conn):
    titles = _queue_many(conn, 125)
    conn.execute("DELETE FROM subscribers WHERE email = 'carol@example.com'")
    conn.commit()
    outbox = _Log()
    real = render._fits

    def fits_fewer_for_bob(html: str) -> bool:
        return real(html) and (outbox.sent["alice@example.com"] == 0 or len(_names(html)) < 50)

    with patch.object(render, "_fits", fits_fewer_for_bob):
        outbox.scan(conn)

    shown = {to: set(outbox.named(to)) for to in ("alice@example.com", "bob@example.com")}
    assert len(shown["alice@example.com"]) > len(shown["bob@example.com"]) > 0
    assert {titles[a] for a in titles if _notified(conn, a)} == shown["bob@example.com"]
    outbox.scan(conn)
    outbox.scan(conn)
    for to in shown:
        assert sorted(outbox.named(to)) == sorted(titles.values())


def test_a_manual_send_of_a_long_list_records_nothing(conn):
    titles = _queue_many(conn, 300)
    outbox = _Log()
    mailgun = MagicMock()
    mailgun.send.side_effect = outbox.send
    with (
        patch("mediaman.services.mail.mailgun.MailgunClient", return_value=mailgun),
        patch("mediaman.services.infra.get_aggregate_disk_usage", return_value=_DISK),
        patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
        patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
    ):
        for _ in range(2):
            send_newsletter(
                conn, _SECRET_KEY, recipients=["alice@example.com"], mark_notified=False
            )

    first, second = (_names(html) for html in outbox.mails["alice@example.com"])
    assert first == second == sorted(titles.values())[: len(first)]
    assert not any(_notified(conn, a) for a in titles)
    assert conn.execute("SELECT COUNT(*) FROM newsletter_deliveries").fetchone()[0] == 0
