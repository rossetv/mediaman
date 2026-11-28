"""Path parameters that name a database row."""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

#: A row id from the address: 1 up to the largest whole number SQLite takes, so an id past it
#: is refused as malformed rather than failing when it is bound.
RowId = Annotated[int, Path(ge=1, le=2**63 - 1)]
