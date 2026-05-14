"""Integration test for the keep/snooze flow."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.crypto import generate_keep_token, validate_keep_token
from mediaman.db import init_db
from mediaman.web.routes.keep import router as keep_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action


def _templates_state() -> dict[str, object]:
    """Keep route renders via app.state.templates — load real templates from disk
    so the redirect-vs-render branches are exercised end-to-end."""
    tpl_dir = Path(__file__).parent.parent.parent / "src" / "mediaman" / "web" / "templates"
    return {"templates": Jinja2Templates(directory=str(tpl_dir))}


class TestKeepFlow:
    def test_full_keep_lifecycle(self, db_path, secret_key):
        conn = init_db(str(db_path))

        insert_media_item(
            conn,
            id="999",
            title="Test Movie",
            plex_rating_key="999",
            file_path="/media/movies/Test",
            file_size_bytes=5_000_000_000,
        )

        token = generate_keep_token(
            media_item_id="999",
            action_id=1,
            expires_at=int(time.time()) + 86400,
            secret_key=secret_key,
        )
        insert_scheduled_action(
            conn,
            media_item_id="999",
            action="scheduled_deletion",
            scheduled_at="2026-04-10T09:00:00+00:00",
            execute_at="2026-04-24T09:00:00+00:00",
            token=token,
        )

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
        """Create a media item + a legitimate scheduled_action, return (token, action_id)."""
        insert_media_item(
            conn,
            id="999",
            title="Test",
            plex_rating_key="999",
            file_path="/media/Test",
            file_size_bytes=1000,
        )
        token = generate_keep_token(
            media_item_id="999",
            action_id=1,
            expires_at=int(time.time()) + 86400,
            secret_key=secret_key,
        )
        action_id = insert_scheduled_action(
            conn,
            media_item_id="999",
            action="scheduled_deletion",
            scheduled_at="2026-04-10T00:00:00+00:00",
            execute_at="2099-01-01T00:00:00+00:00",
            token=token,
        )
        return token, action_id

    def test_bad_signature_token_rejected(
        self, app_factory, conn, db_path, secret_key, monkeypatch
    ):
        """A token with a bad signature must be rejected, even if it matches a DB row."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(db_path.parent))

        real_token, _seed_action_id = self._seed(conn, secret_key)

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
        insert_media_item(
            conn,
            id="forged-id",
            title="Forged",
            plex_rating_key="998",
            file_path="/media/Forged",
            file_size_bytes=1000,
        )
        forged_action_id = insert_scheduled_action(
            conn,
            media_item_id="forged-id",
            action="scheduled_deletion",
            scheduled_at="2026-04-10T00:00:00+00:00",
            execute_at="2099-01-01T00:00:00+00:00",
            token=forged,
        )

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
        row = conn.execute(
            "SELECT token_used FROM scheduled_actions WHERE id = ?", (forged_action_id,)
        ).fetchone()
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
        # The HMAC is valid, but the payload's action_id (999) doesn't match the
        # row's actual id. Migration 25 forbids two active pending deletions for
        # the same media_item_id, so the second row needs a different media_items entry.
        insert_media_item(
            conn,
            id="mismatch-id",
            title="Mismatch",
            plex_rating_key="997",
            file_path="/media/Mismatch",
            file_size_bytes=1000,
        )
        mismatch_action_id = insert_scheduled_action(
            conn,
            media_item_id="mismatch-id",
            action="scheduled_deletion",
            scheduled_at="2026-04-10T00:00:00+00:00",
            execute_at="2099-01-01T00:00:00+00:00",
            token=wrong_payload_token,
        )

        app = app_factory(keep_router, conn=conn, state_extras=_templates_state())
        client = TestClient(app)

        r = client.post(
            f"/keep/{wrong_payload_token}",
            data={"duration": "30 days"},
            follow_redirects=False,
        )
        assert r.status_code == 400
        row = conn.execute(
            "SELECT token_used FROM scheduled_actions WHERE id = ?", (mismatch_action_id,)
        ).fetchone()
        assert row["token_used"] == 0

    def test_valid_signature_accepted(self, app_factory, conn, db_path, secret_key, monkeypatch):
        """End-to-end: a properly signed token flows through keep_submit."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(db_path.parent))

        real_token, seed_action_id = self._seed(conn, secret_key)

        app = app_factory(keep_router, conn=conn, state_extras=_templates_state())
        client = TestClient(app)

        r = client.post(f"/keep/{real_token}", data={"duration": "30 days"}, follow_redirects=False)
        assert r.status_code in (302, 303)
        row = conn.execute(
            "SELECT token_used, action FROM scheduled_actions WHERE id = ?", (seed_action_id,)
        ).fetchone()
        assert row["token_used"] == 1
        assert row["action"] == "snoozed"
