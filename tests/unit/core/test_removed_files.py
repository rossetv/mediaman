"""The SQL condition agrees with the deletion pass's own rule on what a record says."""

from __future__ import annotations

import json

import pytest

from mediaman.core.removed_files import FILES_GONE
from mediaman.scanner.repository.scheduled_actions import fetch_deletion, is_fully_removed
from tests.helpers.factories import insert_media_item, insert_scheduled_action

A, B = "/m/a.mkv", "/m/b.mkv"


def _record(paths, left=None, size=1):
    return json.dumps({"paths": paths, "bytes": size, "sizes": {}, "left": left})


def _files(*paths):
    return json.dumps([{"path": p, "size": 1} for p in paths])


@pytest.mark.parametrize(
    ("tracked", "record", "gone"),
    [
        (_files(A), None, False),
        (_files(A), _record([A]), True),
        (_files(A, B), _record([A], [B]), False),
        (_files(A, B), _record([A, B]), True),
        (_files("/m//a.mkv", "/m/./b.mkv"), _record([A, B]), True),
        (_files(A), _record([]), False),
        (_files(A), _record([1]), False),
        (None, _record([A], [B]), False),
        (None, _record([A], []), True),
        (None, _record([A]), True),
        (None, _record([A], B), True),
        ("[]", _record([A], [B]), False),
        ("not json", _record([A], []), True),
        ('{"path": "/m/b.mkv"}', _record([A], []), True),
        ('["/m/b.mkv"]', _record([A], []), True),
        ('[{"path": ""}]', _record([A], [B]), False),
        ('[{"path": "/m/a.mkv"}, {"size": 1}]', _record([A], [B]), False),
        (_files(A), "not json", False),
        (_files(A), '["/m/a.mkv"]', False),
        (_files(A), '{"paths": ["/m/a.mkv"]}', False),
        (_files(A), '{"paths": "/m/a.mkv", "bytes": 1}', False),
        (_files(A), _record([A], size="1"), False),
    ],
)
def test_a_deletion_has_no_files_left_as_the_pass_reads_its_record(conn, tracked, record, gone):
    insert_media_item(conn, id="1")
    action_id = insert_scheduled_action(conn, media_item_id="1", token="t")
    conn.execute("UPDATE media_items SET tracked_files = ?", (tracked,))
    conn.execute("UPDATE scheduled_actions SET removed_files = ?", (record,))

    found = conn.execute(
        f"SELECT {FILES_GONE} FROM scheduled_actions sa"
        " JOIN media_items mi ON mi.id = sa.media_item_id"
    ).fetchone()[0]

    row = fetch_deletion(conn, action_id)
    assert (bool(found), row is not None and is_fully_removed(row)) == (gone, gone)
