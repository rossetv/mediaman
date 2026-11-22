"""GET /api/settings says why it failed when a saved secret cannot be decrypted."""

from __future__ import annotations

from unittest.mock import patch

from mediaman.services.infra import ConfigDecryptError
from mediaman.web.routes.settings import router


def test_decrypt_failure_carries_message(app_factory, authed_client, conn):
    client = authed_client(app_factory(router, conn=conn), conn)

    with patch(
        "mediaman.web.routes.settings.api._load_settings",
        side_effect=ConfigDecryptError("bad key"),
    ):
        resp = client.get("/api/settings")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "settings_decrypt_failed"
    assert body["message"] == "Saved secrets couldn't be read. Check the secret key."
