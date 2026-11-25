"""The suite-wide autouse fixtures hold up when what they reset cannot be imported."""

from __future__ import annotations

import sys

import pytest

from tests import conftest


def test_the_scanner_cache_reset_still_yields_when_the_scanner_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "mediaman.scanner.runner", None)
    fixture = conftest._clear_scanner_runner_caches.__wrapped__()

    # Without a yield every test would error with "did not yield a value".
    next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)
