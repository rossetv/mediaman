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

NOT_RECORDED = "Not recorded"
_HIDDEN = "[hidden]"
_VALUE_MAX = 500
_RECORD_MAX = 2000

_CONTROL_BYTES_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# What the security-event writer strips from its free-form fields.
_WRITER_STRIPPED = str.maketrans("", "", "\r\n\0")

# Keys whose values are credentials. Settings key *names* inside a list are not
# values of such a key, so "keys": ["plex_token"] still reads.
_SECRET_KEY_RE = re.compile(r"token|secret|passw|hash|api[_-]?key|authorization|cookie", re.I)
_DROPPED_KEYS = frozenset({"ua_hash"})
_SECRET_TEXT_RE = re.compile(
    r"""([\w.-]*(?:token|secret|passw\w*|hash|api[_-]?key|authorization|cookie)[\w.-]*["']?\s*[=:]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|bearer\s+\S+|\S+)""",
    re.I,
)
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
    """Hide the value after any credential-looking key in free text."""
    return _SECRET_TEXT_RE.sub(lambda m: m.group(1) + _HIDDEN, text)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def _security_summary(event: str, body: dict[str, object] | None) -> str:
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


def _media_summary(detail: str, space_impact: str) -> str:
    if space_impact:
        return space_impact
    if kept := _KEPT_RE.match(detail):
        return kept.group(0)
    if added := _ADDED_RE.match(detail):
        return f"Added to {added.group(1)}"
    return ""


def _exact_time(created_at: str) -> str:
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError:
        return created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _field(label: str, value: object, limit: int = _VALUE_MAX) -> dict[str, object]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = _clip(scrub_control(text), limit)
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
        _field("Time", _exact_time(created_at)),
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
        record = _clip(head + json.dumps(cleaned, separators=(",", ":")), _RECORD_MAX)
    else:
        record = _clip(redact_text(stored), _RECORD_MAX)
        fields.append(_field("Details", redact_text(rest), _RECORD_MAX))
    if is_security:
        fields.append(
            {"label": "Stored record", "value": record or NOT_RECORDED, "empty": not record}
        )

    if is_security:
        summary = _security_summary(event, body)
    else:
        summary = _media_summary(stored, space_impact)
    return EventView(summary=summary, actor=who, ip=ip, fields=fields)
