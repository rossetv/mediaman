"""NZBGet JSON-RPC client."""

import requests


class NzbgetClient:
    def __init__(self, url: str, username: str, password: str):
        self._url = url.rstrip("/")
        self._auth = (username, password)

    def _call(self, method: str) -> dict | list:
        resp = requests.post(f"{self._url}/jsonrpc", json={"method": method}, auth=self._auth, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", {})

    def get_status(self) -> dict:
        return self._call("status")

    def get_queue(self) -> list[dict]:
        return self._call("listgroups")

    def test_connection(self) -> bool:
        try:
            self.get_status()
            return True
        except Exception:
            return False
