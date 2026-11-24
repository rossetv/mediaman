"""The plex.tv calls behind linking an account: a sign-in PIN, who signed in, and signing out.

No error, log line or return value here carries a sign-in token.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode

import requests

from mediaman.services.infra import SafeHTTPClient, SafeHTTPError

logger = logging.getLogger(__name__)

_HOST = "plex.tv"
_API = f"https://{_HOST}/api/v2"
_AUTH_PAGE = "https://app.plex.tv/auth#?"
_TIMEOUT: tuple[float, float] = (5.0, 10.0)
_MAX_BYTES = 1024 * 1024
_MAX_EXPIRES_S = 600
_NAME_MAX = 80
_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,1024}")
_FLAGS = {"true": True, "1": True, "false": False, "0": False}

# On the Plex server itself the owner is always account 1; everyone else keeps their plex.tv id.
OWNER_ACCOUNT_ID = 1

RefusalReason = Literal["not_on_server", "unsupported_account", "signin_rejected"]


class PlexTvUnavailable(Exception):
    """plex.tv cannot be reached, refused the call, or sent something unreadable."""


class PinExpired(Exception):
    """The sign-in PIN is no longer known to plex.tv."""


class AccountRefused(Exception):
    """The account that signed in cannot be linked; ``reason`` says why."""

    def __init__(self, reason: RefusalReason) -> None:
        self.reason: RefusalReason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class Pin:
    id: int
    code: str
    expires_in_s: int


@dataclass(frozen=True, slots=True)
class PlexIdentity:
    plex_account_id: int
    server_account_id: int
    name: str


@dataclass(frozen=True, slots=True)
class SignIn:
    """What one sign-in came to, and whether its token was revoked afterwards."""

    identity: PlexIdentity | None
    reason: RefusalReason | Literal["plex_unavailable"] | None
    signed_out: bool


@dataclass(frozen=True, slots=True)
class SignInToken:
    value: str = field(repr=False)


def _flag(value: object) -> bool | None:
    """Plex's spellings of a yes or no; None for anything else."""
    return _FLAGS.get(str(value).lower())


def _account_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if OWNER_ACCOUNT_ID < number < 2**53 else None


class PlexTv:
    """plex.tv as one mediaman install, named *product* on Plex's approval page."""

    def __init__(self, client_id: str, product: str, *, http: SafeHTTPClient | None = None) -> None:
        self._client_id = client_id
        self.product = product
        self._http = http or SafeHTTPClient(
            default_timeout=_TIMEOUT,
            default_max_bytes=_MAX_BYTES,
            allowed_hosts=frozenset({_HOST}),
        )

    def _call(
        self,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        *,
        token: SignInToken | None = None,
        params: dict[str, str] | None = None,
    ) -> requests.Response:
        headers = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": self._client_id,
            "X-Plex-Product": self.product,
            "X-Plex-Device-Name": self.product,
        }
        if token is not None:
            headers["X-Plex-Token"] = token.value
        url = f"{_API}{path}"
        if method == "GET":
            return self._http.get(url, headers=headers, params=params, retry=False)
        if method == "POST":
            return self._http.post(url, headers=headers, params=params)
        return self._http.delete(url, headers=headers, params=params)

    def _json(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        token: SignInToken | None = None,
        params: dict[str, str] | None = None,
    ) -> object:
        """The decoded answer. SafeHTTPError keeps its status; its body is never passed on."""
        resp = self._call(method, path, token=token, params=params)
        try:
            return resp.json()
        except (ValueError, RecursionError):
            raise PlexTvUnavailable("plex.tv sent an unreadable answer") from None
        finally:
            resp.close()

    @staticmethod
    def _unavailable(step: str, exc: Exception) -> PlexTvUnavailable:
        status = exc.status_code if isinstance(exc, SafeHTTPError) else type(exc).__name__
        logger.warning("plex_tv.call_failed step=%s status=%s", step, status)
        return PlexTvUnavailable("plex.tv could not be reached")

    def create_pin(self) -> Pin:
        try:
            data = self._json("POST", "/pins", params={"strong": "true"})
        except (SafeHTTPError, requests.RequestException) as exc:
            raise self._unavailable("create_pin", exc) from None
        if not isinstance(data, dict):
            raise PlexTvUnavailable("plex.tv sent an unreadable answer")
        pin_id, code, expires = data.get("id"), data.get("code"), data.get("expiresIn")
        if (
            not isinstance(pin_id, int)
            or isinstance(pin_id, bool)
            or pin_id <= 0
            or not isinstance(code, str)
            or not _CODE_RE.fullmatch(code)
        ):
            raise PlexTvUnavailable("plex.tv sent an unreadable answer")
        if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
            expires = _MAX_EXPIRES_S
        return Pin(id=pin_id, code=code, expires_in_s=min(expires, _MAX_EXPIRES_S))

    def approval_url(self, pin: Pin) -> str:
        """Where the person signs in. No forwardUrl: our session cookie is SameSite=Strict, so a
        return from Plex would look signed out."""
        return _AUTH_PAGE + urlencode(
            {
                "clientID": self._client_id,
                "code": pin.code,
                "context[device][product]": self.product,
            }
        )

    def poll_pin(self, pin: Pin) -> SignInToken | None:
        """The token once the person has approved, else None. Raises PinExpired when it is gone."""
        try:
            data = self._json("GET", f"/pins/{pin.id}")
        except SafeHTTPError as exc:
            if exc.status_code == 404:
                raise PinExpired from None
            raise self._unavailable("poll_pin", exc) from None
        except requests.RequestException as exc:
            raise self._unavailable("poll_pin", exc) from None
        token = data.get("authToken") if isinstance(data, dict) else None
        if not token:
            return None
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise PlexTvUnavailable("plex.tv sent an unreadable answer")
        return SignInToken(token)

    def use_sign_in(self, token: SignInToken, server_id: str) -> SignIn:
        """Learn who signed in and that they may use *server_id*, then revoke the token."""
        identity: PlexIdentity | None = None
        reason: RefusalReason | Literal["plex_unavailable"] | None = None
        signed_out = False
        try:
            identity = self._identify(token, server_id)
        except AccountRefused as exc:
            reason = exc.reason
        except SafeHTTPError as exc:
            if exc.status_code == 401:
                reason = "signin_rejected"
            else:
                self._unavailable("identify", exc)
                reason = "plex_unavailable"
        except (PlexTvUnavailable, requests.RequestException) as exc:
            self._unavailable("identify", exc)
            reason = "plex_unavailable"
        finally:
            signed_out = self.sign_out(token)
        return SignIn(identity=identity, reason=reason, signed_out=signed_out)

    def sign_out(self, token: SignInToken) -> bool:
        try:
            self._call("DELETE", "/users/signout", token=token).close()
        except (SafeHTTPError, requests.RequestException) as exc:
            # A 401 means the token is dead already.
            if isinstance(exc, SafeHTTPError) and exc.status_code == 401:
                return True
            self._unavailable("sign_out", exc)
            return False
        return True

    def _identify(self, token: SignInToken, server_id: str) -> PlexIdentity:
        user = self._json("GET", "/user", token=token)
        if not isinstance(user, dict):
            raise PlexTvUnavailable("plex.tv sent an unreadable answer")
        account_id = _account_id(user.get("id"))
        # A managed Home user has no sign-in of its own, so its owner could link it for them.
        if account_id is None or _flag(user.get("restricted")) is not False:
            raise AccountRefused("unsupported_account")

        resources = self._json("GET", "/resources", token=token)
        if not isinstance(resources, list):
            raise PlexTvUnavailable("plex.tv sent an unreadable answer")
        server = next(
            (
                r
                for r in resources
                if isinstance(r, dict)
                and r.get("clientIdentifier") == server_id
                and "server" in str(r.get("provides") or "").split(",")
            ),
            None,
        )
        if server is None:
            raise AccountRefused("not_on_server")
        owned = _flag(server.get("owned"))
        if owned is None or (owned and _flag(server.get("home")) is not False):
            raise AccountRefused("unsupported_account")
        raw_name = user.get("username") or user.get("title") or ""
        name = "".join(c for c in str(raw_name) if c.isprintable()).strip()[:_NAME_MAX]
        return PlexIdentity(
            plex_account_id=account_id,
            server_account_id=OWNER_ACCOUNT_ID if owned else account_id,
            name=name or f"Plex account {account_id}",
        )
