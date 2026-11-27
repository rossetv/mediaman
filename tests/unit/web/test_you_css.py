"""The pane over Watch's stylesheet (_you.css) and where Watch puts the pane (watch.html)."""

from __future__ import annotations

import re

from tests.unit.web._css import CSS_DIR, rules, style, text, token

WEB = CSS_DIR.parents[1]
WATCH = (WEB / "templates/watch.html").read_text()
REDUCED = "(prefers-reduced-motion: reduce)"
PHONE = "(max-width: 699.98px)"


def _transitions() -> list[tuple[tuple[str, ...], str, str]]:
    return [
        (media, ", ".join(selectors), decls["transition"])
        for media, selectors, decls in rules("_you.css")
        if "transition" in decls
    ]


def test_with_reduced_motion_the_pane_and_its_views_only_fade_on_the_fast_duration() -> None:
    calm = {
        sel: {"transition": value} for media, sel, value in _transitions() if media == (REDUCED,)
    }
    assert {".you-view", ".you-view.is-on"} <= set(calm)
    assert any(".you__panel" in sel and ".you__veil" in sel for sel in calm)
    for sel, decls in calm.items():
        for part in decls["transition"].split(","):
            prop, duration = part.split()[:2]
            assert prop in {"opacity", "visibility"}, (sel, part)
            assert duration in {"var(--dur-fast)", "0s"}, (sel, part)
        # The page's own reduced motion cuts every transition to nothing; the pane's fade must win.
        assert "!important" in decls["transition"], sel
    # What slides in full motion stands still.
    for still in (".you-view", ".you__panel"):
        assert style(still, "_you.css", REDUCED)["transform"] == "none", still


def test_the_pane_moves_only_by_transform_and_opacity_on_the_app_s_duration_tokens() -> None:
    for _, sel, value in _transitions():
        for part in value.split(","):
            prop, *rest = part.split()
            assert prop in {
                "opacity",
                "transform",
                "visibility",
                "background-color",
                "color",
                "none",
            }, (sel, part)
            assert re.search(r"var\(--dur(-fast|-slow)?\)|\b0s\b|^\s*none\s*$", part), (sel, part)


def test_on_a_phone_the_sheet_is_dragged_by_the_finger_with_no_easing_in_the_way() -> None:
    for sel in (".you__grab", ".you__bar"):
        assert style(sel, "_you.css", PHONE)["touch-action"] == "none", sel
    assert style(".you__panel.is-dragged", "_you.css", PHONE)["transition"] == "none"


def test_the_pane_stands_over_the_page_and_under_a_question_or_a_toast() -> None:
    def z(selector: str, sheet: str) -> int:
        value = style(selector, sheet)["z-index"]
        named = re.fullmatch(r"var\((--[\w-]+)\)", value)
        return int(token(named[1]) if named else value)

    pane = z(".you", "_you.css")
    assert int(token("--z-nav")) < pane
    assert (
        pane
        < z(".ui-confirm-overlay", "_ui_feedback.css")
        < z(".ui-toast-host", "_ui_feedback.css")
    )


def test_watch_loads_the_pane_s_script_before_the_one_that_starts_the_address() -> None:
    assert WATCH.index("watch/address.js") < WATCH.index("watch/you.js") < WATCH.index("/watch.js")
    assert "_you.css" in WATCH
    assert '{% include "_you.html" %}' in WATCH.split("{% block overlays %}")[1]


def test_settings_opens_wider_than_history_and_profile_keeps_the_narrow_panel() -> None:
    px = {k: int(v[:-2]) for k, v in style(".you", "_you.css").items() if k.startswith("--you-")}
    assert px["--you-narrow"] < px["--you-wide"] < px["--you-wider"] <= 1100
    panes = WEB / "templates/panes"
    widths = {}
    for name in ("profile", "history", "settings"):
        tag = re.search(
            r'<div class="you-view" data-view="' + name + r'"[^>]*>',
            (panes / f"_{name}.html").read_text(),
        )
        widths[name] = re.findall(r"\sdata-(wide|wider)\b", tag.group(0))  # type: ignore[union-attr]
    assert widths == {"profile": [], "history": ["wide"], "settings": ["wider"]}


def test_dragging_the_sheet_by_its_bar_or_handle_selects_no_text() -> None:
    for sel in (".you__bar", ".you__grab"):
        body = style(sel, "_you.css")
        assert body["user-select"] == "none", sel
        assert body["-webkit-user-select"] == "none", sel


def _at(value: str, d: int) -> float:
    """A calc() of --dir worked out for a view whose --dir is *d*, its units dropped."""
    expr = re.sub(r"var\(--dir(?:, 1)?\)", str(d), value).replace("calc", "").replace("%", "")
    assert re.fullmatch(r"[\d\s.+\-*/()]+", expr), value
    return float(eval(expr))


def test_a_step_pushes_the_new_view_over_the_old_which_slips_a_little_way_under_and_fades() -> None:
    view = style(".you-view", "_you.css")
    shift = re.fullmatch(r"translateX\((.*)\)", view["transform"])[1]
    # The deeper view, on the right (--dir 1), comes or goes the full width, on top.
    assert _at(shift, 1) == 100
    # The shallower, on the left (--dir -1), slips a short way under it.
    assert -40 <= _at(shift, -1) <= -20
    assert _at(view["z-index"], 1) > _at(view["z-index"], -1)


def test_views_on_the_glass_never_show_their_words_through_each_other() -> None:
    # A view draws no fill: the glass is the panel's, so no view covers the one under it.
    assert "background" not in style(".you-view", "_you.css")
    # Whichever way a step goes, the view it leaves fades out, even where the step holds it at full
    # strength while the panel changes width (watch/you.js).
    assert style(".you-view:not(.is-on)", "_you.css")["opacity"] == "0 !important"
    assert style(".you-view.is-on", "_you.css")["opacity"] == "1"


def _press_after_hover(sheet: str) -> list[str]:
    """The :active rules of *sheet* that come before the hover rule of the same thing, which would then
    win over them under a desktop's mouse."""
    css = text(sheet)
    early = []
    for m in re.finditer(r"([^{}]*:active[^{}]*)\{", css):
        for sel in re.split(r",(?![^()]*\))", m.group(1)):
            hover = css.find(sel.strip().replace(":active", ":hover"))
            if hover > m.start():
                early.append(sel.strip())
    return early


def test_a_press_lights_on_a_desktop_too_as_it_comes_after_the_hover_rule() -> None:
    for sheet, pressed in (
        ("_you.css", (".you__back", ".you__close")),
        ("_profile.css", (".yo-row", ".yo-tile")),
    ):
        lit = [
            s
            for _, sels, d in rules(sheet)
            if "background" in d
            for s in sels
            if s.endswith(":active")
        ]
        for name in pressed:
            assert any(s.endswith(name + ":active") for s in lit), name
    assert _press_after_hover("_profile.css") == []
    assert _press_after_hover("_you.css") == []
