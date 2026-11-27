"""The search field as the AI's: its pill reads clearly, its pink stays a token, it keeps still
where motion is unwanted, and on a phone it is its sparkle alone, so the words typed keep the room.

These read the stylesheet rules, since no layout engine runs here."""

from __future__ import annotations

import re

from tests.unit.web._css import contrast, rgb, style, token
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


def _sparkle_alone(media: str) -> None:
    block = " ".join(_media_blocks(CSS, media))

    def ending(tail: str) -> str:
        found = [b for heads, b in _rules(block) if any(h.endswith(tail) for h in heads)]
        assert found, tail
        return " ".join(found)

    # Its words go; it is a round button of the sparkle, as wide as it is high.
    assert _decl(ending(".watch-search__ai > span"), "display") == "none"
    pill = ending(".watch-search__ai")
    assert _decl(pill, "width") == _decl(pill, "height") == "30px"
    assert _decl(pill, "border-radius") == "50%"
    # Shown, it stays shown when the field is left: nothing folds it away again.
    assert not any(
        "is-focus" in h and "watch-search__ai" in h for heads, _ in _rules(block) for h in heads
    )


def test_on_a_phone_the_pill_is_its_sparkle_alone() -> None:
    _sparkle_alone(PHONE)


def test_the_pill_has_an_edge_and_the_system_s_colours_in_forced_colours() -> None:
    forced = " ".join(_media_blocks(CSS, "(forced-colors: active)"))
    body = _body(forced, PILL)
    assert "border: 1px solid ButtonText" in body and "color: ButtonText" in body


def _decl(body: str, prop: str) -> str:
    found = re.findall(rf"(?<![\w-]){re.escape(prop)}:\s*([^;]+);", body)
    assert found, prop
    return found[-1].strip()


def test_the_ai_ring_takes_the_resting_field_s_shape_so_none_of_it_is_cut_off() -> None:
    pill = _body(CSS, ".watch-search__pill")
    clip = re.search(r"clip-path:\s*inset\((.+?) round", pill)
    assert clip
    ring = _body(CSS, ".watch-search__pill::after")
    assert _decl(ring, "inset") == clip.group(1)
    # It follows the field as it grows and shrinks, as the cut does.
    clip_move = re.search(r"clip-path (var\(--dur-open\) var\(--ease-spring\))", pill)
    assert clip_move and f"inset {clip_move.group(1)}" in _decl(ring, "transition")


def test_the_halo_s_colour_turns_with_the_ring_not_after_it() -> None:
    moves = [
        m.split()[0] for m in _decl(_body(CSS, ".watch-search__halo"), "transition").split(",")
    ]
    focus = _body(CSS, ".watch-search.is-focus .watch-search__halo")
    delays = [d.strip() for d in _decl(focus, "transition-delay").split(",")]
    # A shorter list of delays repeats over the transitions.
    delay = delays[moves.index("outline-color") % len(delays)]
    assert float(delay.rstrip("s")) == 0, delay


def test_on_a_phone_held_sideways_the_pill_is_its_sparkle_alone() -> None:
    _sparkle_alone("(max-height: 500px) and (pointer: coarse)")


def test_where_motion_is_unwanted_what_thinks_pulses_over_the_page_s_stillness() -> None:
    # The page stops every animation after one short run, with !important: a pulse must outrank it.
    ask = " ".join(_media_blocks(_text(CSS_DIR / "_watch_ask.css"), REDUCE))
    field = " ".join(_media_blocks(CSS, REDUCE))
    for block, selector in (
        (ask, ".is-thinking .ask__skel .skel--poster::after"),
        (ask, ".is-thinking .ask__orb > .ico"),
        (field, ".watch-search .watch-search__ai.is-thinking > .ico"),
    ):
        pulse = _decl(_body(block, selector), "animation")
        assert pulse.startswith("ai-twinkle-pulse") and pulse.endswith("infinite !important"), (
            selector
        )


GLOW = ".wbar .watch-search::after"
GLOW_ON = ".watch-search.is-ai::after"


def test_the_ai_glow_hugs_the_field_as_the_ring_does() -> None:
    glow, ring = _body(CSS, GLOW), _body(CSS, ".watch-search__pill::after")
    assert _decl(glow, "inset") == _decl(ring, "inset")


def _top_level(value: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(value):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(value[start:i])
            start = i + 1
    return [*parts, value[start:]]


def test_the_ai_glow_falls_evenly_all_round_and_never_below() -> None:
    assert _decl(_body(CSS, GLOW_ON), "opacity") == "1"
    assert _decl(_body(CSS, GLOW), "box-shadow") == "var(--glow-ai)"
    shadows = _top_level(" ".join(_token("glow-ai").split()))
    assert len(shadows) >= 2
    for shadow in shadows:
        offsets = re.match(r"(-?[\d.]+)(?:px)? (-?[\d.]+)(?:px)?", shadow.strip())
        assert offsets, shadow
        assert float(offsets.group(2)) == 0, f"a glow cast downward pools: {shadow}"
        assert any(f"var(--{c})" in shadow for c in AI.values()), shadow


def test_where_motion_is_unwanted_the_ai_ring_and_glow_only_fade_quickly() -> None:
    still = " ".join(_media_blocks(CSS, REDUCE))
    for selector in (".watch-search__pill::after", GLOW):
        body = _body(still, selector)
        assert _decl(body, "transition-property") == "opacity !important", selector
        assert _decl(body, "transition-duration") == "var(--dur-fast) !important", selector


def test_the_words_that_say_how_to_ask_keep_aa_on_every_surface_up_to_a_raised_card() -> None:
    colour = re.fullmatch(
        r"var\((--[\w-]+)\)", style(".results-none--ai", "_watch_search.css")["color"]
    )
    assert colour
    for surface in ("--bg", "--s1", "--s2", "--s3", "--s4"):
        assert contrast(rgb(token(colour[1])), rgb(token(surface))) >= 4.5, surface
