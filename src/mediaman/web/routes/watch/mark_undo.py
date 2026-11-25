"""What each person's last marks changed, held in memory for the time they can be undone.

After a restart a change simply stays.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from mediaman.web.repository.seen_titles import SeenChange, TitleKey
from mediaman.web.repository.watch_progress import MarkChange

#: How long a mark or unmark can be undone.
UNDO_WINDOW = timedelta(minutes=5)

# An account and what it marked: by account, as a name may be another's later.
Slot = tuple[int, str, str]


@dataclass(frozen=True, slots=True)
class Undo:
    watched: bool  # what the change it takes back did
    changes: Sequence[MarkChange]
    title: SeenChange | None  # of the title's own mark
    of: TitleKey | None
    expires: datetime


_held: dict[Slot, Undo] = {}
_lock = threading.Lock()


def hold(
    slot: Slot,
    of: TitleKey | None,
    *,
    watched: bool,
    changes: Sequence[MarkChange],
    title: SeenChange | None,
    now: datetime,
) -> None:
    """Hold a change of *slot*, a title *of* that key or a part of it, if it changed anything.
    Where it or an earlier one changed the title's own mark, the earlier can be undone no more:
    that would take this one back too."""
    if title is not None and title.before == title.after:
        title = None
    if not changes and title is None:
        return
    with _lock:
        for old, undo in list(_held.items()):
            same = of is not None and (old[0], undo.of) == (slot[0], of)
            outdated = same and (undo.title or title) is not None
            if undo.expires <= now or outdated:
                del _held[old]
        _held[slot] = Undo(watched, changes, title, of, now + UNDO_WINDOW)


def take(slot: Slot, *, watched: bool, now: datetime) -> Undo | None:
    """The last change of *slot*, held no more, when it set the opposite of *watched*."""
    with _lock:
        undo = _held.get(slot)
        if undo is None or undo.watched is watched or undo.expires <= now:
            return None
        return _held.pop(slot)


__all__ = ["UNDO_WINDOW", "Slot", "hold", "take"]
