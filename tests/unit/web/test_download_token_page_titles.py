"""The download link's page, rendered by its real template: each state's title, status and words."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.crypto import generate_download_token
from mediaman.web.routes.download import _tokens as _download_tokens
from mediaman.web.routes.download import confirm as _download_confirm
from mediaman.web.routes.download import router as download_router


@pytest.fixture
def client(app_factory, conn):
    _download_tokens._USED_TOKENS.clear()
    _download_confirm._DOWNLOAD_LIMITER_GET.reset()
    _download_confirm._reset_arr_cache_for_tests()
    app = app_factory(
        download_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )
    yield TestClient(app)
    _download_confirm._DOWNLOAD_LIMITER_GET.reset()


def _title(text: str) -> str:
    match = re.search(r"<title>\s*(.*?)\s*</title>", text, re.S)
    assert match
    return match.group(1)


def test_a_good_link_is_titled_by_its_film(client, secret_key) -> None:
    token = generate_download_token(
        email="a@example.com",
        action="download",
        title="Dune",
        media_type="movie",
        tmdb_id=42,
        recommendation_id=None,
        secret_key=secret_key,
    )
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with patch("mediaman.web.routes.download.confirm.build_radarr_from_db", return_value=radarr):
        resp = client.get(f"/download/{token}")

    assert resp.status_code == 200
    assert _title(resp.text) == "Download · Dune · Mediaman"


@pytest.mark.parametrize("token", ["this.is.not.a.valid.token", "x" * 4097])
def test_a_link_that_does_not_work_says_so_in_its_title(client, token) -> None:
    resp = client.get(f"/download/{token}")

    assert resp.status_code == 200
    assert _title(resp.text) == "Link does not work · Mediaman"


def test_too_many_looks_answer_429_with_a_wait_and_their_own_title(client) -> None:
    cap = _download_confirm._DOWNLOAD_LIMITER_GET._max_attempts
    for _ in range(cap):
        client.get("/download/this.is.not.a.valid.token")
    resp = client.get("/download/this.is.not.a.valid.token")

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"
    assert _title(resp.text) == "Too many requests · Mediaman"
