"""The poster look-ups of the storage page against a TMDB that does not answer."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from mediaman.web.routes.dashboard import _poster_fanout


def _run(search, titles, budget):
    items: list[dict[str, object]] = [{} for _ in titles]
    client = MagicMock()
    client.search_multi.side_effect = search
    with (
        patch(
            "mediaman.services.media_meta.tmdb.TmdbClient.from_db", return_value=client
        ) as from_db,
        patch.object(_poster_fanout, "_POSTER_FANOUT_BUDGET_SECONDS", budget),
    ):
        _poster_fanout._get_poster_executor.cache_clear()
        _poster_fanout._fill_tmdb_posters(None, items, list(enumerate(titles)), "k")  # type: ignore[arg-type]
    return items, from_db


def test_a_lookup_is_short_and_not_retried():
    _, from_db = _run(lambda _t: None, ["a"], 1.0)

    assert from_db.call_args.kwargs["retry"] is False
    assert from_db.call_args.kwargs["timeout"] <= 3.0


def test_titles_not_started_when_the_budget_ends_are_never_asked():
    release = threading.Event()
    asked: list[str] = []

    def hang(title):
        asked.append(title)
        release.wait(5)

    titles = [f"t{i}" for i in range(12)]
    try:
        _run(hang, titles, 0.2)
        # Four workers are busy; what waits behind them must not start once they are free.
        release.set()
        _poster_fanout._get_poster_executor().submit(lambda: None).result(5)
    finally:
        release.set()
        _poster_fanout._get_poster_executor.cache_clear()

    assert len(asked) == _poster_fanout._POSTER_FANOUT_WORKERS
