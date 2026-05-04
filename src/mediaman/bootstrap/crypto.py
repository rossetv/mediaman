"""Back-compat shim — crypto bootstrap logic lives in :mod:`mediaman.app_factory`.

This module re-exports the public API so existing imports such as::

    from mediaman.bootstrap import crypto as crypto_mod
    crypto_mod.bootstrap_crypto(app, config)

continue to work without change.
"""

from mediaman.app_factory import bootstrap_crypto

__all__ = ["bootstrap_crypto"]
