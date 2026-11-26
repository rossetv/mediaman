"""fetch_arr_queue_result: which services did not answer, and which refused their key."""

from __future__ import annotations

from unittest.mock import MagicMock

import requests

from mediaman.services.arr.fetcher import fetch_arr_queue_result
from mediaman.services.infra import SafeHTTPError


def test_a_service_that_answers_401_refused_its_key_and_one_that_is_down_did_not(monkeypatch, conn):
    radarr, sonarr = MagicMock(), MagicMock()
    radarr.get_queue.side_effect = SafeHTTPError(401, "", "http://radarr:7878/api/v3/queue")
    sonarr.get_queue.side_effect = requests.ConnectionError("down")
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: radarr)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: sonarr)

    result = fetch_arr_queue_result(conn, "k" * 64)

    assert result.unreachable == ["Radarr", "Sonarr"]
    assert result.refused == ["Radarr"]
