"""How Watch next mixes its sources into pages: a smooth weighted round robin over the kinds
of source, never one kind twice running, and within a kind its seeds (the titles watched, the
searches made) in turn. Where each one is, and what was given, travels in the cursor; nothing
is kept on the server."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# The Bloom filter of what a cursor has given: 12288 bits, about 2 KB once written out.
BLOOM_BYTES = 1536
_BLOOM_BITS = BLOOM_BYTES * 8
_BLOOM_HASHES = 4
# Past half its bits set it says "given" of one title in sixteen it never saw: it starts
# afresh, and the page's own check of what it shows catches the few that come again.
_BLOOM_FULL = _BLOOM_BITS // 2
# Seeds of one kind a cursor keeps a place for.
MAX_SEEDS = 8
_MAX_PAGE = 500
_MAX_INDEX = 1000
_MAX_CREDIT = 100
# Pages one seed may read for a page, so that a seed of little to show (all of it given, or
# not of the kind asked for) leaves the rest of the reads to the others.
_SEED_READS = 4
# A seed's id in a cursor: short, as the cursor has little room; two seeds of a kind that share
# one (one in 4096) share a place, which costs no more than a title passed over.
_ID_CHARS = 2
_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class Bloom:
    """A set of keys that may say a key is in it when it is not, never the other way."""

    def __init__(self, bits: bytes | None = None) -> None:
        self._bits = bytearray(bits if bits is not None else bytes(BLOOM_BYTES))
        self._set = sum(bin(b).count("1") for b in self._bits)

    def _places(self, key: str) -> list[int]:
        digest = hashlib.sha256(key.encode()).digest()
        return [
            int.from_bytes(digest[i * 4 : i * 4 + 4], "big") % _BLOOM_BITS
            for i in range(_BLOOM_HASHES)
        ]

    def __contains__(self, key: str) -> bool:
        return all(self._bits[p >> 3] & (1 << (p & 7)) for p in self._places(key))

    def add(self, key: str) -> None:
        if self._set >= _BLOOM_FULL:
            self._bits = bytearray(BLOOM_BYTES)
            self._set = 0
        for p in self._places(key):
            if not self._bits[p >> 3] & (1 << (p & 7)):
                self._bits[p >> 3] |= 1 << (p & 7)
                self._set += 1

    def text(self) -> str:
        return base64.urlsafe_b64encode(bytes(self._bits)).decode()

    @classmethod
    def read(cls, text: object) -> Bloom | None:
        if not isinstance(text, str) or len(text) != (BLOOM_BYTES + 2) // 3 * 4:
            return None
        try:
            bits = base64.urlsafe_b64decode(text)
        except (binascii.Error, ValueError):
            return None
        return cls(bits) if len(bits) == BLOOM_BYTES else None


@dataclass(frozen=True, slots=True)
class Seed:
    """One list a kind of source draws from. *read* answers a page of it, and the page after
    it or None at its end; *reason* says why a title of it is shown. *key* names it among its
    kind's seeds (the title played, the words searched), so that its place follows it when
    the seeds before it change."""

    reason: Mapping[str, str]
    read: Callable[[int], tuple[Sequence[Any], int | None]]
    first_page: int = 1
    key: str = ""


@dataclass(frozen=True, slots=True)
class Source:
    kind: str
    weight: int
    seeds: Sequence[Seed]


@dataclass(slots=True)
class MixState:
    """Each kind's round-robin credit, the kind last given, and each kind's turn among its
    seeds, place in each (page, index) and the seeds' ids, two characters each."""

    credit: dict[str, int] = field(default_factory=dict)
    last: str = ""
    places: dict[str, tuple[int, list[list[int]], str]] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "c": self.credit,
            "l": self.last,
            "p": {k: [turn, seeds, ids] for k, (turn, seeds, ids) in self.places.items()},
        }

    @classmethod
    def read(cls, raw: Mapping[str, object], kinds: Sequence[str]) -> MixState | None:
        """The state of a cursor's *raw* JSON; None where anything in it is not as written."""
        credit, last, places = raw.get("c"), raw.get("l"), raw.get("p")
        if (
            not isinstance(credit, dict)
            or not isinstance(places, dict)
            or not isinstance(last, str)
        ):
            return None
        if any(
            k not in kinds or not _small_int(v, -_MAX_CREDIT, _MAX_CREDIT)
            for k, v in credit.items()
        ):
            return None
        state = cls(credit=dict(credit), last=last if last in kinds else "")
        for kind, place in places.items():
            if kind not in kinds or not isinstance(place, list) or len(place) != 3:
                return None
            turn, seeds, ids = place
            if not _small_int(turn, 0, MAX_SEEDS) or not isinstance(seeds, list):
                return None
            if len(seeds) > MAX_SEEDS or not all(_page_place(s) for s in seeds):
                return None
            if not isinstance(ids, str) or len(ids) != _ID_CHARS * len(seeds):
                return None
            state.places[kind] = (turn, [list(s) for s in seeds], ids)
        return state


def _seed_id(seed: Seed) -> str:
    digest = hashlib.sha256(seed.key.encode()).digest()
    return "".join(_ID_ALPHABET[b & 63] for b in digest[:_ID_CHARS])


def _small_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _page_place(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and _small_int(value[0], 0, _MAX_PAGE + 1)
        and _small_int(value[1], 0, _MAX_INDEX)
    )


# A raw item of a seed and the seed's reason -> its card, or None where it is not shown.
Accept = Callable[[Any, Mapping[str, str]], dict[str, Any] | None]


@dataclass(slots=True)
class _Kind:
    source: Source
    turn: int
    places: list[list[int]]
    ids: str


class Mixer:
    """Takes cards from *sources* in turn, from where *state* left them. A seed whose read
    raises one of *failures* is passed over for this page and kept where it was; reads past
    *reads* (pages of any seed, cached or not) are not made this page."""

    def __init__(
        self,
        sources: Sequence[Source],
        state: MixState,
        accept: Accept,
        failures: tuple[type[Exception], ...],
        reads: int,
    ) -> None:
        self._state = state
        self._accept = accept
        self._failures = failures
        self._reads = reads
        self._pages: dict[tuple[str, int, int], tuple[Sequence[Any], int | None]] = {}
        self._kinds: dict[str, _Kind] = {}
        for source in sources:
            if not source.seeds:
                continue
            seeds = source.seeds[:MAX_SEEDS]
            turn, held, held_ids = state.places.get(source.kind, (0, [], ""))
            by_id = {
                held_ids[i * _ID_CHARS : (i + 1) * _ID_CHARS]: place for i, place in enumerate(held)
            }
            ids = [_seed_id(s) for s in seeds]
            places = [
                list(by_id.get(i, [s.first_page, 0])) for i, s in zip(ids, seeds, strict=True)
            ]
            self._kinds[source.kind] = _Kind(source, turn % len(seeds), places, "".join(ids))
        self._out: set[tuple[str, int]] = set()
        self._seed_reads: dict[tuple[str, int], int] = {}

    def take(self, count: int) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        live = list(self._kinds)
        while len(cards) < count and live:
            kind = self._choose(live)
            card = self._from_kind(self._kinds[kind])
            if card is None:
                live.remove(kind)
                continue
            self._commit(kind, live)
            cards.append(card)
        return cards

    def state(self) -> MixState:
        for kind, held in self._kinds.items():
            self._state.places[kind] = (held.turn, held.places, held.ids)
        return self._state

    def _weights(self, live: Sequence[str]) -> dict[str, int]:
        weighted = {k: self._kinds[k].source.weight for k in live if self._kinds[k].source.weight}
        # A kind of no weight joins only once every weighted one has run dry.
        return weighted or dict.fromkeys(live, 1)

    def _choose(self, live: Sequence[str]) -> str:
        weights = self._weights(live)
        credit = self._state.credit
        others = [k for k in weights if k != self._state.last] or list(weights)
        return max(others, key=lambda k: credit.get(k, 0) + weights[k])

    def _commit(self, kind: str, live: Sequence[str]) -> None:
        weights = self._weights(live)
        credit = self._state.credit
        for k, w in weights.items():
            credit[k] = credit.get(k, 0) + w
        credit[kind] -= sum(weights.values())
        # Kinds that ran dry keep what they held; the rest stay within reach of one another.
        for k in credit:
            credit[k] = max(-_MAX_CREDIT, min(_MAX_CREDIT, credit[k]))
        self._state.last = kind

    def _from_kind(self, held: _Kind) -> dict[str, Any] | None:
        seeds = held.source.seeds[:MAX_SEEDS]
        for step in range(len(seeds)):
            at = (held.turn + step) % len(seeds)
            card = self._from_seed(held, at)
            if card is not None:
                held.turn = (at + 1) % len(seeds)
                return card
        return None

    def _from_seed(self, held: _Kind, at: int) -> dict[str, Any] | None:
        kind = held.source.kind
        if (kind, at) in self._out:
            return None
        seed = held.source.seeds[at]
        place = held.places[at]
        while place[0] <= _MAX_PAGE:
            found = self._page(kind, at, seed, place[0])
            if found is None:
                self._out.add((kind, at))
                return None
            items, following = found
            # Past the index a cursor may hold, the rest of a page is passed over.
            while place[1] < min(len(items), _MAX_INDEX):
                item = items[place[1]]
                place[1] += 1
                card = self._accept(item, seed.reason)
                if card is not None:
                    return card
            if following is None or following <= place[0]:
                break
            place[0], place[1] = following, 0
        place[0], place[1] = _MAX_PAGE + 1, 0
        self._out.add((kind, at))
        return None

    def _page(
        self, kind: str, at: int, seed: Seed, page: int
    ) -> tuple[Sequence[Any], int | None] | None:
        key = (kind, at, page)
        if key in self._pages:
            return self._pages[key]
        if self._reads <= 0 or self._seed_reads.get((kind, at), 0) >= _SEED_READS:
            return None
        self._reads -= 1
        self._seed_reads[(kind, at)] = self._seed_reads.get((kind, at), 0) + 1
        try:
            found = seed.read(page)
        except self._failures:
            return None
        self._pages[key] = found
        return found


__all__ = ["BLOOM_BYTES", "MAX_SEEDS", "Accept", "Bloom", "MixState", "Mixer", "Seed", "Source"]
