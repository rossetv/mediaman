"""How the history's stored text is cleaned, and the title an event is listed under."""

from __future__ import annotations

import re
from urllib.parse import unquote

from mediaman.core.format import title_from_audit_detail

#: What stands where a credential or an address was.
HIDDEN = "[hidden]"
_CONTROL_BYTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Keys whose values are credentials. Settings key *names* inside a list are not
# values of such a key, so "keys": ["plex_token"] still reads.
SECRET_KEY_RE = re.compile(r"token|secret|passw|hash|api[_-]?key|authorization|cookie", re.I)
# Every run of key characters followed by a separator. A run is only tried from its start and
# never given back, which keeps the search linear.
_KEY_RE = re.compile(
    r"""(?<![\w.-])(?P<key>[\w.-]++)\\*+["']?+\s*+(?:[=:]++>?|%(?:25)*+3[AD])\s*+""", re.I
)
_QUOTED = (
    r"""(?:"(?:[^"\\]++|\\.)*+(?:"|\\?\Z)"""
    r"""|'(?:[^'\\]++|\\.)*+(?:'|\\?\Z)"""
    r"""|(?P<esc>\\{1,8}+)(?P<q>["'])(?:(?!(?<!\\)(?P=esc)(?P=q)).)*+(?:(?P=esc)(?P=q)|\Z))"""
)
_VALUE_RE = re.compile(rf"{_QUOTED}|(?:bearer|basic)\s++\S++|\S++", re.I | re.S)
# A header's value is a scheme and its credentials, or a list of pairs, up to the end of the list.
_PART = r"""(?:"[^"\n]*+(?:"|\Z)|[^\s"])++"""
_AUTH_VALUE_RE = re.compile(
    rf"{_QUOTED}|{_PART}(?:[ \t]++{_PART})?+(?:(?<=[,;])[ \t]*+{_PART})*+", re.I | re.S
)
_COOKIE_VALUE_RE = re.compile(rf"{_QUOTED}|{_PART}(?:(?<=[,;])[ \t]*+{_PART})*+", re.S)
# What a value that is a list goes on with: another quoted text that is not itself a key.
_NEXT_ITEM_RE = re.compile(rf"\s*+,\s*+{_QUOTED}(?!\s*+[=:])", re.S)
_NESTED_RE = re.compile(
    rf"""{_QUOTED}|(?P<open>[\[({{])|(?P<close>[\])}}])|[^\[\](){{}}"'\\]++|.""", re.S
)
# After a colon a password may hold a slash, but a port followed by a path is no password. The
# first form takes the text whether or not an @ comes, which keeps the search linear.
_URL_USERINFO_RE = re.compile(
    r"""://(?:[^\s/"@:\[]*+:(?!\d{1,5}+[/?#])[^\s"@]*+(?P<at>@(?:[^\s/"@]*+@)*+)?+"""
    r"""|(?P<plain>(?:[^\s/"@]*+@)++))"""
)
_BEARER_RE = re.compile(r"\b(bearer(\s*+[=:]\s*+|\s++|%20|\+))([\w.~+/=%-]++)", re.I)

_QUOTED_TITLE_RE = re.compile(r"'([^'/\\][^']*)'")
# Deletion problems' stored text quotes paths and errors, so a quoted phrase is no title there.
_DELETE_PREFIX = "delete_"
#: Where an event of a delete folder, not of a title, keeps the folder.
DELETE_ROOT_PREFIX = "_delete_root:"


def event_title(action: str, media_item_id: str, detail: str | None) -> str:
    """The title an event's stored text names, as its line may show it; empty when none."""
    if (
        not detail
        or action.startswith("sec:")
        or action == "library_left_out"
        or media_item_id.startswith(DELETE_ROOT_PREFIX)
    ):
        return ""
    title = title_from_audit_detail(detail, strict=True)
    if title != "Unknown":
        return hide_emails(title).strip()
    found = None if action.startswith(_DELETE_PREFIX) else _QUOTED_TITLE_RE.search(detail)
    return hide_emails(redact_text(found[1])).strip() if found else ""


def scrub_control(text: str | None) -> str:
    """Return *text* with terminal-corrupting control bytes removed."""
    return _CONTROL_BYTES_RE.sub("", text) if text else ""


def redact_text(text: str) -> str:
    """Hide credentials in free text: the value after a credential-looking key, the user and
    password of an address, and a bare bearer value.
    """
    text = _URL_USERINFO_RE.sub(lambda m: f"://{HIDDEN}@" if m["at"] or m["plain"] else m[0], text)
    out: list[str] = []
    done = pos = 0
    while m := _KEY_RE.search(text, pos):
        pos = m.end()
        key = m["key"].lower()
        if not SECRET_KEY_RE.search(key):
            continue
        value_re = _VALUE_RE
        if "authorization" in key:
            value_re = _AUTH_VALUE_RE
        elif "cookie" in key:
            value_re = _COOKIE_VALUE_RE
        end = _value_end(text, pos, value_re)
        if end > pos:
            out += (text[done:pos], HIDDEN)
            done = pos = end
    return _BEARER_RE.sub(_hide_bearer, "".join(out) + text[done:])


def _value_end(text: str, pos: int, value_re: re.Pattern[str]) -> int:
    """Where the value at *pos* ends: a bracketed one at its closing bracket, and a quoted one
    after the quoted items that follow it.
    """
    if text[pos : pos + 1] in ("[", "(", "{"):
        depth = 0
        while token := _NESTED_RE.match(text, pos):
            pos = token.end()
            depth += bool(token["open"]) - bool(token["close"])
            if depth == 0:
                break
        return pos
    value = value_re.match(text, pos)
    if value is None:
        return pos
    end = value.end()
    if value[0][0] in "\"'\\":
        while item := _NEXT_ITEM_RE.match(text, end):
            end = item.end()
    return end


def _hide_bearer(m: re.Match[str]) -> str:
    """Hide what follows "bearer" unless it reads as the next word of a title."""
    # In a query string a space is written as a plus.
    value = unquote(m[3].replace("+", " ") if m[2] == "+" else m[3])
    word = (value.split() or [""])[0].rstrip(".")
    if "=" in m[2] or len(word) >= 16 or re.search(r"[\d.]", word):
        return m[1] + HIDDEN
    return m[0]


def hide_emails(text: str) -> str:
    """Hide every word shaped like an e-mail address."""
    if "@" not in text:
        return text
    return "".join(HIDDEN if "@" in word.strip("@") else word for word in re.split(r"(\s+)", text))
