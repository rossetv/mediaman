"""Mediaman — media lifecycle management for Plex."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__: str = _pkg_version("mediaman")
except PackageNotFoundError:
    # Package is not installed (e.g. running directly from the source tree
    # without `pip install -e .`). Fall back to the literal so imports
    # never raise.
    __version__ = "0.1.0"
