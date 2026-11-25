"""The codes TVs show to be paired, kept in memory and only as keyed hashes."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from mediaman.services.rate_limit.limiters import _bucket_key

CODE_SECONDS = 300.0
MAX_OPEN = 100


@dataclass(frozen=True)
class Asked:
    """A TV that shows a code."""

    #: Hash of the token the TV will hold once its code is entered.
    token_hash: str
    #: The TV's row, where it was paired when it asked.
    tv_id: int | None
    #: The block of addresses it asked from.
    block: str
    expires: float


class PairingCodes:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # Six digits are soon guessed from a plain hash.
        self._key = secrets.token_bytes(32)
        self._open: dict[str, Asked] = {}
        # Those of them being paired right now; they neither run out nor give way.
        self._claimed: set[str] = set()

    def _hash(self, code: str) -> str:
        return hmac.new(self._key, code.encode(), hashlib.sha256).hexdigest()

    def _prune(self) -> None:
        now = self._clock()
        self._open = {
            key: a for key, a in self._open.items() if a.expires > now or key in self._claimed
        }

    def issue(self, token_hash: str, tv_id: int | None, address: str) -> str:
        """Return a new code for the TV; a paired TV's last one ends.

        Past the limit the oldest code of the addresses that have the most open gives way, so that
        whoever asks for many ends their own first and keeps nobody from pairing.
        """
        with self._lock:
            self._prune()
            if tv_id is not None:
                self._open = {
                    key: a
                    for key, a in self._open.items()
                    if a.tv_id != tv_id or key in self._claimed
                }
            block = _bucket_key(address)
            if len(self._open) >= MAX_OPEN:
                # Kept in the order they were asked for in.
                may_go = {k: a.block for k, a in self._open.items() if k not in self._claimed}
                held = Counter([*may_go.values(), block])
                most = max(held.values())
                oldest = next((k for k, b in may_go.items() if held[b] == most), None)
                if oldest is not None:
                    del self._open[oldest]
            while True:
                code = f"{secrets.randbelow(1_000_000):06d}"
                key = self._hash(code)
                if key not in self._open:
                    break
            self._open[key] = Asked(token_hash, tv_id, block, self._clock() + CODE_SECONDS)
        return code

    def look(self, code: str) -> Asked | None:
        with self._lock:
            return self._unclaimed(self._hash(code))

    def _unclaimed(self, key: str) -> Asked | None:
        self._prune()
        return None if key in self._claimed else self._open.get(key)

    def claim(self, code: str) -> Asked | None:
        """As ``look``, for one alone, until ``end`` or ``release``; to its TV it stays open."""
        with self._lock:
            key = self._hash(code)
            asked = self._unclaimed(key)
            if asked is not None:
                self._claimed.add(key)
            return asked

    def end(self, code: str) -> None:
        with self._lock:
            key = self._hash(code)
            self._claimed.discard(key)
            self._open.pop(key, None)

    def release(self, code: str) -> None:
        with self._lock:
            self._claimed.discard(self._hash(code))

    def seconds_left(self, token_hash: str) -> int | None:
        """How long the code of the TV that holds the token still works; None once it does not."""
        with self._lock:
            self._prune()
            for asked in self._open.values():
                if asked.token_hash == token_hash:
                    return max(1, math.ceil(asked.expires - self._clock()))
        return None


_codes = PairingCodes()


def get_codes() -> PairingCodes:
    return _codes
