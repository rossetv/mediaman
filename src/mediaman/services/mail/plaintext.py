"""Plain-text rendering of an HTML mail, for the text/plain part of a message."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol"}
)
_CELL_TAGS = frozenset({"td", "th"})
_SKIPPED_TAGS = frozenset({"style", "script", "head", "title"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        # Open links as (href, index in _parts where its text starts).
        self._links: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag == "a":
            self._links.append((dict(attrs).get("href") or "", len(self._parts)))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in _CELL_TAGS:
            self._parts.append(" ")
        elif tag == "a" and self._links:
            href, start = self._links.pop()
            label = " ".join("".join(self._parts[start:]).split())
            if href.startswith(("http://", "https://")) and href != label:
                self._parts.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).split("\n"))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def html_to_text(html: str) -> str:
    """Return *html* as readable plain text, with each link shown as ``label (url)``."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()
