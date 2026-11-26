"""The search field as the AI's: its pill reads clearly, its pink stays a token, it keeps still
where motion is unwanted, and on a phone it folds once the field is left.

These read the stylesheet rules, since no layout engine runs here."""

from __future__ import annotations

import re

from tests.unit.web.test_css_system import (
    COLOUR_FILES,
    CSS_DIR,
    PHONE,
    REDUCE,
    _hex,
    _luminance,
    _media_blocks,
    _rules,
    _text,
    _token,
)

CSS = _text(CSS_DIR / "_watch_search.css")
PILL = ".watch-search .watch-search__ai"
SHOWN = ".watch-search.is-ai .watch-search__ai"
# What the field's two colours stand for.
AI = {"ai-a": "purple", "ai-b": "pink"}


def _body(css: str, selector: str) -> str:
    found = [body for heads, body in _rules(css) if selector in heads]
    assert found, selector
    return " ".join(found)


def _contrast(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_pill_s_words_keep_7_to_1_across_its_whole_violet() -> None:
    body = _body(CSS, PILL)
    assert re.search(r"(?<![\w-])color:\s*var\(--ai-pill-text\)", body)
    text = _hex(_token("ai-pill-text"))
    stops = re.findall(r"color-mix\(in srgb, var\(--(ai-[ab])\) (\d+)%, var\(--(\w+)\)\)", body)
    assert len(stops) == 2
    for colour, share, under in stops:
        top, base = _hex(_token(AI[colour])), _hex(_token(under))
        fill = tuple(
            c * int(share) / 100 + u * (1 - int(share) / 100)
            for c, u in zip(top, base, strict=True)
        )
        assert _contrast(text, fill) >= 7, (
            f"--ai-pill-text on --{AI[colour]} {share}% over --{under}"
        )


def test_the_words_that_say_how_to_ask_use_the_purple_text_token() -> None:
    assert re.search(r"(?<![\w-])color:\s*var\(--purple-text\)", _body(CSS, ".results-none--ai"))


def test_the_ai_pink_is_written_out_only_as_its_token() -> None:
    assert _token("pink") == "#ff6fae"
    offenders = [p.name for p in COLOUR_FILES if "#ff6fae" in p.read_text(encoding="utf-8").lower()]
    assert not offenders, f"the pink written out beside its token: {offenders}"


def test_a_hidden_pill_is_out_of_sight_and_of_the_way_of_tab_and_a_shown_one_is_not() -> None:
    hidden, shown = _body(CSS, PILL), _body(CSS, SHOWN)
    assert "visibility: hidden" in hidden and "max-width: 0" in hidden
    assert "visibility: visible" in shown and "pointer-events: auto" in shown


def test_the_ring_s_sheen_and_the_pill_s_twinkle_are_still_where_motion_is_unwanted() -> None:
    still = " ".join(_media_blocks(CSS, REDUCE))
    for selector in (
        ".watch-search.is-ai .watch-search__pill::after",
        ".watch-search.is-ai .watch-search__ai > .ico",
    ):
        assert "animation: none" in _body(still, selector), selector


def test_on_a_phone_the_pill_folds_once_the_field_is_left() -> None:
    phone = " ".join(_media_blocks(CSS, PHONE))
    body = _body(phone, ".watch-search.is-ai:not(.is-focus) .watch-search__ai")
    for part in ("max-width: 0", "opacity: 0", "visibility: hidden", "pointer-events: none"):
        assert part in body, part


def test_the_pill_has_an_edge_and_the_system_s_colours_in_forced_colours() -> None:
    forced = " ".join(_media_blocks(CSS, "(forced-colors: active)"))
    body = _body(forced, PILL)
    assert "border: 1px solid ButtonText" in body and "color: ButtonText" in body
