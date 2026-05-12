"""Integration test for the keep/snooze flow."""

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.crypto import generate_keep_token, validate_keep_token
from mediaman.db import init_db
from mediaman.web.routes.keep import router as keep_router


def _templates_state() -> dict[str, object]:
    """Keep route renders via app.state.templates — load real templates from disk
    so the redirect-vs-render branches are exercised end-to-end."""
    tpl_dir = Path(__file__).parent.parent.parent / "src" / "mediaman" / "web" / "templates"
    return {"templates": Jinja2Templates(directory=str(tpl_dir))}


class TestKeepFlow:
    def test_full_keep_lifecycle(self, db_path, secret_key):
        conn = init_db(str(db_path))

        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "999",
                "Test Movie",
                "movie",
                1,
                "999",
                "2026-01-01T00:00:00+00:00",
                "/media/movies/Test",
                5_000_000_000,
            ),
        )

        token = generate_keep_token(
            media_item_id="999",
            action_id=1,
            expires_at=int(time.time()) + 86400,
            secret_key=secret_key,
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "execute_at, token) VALUES (?, ?, ?, ?, ?)",
            (
                "999",
                "scheduled_deletion",
                "2026-04-10T09:00:00+00:00",
                "2026-04-24T09:00:00+00:00",
                token,
            ),
        )
        conn.commit()

        payload = validate_keep_token(token, secret_key)
        assert payload is not None
        assert payload["media_item_id"] == "999"

        conn.execute(
            "UPDATE scheduled_actions SET action='snoozed', token_used=1, "
            "snooze_duration='30 days', snoozed_at='2026-04-11T12:00:00+00:00' "
            "WHERE token=?",
            (token,),
        )
        conn.commit()

        row = conn.execute(
            "SELECT token_used, action FROM scheduled_actions WHERE token=?",
            (token,),
        ).fetchone()
        assert row["token_used"] == 1
        assert row["action"] == "snoozed"


class TestKeepSignatureEnforcement:
    """The keep route must HMAC-verify every token before trusting the DB row."""

    def _seed(self, conn, secret_key):
        """Create a media item + a legitimate scheduled_action, return token."""
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('999', 'Test', 'movie', 1, '999', '2026-01-01T00:00:00+00:00', "
            "'/media/Test', 1000)"
        )
        token = generate_keep_token(
            media_item_id="999",
            action_id=1,
            expires_at=int(time.time()) + 86400,
            secret_key=secret_key,
        )
        conn.execute(
            "INSERT INTO scheduled_actions (id, media_item_id, action, scheduled_at, "
            "execute_at, token) VALUES (1, '999', 'scheduled_deletion', "
            "'2026-04-10T00:00:00+00:00', '2099-01-01T00:00:00+00:00', ?)",
            (token,),
        )
        conn.commit()
        return token

    def test_bad_signature_token_rejected(
        self, app_factory, conn, db_path, secret_key, monkeypatch
    ):
        """A token with a bad signature must be rejected, even if it matches a DB row."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(db_path.parent))

        real_token = self._seed(conn, secret_key)

        # Mutate the real token's signature byte — keeps the same DB row lookup
        # viable but breaks HMAC.
        parts = real_token.split(".")
        forged = parts[0] + "." + ("AAAA" + parts[1][4:])

        # Insert a separate media_items row + scheduled_actions row keyed
        # on the forged token so the DB lookup would hit if the signature
        # check weren't enforced. Migration 25 forbids two active pending
        # deletions for the same media_item_id, so this fixture uses a
        # second item id to keep the constraint happy without changing the
        # behaviour the test exercises.
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('forged-id', 'Forged', 'movie', 1, '998', "
            "'2026-01-01T00:00:00+00:00', '/media/Forged', 1000)"
        )
        conn.execute(
            "INSERT INTO scheduled_actions (id, media_item_id, action, scheduled_at, "
            "execute_at, token) VALUES (2, 'forged-id', 'scheduled_deletion', "
            "'2026-04-10T00:00:00+00:00', '2099-01-01T00:00:00+00:00', ?)",
            (forged,),
        )
        conn.commit()

        app = app_factory(keep_router, conn=conn, state_extras=_templates_state())
        client = TestClient(app)

        # GET: forged token must render the "expired" page (state=expired),
        # not the "active" page with item details.
        r = client.get(f"/keep/{forged}")
        assert r.status_code == 200
        # "expired" template branch has no item card; "active" would render title.
        assert "Test" not in r.text or "expired" in r.text.lower()

        # POST: forged token must return 400 (invalid_or_expired) and must NOT
        # flip the genuine row's token_used flag.
        r = client.post(
            f"/keep/{forged}",
            data={"duration": "30 days"},
            follow_redirects=False,
        )
        assert r.status_code == 400
        row = conn.execute("SELECT token_used FROM scheduled_actions WHERE id = 2").fetchone()
        assert row["token_used"] == 0  # Forged token must not have flipped anything.

    def test_payload_mismatch_rejected(self, app_factory, conn, db_path, secret_key, monkeypatch):
        """A valid signature whose payload references a different action must be rejected."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(db_path.parent))

        self._seed(conn, secret_key)

        # Mint a token whose payload claims action_id=999 (doesn't match the
        # scheduled_action we'll insert below with a different id).
        wrong_payload_token = generate_keep_token(
            media_item_id="999",
            action_id=999,  # mismatched
            expires_at=int(time.time()) + 86400,
            secret_key=secret_key,
        )
        # Store it against id=3 — the HMAC is valid, but the payload's
        # action_id (999) doesn't match the row's id (3). Migration 25
        # forbids two active pending deletions for the same media_item_id,
        # so the second row needs a different media_items entry.
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('mismatch-id', 'Mismatch', 'movie', 1, '997', "
            "'2026-01-01T00:00:00+00:00', '/media/Mismatch', 1000)"
        )
        conn.execute(
            "INSERT INTO scheduled_actions (id, media_item_id, action, scheduled_at, "
            "execute_at, token) VALUES (3, 'mismatch-id', 'scheduled_deletion', "
            "'2026-04-10T00:00:00+00:00', '2099-01-01T00:00:00+00:00', ?)",
            (wrong_payload_token,),
        )
        conn.commit()

        app = app_factory(keep_router, conn=conn, state_extras=_templates_state())
        client = TestClient(app)

        r = client.post(
            f"/keep/{wrong_payload_token}",
            data={"duration": "30 days"},
            follow_redirects=False,
        )
        assert r.status_code == 400
        row = conn.execute("SELECT token_used FROM scheduled_actions WHERE id = 3").fetchone()
        assert row["token_used"] == 0

    def test_valid_signature_accepted(self, app_factory, conn, db_path, secret_key, monkeypatch):
        """End-to-end: a properly signed token flows through keep_submit."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(db_path.parent))

        real_token = self._seed(conn, secret_key)

        app = app_factory(keep_router, conn=conn, state_extras=_templates_state())
        client = TestClient(app)

        r = client.post(f"/keep/{real_token}", data={"duration": "30 days"}, follow_redirects=False)
        assert r.status_code in (302, 303)
        row = conn.execute(
            "SELECT token_used, action FROM scheduled_actions WHERE id = 1"
        ).fetchone()
        assert row["token_used"] == 1
        assert row["action"] == "snoozed"
