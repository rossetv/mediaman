"""Web authentication package.

Relocated from :mod:`mediaman.auth` (which now contains only a back-compat
shim). This package owns all web-specific authentication concerns: sessions,
login lockout, password hashing and policy, reauth tickets, and rate limiting.

Depends on: anything below ``web/`` in the package tree.

Forbidden elsewhere: this is the **only** package permitted to ``import bcrypt``
or to read/write the ``admin_users``, ``admin_sessions``, ``login_failures``,
and ``reauth_tickets`` tables (§2.7). All other packages must treat these as
opaque implementation details of this package.
"""

from __future__ import annotations
