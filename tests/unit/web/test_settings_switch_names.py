"""Every toggle on the rendered settings page must have an accessible name."""

from __future__ import annotations

import re
from html.parser import HTMLParser

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

    for input_id in (
        "min_age_days",
        "inactivity_days",
        "grace_days",
        "scan_time",
        "ai_questions_per_day",
        "ai_everyone_per_day",
    ):
        tag = re.search(rf"<input\b[^>]*\bid=\"{input_id}\"[^>]*>", html)
        assert tag, input_id
        assert re.search(r"aria-label(ledby)?=\"[^\"]+\"", tag.group(0)), tag.group(0)


class _ControlNames(HTMLParser):
    """Collect form controls that no label, aria-label or aria-labelledby names."""

    def __init__(self) -> None:
        super().__init__()
        self.label_depth = 0
        self.label_for: set[str] = set()
        self.controls: list[tuple[dict[str, str | None], int]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        elif tag in ("input", "select", "textarea") and a.get("type") not in (
            "hidden",
            "submit",
            "button",
        ):
            self.controls.append((a, self.label_depth))

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth -= 1

    def unnamed(self):
        return [
            a.get("id")
            for a, depth in self.controls
            if not (
                depth
                or a.get("aria-label")
                or a.get("aria-labelledby")
                or a.get("id") in self.label_for
            )
        ]


def test_every_settings_control_has_an_accessible_name(app_factory, authed_client, conn):
    app = app_factory(
        router, conn=conn, state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))}
    )
    parser = _ControlNames()
    parser.feed(authed_client(app, conn).get("/settings").text)

    assert len(parser.controls) > 20
    assert parser.unnamed() == []
