"""What the storage panel reads: disks, scheduled states, title refs and unknown sizes.

Admin only, at the API itself and not just behind the viewer guard.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.core.delete_root_state import write_delete_root_state
from mediaman.core.time import now_utc
from mediaman.web.repository.watch_library import pending_deletions, scanned_library_ids
from mediaman.web.routes.dashboard import _data
from mediaman.web.routes.dashboard import router as dashboard_router
from mediaman.web.routes.library_api import router as library_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action, insert_settings

TB = 1 << 40
_NEW_WORDS = ("disks", "unavailable_roots", "scheduled", "items", "no_size")


@pytest.fixture
def app(app_factory, conn):
    return app_factory(dashboard_router, library_router, conn=conn)


@pytest.fixture
def get(app, authed_client, conn):
    client = authed_client(app, conn)

    def _get(path: str):
        resp = client.get(path)
        assert resp.status_code == 200
        return resp.json()

    return _get


@pytest.fixture
def disks(monkeypatch, tmp_path):
    """Make folders whose disk readings the test sets as ``{folder name: (total, used)}``."""
    readings: dict[str, tuple[int, int] | None] = {}

    def fake(path):
        reading = readings.get(Path(path).name)
        if reading is None:
            raise OSError("stale mount")
        total, used = reading
        return SimpleNamespace(total=total, used=used, free=total - used)

    def put(**values):
        for name, value in values.items():
            (tmp_path / name).mkdir(exist_ok=True)
            readings[name] = value

    monkeypatch.setattr(shutil, "disk_usage", fake)
    monkeypatch.setattr(_data, "_disks_cache", {})
    monkeypatch.setattr(_data, "_disk_usage_cache", {})
    monkeypatch.setenv("MEDIAMAN_MEDIA_PATH", str(tmp_path / "no-media"))
    return put


def _libraries(conn, tmp_path, thresholds: dict[str, tuple[str, object]], ticked: list[str]):
    """Set each library's folder and threshold, and the ticked libraries."""
    insert_settings(
        conn,
        plex_libraries=json.dumps(ticked),
        disk_thresholds=json.dumps(
            {
                lib: {"path": str(tmp_path / folder), "threshold": threshold}
                for lib, (folder, threshold) in thresholds.items()
            }
        ),
    )


def _schedule(conn, media_id: str, *, days: float, size=1, delete_status="pending", **item):
    insert_media_item(
        conn, **{"id": media_id, "plex_rating_key": media_id, "file_size_bytes": size, **item}
    )
    return insert_scheduled_action(
        conn,
        media_item_id=media_id,
        execute_at=(now_utc() + timedelta(days=days)).isoformat(),
        token=f"tok-{media_id}",
        delete_status=delete_status,
    )


@pytest.mark.parametrize(
    "path", ["/api/dashboard/stats", "/api/dashboard/scheduled", "/api/library"]
)
class TestAdminsOnly:
    def test_a_viewer_gets_none_of_it(self, app, viewer_client, conn, path):
        resp = viewer_client(app, conn).get(path)

        assert resp.status_code == 403
        assert not any(word in resp.text for word in _NEW_WORDS)

    def test_nobody_signed_in_gets_none_of_it(self, app, path):
        resp = TestClient(app).get(path)

        assert resp.status_code == 401
        assert not any(word in resp.text for word in _NEW_WORDS)


class TestDisks:
    def test_folders_on_one_device_are_one_disk_with_the_lowest_threshold(
        self, get, conn, disks, tmp_path
    ):
        disks(movies=(8 * TB, 7 * TB), tv=(8 * TB, 7 * TB))
        _libraries(conn, tmp_path, {"1": ("movies", 90), "2": ("tv", "80")}, ["1", "2"])

        storage = get("/api/dashboard/stats")["storage"]

        assert storage["readable"] is True
        assert (storage["total_bytes"], storage["used_bytes"], storage["free_bytes"]) == (
            8 * TB,
            7 * TB,
            TB,
        )
        assert storage["disks"] == [
            {
                "path": str(tmp_path / "movies"),
                "readable": True,
                "total_bytes": 8 * TB,
                "used_bytes": 7 * TB,
                "free_bytes": TB,
                "total": "8.0 TB",
                "free": "1.0 TB",
                "used_pct": 87.5,
                "threshold_pct": 80,
                "threshold_shared": False,
                "over_threshold": True,
            }
        ]

    def test_several_devices_are_listed_apart_and_add_up_to_the_totals(
        self, get, conn, disks, tmp_path
    ):
        disks(movies=(4 * TB, 3 * TB), tv=(3 * TB, 2 * TB), anime=(TB, TB // 2))
        _libraries(
            conn,
            tmp_path,
            {"1": ("movies", 75), "2": ("tv", 90), "3": ("anime", 0)},
            ["1", "2", "3"],
        )

        storage = get("/api/dashboard/stats")["storage"]

        assert [
            (Path(d["path"]).name, d["threshold_pct"], d["over_threshold"])
            for d in storage["disks"]
        ] == [("movies", 75, True), ("tv", 90, False), ("anime", None, False)]
        for key in ("total_bytes", "used_bytes", "free_bytes"):
            assert storage[key] == sum(d[key] for d in storage["disks"])

    def test_a_threshold_is_shared_when_every_ticked_library_has_it(
        self, get, conn, disks, tmp_path
    ):
        disks(movies=(8 * TB, TB), tv=(8 * TB, TB))
        _libraries(
            conn, tmp_path, {"1": ("movies", 90), "2": ("tv", 90), "3": ("tv", 50)}, ["1", "2"]
        )

        [disk] = get("/api/dashboard/stats")["storage"]["disks"]

        assert (disk["threshold_pct"], disk["threshold_shared"]) == (90, True)

    @pytest.mark.parametrize(
        ("other", "why"),
        [({"2": ("movies", 0)}, "always scanned"), ({}, "ticked without a folder")],
    )
    def test_a_threshold_is_not_shared_with_a_library_that_is_always_scanned(
        self, get, conn, disks, tmp_path, other, why
    ):
        disks(movies=(8 * TB, TB))
        _libraries(conn, tmp_path, {"1": ("movies", 90), **other}, ["1", "2"])

        [disk] = get("/api/dashboard/stats")["storage"]["disks"]

        assert (disk["threshold_pct"], disk["threshold_shared"]) == (90, False), why

    def test_a_disk_that_cannot_be_read_says_so_and_the_totals_leave_it_out(
        self, get, conn, disks, tmp_path
    ):
        disks(movies=(4 * TB, TB), tv=None)
        _libraries(conn, tmp_path, {"1": ("movies", 0), "2": ("tv", 90)}, ["1", "2"])

        storage = get("/api/dashboard/stats")["storage"]

        assert storage["readable"] is True
        assert storage["total_bytes"] == 4 * TB
        assert [(d["readable"], d["total_bytes"]) for d in storage["disks"]] == [
            (True, 4 * TB),
            (False, 0),
        ]
        assert storage["disks"][1]["over_threshold"] is False

    def test_the_media_folder_stands_in_for_folders_that_are_not_there_but_is_no_reading_of_them(
        self, get, conn, disks, tmp_path
    ):
        disks(**{"no-media": (2 * TB, TB)})
        _libraries(conn, tmp_path, {"1": ("gone", 90)}, ["1"])

        storage = get("/api/dashboard/stats")["storage"]

        assert (storage["readable"], storage["total_bytes"]) == (False, 2 * TB)

    def test_a_folder_that_refuses_to_be_read_does_not_hide_the_other_disks(
        self, get, conn, disks, tmp_path, monkeypatch
    ):
        disks(movies=(4 * TB, TB), tv=(2 * TB, TB))
        _libraries(conn, tmp_path, {"1": ("movies", 0), "2": ("tv", 0)}, ["1", "2"])
        exists = Path.exists

        def refuse(self, **kwargs):
            if self.name == "tv":
                raise PermissionError("no search permission")
            return exists(self, **kwargs)

        monkeypatch.setattr(Path, "exists", refuse)

        storage = get("/api/dashboard/stats")["storage"]

        assert (storage["readable"], storage["total_bytes"]) == (True, 4 * TB)

    def test_unreadable_storage_is_not_an_empty_disk_and_the_next_call_tries_again(
        self, get, conn, disks, tmp_path
    ):
        disks(movies=None)
        _libraries(conn, tmp_path, {"1": ("movies", 90)}, ["1"])
        insert_media_item(conn, media_type="movie", file_size_bytes=5)

        storage = get("/api/dashboard/stats")["storage"]
        assert (storage["readable"], storage["total_bytes"]) == (False, 0)
        assert storage["movies_bytes"] == 5

        disks(movies=(TB, 0))
        assert get("/api/dashboard/stats")["storage"]["readable"] is True

    def test_with_no_folder_set_the_media_folder_is_read_and_no_disk_is_listed(
        self, get, disks, tmp_path, monkeypatch
    ):
        disks(**{"no-media": (2 * TB, TB)})

        body = get("/api/dashboard/stats")

        assert (body["storage"]["readable"], body["storage"]["used_bytes"]) == (True, TB)
        assert body["storage"]["disks"] == []
        assert str(tmp_path) not in json.dumps(body)

    def test_a_missing_media_folder_is_unreadable_and_is_read_again_once_it_is_back(
        self, get, disks
    ):
        assert get("/api/dashboard/stats")["storage"]["readable"] is False

        disks(**{"no-media": (2 * TB, TB)})

        assert get("/api/dashboard/stats")["storage"]["readable"] is True

    def test_a_media_folder_reading_of_no_disk_is_not_kept(self, get, disks):
        disks(**{"no-media": None})
        assert get("/api/dashboard/stats")["storage"]["readable"] is False

        disks(**{"no-media": (2 * TB, TB)})

        assert get("/api/dashboard/stats")["storage"]["readable"] is True

    @pytest.mark.parametrize("value", ["[]", "not json", '{"1": 5, "2": {"path": 7}}'])
    def test_a_malformed_setting_lists_no_disk(self, get, conn, disks, value):
        insert_settings(conn, plex_libraries='["1"]', disk_thresholds=value)

        assert get("/api/dashboard/stats")["storage"]["disks"] == []

    @pytest.mark.parametrize("value", ["ninety", None, "", -5, [90]])
    def test_a_threshold_the_scanner_would_not_read_is_none(
        self, get, conn, disks, tmp_path, value
    ):
        disks(movies=(TB, TB))
        _libraries(conn, tmp_path, {"1": ("movies", value)}, ["1"])

        [disk] = get("/api/dashboard/stats")["storage"]["disks"]

        assert (disk["threshold_pct"], disk["over_threshold"]) == (None, False)


class TestNotices:
    def test_missing_folders_are_listed_with_what_waits(self, get, conn, disks):
        ids = [_schedule(conn, f"{n}", days=-1) for n in range(2)]
        write_delete_root_state(conn, {"/media/tv": 2}, {"/media/tv": ids})
        conn.commit()

        assert get("/api/dashboard/stats")["unavailable_roots"] == [
            {"path": "/media/tv", "waiting": 2}
        ]

    @pytest.mark.parametrize("roots", ["", "/media"])
    def test_deletions_off_without_a_folder_to_delete_from(self, get, disks, monkeypatch, roots):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", roots)

        assert get("/api/dashboard/stats")["deletions_off"] is (not roots)

    def test_plex_not_set_up_hides_an_empty_bar_only(self, get, conn, disks):
        body = get("/api/dashboard/stats")
        assert (body["plex_configured"], body["show_storage"]) == (False, False)

        insert_media_item(conn, file_size_bytes=5)
        assert get("/api/dashboard/stats")["show_storage"] is True

        insert_settings(conn, plex_url="http://plex:32400", plex_token="tok")
        assert get("/api/dashboard/stats")["plex_configured"] is True

    def test_titles_are_counted_by_type_with_a_show_once(self, get, conn, disks):
        insert_media_item(conn, id="1")
        for n, (kind, show) in enumerate(
            [("tv_season", "50"), ("tv_season", "50"), ("anime_season", "60"), ("tv_season", "70")]
        ):
            insert_media_item(
                conn, id=f"s{n}", media_type=kind, show_rating_key=show, show_title=show
            )

        assert get("/api/dashboard/stats")["storage"]["counts"] == {
            "movies": 1,
            "tv": 2,
            "anime": 1,
        }


class TestScheduled:
    def test_the_leaving_count_is_the_leaving_soon_row_and_the_rest_say_why(self, get, conn, disks):
        insert_settings(conn, plex_libraries='["1"]')
        _schedule(conn, "1", days=5, size=100)
        _schedule(conn, "2", days=0.001, size=10)
        _schedule(conn, "3", days=-1, size=1000)
        _schedule(conn, "4", days=-1, delete_status="deleting", size=2000)
        _schedule(conn, "5", days=5, plex_library_id=9)

        items = get("/api/dashboard/scheduled")["items"]
        summary = get("/api/dashboard/stats")["scheduled"]

        assert {i["media_item_id"]: i["state"] for i in items} == {
            "1": "leaving",
            "2": "leaving",
            "3": "overdue",
            "4": "deleting",
        }
        row = pending_deletions(conn, scanned_library_ids(conn), now_utc())
        assert summary == {
            "total": 4,
            "total_bytes": 3110,
            "total_size": "3.0 KB",
            "leaving": len(row),
            "leaving_bytes": 110,
            "leaving_size": "110 B",
            "overdue": 1,
            "deleting": 1,
        }

    def test_a_row_says_its_days_left_and_the_title_to_open(self, get, conn):
        insert_settings(conn, plex_libraries='["1"]')
        season = {"media_type": "tv_season", "show_title": "Show", "season_number": 3}
        _schedule(conn, "10", days=-1)
        _schedule(conn, "11", days=5, **season, show_rating_key="900")
        _schedule(conn, "12", days=6, **season)
        _schedule(conn, "not-a-key", days=7)

        items = get("/api/dashboard/scheduled")["items"]

        assert [(i["ref"], i["season_number"], i["days_left"]) for i in items] == [
            ("movie-10", None, 0),
            ("show-900", 3, 5),
            (None, 3, 6),
            (None, None, 7),
        ]
