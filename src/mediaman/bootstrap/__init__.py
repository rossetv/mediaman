"""Bootstrap sub-modules for the mediaman application.

Re-exports the public bootstrap API so callers can use either form:

.. code-block:: python

    from mediaman.bootstrap import bootstrap_db, bootstrap_crypto, ...
    from mediaman.bootstrap.db import DataDirNotWritableError

All names are re-exported unchanged from their canonical homes:

- :mod:`mediaman.bootstrap.db` — DB open, migrations, ``app.state``
- :mod:`mediaman.bootstrap.crypto` — AES canary, legacy-ciphertext migration
- :mod:`mediaman.bootstrap.scan_jobs` — scheduler start/stop
- :mod:`mediaman.bootstrap.data_dir` — data-dir writability probe
"""

from __future__ import annotations

from mediaman.bootstrap.crypto import bootstrap_crypto
from mediaman.bootstrap.db import bootstrap_db
from mediaman.bootstrap.scan_jobs import bootstrap_scheduling, shutdown_scheduling

__all__ = [
    "bootstrap_crypto",
    "bootstrap_db",
    "bootstrap_scheduling",
    "shutdown_scheduling",
]
