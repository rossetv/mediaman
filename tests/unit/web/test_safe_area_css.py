"""What is fixed at an edge of the window stays clear of a phone's notch, island and home
indicator: each length is worked out with the insets of an iPhone held upright or on its side."""

import re

import pytest

from tests.unit.web.test_watch_css import SHEET, _of

CSS = SHEET.parent
PHONE = "(max-width: 699.98px)"
WIDE = "(min-width: 700px)"
UPRIGHT = {"top": 62, "right": 0, "bottom": 34, "left": 0}
SIDEWAYS = {"top": 0, "right": 62, "bottom": 21, "left": 62}
NO_INSETS = {"top": 0, "right": 0, "bottom": 0, "left": 0}


def _last(prop: str, decls: str) -> str:
    return re.findall(rf"(?<![\w-]){re.escape(prop)}:\s*([^;]+)", decls)[-1].strip()


def _px(value: str, insets: dict[str, int], **vars_: float) -> float:
    """What a length comes to in pixels with *insets*, the custom properties given as *vars_*."""
    value = re.sub(r"env\(safe-area-inset-(\w+)(?:,[^()]*)?\)", lambda m: str(insets[m[1]]), value)
    value = re.sub(
        r"var\(--([\w-]+)(?:,\s*([^()]*))?\)",
        lambda m: str(vars_.get(m[1].replace("-", "_"), m[2] or "")),
        value,
    )
    value = value.replace("calc", "").replace("px", "")
    return float(eval(value, {"__builtins__": {}}, {"max": max, "min": min}))


def _nav_h(insets: dict[str, int], phone: bool) -> float:
    rule = _of(":root", PHONE if phone else None, CSS / "_nav.css")
    return _px(_last("--nav-h", rule), insets)


@pytest.mark.parametrize("insets", [NO_INSETS, UPRIGHT])
def test_the_wide_search_pill_stands_in_the_middle_of_the_bar_below_the_notch(insets) -> None:
    # The islands are 44px high in the middle of the bar's own 72px, under the inset.
    top = _last("top", _of(".wbar .watch-search", sheet=CSS / "_watch_search.css"))
    assert _px(top, insets, nav_h=_nav_h(insets, False), ph=44) == insets["top"] + 14


def test_a_phone_s_focused_search_field_and_its_sheet_sit_below_the_island() -> None:
    sheet = CSS / "_watch_search.css"
    field = _px(
        _last("top", _of(".wbar .watch-search.is-focus", PHONE, sheet)), UPRIGHT, seen_top=0
    )
    # Where the bar's islands are on a phone: in the middle of its 64px, under the inset.
    assert field == UPRIGHT["top"] + 10
    below = _px(_last("top", _of(".watch-sheet", PHONE, sheet)), UPRIGHT, seen_top=0)
    assert below - field == 72 - 10


@pytest.mark.parametrize(("media", "insets"), [(None, SIDEWAYS), (PHONE, UPRIGHT)])
def test_the_settings_save_bar_keeps_its_buttons_off_the_home_indicator(media, insets) -> None:
    padding = _last("padding", _of(".setg-pg .savebar", media, CSS / "_settings_pg.css"))
    # Its lengths, a function counting as one: the third is the bottom, or with two, the first.
    lengths = re.findall(r"(?:[\w.-]+\((?:[^()]|\([^()]*\))*\)|\S)+", padding)
    bottom = lengths[2] if len(lengths) > 2 else lengths[0]
    assert _px(bottom, insets) >= insets["bottom"]


def test_toasts_at_the_foot_of_the_screen_stand_above_the_home_indicator() -> None:
    feedback = CSS / "_ui_feedback.css"
    wide = _px(_last("bottom", _of(".ui-toast-host", WIDE, feedback)), SIDEWAYS, toast_bottom=20)
    assert wide >= SIDEWAYS["bottom"]
    watch = _of("html.has-watch-search .ui-toast-host", PHONE, feedback)
    assert _px(_last("bottom", watch), UPRIGHT) >= UPRIGHT["bottom"]


@pytest.mark.parametrize("insets", [NO_INSETS, UPRIGHT])
def test_a_phone_s_tv_bar_is_off_the_home_indicator_and_what_stands_over_it_rises_with_it(
    insets,
) -> None:
    remote = CSS / "_tv_remote.css"
    bar = _px(_last("bottom", _of(".tv-mini", PHONE, remote)), insets)
    assert bar >= max(8, insets["bottom"])
    toast = _of("html.has-tv-mini.has-watch-search .ui-toast-host", PHONE, remote)
    assert _px(_last("bottom", toast), insets) - bar >= 68
    ask = _px(
        _last("bottom", _of("html.has-tv-mini .ask__below", sheet=CSS / "_watch_ask.css")), insets
    )
    assert ask - bar >= 80


def test_the_storage_panel_and_its_toasts_keep_clear_of_a_sideways_phone_s_notch() -> None:
    space = CSS / "_watch_space.css"
    panel = _of(".space__panel", sheet=space)
    for side in ("top", "right", "bottom"):
        assert _px(_last(side, panel), SIDEWAYS) >= max(12, SIDEWAYS[side])
    toast = _of("html.has-space body .ui-toast-host", WIDE, space)
    assert _px(_last("left", toast), SIDEWAYS) >= SIDEWAYS["left"]
