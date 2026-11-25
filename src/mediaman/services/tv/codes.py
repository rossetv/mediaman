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

    def _hash(self, code: str) -> str:
        return hmac.new(self._key, code.encode(), hashlib.sha256).hexdigest()

    def _prune(self) -> None:
        now = self._clock()
        self._open = {key: a for key, a in self._open.items() if a.expires > now}

    def issue(self, token_hash: str, tv_id: int | None, address: str) -> str:
        """Return a new code for the TV; a paired TV's last one ends.

        Past the limit the oldest code of the addresses that have the most open gives way, so that
        whoever asks for many ends their own first and keeps nobody from pairing.
        """
        with self._lock:
            self._prune()
            if tv_id is not None:
                self._open = {key: a for key, a in self._open.items() if a.tv_id != tv_id}
            block = _bucket_key(address)
            if len(self._open) >= MAX_OPEN:
                held = Counter(a.block for a in self._open.values()) + Counter([block])
                most = max(held.values())
                # Kept in the order they were asked for in.
                del self._open[next(k for k, a in self._open.items() if held[a.block] == most)]
            while True:
                code = f"{secrets.randbelow(1_000_000):06d}"
                key = self._hash(code)
                if key not in self._open:
                    break
            self._open[key] = Asked(token_hash, tv_id, block, self._clock() + CODE_SECONDS)
        return code

    def look(self, code: str) -> Asked | None:
        with self._lock:
            self._prune()
            return self._open.get(self._hash(code))

    def take(self, code: str) -> Asked | None:
        """As ``look``; the code is used up."""
        with self._lock:
            self._prune()
            return self._open.pop(self._hash(code), None)

    def seconds_left(self, token_hash: str) -> int | None:
        """How long the code of the TV that holds the token still works; None once it does not."""
        with self._lock:
            self._prune()
            for asked in self._open.values():
                if asked.token_hash == token_hash:
                    return math.ceil(asked.expires - self._clock())
        return None


_codes = PairingCodes()


def get_codes() -> PairingCodes:
    return _codes
