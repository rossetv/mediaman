"""Back-compat shim — the real bootstrap logic now lives in the bootstrap sub-modules.

This package is kept so that existing import paths continue to work:

.. code-block:: python

    from mediaman.bootstrap import bootstrap_db, bootstrap_crypto, ...
    from mediaman.bootstrap.db import DataDirNotWritableError
    from mediaman.bootstrap.scheduling import _validate_scan_time, ...

All names are re-exported unchanged from their canonical homes.

``shutdown_scheduling`` is sourced from the :mod:`.scheduling` shim
(not directly from ``scan_jobs``) so that test monkeypatches on
``mediaman.bootstrap.scheduling._SHUTDOWN_TIMEOUT_SECONDS`` are respected.
"""

from mediaman.app_factory import bootstrap_crypto, bootstrap_db
from mediaman.bootstrap.scan_jobs import bootstrap_scheduling
from mediaman.bootstrap.scheduling import shutdown_scheduling

__all__ = [
    "bootstrap_crypto",
    "bootstrap_db",
    "bootstrap_scheduling",
    "shutdown_scheduling",
]
