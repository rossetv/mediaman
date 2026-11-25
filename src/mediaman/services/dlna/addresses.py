"""Which addresses, ports and URLs a TV may be reached on."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urljoin

from mediaman.services.dlna.errors import TvBadAnswer, TvRefused

_PRIVATE = tuple(
    ipaddress.IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

# 7676, 9197 and 52235 are Samsung's; the rest are where libupnp starts counting.
ALLOWED_PORTS = frozenset({7676, 9197, 52235, *range(49152, 49160)})

_MAX_URL = 1024
_URL_PART = re.compile(r"[!$-;=?-\[\]_a-z~]+")
_DEVICE_URL = re.compile(r"http://([0-9.]+):([0-9]{1,5})(/.*)?")


_OWN_NETWORK = "To reach TVs, mediaman must run on a container network of its own"


def own_networks(
    routes: Path = Path("/proc/net/route"), local: Path = Path("/proc/net/fib_trie")
) -> list[ipaddress.IPv4Network]:
    """Return the networks this machine or container sits on, and its own addresses.

    Refuses when either cannot be told, since nothing could then be ruled out.
    """
    try:
        header, *rows = (line.split() for line in routes.read_text("ascii").splitlines())
        if [header[i] for i in (1, 2, 7)] != ["Destination", "Gateway", "Mask"]:
            raise ValueError
        networks = []
        for row in rows:
            destination, gateway, mask = (
                ipaddress.IPv4Address(int(row[i], 16).to_bytes(4, "little")) for i in (1, 2, 7)
            )
            if int(gateway) == 0 and int(mask) != 0:
                networks.append(ipaddress.IPv4Network(f"{destination}/{mask}"))
        # Behind a /32 gateway no route covers mediaman's own address.
        own = {
            ipaddress.IPv4Network(address)
            for address in re.findall(r"\|-- (\S+)\n\s+/32 host LOCAL", local.read_text("ascii"))
        }
        addresses = [net for net in own if not net.is_loopback]
        if not networks or not addresses:
            raise ValueError
        return networks + addresses
    except (OSError, ValueError, IndexError, OverflowError) as exc:
        raise TvRefused(f"{_OWN_NETWORK}, and it cannot tell which network it is on.") from exc


def check_address(address: str, own: list[ipaddress.IPv4Network]) -> str:
    """Return *address* if it is a literal private IPv4 address off *own* networks."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError as exc:
        raise TvRefused("Type the TV's address as four numbers, such as 192.168.1.20.") from exc
    if not any(ip in net for net in _PRIVATE):
        raise TvRefused("That is not an address on a home network.")
    if any(ip in net for net in own):
        raise TvRefused(f"{_OWN_NETWORK}, and that address is on mediaman's network.")
    return str(ip)


def check_device_url(url: str, address: str, base: str = "") -> tuple[int, str]:
    """Return the port and path of a URL the TV gave, which must stay on the TV."""
    # Checked before joining: urllib silently drops tabs and line breaks.
    if len(url) > _MAX_URL or not _URL_PART.fullmatch(url):
        raise TvBadAnswer("The TV gave an address that cannot be used.")
    try:
        match = _DEVICE_URL.fullmatch(urljoin(base, url))
    except ValueError as exc:
        raise TvBadAnswer("The TV gave an address that cannot be used.") from exc
    if match is None or match[1] != address:
        raise TvRefused("The TV pointed to an address other than its own.")
    port = int(match[2])
    if port not in ALLOWED_PORTS:
        raise TvRefused(f"The TV uses port {port}, which mediaman does not allow.")
    return port, match[3] or "/"
