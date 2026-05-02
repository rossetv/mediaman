"""Application bootstrap helpers — split from ``main.py::lifespan`` (R23).

Each submodule exposes a ``bootstrap_*`` helper that ``main.lifespan``
calls in order. Keeping them here means the lifespan function remains a
slim orchestrator without inline DB / crypto / scheduling logic.
"""

from .crypto import bootstrap_crypto
from .db import bootstrap_db
from .scheduling import bootstrap_scheduling, shutdown_scheduling

__all__ = [
    "bootstrap_crypto",
    "bootstrap_db",
    "bootstrap_scheduling",
    "shutdown_scheduling",
]
