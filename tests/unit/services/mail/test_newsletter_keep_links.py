"""Keep buttons in the newsletter carry a token the /keep route accepts."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.crypto import generate_keep_token, validate_keep_token
from mediaman.db import init_db
from mediaman.scanner.phases.upsert import schedule_deletion
from mediaman.services.mail.newsletter import send_newsletter
from mediaman.services.scheduled_actions import lookup_verified_action
from tests.helpers.factories import insert_media_item, insert_scheduled_action, insert_subscriber

_SECRET_KEY = "0123456789abcdef" * 4
_BASE_URL = "http://mediaman.local"
_DISK = {"total_bytes": 10 << 40, "used_bytes": 5 << 40, "free_bytes": 5 << 40}
_KEEP_HREF = re.compile(r'href="' + re.escape(_BASE_URL) + r'/keep/([^"]*)"')


@pytest.fixture
def conn(db_path):
    conn = init_db(str(db_path))
    now = datetime.now(UTC).isoformat()
    for key, value in (
        ("mailgun_domain", "mg.example.com"),
        ("mailgun_api_key", "key-testvalue"),
        ("mailgun_from_address", "media@example.com"),
        ("base_url", _BASE_URL),
    ):
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            (key, value, now),
        )
    conn.commit()
    insert_subscriber(conn, email="alice@example.com")
    insert_media_item(conn, id="mi1", title="Test Movie", plex_rating_key="mi1")
    yield conn
    conn.close()


def _send(conn, *, manual: bool = False, grace_days: int = 14) -> str:
    """Send the newsletter the way the scan (or the admin's manual send) does; return the HTML."""
    mailgun = MagicMock()
    with (
        patch("mediaman.services.mail.mailgun.MailgunClient", return_value=mailgun),
        patch("mediaman.services.infra.get_aggregate_disk_usage", return_value=_DISK),
        patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
        patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
    ):
        if manual:
            send_newsletter(
                conn,
                _SECRET_KEY,
                grace_days=grace_days,
                recipients=["alice@example.com"],
                mark_notified=False,
            )
        else:
            send_newsletter(conn, _SECRET_KEY, grace_days=grace_days)
    mailgun.send.assert_called_once()
    return mailgun.send.call_args.kwargs["html"]


def _token_hash(conn, action_id: int) -> str:
    return conn.execute(
        "SELECT token_hash FROM scheduled_actions WHERE id = ?", (action_id,)
    ).fetchone()["token_hash"]


def _schedule(conn, grace_days: int) -> int:
    schedule_deletion(
        conn, media_id="mi1", is_reentry=False, grace_days=grace_days, secret_key=_SECRET_KEY
    )
    conn.commit()
    return conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='mi1'").fetchone()[
        "id"
    ]


@pytest.mark.parametrize("manual", [False, True], ids=["scan_send", "manual_send"])
def test_keep_button_opens_the_items_keep_page(conn, manual):
    action_id = _schedule(conn, grace_days=14)

    tokens = _KEEP_HREF.findall(_send(conn, manual=manual))

    assert len(tokens) == 1
    token = tokens[0]
    assert hashlib.sha256(token.encode()).hexdigest() == _token_hash(conn, action_id)
    row = lookup_verified_action(conn, token, _SECRET_KEY)
    assert row is not None and row.id == action_id
    raw = conn.execute("SELECT token FROM scheduled_actions WHERE id = ?", (action_id,)).fetchone()[
        "token"
    ]
    assert raw is None


def test_row_minted_under_the_thirty_day_rule_still_gets_its_keep_link(conn):
    scheduled_at = datetime.now(UTC) - timedelta(days=2)
    execute_at = scheduled_at + timedelta(days=60)
    action_id = insert_scheduled_action(
        conn,
        media_item_id="mi1",
        scheduled_at=scheduled_at.isoformat(),
        execute_at=execute_at.isoformat(),
        token="placeholder",
    )
    legacy = generate_keep_token(
        media_item_id="mi1",
        action_id=action_id,
        expires_at=int((scheduled_at + timedelta(days=30)).timestamp()),
        secret_key=_SECRET_KEY,
    )
    conn.execute(
        "UPDATE scheduled_actions SET token = NULL, token_hash = ? WHERE id = ?",
        (hashlib.sha256(legacy.encode()).hexdigest(), action_id),
    )
    conn.commit()

    assert _KEEP_HREF.findall(_send(conn)) == [legacy]


def test_grace_setting_changed_after_scheduling_keeps_the_link(conn):
    action_id = _schedule(conn, grace_days=45)
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES ('grace_days', '7', 0, ?)",
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()

    tokens = _KEEP_HREF.findall(_send(conn, grace_days=7))

    assert [hashlib.sha256(t.encode()).hexdigest() for t in tokens] == [
        _token_hash(conn, action_id)
    ]


def test_keep_link_with_long_grace_works_until_the_deletion_day(conn):
    action_id = _schedule(conn, grace_days=60)
    (token,) = _KEEP_HREF.findall(_send(conn))
    execute_at = datetime.fromisoformat(
        conn.execute(
            "SELECT execute_at FROM scheduled_actions WHERE id = ?", (action_id,)
        ).fetchone()["execute_at"]
    )

    last_hour = (execute_at - timedelta(hours=1)).timestamp()
    with patch("mediaman.crypto.tokens.time.time", return_value=last_hour):
        assert validate_keep_token(token, _SECRET_KEY) is not None


def test_unrebuildable_row_hides_keep_button_and_logs_the_row(conn, caplog):
    action_id = insert_scheduled_action(conn, media_item_id="mi1", token="tok-not-signed")

    with caplog.at_level(logging.WARNING, logger="mediaman.services.mail.newsletter"):
        html = _send(conn)

    assert "Test Movie" in html
    assert "/keep/" not in html
    assert ">Keep</a>" not in html
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(action_id) in m for m in warnings)
    assert not any("tok-not-signed" in m for m in warnings)


def test_row_whose_keep_link_has_expired_hides_the_button_and_logs_the_row(conn, caplog):
    scheduled_at = datetime.now(UTC) - timedelta(days=40)
    execute_at = scheduled_at + timedelta(days=60)
    action_id = insert_scheduled_action(
        conn,
        media_item_id="mi1",
        scheduled_at=scheduled_at.isoformat(),
        execute_at=execute_at.isoformat(),
        token="placeholder",
    )
    legacy = generate_keep_token(
        media_item_id="mi1",
        action_id=action_id,
        expires_at=int((scheduled_at + timedelta(days=30)).timestamp()),
        secret_key=_SECRET_KEY,
    )
    conn.execute(
        "UPDATE scheduled_actions SET token = NULL, token_hash = ? WHERE id = ?",
        (hashlib.sha256(legacy.encode()).hexdigest(), action_id),
    )
    conn.commit()

    with caplog.at_level(logging.WARNING, logger="mediaman.services.mail.newsletter"):
        html = _send(conn)

    assert "Test Movie" in html
    assert "/keep/" not in html
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(action_id) in m for m in warnings)
    assert not any(legacy in m for m in warnings)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("delete_status", "deleting"),
        ("execute_at", (datetime.now(UTC) - timedelta(hours=2)).isoformat()),
    ],
    ids=["being_deleted", "overdue"],
)
def test_row_the_keep_page_would_refuse_is_listed_without_a_keep_button(conn, column, value):
    _schedule(conn, grace_days=14)
    assert len(_KEEP_HREF.findall(_send(conn, manual=True))) == 1
    conn.execute(f"UPDATE scheduled_actions SET {column} = ?", (value,))
    conn.commit()

    html = _send(conn, manual=True)

    assert "Test Movie" in html
    assert "/keep/" not in html
