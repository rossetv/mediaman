"""Bootstrap sub-modules for the mediaman application.

Re-exports the public bootstrap API so that existing import paths continue to work:

.. code-block:: python

    from mediaman.bootstrap import bootstrap_db, bootstrap_crypto, ...
    from mediaman.bootstrap.db import DataDirNotWritableError

All names are re-exported unchanged from their canonical homes.
"""

from mediaman.app_factory import bootstrap_crypto, bootstrap_db
from mediaman.bootstrap.scan_jobs import bootstrap_scheduling, shutdown_scheduling

__all__ = [
    "bootstrap_crypto",
    "bootstrap_db",
    "bootstrap_scheduling",
    "shutdown_scheduling",
]
