"""Activity's history: a point a step, averaged into coarser steps, pruned, and read by period."""

from __future__ import annotations

from mediaman.services.activity import history

# Noon on a set day, on a whole hour.
NOON = 1_795_867_200


def point(down: float, cpu: float | None = 50.0, sonarr: float | None = 1.0) -> dict[str, object]:
    return {"down": down, "up": 1.0, "cpu": cpu, "mem": 8.0, "apps": {"sonarr": [sonarr, 100.0]}}


def count(conn, tier: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM activity_samples WHERE tier=?", (tier,)).fetchone()[0]


def test_no_more_than_one_point_is_kept_a_step(conn):
    assert history.store(conn, point(1), NOON)
    assert not history.store(conn, point(2), NOON + 5)
    assert history.store(conn, point(3), NOON + 15)
    assert count(conn, 0) == 2


def test_a_minute_that_ended_is_kept_as_the_mean_of_its_points(conn):
    for i, down in enumerate((10, 20, 30, 40)):
        history.store(conn, point(down, cpu=None if i == 0 else 60.0, sonarr=i), NOON + i * 15)
    history.store(conn, point(99), NOON + 60)
    (minute,) = history.rows(conn, 1, NOON)
    assert minute["at"] == NOON
    assert minute["down"] == 25
    assert minute["cpu"] == 60  # a figure not known is left out of the mean, not counted as 0
    assert minute["apps"] == {"sonarr": [1.5, 100.0]}


def test_each_step_keeps_only_its_own_span_so_the_table_stays_bounded(conn):
    for i in range(int(2.5 * 3600 / 15)):
        history.store(conn, point(i), NOON + i * 15)
    assert count(conn, 0) <= 3600 // 15 + 1
    assert count(conn, 1) == 2.5 * 60 - 1
    assert count(conn, 2) == 2.5 * 6 - 1
    assert count(conn, 3) == 2  # two whole hours have ended
    oldest = conn.execute("SELECT MIN(at) FROM activity_samples WHERE tier=0").fetchone()[0]
    assert oldest >= NOON + 2.5 * 3600 - 15 - 3600


def test_a_day_reads_its_minutes_and_the_points_after_the_last_whole_minute(conn):
    for i in range(10):
        history.store(conn, point(i), NOON + i * 15)
    now = NOON + 9 * 15
    got = history.period(conn, "24h", now)
    ats = [p["at"] for p in got]
    # Two whole minutes, then the two points since, none of them twice.
    assert ats == [NOON, NOON + 60, NOON + 120, NOON + 135]


def test_a_period_is_drawn_with_at_most_its_number_of_points(conn):
    points = [{"at": NOON + i, "down": float(i)} for i in range(1000)]
    got = history.downsample(points, NOON, NOON + 1000, most=100)
    assert len(got) == 100
    assert got[0]["down"] == 4.5  # the mean of the first ten


def test_since_is_the_oldest_point_kept(conn):
    assert history.since(conn) is None
    history.store(conn, point(1), NOON)
    history.store(conn, point(1), NOON + 30)
    assert history.since(conn) == NOON


def test_since_is_when_the_first_reading_was_taken_after_its_hour_is_rolled_up(conn):
    first = NOON + 37 * 60
    for i in range(int(2 * 3600 / 15)):
        history.store(conn, point(i), first + i * 15)
    history.store(conn, point(1), first + 29 * 24 * 3600)  # the finer steps are gone
    assert history.since(conn) == first
