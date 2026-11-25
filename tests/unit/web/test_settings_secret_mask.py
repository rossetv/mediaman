"""A save that sends the mask of a secret, or an edit of it, keeps the secret that is stored."""

from __future__ import annotations

import pytest

from mediaman.crypto import encrypt_value
from mediaman.services.rate_limit.instances import SETTINGS_WRITE_LIMITER
from mediaman.web.repository.settings import load_settings
from mediaman.web.routes.settings import router
from tests.helpers.factories import insert_settings


@pytest.fixture
def client(app_factory, authed_client, conn, secret_key):
    SETTINGS_WRITE_LIMITER.reset()
    stored = encrypt_value("stored", secret_key, conn=conn, aad=b"nzbget_password")
    insert_settings(conn, nzbget_password=stored, encrypted=1, updated_at="2026-01-01T00:00:00")
    yield authed_client(app_factory(router, conn=conn), conn, with_reauth=True)
    SETTINGS_WRITE_LIMITER.reset()


def _stored(conn, secret_key):
    return load_settings(conn, secret_key, keys={"nzbget_password"})["nzbget_password"]


@pytest.mark.parametrize("sent", ["*", "***", "*****", "****x", " ****x", "x****"])
def test_an_edited_mask_is_refused_and_the_secret_kept(client, conn, secret_key, sent):
    resp = client.put("/api/settings", json={"nzbget_password": sent})

    assert resp.status_code == 422
    assert "asterisks" in resp.json()["detail"][0]["msg"]
    assert _stored(conn, secret_key) == "stored"


def test_the_untouched_mask_passes_and_keeps_the_secret(client, conn, secret_key):
    assert client.put("/api/settings", json={"nzbget_password": "****"}).status_code == 200
    assert _stored(conn, secret_key) == "stored"


def test_a_secret_with_asterisks_of_its_own_is_stored(client, conn, secret_key):
    assert client.put("/api/settings", json={"nzbget_password": "**a*b**"}).status_code == 200
    assert _stored(conn, secret_key) == "**a*b**"
