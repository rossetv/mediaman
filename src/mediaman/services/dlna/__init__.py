"""Playing on an older TV through UPnP AV (DLNA), at an address an admin typed.

Every answer from that address is hostile until checked. All traffic goes
through :class:`~mediaman.services.dlna.wire.Wire`, which holds the limits.

Allowed dependencies: the standard library and ``defusedxml``.

Forbidden patterns: do not import from ``mediaman.web`` or ``mediaman.scanner``;
no socket may be opened outside ``wire``.
"""
