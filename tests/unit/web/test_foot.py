"""TMDB's API terms ask for a notice, in their words, and their logo: every page with the nav, and
every emailed page that shows a title, ends in both."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.app_factory import create_app
from mediaman.core.time import now_utc
from mediaman.crypto import generate_download_token, generate_keep_token
from mediaman.db import set_connection
from mediaman.web.auth._token_hashing import hash_token
from tests.helpers.factories import insert_media_item, insert_scheduled_action

_NOTICE = (
    "This product uses TMDB and the TMDB APIs but is not endorsed, certified, "
    "or otherwise approved by TMDB."
)


@pytest.fixture
def app(conn, secret_key):
    set_connection(conn)
    app = create_app()
    app.state.config = SimpleNamespace(secret_key=secret_key)
    app.state.db = conn
    return app


@pytest.mark.parametrize("path", ["/watch", "/account"])
def test_a_page_with_the_nav_ends_in_the_tmdb_notice_and_logo(app, conn, viewer_client, path):
    foot = viewer_client(app, conn).get(path).text.split("</main>")[1].split("</footer>")[0]

    assert _NOTICE in foot
    assert 'aria-label="TMDB"' in foot


def test_the_sign_in_page_has_no_foot(app):
    assert "<footer" not in TestClient(app).get("/login").text


def _confirm_page(client: TestClient, secret_key: str) -> str:
    token = generate_download_token(
        email="guest@example.com",
        action="download",
        title="Dune",
        media_type="movie",
        tmdb_id=42,
        recommendation_id=None,
        secret_key=secret_key,
    )
    return client.get(f"/download/{token}").text


def _keep_page(client: TestClient, conn, secret_key: str) -> str:
    insert_media_item(conn, id="mi1", title="Dune", file_path="/f", plex_rating_key="41")
    action_id = insert_scheduled_action(
        conn,
        media_item_id="mi1",
        token="placeholder",
        execute_at=(now_utc() + timedelta(days=5)).isoformat(),
    )
    token = generate_keep_token(
        media_item_id="mi1",
        action_id=action_id,
        expires_at=int((now_utc() + timedelta(days=5)).timestamp()),
        secret_key=secret_key,
    )
    conn.execute(
        "UPDATE scheduled_actions SET token = ?, token_hash = ? WHERE id = ?",
        (token, hash_token(token), action_id),
    )
    conn.commit()
    return client.get(f"/keep/{token}").text


def test_the_download_page_of_an_email_ends_in_the_notice(app, secret_key):
    page = _confirm_page(TestClient(app), secret_key)

    assert _NOTICE in page.split("</main>")[1]


def test_the_keep_page_of_an_email_ends_in_the_notice(app, conn, secret_key):
    page = _keep_page(TestClient(app), conn, secret_key)

    assert 'value="7 days"' in page
    assert _NOTICE in page.split("</main>")[1]


@pytest.mark.parametrize("path", ["/keep/bad", "/download/bad"])
def test_a_dead_link_that_shows_no_title_has_no_foot(app, path):
    assert "<footer" not in TestClient(app).get(path).text
