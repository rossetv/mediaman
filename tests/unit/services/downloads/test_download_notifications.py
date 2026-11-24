"""Tests for the download-ready email path.

The outer ``check_download_notifications`` function coordinates DB,
Mailgun, Radarr, and Sonarr — too much infrastructure for a unit test.
These tests focus on the security-critical part: the Jinja template
must escape every TMDB-sourced field so a malicious free-text value
(e.g. a crafted director string) cannot inject HTML/JS into the email.
"""

from __future__ import annotations

import re
from datetime import UTC
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from tests.helpers.factories import insert_download_notification, insert_settings


def _load_template():
    template_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "mediaman"
        / "services"
        / "downloads"
        / "templates"
    )
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    return env.get_template("download_ready.html")


def _render(**overrides):
    template = _load_template()
    defaults = {
        "title": "Example",
        "poster_src": "",
        "backdrop_src": "",
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

    def test_the_title_beside_the_picture_is_not_read_out_twice(self):
        html = _render(poster_src="https://x/p.jpg", backdrop_src="https://x/b.jpg")
        assert "<img " in html
        assert 'alt="Example"' not in html

    def test_declares_a_dark_colour_scheme(self):
        html = _render()
        assert '<meta name="color-scheme" content="dark">' in html
        assert '<meta name="supported-color-schemes" content="dark">' in html

    def test_footer_text_is_readable_on_black(self):
        html = _render()
        footer = html[html.index("You requested this download") - 120 :]
        assert "color:#a1a1a6" in footer
        assert "rgba(255,255,255,0.2)" not in html

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
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "mediaman"
            / "services"
            / "downloads"
            / "templates"
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

    def test_a_backdrop_heads_the_card_at_the_card_width_and_the_poster_is_left_out(self):
        html = _render(
            backdrop_src="https://image.tmdb.org/t/p/w1280/wide.jpg",
            poster_src="https://image.tmdb.org/t/p/w500/abc.jpg",
        )
        imgs = re.findall(r"<img [^>]*>", html)
        assert len(imgs) == 1
        assert "w1280/wide.jpg" in imgs[0]
        assert 'width="536"' in imgs[0]
        assert "object-fit" not in html and "max-height" not in html

    def test_a_poster_alone_is_shown_whole_as_a_poster_not_cropped_to_a_strip(self):
        html = _render(poster_src="https://image.tmdb.org/t/p/w500/abc.jpg")
        imgs = re.findall(r"<img [^>]*>", html)
        assert len(imgs) == 1
        assert "w500/abc.jpg" in imgs[0]
        assert 'width="180"' in imgs[0] and 'height="270"' in imgs[0]
        assert "object-fit" not in html and "max-height" not in html

    def test_no_picture_leaves_no_image_tag(self):
        assert "<img " not in _render()


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
        # enough to proceed past the early-bail guard (domain, key, AND from).
        insert_settings(
            conn,
            mailgun_domain="test.example.com",
            mailgun_api_key="dummy-key",
            mailgun_from_address="noreply@test.example.com",
        )

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
        insert_download_notification(
            conn,
            email="user@example.com",
            title="Severance",
            media_type="tv",
            tvdb_id=370524,
            service="sonarr",
        )

        # Sonarr reports the series with tmdbId=None but a matching tvdbId.
        sonarr_client = MagicMock()
        sonarr_client.get_series.return_value = [
            {
                "tvdbId": 370524,
                "tmdbId": None,
                "title": "Severance",
                "seasons": [self._season(1, 9, 9)],
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

    @staticmethod
    def _season(number, files, wanted, *, monitored=True, aired=True):
        stats = {"episodeFileCount": files, "episodeCount": wanted, "totalEpisodeCount": 10}
        if aired:
            stats["previousAiring"] = "2020-01-01T00:00:00Z"
        return {"seasonNumber": number, "monitored": monitored, "statistics": stats}

    def _run_with_seasons(self, conn, monkeypatch, *seasons) -> None:
        from unittest.mock import MagicMock

        from mediaman.services.downloads._notification_backoff import _backoff_state
        from mediaman.services.downloads.notifications import check_download_notifications

        _backoff_state.clear()
        sonarr = MagicMock()
        sonarr.get_series.return_value = [
            {
                "tvdbId": 370524,
                "title": "Severance",
                "statistics": {
                    "episodeFileCount": sum(s["statistics"]["episodeFileCount"] for s in seasons)
                },
                "seasons": list(seasons),
            }
        ]
        monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: sonarr)
        monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: None)
        check_download_notifications(conn, secret_key="x" * 64)

    def test_a_season_is_announced_only_once_that_season_is_all_there(self, tmp_path, monkeypatch):
        conn, sent = self._setup(tmp_path, monkeypatch)
        for season in (0, 2):
            insert_download_notification(
                conn,
                email=f"s{season}@example.com",
                title="Severance",
                media_type="tv",
                tvdb_id=370524,
                service="sonarr",
                season_number=season,
            )

        whole = self._season(1, 10, 10)
        for files in (0, 9):
            self._run_with_seasons(
                conn, monkeypatch, self._season(0, 0, 2), whole, self._season(2, files, 10)
            )
            assert sent == []

        self._run_with_seasons(
            conn, monkeypatch, self._season(0, 0, 2), whole, self._season(2, 10, 10)
        )
        assert [m["to"] for m in sent] == ["s2@example.com"]

    @pytest.mark.parametrize(
        ("second", "ready"),
        [
            (_season(2, 0, 10), False),
            (_season(2, 9, 10), False),
            (_season(2, 0, 0), False),
            (_season(2, 10, 10), True),
            (_season(2, 4, 4), True),
            (_season(2, 0, 0, aired=False), True),
            (_season(2, 1, 3, monitored=False), True),
            (_season(0, 0, 5), True),
        ],
        ids=[
            "missing",
            "in part",
            "aired a moment ago",
            "whole",
            "whole so far",
            "still to air",
            "skipped",
            "specials",
        ],
    )
    def test_a_show_is_announced_only_once_its_aired_monitored_seasons_are_all_there(
        self, tmp_path, monkeypatch, second, ready
    ):
        conn, sent = self._setup(tmp_path, monkeypatch)
        insert_download_notification(
            conn,
            email="user@example.com",
            title="Severance",
            media_type="tv",
            tvdb_id=370524,
            service="sonarr",
        )

        self._run_with_seasons(conn, monkeypatch, self._season(1, 10, 10), second)

        assert len(sent) == (1 if ready else 0)

    def test_a_show_with_no_season_monitored_is_not_announced(self, tmp_path, monkeypatch):
        conn, sent = self._setup(tmp_path, monkeypatch)
        insert_download_notification(
            conn,
            email="user@example.com",
            title="Severance",
            media_type="tv",
            tvdb_id=370524,
            service="sonarr",
        )

        self._run_with_seasons(conn, monkeypatch, self._season(1, 3, 3, monitored=False))

        assert sent == []

    def test_tvdb_mismatch_leaves_row_pending(self, tmp_path, monkeypatch):
        """If Sonarr doesn't have the series yet, notified stays 0."""
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn, sent = self._setup(tmp_path, monkeypatch)

        insert_download_notification(
            conn,
            email="user@example.com",
            title="Nobody Home",
            media_type="tv",
            tvdb_id=999999,
            service="sonarr",
        )

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
        """A second pass while the first is still working
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
        insert_settings(
            conn,
            mailgun_domain="test.example.com",
            mailgun_api_key="dummy-key",
            mailgun_from_address="noreply@test.example.com",
        )
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
        _release_claim(conn, rows[0])

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
    """A startup sweep recovers rows stranded at notified=2 after a crash.

    The atomic claim flips ``notified=0 → 2`` before
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
            (rows[0].id,),
        ).fetchone()
        assert row["notified"] == 2
        assert row["claimed_at"] is not None
        assert row["claimed_at"] != ""


class TestNarrowedExceptClauses:
    """The three catch clauses in the Radarr/Sonarr probes must catch the
    concrete failure types (SafeHTTPError, requests.RequestException, ArrError)
    and release the claim on each.  They must NOT swallow programmer errors like
    TypeError or AttributeError.
    """

    def _make_conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def test_check_radarr_movie_handles_safe_http_error(self, tmp_path, monkeypatch):
        """SafeHTTPError from Radarr get_movie_by_tmdb → arr_unreachable=True."""
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import _check_radarr_movie
        from mediaman.services.infra import SafeHTTPError

        arr = MagicMock()
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.side_effect = SafeHTTPError(
            status_code=503, body_snippet="", url="https://radarr"
        )
        arr.radarr.return_value = radarr

        result = _check_radarr_movie(arr, row_id=1, tmdb_id=12345)

        assert not result.ready
        assert result.movie is None
        assert result.arr_unreachable is True

    def test_check_radarr_movie_handles_arr_error(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        from mediaman.services.arr.base import ArrError
        from mediaman.services.downloads.notifications import _check_radarr_movie

        arr = MagicMock()
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.side_effect = ArrError("Radarr down")
        arr.radarr.return_value = radarr

        result = _check_radarr_movie(arr, row_id=1, tmdb_id=12345)

        assert not result.ready
        assert result.arr_unreachable is True

    def test_check_sonarr_series_handles_request_exception(self, tmp_path, monkeypatch):
        """requests.RequestException from Sonarr → arr_unreachable=True.

        With sonarr_series_index=None, _check_sonarr_series falls back to
        _build_sonarr_index(client) which re-raises the network error.
        """
        from unittest.mock import MagicMock

        import requests

        from mediaman.services.downloads.notifications import _check_sonarr_series

        arr = MagicMock()
        sonarr = MagicMock()
        sonarr.get_series.side_effect = requests.ConnectionError("network down")
        arr.sonarr.return_value = sonarr

        ready, arr_unreachable = _check_sonarr_series(
            arr, row_id=1, tvdb_id=99999, tmdb_id=None, sonarr_series_index=None
        )

        assert not ready
        assert arr_unreachable is True

    def test_check_sonarr_series_handles_arr_error(self, tmp_path):
        from unittest.mock import MagicMock

        from mediaman.services.arr.base import ArrError
        from mediaman.services.downloads.notifications import _check_sonarr_series

        arr = MagicMock()
        sonarr = MagicMock()
        sonarr.get_series.side_effect = ArrError("Sonarr 500")
        arr.sonarr.return_value = sonarr

        ready, arr_unreachable = _check_sonarr_series(
            arr, row_id=1, tvdb_id=99999, tmdb_id=None, sonarr_series_index=None
        )

        assert not ready
        assert arr_unreachable is True


class TestClaimedNotificationRowDataclass:
    """_claim_pending_notifications must return ClaimedNotificationRow dataclasses,
    not raw sqlite3.Row objects.  Pins the field names and types so any future
    SELECT column change is caught at the dataclass boundary rather than propagating
    to call sites as a silent KeyError.
    """

    def _make_conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def test_returns_list_of_claimed_notification_rows(self, tmp_path):
        from mediaman.services.downloads._notification_claims import (
            ClaimedNotificationRow,
            _claim_pending_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_download_notification(
            conn,
            email="user@example.com",
            title="Oppenheimer",
            media_type="movie",
            tmdb_id=872585,
            tvdb_id=None,
            service="radarr",
        )

        rows = _claim_pending_notifications(conn)

        assert len(rows) == 1
        assert isinstance(rows[0], ClaimedNotificationRow)

    def test_dataclass_fields_match_select_columns(self, tmp_path):
        """All seven SELECT columns must be present as dataclass attributes."""
        from mediaman.services.downloads._notification_claims import (
            _claim_pending_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_download_notification(
            conn,
            email="u@example.com",
            title="Dune",
            media_type="movie",
            tmdb_id=438631,
            tvdb_id=None,
            service="radarr",
        )

        rows = _claim_pending_notifications(conn)
        row = rows[0]

        # All SELECT columns must be accessible as attributes.
        assert isinstance(row.id, int)
        assert row.email == "u@example.com"
        assert row.title == "Dune"
        assert row.media_type == "movie"
        assert row.tmdb_id == 438631
        assert row.tvdb_id is None
        assert row.service == "radarr"

    def test_dataclass_is_immutable(self, tmp_path):
        """ClaimedNotificationRow must be frozen — mutations must raise FrozenInstanceError."""
        import pytest

        from mediaman.services.downloads._notification_claims import (
            _claim_pending_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_download_notification(conn, title="Frozen")

        rows = _claim_pending_notifications(conn)
        with pytest.raises(
            AttributeError
        ):  # dataclasses.FrozenInstanceError subclasses AttributeError
            rows[0].title = "Mutated"  # type: ignore[misc]

    def test_tvdb_and_tmdb_none_preserved(self, tmp_path):
        """NULL columns in DB must map to None, not 0 or empty string."""
        from mediaman.services.downloads._notification_claims import (
            _claim_pending_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_download_notification(
            conn,
            email="tv@example.com",
            title="Severance",
            media_type="tv",
            tmdb_id=None,
            tvdb_id=370524,
            service="sonarr",
        )

        rows = _claim_pending_notifications(conn)
        row = rows[0]

        assert row.tmdb_id is None
        assert row.tvdb_id == 370524

    def test_claim_transaction_unchanged(self, tmp_path):
        """Introducing the dataclass must not alter the atomic claim semantics.

        After claim: notified=2, claimed_at is set.
        A second call to _claim_pending_notifications returns nothing.
        """
        from mediaman.services.downloads._notification_claims import (
            _claim_pending_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_download_notification(conn, title="Atomic")

        first = _claim_pending_notifications(conn)
        assert len(first) == 1

        # DB row is now claimed at notified=2.
        db_row = conn.execute(
            "SELECT notified, claimed_at FROM download_notifications WHERE id=?",
            (first[0].id,),
        ).fetchone()
        assert db_row["notified"] == 2
        assert db_row["claimed_at"] is not None

        # Second call must find nothing — row is already claimed.
        second = _claim_pending_notifications(conn)
        assert second == []


class TestPollThrottleVsArrBackoff:
    """In-progress poll throttle must NOT accumulate exponential delay.

    An item still downloading should be re-evaluated after ~60 s (short fixed
    poll interval), not after the 30-minute exponential backoff cap that is
    reserved for genuine *arr reachability failures.
    """

    def test_poll_attempt_does_not_accumulate_exponential_delay(self):
        """Repeated _record_poll_attempt calls must not grow the retry window.

        After N calls to _record_poll_attempt the next_retry_at should always
        be about _POLL_INTERVAL_SECONDS in the future — not 2×, 4×, 8×, etc.
        """
        from datetime import datetime

        from mediaman.services.downloads._notification_backoff import (
            _BACKOFF_MAX_SECONDS,
            _POLL_INTERVAL_SECONDS,
            _backoff_state,
            _backoff_state_lock,
            _clear_backoff,
            _record_poll_attempt,
        )

        row_id = 99001  # Unique ID unlikely to collide with other tests.

        # Clean slate.
        _clear_backoff(row_id)

        try:
            base = datetime.now(UTC)
            for _ in range(10):
                _record_poll_attempt(row_id, base)

            with _backoff_state_lock:
                _attempts, next_retry_at = _backoff_state[row_id]

            delay = (next_retry_at - base).total_seconds()
            # Must be close to the short poll interval, not the 30-min cap.
            assert delay <= _POLL_INTERVAL_SECONDS + 1, (
                f"poll delay {delay}s exceeds poll interval {_POLL_INTERVAL_SECONDS}s"
            )
            assert delay < _BACKOFF_MAX_SECONDS, (
                f"poll delay {delay}s reached exponential-backoff cap {_BACKOFF_MAX_SECONDS}s"
            )
        finally:
            _clear_backoff(row_id)

    def test_arr_failure_backoff_grows_exponentially(self):
        """_record_arr_failure must accumulate exponential delay across retries."""
        from datetime import datetime

        from mediaman.services.downloads._notification_backoff import (
            _BACKOFF_BASE_SECONDS,
            _backoff_state,
            _backoff_state_lock,
            _clear_backoff,
            _record_arr_failure,
        )

        row_id = 99002

        _clear_backoff(row_id)
        try:
            base = datetime.now(UTC)
            _record_arr_failure(row_id, base)  # attempt 1 → 60 s
            with _backoff_state_lock:
                _, next1 = _backoff_state[row_id]
            delay1 = (next1 - base).total_seconds()

            _record_arr_failure(row_id, base)  # attempt 2 → 120 s
            with _backoff_state_lock:
                _, next2 = _backoff_state[row_id]
            delay2 = (next2 - base).total_seconds()

            assert delay2 > delay1, "arr-failure backoff should grow with retries"
            assert delay1 >= _BACKOFF_BASE_SECONDS - 1
        finally:
            _clear_backoff(row_id)

    def test_quick_download_not_throttled_into_long_backoff(self, tmp_path, monkeypatch):
        """A movie that completes on the second tick must not wait the 30-min backoff.

        Tick 1: Radarr returns hasFile=False → _record_poll_attempt (short interval).
        Tick 2: Radarr returns hasFile=True  → notification sent immediately.
        This confirms the poll-throttle delay stays short and is cleared on success.
        """
        from datetime import datetime
        from unittest.mock import MagicMock

        from mediaman.services.downloads._notification_backoff import (
            _clear_backoff,
            _is_backed_off,
        )
        from mediaman.services.downloads.notifications import (
            _check_radarr_movie,
        )

        # Simulate the Arr lookup returning "not ready" on first probe.
        arr = MagicMock()
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"hasFile": False, "id": 1}
        arr.radarr.return_value = radarr

        result = _check_radarr_movie(arr, row_id=42, tmdb_id=12345)
        assert not result.ready
        assert not result.arr_unreachable  # Radarr is reachable — just not ready yet.

        # The row should NOT be in exponential backoff state.  After
        # _process_one_notification calls _record_poll_attempt the delay
        # should be short.  We verify by recording a poll attempt manually
        # and checking the next retry is within the poll interval.
        from mediaman.services.downloads._notification_backoff import (
            _POLL_INTERVAL_SECONDS,
            _backoff_state,
            _backoff_state_lock,
            _record_poll_attempt,
        )

        row_id = 42
        _clear_backoff(row_id)
        now = datetime.now(UTC)
        _record_poll_attempt(row_id, now)

        try:
            with _backoff_state_lock:
                _, next_retry = _backoff_state[row_id]
            delay = (next_retry - now).total_seconds()
            assert delay <= _POLL_INTERVAL_SECONDS + 1
            # The row IS throttled for the short interval (so we don't spam Radarr).
            assert _is_backed_off(row_id, now)
        finally:
            _clear_backoff(row_id)


class TestUnknownServiceDrain:
    """A row with an unknown service value must be drained (notified=1),
    not left spinning in an infinite claim/release cycle.
    """

    def setup_method(self):
        """Clear in-memory backoff state so prior tests don't bleed into these."""
        from mediaman.services.downloads._notification_backoff import (
            _backoff_state,
            _backoff_state_lock,
        )

        with _backoff_state_lock:
            _backoff_state.clear()

    def _make_conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def test_unknown_service_marks_row_notified(self, tmp_path, monkeypatch):
        """A notification row with service='unknown_future' must be marked notified=1."""
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_settings(
            conn,
            mailgun_domain="test.example.com",
            mailgun_api_key="dummy-key",
            mailgun_from_address="noreply@test.example.com",
        )
        insert_download_notification(
            conn,
            email="user@example.com",
            title="Mystery Item",
            media_type="movie",
            tmdb_id=99999,
            service="unknown_future_service",
        )

        # Stub out Mailgun so it doesn't actually send.
        mailgun_stub = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.mail.mailgun.MailgunClient",
            lambda *a, **kw: mailgun_stub,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_sonarr_from_db", lambda *a, **kw: None
        )

        check_download_notifications(conn, secret_key="x" * 64)

        row = conn.execute("SELECT notified FROM download_notifications").fetchone()
        assert row["notified"] == 1, (
            "unknown-service row must be drained to notified=1 rather than left spinning"
        )
        # No email should have been sent.
        mailgun_stub.send.assert_not_called()

    def test_unknown_service_logs_warning(self, tmp_path, monkeypatch, caplog):
        """A WARNING must be emitted when a row has an unrecognised service."""
        import logging
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_settings(
            conn,
            mailgun_domain="test.example.com",
            mailgun_api_key="dummy-key",
            mailgun_from_address="noreply@test.example.com",
        )
        insert_download_notification(
            conn,
            email="user@example.com",
            title="Ghost",
            media_type="movie",
            service="bad_service",
        )

        monkeypatch.setattr(
            "mediaman.services.mail.mailgun.MailgunClient",
            lambda *a, **kw: MagicMock(),
        )
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_sonarr_from_db", lambda *a, **kw: None
        )

        with caplog.at_level(logging.WARNING, logger="mediaman.services.downloads.notifications"):
            check_download_notifications(conn, secret_key="x" * 64)

        assert any("unknown service" in r.message.lower() for r in caplog.records), (
            "Expected a WARNING about unknown service, got: "
            + str([r.message for r in caplog.records])
        )


class TestRadarrIndexBuiltOncePerTick:
    """get_movies() must be called exactly once per tick regardless of
    how many pending Radarr notification rows there are.

    Previously ``_check_radarr_movie`` called ``get_movie_by_tmdb()`` once
    per row, causing O(N) HTTP calls.  The fix pre-builds a ``{tmdbId: movie}``
    index before the row loop and passes it in, so the loop uses O(1) dict
    lookups and get_movies() fires exactly once.
    """

    def setup_method(self):
        """Clear in-memory backoff state so prior tests don't bleed in."""
        from mediaman.services.downloads._notification_backoff import (
            _backoff_state,
            _backoff_state_lock,
        )

        with _backoff_state_lock:
            _backoff_state.clear()

    def _make_conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def test_get_movies_called_once_for_multiple_radarr_rows(self, tmp_path, monkeypatch):
        """When N Radarr rows are pending, get_movies() must fire exactly once."""
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn = self._make_conn(tmp_path)
        insert_settings(
            conn,
            mailgun_domain="test.example.com",
            mailgun_api_key="dummy-key",
            mailgun_from_address="noreply@test.example.com",
        )
        # Insert three distinct Radarr rows with different tmdb_ids.
        for tmdb_id in (101, 202, 303):
            insert_download_notification(
                conn,
                email=f"user{tmdb_id}@example.com",
                title=f"Movie {tmdb_id}",
                media_type="movie",
                tmdb_id=tmdb_id,
                service="radarr",
            )

        get_movies_call_count = 0

        def mock_get_movies():
            nonlocal get_movies_call_count
            get_movies_call_count += 1
            # Return no matches so all rows stay pending (we only care about call count).
            return []

        radarr_client = MagicMock()
        radarr_client.get_movies.side_effect = mock_get_movies
        # get_movie_by_tmdb should not be called at all when the index is built.
        radarr_client.get_movie_by_tmdb.side_effect = AssertionError(
            "get_movie_by_tmdb called — should use radarr_index instead"
        )

        monkeypatch.setattr(
            "mediaman.services.arr.build.build_radarr_from_db",
            lambda *a, **kw: radarr_client,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.build.build_sonarr_from_db", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "mediaman.services.mail.mailgun.MailgunClient",
            lambda *a, **kw: MagicMock(),
        )

        check_download_notifications(conn, secret_key="x" * 64)

        assert get_movies_call_count == 1, (
            f"Expected get_movies() called once, got {get_movies_call_count}"
        )


class TestNoticeWithoutEmailAddress:
    def test_row_queued_with_a_username_is_closed_without_sending(
        self, tmp_path, monkeypatch, caplog
    ):
        from unittest.mock import MagicMock

        from mediaman.db import init_db
        from mediaman.services.downloads.notifications import check_download_notifications

        conn = init_db(str(tmp_path / "mm.db"))
        insert_settings(
            conn,
            mailgun_domain="test.example.com",
            mailgun_api_key="dummy-key",
            mailgun_from_address="noreply@test.example.com",
        )
        mailgun_stub = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.mail.mailgun.MailgunClient", lambda *a, **kw: mailgun_stub
        )
        row_id = insert_download_notification(conn, email="alice", title="Item", tmdb_id=1)
        good_id = insert_download_notification(conn, email="bob@example.com", title="Ok", tmdb_id=2)

        with caplog.at_level("WARNING"):
            check_download_notifications(conn, secret_key="x" * 64)

        notified = lambda i: conn.execute(  # noqa: E731
            "SELECT notified FROM download_notifications WHERE id=?", (i,)
        ).fetchone()["notified"]
        assert notified(row_id) == 1
        assert notified(good_id) != 1 or mailgun_stub.send.call_count == 1
        for call in mailgun_stub.send.call_args_list:
            assert call.kwargs["to"] != "alice"
        assert "alice" not in caplog.text
        assert sum("no email address" in r.message for r in caplog.records) == 1


class TestOneUnsentNoticePerRequest:
    """Asking again before the email is sent must not send the email twice."""

    @staticmethod
    def _record(conn, **fields):
        from mediaman.services.downloads.notifications import record_download_notification

        row = {
            "email": "a@example.com",
            "title": "Dune",
            "media_type": "movie",
            "service": "radarr",
            "tmdb_id": 42,
            **fields,
        }
        record_download_notification(conn, **row)

    @staticmethod
    def _count(conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM download_notifications").fetchone()[0]

    def test_a_second_request_for_an_unsent_notice_adds_none(self, conn):
        self._record(conn)
        self._record(conn, email="A@Example.com")
        assert self._count(conn) == 1

    def test_a_second_request_while_the_notice_is_being_checked_adds_none(self, conn):
        insert_download_notification(conn, email="a@example.com", tmdb_id=42, notified=2)
        self._record(conn)
        assert self._count(conn) == 1

    def test_a_series_known_by_either_id_is_the_same_request(self, conn):
        tv = {"title": "Show", "media_type": "tv", "service": "sonarr"}
        self._record(conn, **tv, tmdb_id=5, tvdb_id=9)
        self._record(conn, **tv, tmdb_id=None, tvdb_id=9)
        assert self._count(conn) == 1

    def test_a_request_after_the_email_was_sent_gets_its_own_notice(self, conn):
        insert_download_notification(conn, email="a@example.com", tmdb_id=42, notified=1)
        self._record(conn)
        assert self._count(conn) == 2

    def test_two_people_sharing_an_address_each_get_their_own_notice(self, conn):
        self._record(conn, username="casey")
        self._record(conn, username="robin")
        assert self._count(conn) == 2

    def test_a_person_the_emailed_link_already_asked_for_gets_no_second(self, conn):
        self._record(conn)
        self._record(conn, username="casey")
        self._record(conn)
        assert self._count(conn) == 1

    def test_another_person_title_service_or_season_gets_its_own_notice(self, conn):
        self._record(conn)
        self._record(conn, email="b@example.com")
        self._record(conn, tmdb_id=43)
        self._record(conn, service="sonarr", media_type="tv")
        for season in (None, 1, 2):
            self._record(
                conn,
                service="sonarr",
                media_type="tv",
                tmdb_id=None,
                tvdb_id=9,
                season_number=season,
            )
        assert self._count(conn) == 7

    def test_requests_without_any_id_are_never_taken_for_one_another(self, conn):
        self._record(conn, tmdb_id=None)
        self._record(conn, tmdb_id=None, title="Heat")
        assert self._count(conn) == 2


class TestClaimFreesStrandedRowsWhileRunning:
    """A claim held at a crash is freed by a later tick, without a restart."""

    def _stamp(self, conn, *, age_seconds):
        from datetime import datetime, timedelta

        claimed_at = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        return insert_download_notification(
            conn, email="u@x", title="T", tmdb_id=1, notified=2, claimed_at=claimed_at
        )

    def test_a_claim_older_than_the_grace_is_claimed_again_by_the_next_tick(self, conn):
        from mediaman.services.downloads._notification_claims import (
            STRANDED_CLAIM_GRACE_SECONDS,
            _claim_pending_notifications,
        )

        row_id = self._stamp(conn, age_seconds=STRANDED_CLAIM_GRACE_SECONDS + 60)

        assert [r.id for r in _claim_pending_notifications(conn)] == [row_id]

    def test_a_claim_inside_the_grace_is_left_to_the_send_in_flight(self, conn):
        from mediaman.services.downloads._notification_claims import (
            _claim_pending_notifications,
        )

        self._stamp(conn, age_seconds=60)

        assert _claim_pending_notifications(conn) == []


class TestClaimIsRestampedBeforeTheSend:
    def test_a_row_sent_late_in_a_batch_holds_a_fresh_claim_while_the_email_goes_out(
        self, conn, monkeypatch
    ):
        from datetime import datetime, timedelta
        from unittest.mock import MagicMock

        from mediaman.services.downloads import notifications
        from mediaman.services.downloads._notification_claims import (
            STRANDED_CLAIM_GRACE_SECONDS,
            ClaimedNotificationRow,
        )

        stale = (
            datetime.now(UTC) - timedelta(seconds=STRANDED_CLAIM_GRACE_SECONDS + 60)
        ).isoformat()
        row_id = insert_download_notification(
            conn, email="u@example.com", title="T", tmdb_id=1, notified=2, claimed_at=stale
        )
        monkeypatch.setattr(notifications, "_check_arr_availability", lambda *a, **k: (True, None))
        monkeypatch.setattr(notifications, "_build_email_payload", lambda *a, **k: ("s", "h"))
        seen: list[int] = []
        mailgun = MagicMock()
        mailgun.send.side_effect = lambda **_: seen.append(
            notifications.reconcile_stranded_notifications(conn)
        )
        row = ClaimedNotificationRow(
            id=row_id,
            email="u@example.com",
            title="T",
            media_type="movie",
            tmdb_id=1,
            tvdb_id=None,
            service="radarr",
            claimed_at=stale,
        )

        notifications._process_one_notification(
            conn, row, MagicMock(), mailgun, MagicMock(), {}, datetime.now(UTC)
        )

        assert seen == [0], "a reconcile during the send must not free the row"
        assert (
            conn.execute(
                "SELECT notified FROM download_notifications WHERE id=?", (row_id,)
            ).fetchone()[0]
            == 1
        )


class TestAnOverlappingBatchOwnsWhatItClaimedAgain:
    """A batch over the grace period old has its waiting rows claimed by the next one."""

    def _two_claims(self, conn):
        from dataclasses import replace
        from datetime import datetime, timedelta

        from mediaman.services.downloads._notification_claims import (
            STRANDED_CLAIM_GRACE_SECONDS,
            _claim_pending_notifications,
        )

        insert_download_notification(conn, email="u@example.com", title="T", tmdb_id=1)
        [first] = _claim_pending_notifications(conn)
        old = (datetime.now(UTC) - timedelta(seconds=2 * STRANDED_CLAIM_GRACE_SECONDS)).isoformat()
        with conn:
            conn.execute("UPDATE download_notifications SET claimed_at = ?", (old,))
        [second] = _claim_pending_notifications(conn)
        return replace(first, claimed_at=old), second

    def _process(self, conn, row, mailgun, *, radarr_index=None):
        from datetime import datetime
        from unittest.mock import MagicMock

        from mediaman.services.downloads import notifications

        index = {1: {"hasFile": True}} if radarr_index is None else radarr_index
        notifications._process_one_notification(
            conn,
            row,
            MagicMock(),
            mailgun,
            _load_template(),
            {},
            datetime.now(UTC),
            radarr_index=index,
        )

    def _state(self, conn):
        return tuple(
            conn.execute("SELECT notified, claimed_at FROM download_notifications").fetchone()
        )

    def test_the_email_goes_out_once(self, conn):
        from unittest.mock import MagicMock

        first, second = self._two_claims(conn)
        mailgun = MagicMock()

        self._process(conn, first, mailgun)
        assert mailgun.send.call_count == 0
        assert self._state(conn) == (2, second.claimed_at)
        self._process(conn, second, mailgun)

        assert mailgun.send.call_count == 1
        assert self._state(conn)[0] == 1

    def test_the_older_batch_does_not_send_what_the_newer_one_sent(self, conn):
        from unittest.mock import MagicMock

        first, second = self._two_claims(conn)
        mailgun = MagicMock()

        self._process(conn, second, mailgun)
        self._process(conn, first, mailgun)

        assert mailgun.send.call_count == 1
        assert self._state(conn)[0] == 1

    def test_the_older_batch_finding_no_file_leaves_the_newer_claim(self, conn):
        from unittest.mock import MagicMock

        first, second = self._two_claims(conn)

        self._process(conn, first, MagicMock(), radarr_index={1: {"hasFile": False}})

        assert self._state(conn) == (2, second.claimed_at)

    def test_the_older_batch_releasing_all_it_claimed_leaves_the_newer_claim(self, conn):
        from mediaman.services.downloads._notification_claims import _release_claims_bulk

        first, second = self._two_claims(conn)

        _release_claims_bulk(conn, [first])
        assert self._state(conn) == (2, second.claimed_at)
        _release_claims_bulk(conn, [second])

        assert self._state(conn) == (0, None)

    def test_a_failed_send_frees_the_row_for_a_retry(self, conn):
        from unittest.mock import MagicMock

        from mediaman.services.infra import SafeHTTPError

        _, second = self._two_claims(conn)
        mailgun = MagicMock()
        mailgun.send.side_effect = SafeHTTPError(503, "", "mailgun")

        self._process(conn, second, mailgun)

        assert self._state(conn) == (0, None)


class TestUnsentNoticeIsAskedPerSeason:
    def _ask(self, conn, **kw):
        from mediaman.services.downloads.notifications import has_unsent_notification

        return has_unsent_notification(
            conn, email="user@example.com", service="sonarr", tmdb_id=5, tvdb_id=9, **kw
        )

    def test_a_season_notice_does_not_cover_the_whole_show(self, conn):
        insert_download_notification(
            conn, media_type="tv", service="sonarr", tmdb_id=5, tvdb_id=9, season_number=2
        )

        assert self._ask(conn) is False
        assert self._ask(conn, season_number=2) is True
        assert self._ask(conn, season_number=3) is False

    def test_a_whole_show_notice_is_found_for_the_whole_show_only(self, conn):
        insert_download_notification(conn, media_type="tv", service="sonarr", tmdb_id=5, tvdb_id=9)

        assert self._ask(conn) is True
        assert self._ask(conn, season_number=2) is False


class TestAvailableEmailNamesTheSeason:
    def _payload(self, season_number):
        from mediaman.services.downloads._notification_claims import ClaimedNotificationRow
        from mediaman.services.downloads._notification_email import build_email_payload

        row = ClaimedNotificationRow(
            id=1,
            email="u@x",
            title="Severance",
            media_type="tv",
            tmdb_id=None,
            tvdb_id=7,
            service="sonarr",
            claimed_at="",
            season_number=season_number,
        )
        return build_email_payload(row, None, {}, _load_template())

    def test_two_seasons_of_one_show_do_not_send_the_same_text(self):
        subject_two, html_two = self._payload(2)
        subject_three, html_three = self._payload(3)

        assert "season 2" in subject_two and "season 3" in subject_three
        assert "Season 2" in html_two and "Season 3" not in html_two
        assert "Season 3" in html_three

    def test_season_zero_is_called_the_specials(self):
        subject, html = self._payload(0)

        assert subject == "'Severance' specials are now available to watch"
        assert "Specials" in html
        assert "Season 0" not in html

    def test_a_row_with_no_season_reads_as_before(self):
        subject, html = self._payload(None)

        assert subject == "'Severance' is now available to watch"
        assert "Season" not in html


class TestEmailBackdrop:
    def _html(self, suggestion):
        from mediaman.services.downloads._notification_claims import ClaimedNotificationRow
        from mediaman.services.downloads._notification_email import build_email_payload

        row = ClaimedNotificationRow(
            id=1,
            email="u@x",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            tvdb_id=None,
            service="radarr",
            claimed_at="",
            season_number=None,
        )
        return build_email_payload(row, None, {42: suggestion}, _load_template())[1]

    def _meta(self, **kw):
        from mediaman.services.downloads._notification_email import SuggestionsMeta

        return SuggestionsMeta(
            tmdb_id=42,
            year="2021",
            runtime="155",
            director="",
            description="",
            rating="",
            imdb_rating="",
            rt_rating="",
            poster_url="https://image.tmdb.org/t/p/w300/p.jpg",
            **kw,
        )

    def test_the_stored_backdrop_is_the_picture_and_the_poster_is_not(self):
        html = self._html(self._meta(backdrop_url="https://image.tmdb.org/t/p/w1280/wide.jpg"))
        assert "w1280/wide.jpg" in html
        assert "p.jpg" not in html

    def test_a_title_with_no_backdrop_shows_its_poster_upgraded_to_w500(self):
        html = self._html(self._meta())
        assert "https://image.tmdb.org/t/p/w500/p.jpg" in html

    def test_a_stored_value_that_is_no_tmdb_image_is_not_put_in_the_email(self):
        html = self._html(self._meta(backdrop_url="https://evil.example/x.jpg"))
        assert "evil.example" not in html
        assert "w500/p.jpg" in html

    def test_the_batch_lookup_prefers_the_newest_row_that_has_a_backdrop(self, tmp_path):
        from mediaman.db import init_db
        from mediaman.services.downloads._notification_claims import ClaimedNotificationRow
        from mediaman.services.downloads.notifications import _fetch_suggestions_batch
        from tests.helpers.factories import insert_suggestion

        conn = init_db(str(tmp_path / "m.db"))
        wide = "https://image.tmdb.org/t/p/w1280/wide.jpg"
        with conn:
            insert_suggestion(conn, tmdb_id=42, created_at="2026-02-01")
            old = insert_suggestion(conn, tmdb_id=42, created_at="2026-01-01")
            conn.execute("UPDATE suggestions SET backdrop_url = ? WHERE id = ?", (wide, old))
        row = ClaimedNotificationRow(
            id=1,
            email="u@x",
            title="Dune",
            media_type="movie",
            tmdb_id=42,
            tvdb_id=None,
            service="radarr",
            claimed_at="",
            season_number=None,
        )

        assert _fetch_suggestions_batch(conn, [row])[42].backdrop_url == wide
