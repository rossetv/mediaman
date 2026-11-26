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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from mediaman.core.audit import CLI_UNLOCK, UNSIGNED_ACTIONS
from mediaman.web.history_text import (
    HIDDEN,
    SECRET_KEY_RE,
    hide_emails,
    redact_text,
    scrub_control,
)
from mediaman.web.repository.history import Play, PlayPart, Who

NOT_RECORDED = "Not recorded"
# Only a runaway value is cut: the details are where the whole event is read.
_VALUE_MAX = 20_000

# What the security-event writer strips from its free-form fields.
_WRITER_STRIPPED = str.maketrans("", "", "\r\n\0")

_DROPPED_KEYS = frozenset({"ua_hash"})
_LEGACY_BODY_RE = re.compile(r"^actor=(.+?) ip=(\S+)(?: (.*))?$", re.S)
_KEPT_RE = re.compile(r"Kept (?:for [\w ]+?|forever)(?= by| \(|;|$)")
_ADDED_RE = re.compile(r"Added '.*' to (?:Radarr|Sonarr)")


@dataclass(frozen=True, slots=True)
class EventView:
    summary: str
    actor: str | None
    ip: str | None
    fields: list[dict[str, object]]


def _clip(text: str) -> str:
    if len(text) <= _VALUE_MAX:
        return text
    return f"{text[:_VALUE_MAX]}… ({len(text) - _VALUE_MAX:,} more characters)"


def _clean(value: object, key: str = "") -> object:
    """Copy of a decoded JSON value with credentials hidden and ``ua_hash`` gone."""
    if key and SECRET_KEY_RE.search(key):
        return HIDDEN
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
    if _ADDED_RE.match(detail):
        return "Added to downloads"
    return ""


def _time_field(created_at: str, label: str = "When", key: str = "when") -> dict[str, object]:
    """A moment's line; ``at`` is for the browser to show it in its own zone."""
    try:
        moment = datetime.fromisoformat(created_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        utc = moment.astimezone(UTC)
    except (ValueError, OverflowError):
        return _field(key, label, created_at)
    return _field(key, label, utc.strftime("%Y-%m-%d %H:%M:%S UTC")) | {
        "at": utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    }


def _field(key: str, label: str, value: str) -> dict[str, object]:
    """One line of the details; *key* is fixed, so nothing has to go by the label's words."""
    text = _clip(scrub_control(value))
    return {"key": key, "label": label, "value": text or NOT_RECORDED, "empty": not text}


# The lines of a security event's own keys, where the key alone does not read well.
_KEY_LABELS = {
    "already_deleting": "Already being deleted",
    "arr_id": "Number in Radarr or Sonarr",
    "dl_id": "Download",
    "expires_in_seconds": "Ends after",
    "force_rotation": "Must change password",
    "had_lock": "Was locked out",
    "id": "Number",
    "keys": "Settings",
    "new_username": "New user",
    "rejected": "Refused",
    "revoked": "Sessions ended",
    "run_id": "Scan number",
    "searching_for_seconds": "Searched for",
    "sent_to": "Sent to",
    "synced": "Titles synced",
    "target_id": "User number",
    "target_username": "User",
    "window_seconds": "Lasts",
}
_VALUE_WORDS = {
    "bad_credentials": "Wrong name or password",
    "wrong_old_password": "The current password was wrong",
    "server_changed": "Another Plex server was set",
    "moved": "Linked to another person instead",
    "self": "Unlinked by the person",
    "admin": "Unlinked by an admin",
}
_CODE_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)+")
_NAME_KEYS = frozenset({"username", "new_username", "target_username", "user", "device", "email"})


def _words(code: str) -> str:
    return code.replace("_", " ").replace(".", " ").strip().capitalize()


def _plain(key: str, value: object) -> str:
    """A stored value as a person would say it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int | float):
        if key.endswith("_seconds") and 0 < value < 10**9:
            return _duration(value) if value >= 60 else f"{value:g} s"
        return str(value)
    if isinstance(value, list):
        return ", ".join(_plain(key, v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{_key_label(str(k))}: {_plain(str(k), v)}" for k, v in value.items())
    text = str(value)
    if key in _NAME_KEYS or key.endswith("id"):
        return text
    if key == "reason" and text in _VALUE_WORDS:
        return _VALUE_WORDS[text]
    if key in ("role", "service", "kind") or _CODE_RE.fullmatch(text):
        return _words(text)
    return text


def _key_label(key: str) -> str:
    return _KEY_LABELS.get(key) or _words(key.removesuffix("_seconds"))


def _body_field(key: str, value: object) -> dict[str, object]:
    if isinstance(value, str) and re.match(r"\d{4}-\d\d-\d\dT\d\d:", value):
        return _time_field(value, _key_label(key), "detail")
    return _field("detail", _key_label(key), _plain(key, value))


def _who_field(event: str, who: str | None, unsigned: bool) -> dict[str, object]:
    if event == "login.failed":
        return _field("who", "Name tried", who or "")
    if event == "subscriber.opted_out":
        return _field("who", "Subscriber", who or "")
    if unsigned:
        return _field("who", "Who", "The command line")
    return _field("who", "Who", who or "mediaman, by itself")


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

    unsigned = action in UNSIGNED_ACTIONS or (action, actor) == CLI_UNLOCK
    fields = [
        _who_field(event, who, unsigned),
        _time_field(created_at),
        _field("event", "Event", action_label[:1].upper() + action_label[1:]),
    ]
    if is_security:
        fields.append(_field("address", "Address", ip or ""))
    else:
        fields += [
            _field("title", "Title", title if title != "Unknown" else ""),
            _field("space", "Space freed", space_impact),
        ]
    if body is not None:
        cleaned = _clean(body)
        assert isinstance(cleaned, dict)
        fields += [_body_field(str(k), v) for k, v in cleaned.items()]
        head = redact_text(stored[: len(stored) - len(rest)])
        record = _clip(head + json.dumps(cleaned, separators=(",", ":")))
    else:
        record = _clip(redact_text(stored))
        fields.append(_field("detail", "Details", redact_text(rest)))
    if not is_security:
        fields += [
            _field("media_id", "Library number", media_item_id),
            _field("plex_key", "Plex number", plex_rating_key or ""),
        ]
    fields += [
        _field("id", "Event number", str(event_id)),
        _field("code", "Recorded as", action),
    ]
    if is_security:
        # Any other event's text is its "Details".
        fields.append(
            {
                "key": "record",
                "label": "Stored record",
                "value": record or NOT_RECORDED,
                "empty": not record,
            }
        )

    if is_security:
        summary = hide_emails(_security_summary(event, body, who))
    else:
        summary = _media_summary(action, stored, space_impact)
    return EventView(summary=summary, actor=who, ip=ip, fields=fields)


# What a person who is no admin reads of their own events. The stored text is not among it:
# it can hold other people's names, addresses and settings.
_OWN_FIELDS = ("when", "event", "address", "title", "space", "id")
_OWN_KEYS = (
    "id",
    "action",
    "action_label",
    "badge_class",
    "space_impact",
    "created_at",
    "is_security",
)


def own_item(item: dict[str, object], username: str) -> dict[str, object]:
    """*item*, the admin's reading of an event of *username*'s, as they may read it themselves.

    The keys are the same, so one template draws both.
    """
    title = item["line_title"]
    stored = item["fields"]
    assert isinstance(stored, list)
    fields = [_field("who", "Who", username)]
    for field in stored:
        if field["key"] == "title":
            fields.append(_field("title", "Title", str(title) if title != "Unknown" else ""))
        elif field["key"] in _OWN_FIELDS:
            fields.append(field)
    security = bool(item["is_security"])
    return {k: item[k] for k in _OWN_KEYS} | {
        "media_item_id": "",
        "plex_rating_key": item["plex_rating_key"],
        "title": title,
        "line_title": title,
        "detail": "",
        "summary": "" if security or item["action"] == "library_left_out" else item["summary"],
        "actor": username,
        "ip": item["ip"] if security else None,
        "fields": fields,
    }


def _place(part: PlayPart) -> str:
    season = f"S{part.season_number}" if part.season_number is not None else ""
    return season + (f"E{part.episode_number}" if part.episode_number is not None else "")


def _part_label(part: PlayPart) -> str:
    return " · ".join(p for p in (_place(part), part.title and scrub_control(part.title)) if p)


def _episode_range(parts: Sequence[PlayPart]) -> str:
    """The episodes of one season in a row, as "S1E2 to E4"; empty when they are not that."""
    numbers = sorted({p.episode_number for p in parts if p.episode_number is not None})
    seasons = {p.season_number for p in parts}
    in_a_row = numbers and numbers[-1] - numbers[0] == len(numbers) - 1 == len(parts) - 1
    if len(numbers) < 2 or not in_a_row or len(seasons) > 1 or None in seasons:
        return ""
    return f"S{seasons.pop()}E{numbers[0]} to E{numbers[-1]}"


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, rest = divmod(minutes, 60)
    return " ".join(p for p in (f"{hours} h" if hours else "", f"{rest} min" if rest else "") if p)


def play_item(play: Play) -> dict[str, object]:
    """A sitting in the shape of an event's item."""
    title = scrub_control(play.title)
    label = "finished" if play.finished else "started"
    parts = play.parts if play.media_type == "tv" else ()
    episodes: list[dict[str, object]] = [
        {
            "label": _part_label(p) or "Episode",
            "season": p.season_number,
            "episode": p.episode_number,
            "title": scrub_control(p.title) or None,
            "finished": p.finished,
            "position_s": p.position_s,
            "duration_s": p.duration_s,
        }
        for p in parts
    ]
    last = play.parts[-1] if play.parts else None
    runtime = play.runtime_s or (last.duration_s if last and len(parts) == 1 else None)
    progress: dict[str, object] | None = None
    if last and not play.finished and last.position_s and last.duration_s:
        stopped = f"{_duration(last.position_s)} of {_duration(last.duration_s)}"
        progress = {
            "position_s": last.position_s,
            "duration_s": last.duration_s,
            "fraction": min(1.0, round(last.position_s / last.duration_s, 4)),
            "text": stopped,
        }
    episode_range = _episode_range(parts)
    summary = episode_range or (f"{len(parts)} episodes" if len(parts) > 1 else "")
    if len(parts) == 1:
        summary = _part_label(parts[0])
    shown = f"{title} ({play.year})" if play.year else title
    fields = [
        _field("who", "Who", play.username),
        _time_field(play.created_at, "Finished" if play.finished else "Stopped"),
    ]
    if play.started_at != play.created_at:
        fields.append(
            _time_field(play.started_at, "First episode ended", "first")
            if parts
            else _time_field(play.started_at, "First stopped", "first")
        )
    fields.append(_field("title", "Title", shown))
    if episode_range:
        fields.append(_field("episodes", "Episodes", episode_range))
    fields += [
        _field("episode", "Episode", f"{e['label']} ({'finished' if e['finished'] else 'started'})")
        for e in episodes
    ]
    if progress:
        fields.append(_field("progress", "Progress", str(progress["text"])))
    if runtime:
        fields.append(_field("runtime", "Runtime", _duration(runtime)))
    stopped_at = f"stopped at {progress['text']}" if progress else ""
    return {
        "id": play.ref,
        "media_item_id": "",
        "action": "play",
        "action_label": label,
        "badge_class": "badge-action-scanned",
        "detail": "",
        "space_impact": "",
        "created_at": play.created_at,
        "title": title,
        "line_title": title,
        "plex_rating_key": play.title_key,
        "is_security": False,
        "summary": " · ".join(p for p in (summary, stopped_at) if p),
        "actor": play.username,
        "ip": None,
        "fields": fields,
        "media_type": play.media_type,
        "finished": play.finished,
        "started_at": play.started_at,
        "episodes": episodes,
        "episode_range": episode_range or None,
        "progress": progress,
        "runtime_s": runtime,
        "runtime_text": _duration(runtime) if runtime else None,
        "year": play.year,
    }


def shown_name(who: Who) -> str:
    """*who*'s name as a list says it: an address in a former user's stays in the details."""
    return hide_emails(who.name) if who.kind == "former" else who.name


def with_who(item: dict[str, object], who: Who) -> dict[str, object]:
    """*item* with whose it is; a person with no account now is said to have none."""
    fields = item["fields"]
    assert isinstance(fields, list)
    if who.kind == "former":
        fields = [
            f | {"value": f"{f['value']} (no longer has an account)"} if f["key"] == "who" else f
            for f in fields
        ]
    return item | {
        "fields": fields,
        "who": {"value": who.value, "name": shown_name(who), "kind": who.kind},
    }
