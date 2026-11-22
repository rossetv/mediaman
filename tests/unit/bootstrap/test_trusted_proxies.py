"""The uvicorn forwarded-allow-ips list must drop catch-all networks."""

from __future__ import annotations

import pytest

from mediaman.bootstrap.validators import sanitise_trusted_proxies

_CATCH_ALL = ["*", "0.0.0.0/0", "::/0", "::ffff:0.0.0.0/96", "0.0.0.0/0.0.0.0", "::0/0"]


@pytest.mark.parametrize("entry", _CATCH_ALL)
def test_catch_all_entry_is_dropped(entry):
    assert sanitise_trusted_proxies(f"10.0.0.0/8,{entry}") == "10.0.0.0/8"


def test_specific_networks_are_kept():
    assert sanitise_trusted_proxies("10.0.0.0/8, 192.168.1.5") == "10.0.0.0/8,192.168.1.5"


@pytest.mark.parametrize("entry", ["0.0.0.0/1", "::/1", "::ffff:0:0/97"])
def test_too_wide_entry_is_dropped(entry):
    assert sanitise_trusted_proxies(f"10.0.0.0/8,{entry}") == "10.0.0.0/8"
