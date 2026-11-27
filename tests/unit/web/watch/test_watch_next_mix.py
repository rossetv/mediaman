"""How Watch next mixes its sources: weights, turns, seeds in turn, and a source that fails."""

from __future__ import annotations

import itertools
from collections import Counter

from mediaman.services import watch_catalog as catalog
from mediaman.web.routes.watch.next_mix import Bloom, Mixer, MixState, Seed, Source


def _endless(name: str) -> Seed:
    def read(page: int):
        return [f"{name}-{page}-{i}" for i in range(20)], page + 1

    return Seed({"kind": name}, read)


def _short(name: str, count: int) -> Seed:
    return Seed({"kind": name}, lambda _page: ([f"{name}-{i}" for i in range(count)], None))


def _failing(name: str) -> Seed:
    def read(_page: int):
        raise catalog.TmdbUnavailable(name)

    return Seed({"kind": name}, read)


def _accept(item, reason):
    return {"id": item, "kind": reason["kind"]}


def _mix(sources, state=None, count=24, reads=40):
    mixer = Mixer(sources, state or MixState(), _accept, (catalog.TmdbUnavailable,), reads)
    return mixer.take(count), mixer.state()


_WEIGHTS = {"ai": 3, "watched": 3, "trending": 3, "search": 2, "taste": 2}


def _all_kinds():
    return [Source(kind, w, [_endless(kind)]) for kind, w in _WEIGHTS.items()]


def test_kinds_come_in_proportion_to_their_weights_and_never_twice_running():
    cards, _ = _mix(_all_kinds(), count=26)

    kinds = [c["kind"] for c in cards]
    assert Counter(kinds) == {"ai": 6, "watched": 6, "trending": 6, "search": 4, "taste": 4}
    assert all(a != b for a, b in itertools.pairwise(kinds))


def test_a_heavy_kind_is_still_not_given_twice_running():
    sources = [Source("ai", 5, [_endless("ai")]), Source("taste", 1, [_endless("taste")])]

    cards, _ = _mix(sources, count=6)

    assert [c["kind"] for c in cards] == ["ai", "taste"] * 3


def test_the_kind_last_given_is_not_given_first_on_the_next_page():
    first, state = _mix(_all_kinds(), count=5)
    following, _ = _mix(_all_kinds(), MixState.read(state.as_json(), list(_WEIGHTS)))

    assert following[0]["kind"] != first[-1]["kind"]


def test_the_seeds_of_a_kind_take_turns():
    two = Seed({"kind": "watched", "seed": "b"}, lambda _p: (["b1", "b2"], None))
    one = Seed({"kind": "watched", "seed": "a"}, lambda _p: (["a1", "a2"], None))
    mixer = Mixer(
        [Source("watched", 3, [one, two])],
        MixState(),
        lambda item, reason: {"id": item},
        (catalog.TmdbUnavailable,),
        40,
    )

    assert [c["id"] for c in mixer.take(4)] == ["a1", "b1", "a2", "b2"]


def test_a_page_goes_on_from_where_the_last_one_ended():
    sources = [Source("trending", 1, [_endless("trending")])]
    first, state = _mix(sources, count=25)
    following, _ = _mix(sources, MixState.read(state.as_json(), ["trending"]), count=3)

    assert [c["id"] for c in following] == ["trending-2-5", "trending-2-6", "trending-2-7"]


def test_a_dry_source_passes_its_weight_to_the_others():
    sources = [Source("ai", 3, [_short("ai", 2)]), Source("trending", 3, [_endless("trending")])]

    cards, _ = _mix(sources, count=10)

    assert Counter(c["kind"] for c in cards) == {"ai": 2, "trending": 8}


def test_a_failing_source_is_passed_over_and_kept_where_it_was():
    flaky = {"fail": True}
    calls = []

    def read(page):
        calls.append(page)
        if flaky["fail"]:
            raise catalog.TmdbUnavailable("down")
        return ["w1", "w2"], None

    sources = [
        Source("watched", 3, [Seed({"kind": "watched"}, read)]),
        Source("trending", 3, [_endless("trending")]),
    ]
    cards, state = _mix(sources, count=24)
    flaky["fail"] = False
    later, _ = _mix(sources, MixState.read(state.as_json(), ["watched", "trending"]), count=3)

    assert len(cards) == 24 and {c["kind"] for c in cards} == {"trending"}
    assert [c["id"] for c in later if c["kind"] == "watched"] == ["w1", "w2"]


def test_a_kind_of_no_weight_joins_only_once_the_weighted_ones_run_dry():
    sources = [Source("ai", 3, [_short("ai", 3)]), Source("popular", 0, [_endless("popular")])]

    cards, _ = _mix(sources, count=6)

    assert [c["kind"] for c in cards] == ["ai"] * 3 + ["popular"] * 3


def test_a_page_reads_no_more_pages_than_it_may():
    sources = [Source("trending", 1, [Seed({"kind": "t"}, lambda p: ([], p + 1))])]

    cards, _ = _mix(sources, reads=5)

    assert cards == []


def test_a_bloom_filter_holds_what_it_was_given_and_reads_back_from_its_text():
    bloom = Bloom()
    for n in range(500):
        bloom.add(f"movie:{n}")

    again = Bloom.read(bloom.text())

    assert again is not None and all(f"movie:{n}" in again for n in range(500))
    assert sum(f"tv:{n}" in again for n in range(1000)) < 20


def test_a_full_bloom_filter_starts_afresh_rather_than_take_every_title_as_given():
    bloom = Bloom()
    for n in range(20_000):
        bloom.add(f"movie:{n}")

    assert sum(f"tv:{n}" in bloom for n in range(1000)) < 100


def test_a_state_not_as_written_is_refused():
    kinds = ["ai", "trending"]
    assert MixState.read({"c": {"ai": 1}, "l": "ai", "p": {"ai": [0, [[1, 2]], "Ab"]}}, kinds)
    for bad in (
        {"c": {"nope": 1}, "l": "", "p": {}},
        {"c": {"ai": 10**9}, "l": "", "p": {}},
        {"c": {}, "l": "", "p": {"ai": [0, [[1, 2]] * 50, "Ab" * 50]}},
        {"c": {}, "l": "", "p": {"ai": [0, [["1", 2]], "Ab"]}},
        {"c": {}, "l": "", "p": {"ai": [0, [[1, 2]], "Abc"]}},
        {"c": [], "l": "", "p": {}},
    ):
        assert MixState.read(bad, kinds) is None


def test_a_seed_keeps_its_place_when_a_new_one_comes_before_it():
    def seed(key):
        return Seed({"kind": key}, lambda _p: ([f"{key}{n}" for n in range(3)], None), key=key)

    def take(seeds, state, count):
        mixer = Mixer([Source("watched", 1, seeds)], state, _accept, (), 40)
        return [c["id"] for c in mixer.take(count)], mixer.state()

    first, state = take([seed("a")], MixState(), 3)
    later, _ = take([seed("new"), seed("a")], MixState.read(state.as_json(), ["watched"]), 3)

    assert first == ["a0", "a1", "a2"]
    assert later == ["new0", "new1", "new2"]


def test_a_page_longer_than_a_cursor_may_hold_ends_its_seed_with_a_state_that_reads_back():
    sources = [Source("ai", 1, [Seed({"kind": "ai"}, lambda _p: (list(range(1500)), None))])]

    cards, state = _mix(sources, count=1200)

    assert len(cards) == 1000
    assert MixState.read(state.as_json(), ["ai"]) is not None
