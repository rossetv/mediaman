"""Tests for the download-ready email path.

The outer ``check_download_notifications`` function coordinates DB,
Mailgun, Radarr, and Sonarr — too much infrastructure for a unit test.
These tests focus on the security-critical part: the Jinja template
must escape every TMDB-sourced field so a malicious free-text value
(e.g. a crafted director string) cannot inject HTML/JS into the email.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


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
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        from mediaman.db import init_db

        conn = init_db(str(tmp_path / "mm.db"))

        # Settings needed by the module — Mailgun must be configured
        # enough to proceed past the early-bail guard.
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('mailgun_domain', 'test.example.com', 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('mailgun_api_key', 'dummy-key', 0, ?)",
            (now,),
        )
        conn.commit()

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
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn, sent = self._setup(tmp_path, monkeypatch)

        # Insert a pending Sonarr row with only a TVDB id.
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO download_notifications "
            "(email, title, media_type, tmdb_id, tvdb_id, service, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            ("user@example.com", "Severance", "tv", None, 370524, "sonarr", now),
        )
        conn.commit()

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
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        from mediaman.services.downloads.notifications import (
            check_download_notifications,
        )

        conn, sent = self._setup(tmp_path, monkeypatch)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO download_notifications "
            "(email, title, media_type, tmdb_id, tvdb_id, service, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            ("user@example.com", "Nobody Home", "tv", None, 999999, "sonarr", now),
        )
        conn.commit()

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

    def test_v11_migration_moves_sonarr_ids_to_tvdb_column(self, tmp_path):
        """Existing sonarr rows had TVDB ids mis-stored in tmdb_id.

        The v11 migration must move them into the new tvdb_id column.
        """
        import sqlite3

        from mediaman.db import init_db

        # Simulate a pre-v11 DB: create the table without tvdb_id and
        # insert the legacy broken row, then re-init_db to trigger the
        # migration.
        db_path = tmp_path / "legacy.db"
        conn_old = sqlite3.connect(str(db_path))
        conn_old.executescript(
            """
            CREATE TABLE download_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                title TEXT NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER,
                service TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            INSERT INTO download_notifications
                (email, title, media_type, tmdb_id, service, notified, created_at)
            VALUES ('u@x', 'Legacy Show', 'tv', 370524, 'sonarr', 0, '2026-01-01');
            INSERT INTO download_notifications
                (email, title, media_type, tmdb_id, service, notified, created_at)
            VALUES ('u@x', 'Legacy Movie', 'movie', 12345, 'radarr', 0, '2026-01-01');
            """
        )
        # Pretend it's a v10 DB so the v11 migration will run.
        conn_old.execute("PRAGMA user_version=10")
        conn_old.commit()
        conn_old.close()

        conn = init_db(str(db_path))
        rows = conn.execute(
            "SELECT title, tmdb_id, tvdb_id, service FROM download_notifications ORDER BY title"
        ).fetchall()
        # Sonarr row: TVDB id moved across, tmdb_id cleared.
        sonarr_row = next(r for r in rows if r["service"] == "sonarr")
        assert sonarr_row["tvdb_id"] == 370524
        assert sonarr_row["tmdb_id"] is None
        # Radarr row: tmdb_id untouched.
        radarr_row = next(r for r in rows if r["service"] == "radarr")
        assert radarr_row["tmdb_id"] == 12345
        assert radarr_row["tvdb_id"] is None
