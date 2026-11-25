"""The audio and subtitle tracks of the version of an item that Watch plays."""

from __future__ import annotations

import re
from typing import TypedDict

from mediaman.services.media_meta.plex_watch import PlexWatch

_ID_RE = re.compile(r"[0-9]{1,18}")
_LABEL_MAX = 120


class Track(TypedDict):
    id: str
    label: str
    selected: bool


def read_tracks(plex: PlexWatch, rating_key: str) -> tuple[list[Track], list[Track]]:
    """Return ``(audio, subtitles)``."""
    _, node = plex._metadata(rating_key)
    audio: list[Track] = []
    subtitles: list[Track] = []
    for stream in node.findall("Media[1]/Part[1]/Stream"):
        tracks = {"2": audio, "3": subtitles}.get(stream.get("streamType", ""))
        track_id = stream.get("id", "")
        if tracks is None or not _ID_RE.fullmatch(track_id):
            continue
        name = stream.get("extendedDisplayTitle") or stream.get("displayTitle") or ""
        label = " ".join(name.split())[:_LABEL_MAX] or f"Track {len(tracks) + 1}"
        tracks.append({"id": track_id, "label": label, "selected": stream.get("selected") == "1"})
    return audio, subtitles
