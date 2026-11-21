"""The recommendations JSON embedded in GET /recommended must not be able to
end or comment out its <script> element, whatever text TMDB or OpenAI sends."""

from __future__ import annotations

import json
from unittest.mock import patch

from mediaman.web.routes.recommended.pages import router as pages_router
from tests.helpers.factories import insert_suggestion

_TITLE = "<!--<script>"
_DESCRIPTION = "Fish & chips</script><b>bold</b>"


def test_embedded_json_has_no_markup_characters_and_parses_to_the_same_text(
    app_factory, authed_client, conn, templates_stub
):
    insert_suggestion(conn, title=_TITLE, description=_DESCRIPTION, tmdb_id=1)
    conn.commit()
    app = app_factory(pages_router, conn=conn, state_extras={"templates": templates_stub})
    client = authed_client(app, conn)

    with (
        patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
        patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
    ):
        resp = client.get("/recommended")

    assert resp.status_code == 200
    embedded = resp.json()["all_recommendations_json"]
    for ch in "<>&":
        assert ch not in embedded
    recs = list(json.loads(embedded).values())
    assert [(r["title"], r["description"]) for r in recs] == [(_TITLE, _DESCRIPTION)]
