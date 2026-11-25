"""People's avatars: choosing, uploading, removing and seeing them."""

from __future__ import annotations

import asyncio
import os

import pytest
from starlette.requests import Request

from mediaman.services.avatars import drawn, files, photo
from mediaman.web.auth.password_hash import delete_user
from mediaman.web.middleware.body_size import BodySizeLimitMiddleware
from mediaman.web.repository import avatars
from mediaman.web.routes.avatars import api_upload_avatar, router
from tests.helpers.factories import insert_admin_user
from tests.unit.services.avatars import photos

_MINE = "/api/account/avatar"
_PHOTO = "/api/account/avatar/photo"


@pytest.fixture
def app(app_factory, conn):
    return app_factory(router, conn=conn)


@pytest.fixture
def viewer(app, viewer_client, conn):
    return viewer_client(app, conn)


@pytest.fixture
def stored(conn):
    """The names of the files kept."""

    def names() -> set[str]:
        where = files.folder(conn)
        return {path.name for path in where.iterdir()} if where.exists() else set()

    return names


def _id(conn, username="viewer"):
    return avatars.find_user_id(conn, username)


def test_a_person_who_set_nothing_has_their_default(viewer, conn):
    body = viewer.get(_MINE).json()
    default = drawn.default_choice(_id(conn))

    assert (body["kind"], body["choice"], body["default"]) == ("default", None, default)
    assert body["url"] == drawn.url(default) == avatars.avatar_url(conn, "viewer")
    assert len(body["drawn"]) == 10


def test_a_chosen_drawing_is_shown_until_it_is_removed(viewer, conn):
    chosen = drawn.default_choice(_id(conn)) % 10 + 1

    body = viewer.put(_MINE, json={"choice": chosen}).json()
    assert (body["kind"], body["choice"], body["url"]) == ("drawn", chosen, drawn.url(chosen))
    assert avatars.avatar_url(conn, "viewer") == drawn.url(chosen)

    assert viewer.delete(_MINE).json()["kind"] == "default"
    assert avatars.avatar_url(conn, "viewer") == drawn.url(drawn.default_choice(_id(conn)))


@pytest.mark.parametrize("body", [{"choice": 0}, {"choice": 11}, {"choice": True}, {}])
def test_only_one_of_the_ten_can_be_chosen(viewer, body):
    assert viewer.put(_MINE, json=body).status_code == 422
    assert viewer.get(_MINE).json()["kind"] == "default"


def test_a_choice_this_version_has_no_drawing_for_shows_the_default(viewer, conn):
    avatars.set_drawn(conn, _id(conn), 11)

    assert viewer.get(_MINE).json()["url"] == drawn.url(drawn.default_choice(_id(conn)))


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (photos.jpeg(), "image/jpeg"),
        (photos.png(), "image/png"),
        (photos.webp(), "image/webp"),
    ],
)
def test_a_photo_is_served_as_the_type_its_bytes_are(viewer, data, kind):
    sent = viewer.put(_PHOTO, content=data, headers={"Content-Type": "text/html"})
    assert sent.json()["kind"] == "photo"
    got = viewer.get(sent.json()["url"])

    assert got.content == data
    assert got.headers["content-type"] == kind
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_a_photo_is_kept_for_good_only_at_its_own_version(viewer):
    old = viewer.put(_PHOTO, content=photos.png()).json()["url"]
    new = viewer.put(_PHOTO, content=photos.jpeg()).json()["url"]

    assert new != old
    assert viewer.get(old).headers["cache-control"] == "private, no-cache"


def test_the_metadata_of_a_photo_never_reaches_the_disk(viewer, conn, stored):
    data = photos.jpeg(200, 200, photos.segment(0xE1, b"Exif\0\0somewhere"))
    viewer.put(_PHOTO, content=data)

    (name,) = stored()
    assert (files.folder(conn) / name).read_bytes() == photos.jpeg()


def test_each_change_removes_the_photo_it_replaces(viewer, stored):
    viewer.put(_PHOTO, content=photos.png())
    (first,) = stored()
    viewer.put(_PHOTO, content=photos.png())
    (second,) = stored()
    assert first != second

    viewer.put(_MINE, json={"choice": 1})
    assert stored() == set()

    viewer.put(_PHOTO, content=photos.png())
    viewer.delete(_MINE)
    assert stored() == set()


@pytest.mark.parametrize(
    ("data", "status", "error"),
    [
        (b"<svg/>", 400, "not_a_photo"),
        (photos.png(100, 100), 400, "too_small"),
        (photos.png(5000, 5000), 400, "too_large"),
    ],
)
def test_a_refused_photo_changes_nothing(viewer, stored, data, status, error):
    viewer.put(_MINE, json={"choice": 3})

    refused = viewer.put(_PHOTO, content=data)

    assert (refused.status_code, refused.json()["error"]) == (status, error)
    assert viewer.get(_MINE).json()["choice"] == 3
    assert stored() == set()


def test_a_photo_over_the_limit_is_refused_whatever_it_holds(viewer):
    too_much = photos.png() + b"\0" * photo.MAX_BYTES

    refused = viewer.put(_PHOTO, content=too_much)

    assert (refused.status_code, refused.json()["error"]) == (413, "too_big")


def test_reading_a_photo_stops_once_it_is_over_the_limit():
    read = []

    async def receive():
        read.append(1)
        more = len(read) < 100
        return {"type": "http.request", "body": b"\xff" * 1024 * 1024, "more_body": more}

    answer = asyncio.run(api_upload_avatar(Request({"type": "http"}, receive), "viewer"))

    assert answer.status_code == 413
    assert len(read) == 11


@pytest.fixture
def capped(app, viewer, monkeypatch):
    """The viewer's client, behind the cap on every request's body."""
    monkeypatch.delenv("MEDIAMAN_MAX_REQUEST_BYTES", raising=False)
    app.add_middleware(BodySizeLimitMiddleware)
    return viewer


def test_a_photo_over_the_usual_body_cap_is_taken_up_to_its_own(capped):
    # Metadata, so that the photo is big without being a big picture.
    padding = photos.png_chunk(b"tEXt", b"x" * (photo.MAX_BYTES - len(photos.png()) - 12))
    data = photos.png(200, 200, padding)
    assert len(data) == photo.MAX_BYTES

    assert capped.put(_PHOTO, content=data).json()["kind"] == "photo"


@pytest.mark.parametrize("chunked", [False, True])
def test_a_photo_too_big_is_refused_in_the_same_words_behind_the_body_cap(capped, chunked):
    data = b"\xff" * (photo.MAX_BYTES + 1)

    refused = capped.put(_PHOTO, content=iter([data[:-1], data[-1:]]) if chunked else data)

    assert (refused.status_code, refused.json()["error"]) == (413, "too_big")
    assert refused.json()["message"] == photo.too_big_message()


@pytest.mark.parametrize(
    ("limit", "named"),
    [(3 * 1024 * 1024, "3 MB"), (1572864, "1.5 MB"), (512 * 1024, "512 KB")],
)
def test_a_lower_cap_the_operator_set_holds_for_photos_and_is_named(
    capped, monkeypatch, limit, named
):
    monkeypatch.setenv("MEDIAMAN_MAX_REQUEST_BYTES", str(limit))

    assert capped.put(_PHOTO, content=b"x" * limit).json()["error"] == "not_a_photo"
    refused = capped.put(_PHOTO, content=b"x" * (limit + 1))

    assert (refused.status_code, refused.json()["error"]) == (413, "too_big")
    assert named in refused.json()["message"]


def test_a_higher_cap_the_operator_set_does_not_raise_the_photo_limit(capped, monkeypatch):
    monkeypatch.setenv("MEDIAMAN_MAX_REQUEST_BYTES", str(20 * 1024 * 1024))

    refused = capped.put(_PHOTO, content=b"x" * (photo.MAX_BYTES + 1))

    assert (refused.status_code, refused.json()["error"]) == (413, "too_big")
    assert refused.json()["message"] == photo.too_big_message()


@pytest.mark.parametrize(
    ("method", "path"), [("POST", _PHOTO), ("PUT", _MINE), ("PUT", _PHOTO + "/")]
)
def test_the_usual_body_cap_holds_for_every_other_request(capped, method, path):
    refused = capped.request(method, path, content=b"x" * (9 * 1024 * 1024))

    assert (refused.status_code, refused.text) == (413, "Payload too large")


def test_a_photo_whose_row_cannot_be_written_leaves_no_file(viewer, conn, stored):
    conn.execute(
        "CREATE TRIGGER full BEFORE INSERT ON user_avatars BEGIN SELECT RAISE(ABORT, 'full'); END"
    )

    assert viewer.put(_PHOTO, content=photos.png()).status_code == 500
    assert stored() == set()


def test_a_photo_that_cannot_be_kept_leaves_the_avatar_as_it_was(viewer, stored, monkeypatch):
    def full(*_args):
        raise OSError(28, "No space left on device")

    viewer.put(_PHOTO, content=photos.png())
    before = stored(), viewer.get(_MINE).json()
    monkeypatch.setattr(files.os, "replace", full)

    assert viewer.put(_PHOTO, content=photos.png()).status_code == 500
    assert (stored(), viewer.get(_MINE).json()) == before


def test_a_photo_gone_from_the_disk_falls_back_to_the_default(viewer, conn, stored):
    url = viewer.put(_PHOTO, content=photos.png()).json()["url"]
    (name,) = stored()
    (files.folder(conn) / name).unlink()

    got = viewer.get(url, follow_redirects=False)

    assert got.status_code == 302
    assert got.headers["location"] == drawn.url(drawn.default_choice(_id(conn)))
    assert "no-store" in got.headers["cache-control"]


def test_a_name_that_leaves_the_folder_is_never_read(conn, viewer):
    conn.execute(
        "INSERT INTO user_avatars VALUES (?, 'upload', NULL, '../photo.png', '2026-01-01')",
        (_id(conn),),
    )
    conn.commit()
    (files.folder(conn).parent / "photo.png").write_bytes(photos.png())
    files.folder(conn).mkdir()

    assert viewer.get(avatars.avatar_url(conn, "viewer"), follow_redirects=False).status_code == 302


def test_another_persons_photo_is_seen_by_admins_only(app, authed_client, viewer, conn):
    admin = authed_client(app, conn)
    theirs = viewer.put(_PHOTO, content=photos.png()).json()["url"]
    admins = admin.put(_PHOTO, content=photos.png()).json()["url"]

    assert admin.get(theirs).content == photos.png()
    assert viewer.get(admins).status_code == 404
    assert viewer.get("/api/avatars/999").status_code == 404


def test_nothing_is_answered_without_a_session(viewer):
    url = viewer.put(_PHOTO, content=photos.png()).json()["url"]
    viewer.cookies.clear()

    answers = [
        viewer.get(url),
        viewer.get(_MINE),
        viewer.put(_MINE, json={"choice": 1}),
        viewer.put(_PHOTO, content=photos.png()),
        viewer.delete(_MINE),
    ]

    assert {answer.status_code for answer in answers} == {401}


def test_uploads_and_changes_are_limited(viewer):
    uploads = [viewer.put(_PHOTO, content=b"").status_code for _ in range(21)]
    choices = [viewer.put(_MINE, json={"choice": 1}).status_code for _ in range(21)]

    assert uploads == [400] * 20 + [429]
    assert choices == [200] * 20 + [429]
    assert viewer.delete(_MINE).status_code == 429


def test_a_change_sweeps_the_files_a_crash_left_and_nothing_else(
    app, authed_client, viewer, conn, stored
):
    authed_client(app, conn).put(_PHOTO, content=photos.png())
    (kept,) = stored()
    where = files.folder(conn)
    for name in ("a" * 32, "b" * 32 + ".part", "note.txt", kept):
        (where / name).touch()
        os.utime(where / name, (1_700_000_000, 1_700_000_000))
    (where / ("c" * 32)).touch()

    viewer.put(_MINE, json={"choice": 1})

    assert stored() == {kept, "note.txt", "c" * 32}


def test_deleting_an_account_removes_its_photo(viewer, conn, stored):
    viewer.put(_PHOTO, content=photos.png())
    insert_admin_user(conn, username="boss")

    assert delete_user(conn, _id(conn), "boss")
    assert stored() == set()


def test_a_deletion_that_is_refused_keeps_the_photo(app, authed_client, conn, stored):
    admin = authed_client(app, conn)
    admin.put(_PHOTO, content=photos.png())

    assert delete_user(conn, _id(conn, "admin"), "someone") is False
    assert len(stored()) == 1


def test_a_list_of_people_gets_each_ones_avatar(viewer, conn):
    mine = viewer.put(_PHOTO, content=photos.png()).json()["url"]
    chooser = insert_admin_user(conn, username="chooser")
    avatars.set_drawn(conn, chooser, 7)

    assert avatars.avatar_urls(conn, [_id(conn), chooser, 999]) == {
        _id(conn): mine,
        chooser: drawn.url(7),
        999: drawn.url(drawn.default_choice(999)),
    }
    assert avatars.avatar_url(conn, "nobody") is None
