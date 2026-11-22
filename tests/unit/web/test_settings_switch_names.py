"""Every toggle on the rendered settings page must have an accessible name."""

from __future__ import annotations

import re

from fastapi.templating import Jinja2Templates

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.web.routes.settings import router


def test_every_settings_switch_has_an_accessible_name(app_factory, authed_client, conn):
    app = app_factory(
        router, conn=conn, state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))}
    )
    html = authed_client(app, conn).get("/settings").text

    switches = re.findall(r"<span\b[^>]*role=\"switch\"[^>]*>", html)
    assert len(switches) >= 2
    for tag in switches:
        assert re.search(r"aria-label(ledby)?=\"[^\"]+\"", tag), tag


def test_settings_number_and_time_inputs_have_an_accessible_name(app_factory, authed_client, conn):
    app = app_factory(
        router, conn=conn, state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))}
    )
    html = authed_client(app, conn).get("/settings").text

    for input_id in ("min_age_days", "inactivity_days", "grace_days", "scan_time"):
        tag = re.search(rf"<input\b[^>]*\bid=\"{input_id}\"[^>]*>", html)
        assert tag, input_id
        assert re.search(r"aria-label(ledby)?=\"[^\"]+\"", tag.group(0)), tag.group(0)
