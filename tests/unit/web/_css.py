"""Read the app's stylesheets as rules, so a test can check how one rule relates to another
(a size against a size, a breakpoint against a breakpoint) and not the text of a declaration."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"

# What a rule sits in, outermost first: an @media's query alone, as "(hover: hover)", and any
# other grouping at-rule whole, as "@container (max-width: 259.98px)". () is outside them all.
Where = tuple[str, ...]
Rule = tuple[Where, list[str], dict[str, str]]

_GROUPS = ("@media", "@supports", "@container", "@layer")


def text(sheet: str) -> str:
    """The sheet without its comments."""
    return re.sub(r"/\*.*?\*/", "", (CSS_DIR / sheet).read_text(encoding="utf-8"), flags=re.S)


def _top_level(value: str, sep: str) -> list[str]:
    """*value* cut at each *sep* that stands outside quotes and brackets."""
    parts, depth, quote, start = [], 0, "", 0
    for i, c in enumerate(value):
        if quote:
            quote = "" if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == sep and not depth:
            parts.append(value[start:i])
            start = i + 1
    return [p.strip() for p in [*parts, value[start:]] if p.strip()]


def _decls(body: str) -> dict[str, str]:
    pairs = (d.split(":", 1) for d in _top_level(body, ";") if ":" in d)
    return {k.strip(): v.strip() for k, v in pairs}


def _parse(css: str, where: Where) -> list[Rule]:
    out: list[Rule] = []
    i = 0
    while (open_ := css.find("{", i)) != -1:
        depth, j = 1, open_ + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        # A statement at-rule (`@layer a, b;`, `@import …;`) ends before the head.
        head, body = css[i:open_].rsplit(";", 1)[-1].strip(), css[open_ + 1 : j - 1]
        if head.startswith("@media"):
            out += _parse(body, (*where, head[len("@media") :].strip()))
        elif head.startswith(_GROUPS):
            out += _parse(body, (*where, head))
        elif not head.startswith("@"):
            out.append((where, _top_level(head, ","), _decls(body)))
        i = j
    return out


@cache
def rules(sheet: str) -> list[Rule]:
    """Every rule of *sheet* as (what it sits in, its selectors, its declarations); read once."""
    return _parse(text(sheet), ())


def _norm(condition: str) -> str:
    condition = re.sub(r"\s+", " ", condition.strip().lower())
    return re.sub(r"\s*([():,])\s*", r"\1", condition).replace(":", ": ")


def _within(where: Where, media: str | Where | None) -> bool:
    """Whether a rule in *where* is in *media*: the same conditions, each an exact match or, for an
    @media query, one whole comma-separated alternative of it."""
    asked = () if media is None else (media,) if isinstance(media, str) else media
    return len(where) == len(asked) and all(
        _norm(want) == _norm(have)
        or (not have.startswith("@") and _norm(want) in map(_norm, _top_level(have, ",")))
        for want, have in zip(asked, where, strict=True)
    )


def style(selector: str, sheet: str, media: str | Where | None = None) -> dict[str, str]:
    """What the rules headed by *selector* declare in *media* (outside every at-rule when None);
    later wins. Raises when the sheet has no rule headed by *selector*, or none in *media*."""
    found = rules(sheet)
    assert any(selector in sels for _, sels, _ in found), f"no {selector} in {sheet}"
    assert media is None or any(_within(where, media) for where, _, _ in found), (
        f"no {media} in {sheet}"
    )
    merged: dict[str, str] = {}
    for where, selectors, decls in found:
        if _within(where, media) and selector in selectors:
            merged.update(decls)
    return merged


def medias(selector: str, sheet: str, prop: str, containing: str = "") -> list[Where]:
    """What each rule sits in where *selector* sets *prop* to something holding *containing*, past
    the top level. Raises when *selector* sets *prop* nowhere in the sheet."""
    found = [(w, d) for w, sels, d in rules(sheet) if selector in sels and prop in d]
    assert found, f"{selector} sets no {prop} in {sheet}"
    return [where for where, decls in found if where and containing in decls[prop]]


def token(name: str) -> str:
    """The value of the custom property *name* (with its dashes) in the tokens sheet."""
    found = re.search(rf"{re.escape(name)}:\s*([^;]+);", text("_tokens.css"))
    assert found, name
    return found[1].strip()


def px(value: str) -> float:
    found = re.fullmatch(r"(-?[\d.]+)px", value.strip())
    assert found, value
    return float(found[1])


def rgb(value: str) -> tuple[float, ...]:
    """Channels of a #hex or rgb()/rgba() colour, with the alpha last if it has one; #000 and #fff
    short forms are widened."""
    if value.startswith("#"):
        hexa = value[1:]
        if len(hexa) == 3:
            hexa = "".join(c * 2 for c in hexa)
        return tuple(float(int(hexa[i : i + 2], 16)) for i in (0, 2, 4))
    return tuple(float(x) for x in re.findall(r"[\d.]+", value))


def contrast(fg: tuple[float, ...], bg: tuple[float, ...]) -> float:
    """WCAG contrast ratio of two opaque colours."""
    for colour in (fg, bg):
        assert len(colour) == 3 or colour[3] == 1, f"{colour} lets what is under it show"

    def luminance(c3: tuple[float, ...]) -> float:
        lin = [
            c / 255 / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4
            for c in c3[:3]
        ]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)
