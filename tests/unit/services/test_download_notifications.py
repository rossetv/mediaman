"""Tests for the download-ready email path.

The outer ``check_download_notifications`` function coordinates DB,
Mailgun, Radarr, and Sonarr — too much infrastructure for a unit test.
These tests focus on the security-critical part: the Jinja template
must escape every TMDB-sourced field so a malicious free-text value
(e.g. a crafted director string) cannot inject HTML/JS into the email.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from tests.helpers.factories import insert_download_notification, insert_settings


def _load_template():
    template_dir = (
        Path(__file__).parent.parent.parent.parent / "src" / "mediaman" / "web" / "templates"
    )
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    return env.get_template("email/download_ready.html")


def _render(**overrides):
    template = _load_template()
    defaults = {
        "title": "Example",
        "poster_src": "",
        "meta": {
            "year": "2026",
            "media_label": "Movie",
            "runtime": "120",
            "director": "Jane Doe",
        },
        "ratings": {
            "rating": "",
            "imdb_rating": "",
            "rt_rating": "",
        },
        "description": "",
    }
    defaults.update(overrides)
    return template.render(**defaults)


class TestDownloadReadyTemplate:
    def test_renders_with_basic_context(self):
        html = _render()
        assert "Example" in html
        assert "2026" in html
        assert "Directed by Jane Doe" in html
        assert "READY TO WATCH" in html

    def test_title_escapes_html(self):
        """A crafted title (e.g. Plex metadata tampering) must be escaped."""
        html = _render(title="<script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_director_escapes_html(self):
        """Director is a TMDB free-text field and MUST NOT inject HTML."""
        meta = {
            "year": "2026",
            "media_label": "Movie",
            "runtime": "90",
            "director": '"><img src=x onerror=alert(1)>',
        }
        html = _render(meta=meta)
        # The raw `<img ...>` tag must never appear — every `<` has to be escaped.
        assert "<img src=x" not in html
        assert "<img " not in html
        # The escaped form must be present — `<` becomes `&lt;`.
        assert "&lt;img" in html
        # The stray `>` at the end must also be escaped.
        assert "&gt;" in html

    def test_description_escapes_html(self):
        html = _render(description='</div><script>alert("xss")</script>')
        assert "<script>" not in html
        assert "&lt;/div&gt;" in html

    def test_ratings_escape_html(self):
        """Rating fields come from OMDb/TMDB — still untrusted."""
        ratings = {
            "rating": "8.2",
            "imdb_rating": '7.5"><script>alert(1)</script>',
            "rt_rating": "92%",
        }
        html = _render(ratings=ratings)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        # The benign ratings still render visibly.
        assert "8.2" in html
        assert "92%" in html

    def test_meta_line_uses_middle_dot_separator(self):
        """The meta line preserves the original &middot; separator between parts."""
        html = _render()
        # &middot; is a template literal so Jinja's autoescape leaves it
        # alone; the email client sees the middle-dot character.
        assert "&middot;" in html
        # Parts are present around the separator.
        assert "Movie" in html
        assert "120 min" in html

    def test_no_safe_filter_on_new_variables(self):
        """Regression guard: the template must not reintroduce ``|safe`` on
        user-sourced data. If someone adds it back, this test fails."""
        template_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "mediaman"
            / "web"
            / "templates"
            / "email"
            / "download_ready.html"
        )
        src = template_path.read_text()
        assert "|safe" not in src and "| safe" not in src, (
            "download_ready.html must not use |safe — every field is untrusted TMDB/OMDb data."
        )

    def test_ratings_section_hidden_when_all_empty(self):
        html = _render()
        # No rating row at all when nothing is present.
        assert "IMDb" not in html
        assert "&#9733;" not in html

    def test_poster_rendered_when_present(self):
        html = _render(poster_src="https://image.tmdb.org/t/p/w500/abc.jpg")
        assert "https://image.tmdb.org/t/p/w500/abc.jpg" in html


class TestSonarrSeriesMatching:
    """Regression: Sonarr completion matches on TVDB id, not TMDB.

    Previously the Sonarr path stored a TVDB id in the tmdb_id column
    and then compared it against series' ``tmdbId`` field. Series added
    by TVDB-only carry ``tmdbId=None``, so the match always failed and
    the notification never fired.
    """

    def _setup(self, tmp_path, monkeypatch):
        """Wire a minimal DB and stub out Mailgun + Radarr; return conn."""
        from unittest.mock import MagicMock

        from mediaman.db import init_db

        conn = init_db(str(tmp_path / "mm.db"))

        # Settings needed by the module — Mailgun must be configured
        # enough to proceed past the early-bail guard.
        insert_settings(conn, mailgun_domain="test.example.com", mailgun_api_key="dummy-key")

        # Stub Mailgun + arr builders so the test never hits the network.
        # ``check_download_notifications`` imports ``MailgunClient`` inside
        # the function body, so we patch the source module attribute —
        # that's what the local import resolves.
        sent_calls: list[dict] = []
        mailgun_stub = MagicMock()
        mailgun_stub.send.side_effect = lambda to, subject, html: sent_calls.append(
            {"to": to, "subject": subject}
        )
        monkeypatch.setattr(
            "mediaman.services.mail.mailgun.MailgunClient",
            lambda *a, **kw: mailgun_stub,
        )

        return conn, sent_calls

    def test_tvdb_only_series_matches(self, tmp_path, monkeypatch):
        """Series with tmdbId=None must still flip notified=1 via tvdbId."""
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn, sent = self._setup(tmp_path, monkeypatch)

        # Insert a pending Sonarr row with only a TVDB id.
        insert_download_notification(conn, email="user@example.com", title="Severance",
                                     media_type="tv", tvdb_id=370524, service="sonarr")

        # Sonarr reports the series with tmdbId=None but a matching tvdbId.
        sonarr_client = MagicMock()
        sonarr_client.get_series.return_value = [
            {
                "tvdbId": 370524,
                "tmdbId": None,
                "title": "Severance",
                "statistics": {"episodeFileCount": 9},
            }
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_sonarr_from_db",
            lambda *a, **kw: sonarr_client,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: None,
        )

        check_download_notifications(conn, secret_key="x" * 64)

        row = conn.execute("SELECT notified FROM download_notifications").fetchone()
        assert row["notified"] == 1
        assert sent, "Mailgun send should have been invoked"

    def test_tvdb_mismatch_leaves_row_pending(self, tmp_path, monkeypatch):
        """If Sonarr doesn't have the series yet, notified stays 0."""
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn, sent = self._setup(tmp_path, monkeypatch)

        insert_download_notification(conn, email="user@example.com", title="Nobody Home",
                                     media_type="tv", tvdb_id=999999, service="sonarr")

        sonarr_client = MagicMock()
        sonarr_client.get_series.return_value = [
            {"tvdbId": 111, "tmdbId": None, "title": "Other", "statistics": {"episodeFileCount": 1}}
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_sonarr_from_db",
            lambda *a, **kw: sonarr_client,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: None,
        )

        check_download_notifications(conn, secret_key="x" * 64)

        row = conn.execute("SELECT notified FROM download_notifications").fetchone()
        assert row["notified"] == 0
        assert not sent

    def test_concurrent_check_does_not_double_send(self, tmp_path, monkeypatch):
        """Finding 22: a second pass while the first is still working
        must not pick up the same row.

        Simulates a scheduler tick that re-enters before the previous
        tick has finished by manually claiming the row first, then
        running ``check_download_notifications``: nothing should be
        sent because all rows are already claimed.
        """
        from unittest.mock import MagicMock

        from mediaman.db import init_db
        from mediaman.services.downloads.notifications import (
            _claim_pending_notifications,
            check_download_notifications,
        )

        conn = init_db(str(tmp_path / "mm.db"))
        insert_settings(conn, mailgun_domain="test.example.com", mailgun_api_key="dummy-key")
        insert_download_notification(conn, email="user@example.com", title="Item", tmdb_id=1)

        # Worker A claims the row first.
        claimed = _claim_pending_notifications(conn)
        assert len(claimed) == 1

        # Worker B then runs — must find nothing to do.
        sent_calls: list[dict] = []
        mailgun_stub = MagicMock()
        mailgun_stub.send.side_effect = lambda to, subject, html: sent_calls.append(
            {"to": to, "subject": subject}
        )
        monkeypatch.setattr(
            "mediaman.services.mail.mailgun.MailgunClient",
            lambda *a, **kw: mailgun_stub,
        )
        check_download_notifications(conn, secret_key="x" * 64)
        assert sent_calls == []

    def test_claim_then_release_allows_retry(self, tmp_path):
        """Releasing a claim must roll the row back to notified=0."""
        from mediaman.db import init_db
        from mediaman.services.downloads.notifications import (
            _claim_pending_notifications,
            _release_claim,
        )

        conn = init_db(str(tmp_path / "mm.db"))
        insert_download_notification(conn, email="user@example.com", title="Item", tmdb_id=1)

        rows = _claim_pending_notifications(conn)
        assert len(rows) == 1
        _release_claim(conn, rows[0]["id"])

        # Visible to a follow-up claim again.
        rows2 = _claim_pending_notifications(conn)
        assert len(rows2) == 1

    def test_mailgun_failure_releases_claim(self, tmp_path, monkeypatch):
        """A Mailgun-misconfigured run must release every claim it took."""
        from mediaman.db import init_db
        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn = init_db(str(tmp_path / "mm.db"))
        # No Mailgun config at all — settings table empty.
        insert_download_notification(conn, email="user@example.com", title="Item", tmdb_id=1)

        check_download_notifications(conn, secret_key="x" * 64)

        row = conn.execute("SELECT notified FROM download_notifications").fetchone()
        # Row must be returned to notified=0 so a future tick (after
        # Mailgun is configured) can pick it up.
        assert row["notified"] == 0


class TestReconcileStrandedNotifications:
    """H-5: a startup sweep recovers rows stranded at notified=2 after a crash.

    The atomic claim added for finding 22 flips ``notified=0 → 2`` before
    the actual mail attempt.  An OOM, container restart, or SIGKILL
    between claim and send leaves the row pinned at ``notified=2`` because
    the in-process release path only fires on Python-level exceptions.
    A startup reconcile resets such rows back to ``notified=0`` based on
    the new ``claimed_at`` timestamp.
    """

    def _make_conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def _insert(self, conn, *, notified, claimed_at=None):
        row_id = insert_download_notification(
            conn,
            email="u@x",
            title="T",
            tmdb_id=1,
            notified=notified,
            claimed_at=claimed_at,
            created_at="2026-01-01",
        )
        return row_id

    def test_stranded_claim_is_reset(self, tmp_path):
        """notified=2 with a stale claimed_at → reset to notified=0."""
        from datetime import datetime, timedelta

        from mediaman.services.downloads.notifications import (
            STRANDED_CLAIM_GRACE_SECONDS,
            reconcile_stranded_notifications,
        )

        conn = self._make_conn(tmp_path)
        stale = (
            datetime.now(UTC) - timedelta(seconds=STRANDED_CLAIM_GRACE_SECONDS + 60)
        ).isoformat()
        row_id = self._insert(conn, notified=2, claimed_at=stale)

        reset = reconcile_stranded_notifications(conn)
        assert reset == 1

        row = conn.execute(
            "SELECT notified, claimed_at FROM download_notifications WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["notified"] == 0
        assert row["claimed_at"] is None

    def test_fresh_claim_is_not_reset(self, tmp_path):
        """notified=2 with a fresh claimed_at — still in flight, leave it."""
        from datetime import datetime

        from mediaman.services.downloads.notifications import (
            reconcile_stranded_notifications,
        )

        conn = self._make_conn(tmp_path)
        fresh = datetime.now(UTC).isoformat()
        row_id = self._insert(conn, notified=2, claimed_at=fresh)

        reset = reconcile_stranded_notifications(conn)
        assert reset == 0

        row = conn.execute(
            "SELECT notified, claimed_at FROM download_notifications WHERE id=?",
            (row_id,),
        ).fetchone()
        assert row["notified"] == 2

    def test_notified_zero_not_touched(self, tmp_path):
        from mediaman.services.downloads.notifications import (
            reconcile_stranded_notifications,
        )

        conn = self._make_conn(tmp_path)
        row_id = self._insert(conn, notified=0, claimed_at=None)

        assert reconcile_stranded_notifications(conn) == 0
        row = conn.execute(
            "SELECT notified FROM download_notifications WHERE id=?", (row_id,)
        ).fetchone()
        assert row["notified"] == 0

    def test_notified_one_not_touched(self, tmp_path):
        """Already-sent rows must never be reset."""
        from mediaman.services.downloads.notifications import (
            reconcile_stranded_notifications,
        )

        conn = self._make_conn(tmp_path)
        row_id = self._insert(conn, notified=1, claimed_at=None)

        assert reconcile_stranded_notifications(conn) == 0
        row = conn.execute(
            "SELECT notified FROM download_notifications WHERE id=?", (row_id,)
        ).fetchone()
        assert row["notified"] == 1

    def test_legacy_null_claimed_at_is_swept(self, tmp_path):
        """A row stranded before the claimed_at column existed has NULL.

        The reconcile predicate treats NULL as 'old enough' so legacy
        stranded rows are recovered the first time the new code runs.
        """
        from mediaman.services.downloads.notifications import (
            reconcile_stranded_notifications,
        )

        conn = self._make_conn(tmp_path)
        row_id = self._insert(conn, notified=2, claimed_at=None)

        assert reconcile_stranded_notifications(conn) == 1
        row = conn.execute(
            "SELECT notified FROM download_notifications WHERE id=?", (row_id,)
        ).fetchone()
        assert row["notified"] == 0

    def test_atomic_claim_populates_claimed_at(self, tmp_path):
        """The claim path must stamp claimed_at so the reconcile predicate works."""
        from mediaman.services.downloads.notifications import (
            _claim_pending_notifications,
            record_download_notification,
        )

        conn = self._make_conn(tmp_path)
        record_download_notification(
            conn,
            email="u@x",
            title="T",
            media_type="movie",
            tmdb_id=1,
            service="radarr",
        )
        conn.commit()

        rows = _claim_pending_notifications(conn)
        assert len(rows) == 1

        row = conn.execute(
            "SELECT notified, claimed_at FROM download_notifications WHERE id=?",
            (rows[0]["id"],),
        ).fetchone()
        assert row["notified"] == 2
        assert row["claimed_at"] is not None
        assert row["claimed_at"] != ""
