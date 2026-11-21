"""FastAPI route modules, one sub-package per feature area.

Sub-packages are independently mountable: do not add cross-route imports. Shared helpers
belong in mediaman.web.cookies, mediaman.web.auth.middleware or the relevant service module.
"""

from __future__ import annotations
