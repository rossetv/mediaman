"""Mediaman — media lifecycle management for Plex.

Top-level package. Exposes ``__version__`` and acts as the import root for all
sub-packages: ``scanner``, ``services``, ``web``, ``crypto``, ``db``, and
``bootstrap``.

Allowed dependencies: ``importlib.metadata`` only — this module must import
cleanly in any environment without pulling in third-party packages.

Forbidden patterns: do not add application logic here; keep this file to
version detection and nothing else.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("mediaman")
except PackageNotFoundError:
    # Package is not installed (e.g. running directly from the source tree
    # without `pip install -e .`). Fall back to the literal so imports
    # never raise.
    __version__ = "0.1.0"
