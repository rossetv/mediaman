"""plex.tv and one Plex server, faked at the transport, below the SSRF guard and the size cap."""

from __future__ import annotations

import io
import json
from urllib.parse import parse_qs, urlsplit

import requests

SERVER_ID = "srv-1"
TOKEN = "user-token-abc123"
SERVER = "http://plex.lan:32400"


def response(status: int, body: object) -> requests.Response:
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    resp = requests.Response()
    resp.status_code = status
    resp.raw = io.BytesIO(raw)
    return resp


class FakePlex:
    """Answers by (method, path); anything else fails the test. Change ``answers`` to vary."""

    def __init__(self) -> None:
        self.approved = False
        self.history_rows: list[dict[str, str]] = []
        self.calls: list[tuple[str, str, dict[str, str], dict[str, list[str]]]] = []
        self.user: dict[str, object] = {"id": 4242, "username": "amyplex", "restricted": False}
        self.resource: dict[str, object] = {
            "clientIdentifier": SERVER_ID,
            "provides": "server",
            "owned": False,
            "home": False,
            "accessToken": "server-access-token",
        }
        self.answers: dict[tuple[str, str], tuple[int, object] | Exception] = {}
        self.server_id = SERVER_ID

    def install(self, monkeypatch) -> FakePlex:
        monkeypatch.setattr("mediaman.services.infra.http.client._dispatch", self.dispatch)
        return self

    def paths(self) -> list[tuple[str, str]]:
        return [(method, url) for method, url, _, _ in self.calls]

    def signed_out(self) -> int:
        return sum(1 for m, p in self.paths() if m == "DELETE" and p.endswith("/users/signout"))

    def dispatch(self, caller, method, url, *, headers, params, **_kwargs):
        parts = urlsplit(url)
        where = f"{parts.scheme}://{parts.netloc}{parts.path}"
        query = parse_qs(parts.query)
        self.calls.append((method, where, dict(headers or {}), query))
        answer = self.answers.get((method, where)) or self._default(method, where, query)
        if isinstance(answer, Exception):
            raise answer
        return response(*answer)

    def _default(self, method, where, query):
        tv = "https://plex.tv/api/v2"
        if (method, where) == ("POST", f"{tv}/pins"):
            return 201, {"id": 77, "code": "strongcode", "expiresIn": 1800, "authToken": None}
        if (method, where) == ("GET", f"{tv}/pins/77"):
            return 200, {"id": 77, "authToken": TOKEN if self.approved else None}
        if (method, where) == ("GET", f"{tv}/user"):
            return 200, self.user
        if (method, where) == ("GET", f"{tv}/resources"):
            return 200, [{"clientIdentifier": "other", "provides": "client"}, self.resource]
        if (method, where) == ("DELETE", f"{tv}/users/signout"):
            return 204, ""
        if (method, where) == ("GET", f"{SERVER}/identity"):
            return 200, f'<MediaContainer machineIdentifier="{self.server_id}"/>'
        if (method, where) == ("GET", f"{SERVER}/status/sessions/history/all"):
            start = int(query["X-Plex-Container-Start"][0])
            size = int(query["X-Plex-Container-Size"][0])
            rows = "".join(
                "<Video " + " ".join(f'{k}="{v}"' for k, v in row.items()) + "/>"
                for row in self.history_rows[start : start + size]
            )
            return 200, f"<MediaContainer>{rows}</MediaContainer>"
        raise AssertionError(f"unexpected call {method} {where}")
