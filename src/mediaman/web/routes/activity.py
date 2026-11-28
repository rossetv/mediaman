"""The Activity tab's JSON: the server now, every two seconds while the tab is open, and its
history over a period. Admins only."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.db import get_db
from mediaman.services.activity import SAMPLER, history, live_payload
from mediaman.services.activity.sampler import LIVE_EVERY, LIVE_SECONDS
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.responses import respond_err

router = APIRouter()


@router.get("/api/activity")
def api_activity(request: Request, username: str = Depends(get_current_admin)) -> JSONResponse:
    conn = get_db()
    reading = SAMPLER.read(
        conn, request.app.state.config.secret_key, max_age=LIVE_EVERY * 0.75, now=time.time()
    )
    live = SAMPLER.recent()
    # A tab just opened has seconds of its own: the minute before comes from the history.
    first = float(live[0]["at"]) if live else reading.at  # type: ignore[arg-type]
    kept = history.rows(conn, 0, reading.at - LIVE_SECONDS, first - history.STEP / 2)
    return JSONResponse(live_payload(conn, reading, kept + live))


@router.get("/api/activity/history")
def api_activity_history(
    period: str = "1h", username: str = Depends(get_current_admin)
) -> JSONResponse:
    if period not in history.PERIODS:
        return respond_err("unknown_period", status=400)
    conn = get_db()
    now = time.time()
    return JSONResponse(
        {
            "period": period,
            "now": now,
            "since": history.since(conn),
            "points": history.period(conn, period, now),
        }
    )
