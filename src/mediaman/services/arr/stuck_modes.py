"""Why a Radarr or Sonarr queue record is stuck, read from what the service says of it.

Each record is given one mode, which says how mediaman resolves it: renamed in place where its
files want an extension, imported by hand where the service matched it by id alone, removed with
its release blocklisted and searched for again otherwise. One held by the service's own setup, or
by a hold that passes by itself, or by a download client the service cannot reach, is waited for,
as is one paused, which a person most often paused on purpose.
"""

from __future__ import annotations

from datetime import timedelta

from mediaman.services.downloads.download_format._classify import (
    is_passing_hold,
    not_the_release,
)

# Nothing is wrong, or nothing mediaman may do mends it: it is waited for.
HEALTHY = ""
WAITING = "waiting"
SETUP = "setup"
# Its files want an extension: obfuscated releases often come with none.
NO_FILES = "no_files"
SAMPLE = "sample"
# The service found its show or film through the grab alone, or not at all.
WRONG_MATCH = "wrong_match"
MULTI_SEASON = "multi_season"
FAILED = "failed"
STALLED = "stalled"
PAUSED = "paused"
OTHER = "other"

# Those mended by another copy, or in place first where they can be.
ACTIONABLE = frozenset({NO_FILES, SAMPLE, WRONG_MATCH, MULTI_SEASON, FAILED, STALLED, OTHER})

_HELD_BACK = frozenset({"importpending", "importblocked", "failedpending", "failed"})
_NO_FILES = ("no files found are eligible", "no video files")
_WRONG_MATCH = (
    "matched to series by id",
    "matched to movie by id",
    "matched by id",
    "unknown series",
    "unknown movie",
    "title mismatch",
)

# How long a copy is left stuck before the next one is looked for, by the copies tried before.
BACKOFF = (timedelta(hours=1), timedelta(hours=3), timedelta(hours=12))
DAILY = timedelta(days=1)
# A stalled download may yet go on: it is given longer before it is replaced.
UNFINISHED_FOR = timedelta(hours=6)


def wait_for(tries: int, mode: str = "") -> timedelta:
    """How long a stuck copy is left before it is replaced, after *tries* copies before it."""
    wait = BACKOFF[tries] if 0 <= tries < len(BACKOFF) else DAILY
    return max(wait, UNFINISHED_FOR) if mode == STALLED else wait


def reason_mode(reason: str) -> str:
    """The mode of a record held back for *reason*, as ``attention_reason`` gives it."""
    lower = " ".join(reason.lower().split())
    if is_passing_hold(reason):
        return WAITING
    if "contains all episodes" in lower:
        return MULTI_SEASON
    if not_the_release(reason):
        return SETUP
    if any(words in lower for words in _NO_FILES):
        return NO_FILES
    if lower.strip(" .") == "sample" or "is a sample" in lower:
        return SAMPLE
    if any(words in lower for words in _WRONG_MATCH):
        return WRONG_MATCH
    return OTHER


def classify(status: str, tracked_state: str, tracked_status: str, reason: str) -> str:
    """The mode of a queue record by its status, its trackedDownloadState and
    trackedDownloadStatus, and the *reason* the service gives."""
    lower, tracked = status.lower(), tracked_state.lower()
    warned = tracked_status.lower() in ("warning", "error")
    if lower == "downloadclientunavailable":
        return WAITING
    if warned and tracked in _HELD_BACK:
        mode = reason_mode(reason)
        if tracked in ("failed", "failedpending") and mode in (OTHER, NO_FILES):
            return FAILED
        return mode
    if warned and tracked == "downloading" and "stalled" in reason.lower():
        return STALLED
    if lower == "paused":
        return PAUSED
    return HEALTHY
