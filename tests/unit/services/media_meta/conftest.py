"""Skip the client retry backoff waits in this package's tests."""

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    # Swap only the retry module's view of ``time``, so sleeps in other code
    # and in test threads stay real.
    monkeypatch.setattr(
        "mediaman.services.infra.http.retry.time", SimpleNamespace(sleep=lambda _s: None)
    )
    monkeypatch.setattr("mediaman.services.infra.http.retry.random.uniform", lambda _a, _b: 0.0)
