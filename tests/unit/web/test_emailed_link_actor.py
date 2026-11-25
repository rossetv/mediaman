"""An emailed link opened by someone signed in is recorded as theirs."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.core.time import now_utc
from mediaman.crypto import generate_download_token, generate_keep_token
from mediaman.services.scheduled_actions import token_hash
from mediaman.web.routes.download import reset_used_tokens
from mediaman.web.routes.download.submit import _DOWNLOAD_LIMITER_POST
from mediaman.web.routes.download.submit import router as submit_router
from mediaman.web.routes.keep import _KEEP_POST_LIMITER
from mediaman.web.routes.keep import router as keep_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action


@pytest.fixture(autouse=True)
def _clear():
    reset_used_tokens()
    _DOWNLOAD_LIMITER_POST.reset()
    _KEEP_POST_LIMITER.reset()


def _actor(conn):
    (row,) = conn.execute("SELECT actor, actor_id FROM audit_log").fetchall()
    return tuple(row)


def _keep_token(conn, secret_key):
    insert_media_item(conn, id="mi1")
    action = insert_scheduled_action(
        conn, media_item_id="mi1", execute_at=(now_utc() + timedelta(days=7)).isoformat()
    )
    token = generate_keep_token(
        media_item_id="mi1",
        action_id=action,
        expires_at=int(time.time()) + 86400,
        secret_key=secret_key,
    )
    conn.execute(
        "UPDATE scheduled_actions SET token = ?, token_hash = ?", (token, token_hash(token))
    )
    conn.commit()
    return token


@pytest.mark.parametrize("path", ["/keep/{}", "/api/keep/{}/forever"])
def test_a_keep_is_the_signed_in_persons(conn, app_factory, authed_client, secret_key, path):
    token = _keep_token(conn, secret_key)
    client = authed_client(app_factory(keep_router, conn=conn), conn)
    reply = client.post(path.format(token), data={"duration": "30 days"}, follow_redirects=False)
    assert reply.status_code in (200, 302)
    assert _actor(conn) == ("admin", 1)


def test_a_keep_by_nobody_signed_in_is_nobodys(conn, app_factory, secret_key):
    token = _keep_token(conn, secret_key)
    client = TestClient(app_factory(keep_router, conn=conn))
    client.post(f"/keep/{token}", data={"duration": "30 days"}, follow_redirects=False)
    assert _actor(conn) == (None, None)


def test_a_download_is_the_signed_in_persons(conn, app_factory, authed_client, secret_key):
    client = authed_client(app_factory(submit_router, conn=conn), conn)
    token = generate_download_token(
        email="viewer@example.com",
        action="download",
        title="Dune",
        media_type="movie",
        tmdb_id=42,
        recommendation_id=None,
        secret_key=secret_key,
    )
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with patch("mediaman.web.routes.download.submit.build_radarr_from_db", return_value=radarr):
        assert client.post(f"/download/{token}").status_code == 200
    assert _actor(conn) == ("admin", 1)
