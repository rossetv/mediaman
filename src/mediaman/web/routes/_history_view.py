"""What the History page says about one audit event: a short summary and every detail.

Pure functions over the stored columns. The list line carries only the summary;
the full detail list sits behind the row and is the one place a person, an
address or the stored text appears. Secrets never appear in either: the keys
that hold them are dropped or hidden, and free text is cleaned before it is
cut to length.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote

NOT_RECORDED = "Not recorded"
_HIDDEN = "[hidden]"
# Only a runaway value is cut: the details are where the whole event is read.
_VALUE_MAX = 20_000

_CONTROL_BYTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# What the security-event writer strips from its free-form fields.
_WRITER_STRIPPED = str.maketrans("", "", "\r\n\0")

# Keys whose values are credentials. Settings key *names* inside a list are not
# values of such a key, so "keys": ["plex_token"] still reads.
_SECRET_KEY_RE = re.compile(r"token|secret|passw|hash|api[_-]?key|authorization|cookie", re.I)
_DROPPED_KEYS = frozenset({"ua_hash"})
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
_LEGACY_BODY_RE = re.compile(r"^actor=(.+?) ip=(\S+)(?: (.*))?$", re.S)
_KEPT_RE = re.compile(r"Kept (?:for [\w ]+?|forever)(?= by| \(|;|$)")
_ADDED_RE = re.compile(r"Added '.*' to (Radarr|Sonarr)")


@dataclass(frozen=True, slots=True)
class EventView:
    summary: str
    actor: str | None
    ip: str | None
    fields: list[dict[str, object]]


def scrub_control(text: str | None) -> str:
    """Return *text* with terminal-corrupting control bytes removed."""
    return _CONTROL_BYTES_RE.sub("", text) if text else ""


def redact_text(text: str) -> str:
    """Hide credentials in free text: the value after a credential-looking key, the user and
    password of an address, and a bare bearer value.
    """
    text = _URL_USERINFO_RE.sub(lambda m: f"://{_HIDDEN}@" if m["at"] or m["plain"] else m[0], text)
    out: list[str] = []
    done = pos = 0
    while m := _KEY_RE.search(text, pos):
        pos = m.end()
        key = m["key"].lower()
        if not _SECRET_KEY_RE.search(key):
            continue
        value_re = _VALUE_RE
        if "authorization" in key:
            value_re = _AUTH_VALUE_RE
        elif "cookie" in key:
            value_re = _COOKIE_VALUE_RE
        end = _value_end(text, pos, value_re)
        if end > pos:
            out += (text[done:pos], _HIDDEN)
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
        return m[1] + _HIDDEN
    return m[0]


def hide_emails(text: str) -> str:
    """Hide every word shaped like an e-mail address."""
    return "".join(_HIDDEN if "@" in word.strip("@") else word for word in re.split(r"(\s+)", text))


def _clip(text: str) -> str:
    if len(text) <= _VALUE_MAX:
        return text
    return f"{text[:_VALUE_MAX]}… ({len(text) - _VALUE_MAX:,} more characters)"


def _clean(value: object, key: str = "") -> object:
    """Copy of a decoded JSON value with credentials hidden and ``ua_hash`` gone."""
    if key and _SECRET_KEY_RE.search(key):
        return _HIDDEN
    if isinstance(value, dict):
        return {k: _clean(v, str(k)) for k, v in value.items() if k not in _DROPPED_KEYS}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, str):
        return redact_text(scrub_control(value))
    return value


def split_security_body(detail: str, actor: str | None) -> tuple[str | None, str | None, str]:
    """Return (actor, ip, rest) from a security row's stored text.

    The writer puts ``actor=<actor> ip=<ip>`` first. When the actor column is known its exact
    prefix is stripped, so a typed name that itself contains ``ip=`` cannot pass for the address.
    """
    if actor is not None:
        prefix = f"actor={(actor or '-').translate(_WRITER_STRIPPED)} ip="
        if detail.startswith(prefix):
            ip, _, rest = detail[len(prefix) :].partition(" ")
            return actor, None if ip == "-" else ip, rest
    m = _LEGACY_BODY_RE.match(detail)
    if not m:
        return actor, None, detail
    who, ip, rest = m.groups()
    if actor is None and who != "-":
        actor = who
    return actor, None if ip == "-" else ip, rest or ""


def _decode_body(rest: str) -> dict[str, object] | None:
    if not rest.startswith("{"):
        return None
    try:
        decoded = json.loads(rest)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _plural(n: object, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _security_summary(event: str, body: dict[str, object] | None, actor: str | None) -> str:
    """A few words that say more than the label does, never an address or typed text."""
    if not body:
        return ""
    name = body.get("new_username") or body.get("target_username")
    keys = body.get("keys")
    count = body.get("synced", body.get("sent_to", body.get("count", body.get("revoked"))))
    if event == "user.created" and name:
        return f"{name} ({body['role']})" if body.get("role") else str(name)
    if event == "user.deleted" and name:
        return str(name)
    if event == "plex.unlinked" and body.get("user") and body["user"] != actor:
        return str(body["user"])
    if event == "user.email_updated" and name:
        return str(name)
    if event == "user.role_changed" and name:
        return f"{name} is now {body.get('role')}"
    if event.startswith("settings.write") and isinstance(keys, list):
        return _plural(len(keys), "setting", "settings")
    if not isinstance(count, int):
        return ""
    if event == "library.sync":
        return _plural(count, "item", "items")
    if event == "newsletter.sent":
        return f"{count} sent"
    if event == "scan.cleared":
        return f"{count} cleared"
    if event == "session.revoke_all":
        return _plural(count, "session", "sessions") + " ended"
    return ""


def _media_summary(action: str, detail: str, space_impact: str) -> str:
    if action == "library_left_out":
        reason = detail.rpartition("was not scanned: ")[2]
        if reason.startswith("its disk limit"):
            return "Check its disk limit in Settings"
        return reason[:1].upper() + reason[1:]
    if space_impact:
        return space_impact
    if kept := _KEPT_RE.match(detail):
        return kept.group(0)
    if added := _ADDED_RE.match(detail):
        return f"Added to {added.group(1)}"
    return ""


def _time_field(created_at: str) -> dict[str, object]:
    """The "Time" line; ``at`` is the moment for the browser to show in its own zone."""
    try:
        moment = datetime.fromisoformat(created_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        utc = moment.astimezone(UTC)
    except (ValueError, OverflowError):
        return _field("Time", created_at)
    return _field("Time", utc.strftime("%Y-%m-%d %H:%M:%S UTC")) | {
        "at": utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    }


def _field(label: str, value: object) -> dict[str, object]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = _clip(scrub_control(text))
    return {"label": label, "value": text or NOT_RECORDED, "empty": not text}


def describe(
    *,
    event_id: int,
    action: str,
    action_label: str,
    detail: str | None,
    actor: str | None,
    created_at: str,
    is_security: bool,
    title: str,
    media_item_id: str,
    plex_rating_key: str | None,
    space_impact: str,
) -> EventView:
    """Build the summary and the full ordered detail list for one event."""
    stored = scrub_control(detail)
    event = action[4:] if is_security else action
    who: str | None = actor or None
    ip: str | None = None
    body: dict[str, object] | None = None
    rest = stored
    if is_security:
        who, ip, rest = split_security_body(stored, actor)
        who = who or None
        body = _decode_body(rest)

    shown = f"{action_label} ({action})" if action_label != action else action
    fields = [
        _field("Event", shown),
        _time_field(created_at),
    ]
    if not is_security:
        fields.append(_field("Title", title if title != "Unknown" else ""))
    fields.append(_field("Name typed" if event == "login.failed" else "Who", who or ""))
    if is_security:
        fields.append(_field("IP address", ip or ""))
    else:
        fields += [
            _field("Media ID", media_item_id),
            _field("Plex key", plex_rating_key or ""),
            _field("Space reclaimed", space_impact),
        ]
    fields.append(_field("Event ID", str(event_id)))

    if body is not None:
        cleaned = _clean(body)
        assert isinstance(cleaned, dict)
        fields += [_field(str(k).replace("_", " ").capitalize(), v) for k, v in cleaned.items()]
        head = redact_text(stored[: len(stored) - len(rest)])
        record = _clip(head + json.dumps(cleaned, separators=(",", ":")))
    else:
        record = _clip(redact_text(stored))
        fields.append(_field("Details", redact_text(rest)))
    if is_security:
        fields.append(
            {"label": "Stored record", "value": record or NOT_RECORDED, "empty": not record}
        )

    if is_security:
        summary = hide_emails(_security_summary(event, body, who))
    else:
        summary = _media_summary(action, stored, space_impact)
    return EventView(summary=summary, actor=who, ip=ip, fields=fields)
