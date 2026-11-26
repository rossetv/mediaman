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


SIDEWAYS_TALL = {"top": 62, "right": 62, "bottom": 21, "left": 62}
SCREEN = 874


def _length(rule: str, prop: str, insets: dict[str, int], **vars_: float) -> float:
    value = _last(prop, rule).replace("100dvh", f"{SCREEN}px")
    return _px(value, insets, **vars_)


def _sheet_foot(insets: dict[str, int], phone: bool, tv_bar: bool) -> float:
    """Where the search's sheet ends, counted down from the top of the window."""
    media = PHONE if phone else WIDE
    search, remote = CSS / "_watch_search.css", CSS / "_tv_remote.css"
    base = _of(".watch-sheet", None, search) + _of(".watch-sheet", media, search)
    rule = base
    if tv_bar:
        rule = _of("html.has-tv-mini .watch-sheet", media, remote)
    top = _length(base, "top", insets, nav_h=_nav_h(insets, phone), seen_top=0)
    return top + _length(rule, "max-height", insets, seen=SCREEN)


WIDE_INSETS = [SIDEWAYS_TALL, UPRIGHT]


@pytest.mark.parametrize(("phone", "insets"), [(p, i) for p in (False, True) for i in WIDE_INSETS])
def test_the_search_sheet_ends_where_it_did_whatever_the_top_inset_pushes_it_down(
    phone, insets
) -> None:
    level = {**insets, "top": 0}
    assert _sheet_foot(insets, phone, False) == _sheet_foot(level, phone, False)


@pytest.mark.parametrize(("phone", "insets"), [(p, i) for p in (False, True) for i in WIDE_INSETS])
def test_the_search_sheet_ends_above_the_home_indicator_with_its_rounded_edge_on_screen(
    phone, insets
) -> None:
    assert _sheet_foot(insets, phone, False) <= SCREEN - max(16, insets["bottom"])


@pytest.mark.parametrize(("phone", "insets"), [(p, i) for p in (False, True) for i in WIDE_INSETS])
def test_the_search_sheet_stays_above_the_tv_bar_however_far_the_insets_lift_it(
    phone, insets
) -> None:
    lifted = max(0, insets["bottom"] - (8 if phone else 16))
    assert _sheet_foot(insets, phone, True) <= _sheet_foot(NO_INSETS, phone, True) - lifted


@pytest.mark.parametrize("insets", [NO_INSETS, UPRIGHT])
@pytest.mark.parametrize("media", [None, PHONE])
def test_what_the_tv_bar_stands_over_can_be_scrolled_clear_of_it(insets, media) -> None:
    remote = CSS / "_tv_remote.css"
    bar = _px(
        _last("bottom", _of(".tv-mini", media, remote) or _of(".tv-mini", None, remote)), insets
    )
    body = _of("html.has-tv-mini:not(.is-tv-mini-leaving) body", media, remote)
    body = body or _of("html.has-tv-mini:not(.is-tv-mini-leaving) body", None, remote)
    assert _px(_last("padding-bottom", body), insets) - bar >= 66
    assert (
        _px(_last("scroll-padding-bottom", _of("html.has-tv-mini", media, remote)), insets) - bar
        >= 74
    )


def test_the_phone_storage_toasts_start_below_the_notch() -> None:
    toast = _of("html.has-space body .ui-toast-host", PHONE, CSS / "_watch_space.css")
    top = _px(_last("--toast-top", toast), UPRIGHT)
    assert top >= UPRIGHT["top"] + 12
    assert _px(_last("--toast-top", toast), NO_INSETS) == 60


@pytest.mark.parametrize("insets", [NO_INSETS, UPRIGHT])
@pytest.mark.parametrize("media", [None, PHONE])
def test_the_foot_of_an_open_title_stays_clear_of_the_tv_bar_over_it(insets, media) -> None:
    remote = CSS / "_tv_remote.css"
    bar_rule = _of("html.has-ttl .tv-mini", media, remote) or _of(
        "html.has-ttl .tv-mini", None, remote
    )
    bar = _px(_last("bottom", bar_rule), insets)
    page = _of("html.has-ttl.has-tv-mini .ttl", None, remote)
    assert _px(_last("padding-bottom", page), insets) - bar >= 82
    assert _px(_last("scroll-padding-bottom", page), insets) - bar >= 82
    assert _px(_last("padding-bottom", page), NO_INSETS) == 98
