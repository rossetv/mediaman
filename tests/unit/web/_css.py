"""Read the app's stylesheets as rules, so a test can check how one rule relates to another
(a size against a size, a breakpoint against a breakpoint) and not the text of a declaration."""

from __future__ import annotations

import re
from pathlib import Path

CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"

Rule = tuple[str | None, list[str], dict[str, str]]


def text(sheet: str) -> str:
    """The sheet without its comments."""
    return re.sub(r"/\*.*?\*/", "", (CSS_DIR / sheet).read_text(encoding="utf-8"), flags=re.S)


def _split(selectors: str) -> list[str]:
    return [s.strip() for s in re.split(r",(?![^()]*\))", selectors) if s.strip()]


def _decls(body: str) -> dict[str, str]:
    pairs = (d.split(":", 1) for d in body.split(";") if ":" in d)
    return {k.strip(): v.strip() for k, v in pairs}


def _parse(css: str, media: str | None) -> list[Rule]:
    out: list[Rule] = []
    i = 0
    while (open_ := css.find("{", i)) != -1:
        depth, j = 1, open_ + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        head, body = css[i:open_].strip(), css[open_ + 1 : j - 1]
        if head.startswith("@media"):
            out += _parse(body, head[len("@media") :].strip())
        elif not head.startswith("@"):
            out.append((media, _split(head), _decls(body)))
        i = j
    return out


def rules(sheet: str) -> list[Rule]:
    """Every rule of *sheet* as (the @media query it sits in or None, selectors, declarations)."""
    return _parse(text(sheet), None)


def style(selector: str, sheet: str, media: str | None = None) -> dict[str, str]:
    """What the rules headed by *selector* declare, outside any @media or inside one whose query holds *media*; later wins."""
    merged: dict[str, str] = {}
    for query, selectors, decls in rules(sheet):
        if (media in query if media and query else query == media) and selector in selectors:
            merged.update(decls)
    return merged


def medias(selector: str, sheet: str, prop: str, containing: str = "") -> list[str]:
    """The @media queries in which *selector* sets *prop* to something holding *containing*."""
    return [
        query
        for query, selectors, decls in rules(sheet)
        if query and selector in selectors and containing in decls.get(prop, "")
    ]


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
    """Channels of a #hex or rgb()/rgba() colour; #000 and #fff short forms are widened."""
    if value.startswith("#"):
        hexa = value[1:]
        if len(hexa) == 3:
            hexa = "".join(c * 2 for c in hexa)
        return tuple(float(int(hexa[i : i + 2], 16)) for i in (0, 2, 4))
    return tuple(float(x) for x in re.findall(r"[\d.]+", value))


def contrast(fg: tuple[float, ...], bg: tuple[float, ...]) -> float:
    """WCAG contrast ratio of two colours."""

    def luminance(c3: tuple[float, ...]) -> float:
        lin = [
            c / 255 / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4
            for c in c3[:3]
        ]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)
