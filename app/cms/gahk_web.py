"""The HTTP client for gahk.dk's sync API (/api/*, served by gahk_web on the webhost).

Every call goes FROM intern TO gahk.dk; gahk.dk never calls intern (it holds no credentials for
it). Requests are signed with the shared key settings.GAHK_WEB_KEY, exactly as the contract says
(and gahk_web's signing.py checks):

    X-Gahk-Timestamp: <unix seconds>
    X-Gahk-Signature: hex(hmac_sha256(KEY, "{ts}\\n{METHOD}\\n{path}\\n{sha256_hex(body)}"))

`path` is the request path without the query string. The webhost refuses a request more than five
minutes off its clock.

Errors are typed so the tasks can tell "gahk.dk is down" (GahkWebUnavailable: retry later) from
"gahk.dk said no" (GahkWebRejected, with the status and its JSON error) and from the one refusal
publishing knows how to fix (MissingImages: upload those, then try again).
"""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from django.conf import settings

# (connect, read) seconds. Reading allows for a publish, which renders every page on a shared host.
TIMEOUT = (10, 60)


class GahkWebError(Exception):
    """Talking to gahk.dk failed. The message is English, for logs and the overview."""


class GahkWebDisabled(GahkWebError):
    """No GAHK_WEB_KEY: syncing with gahk.dk is switched off."""


class GahkWebUnavailable(GahkWebError):
    """gahk.dk could not be reached, timed out, or answered with a server error."""


class GahkWebRejected(GahkWebError):
    """gahk.dk refused the request (4xx)."""

    def __init__(self, status: int, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(f"gahk.dk answered {status}: {message}")
        self.status = status
        self.payload = payload or {}


class MissingImages(GahkWebRejected):
    """POST /api/publish answered 409: gahk.dk lacks these images (sha256 list)."""

    @property
    def missing(self) -> list[str]:
        return [sha for sha in self.payload.get("missing", []) if isinstance(sha, str)]


def sign(key: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    message = f"{timestamp}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_headers(key: str, method: str, path: str, body: bytes, now: float | None = None) -> dict[str, str]:
    timestamp = str(int(time.time() if now is None else now))
    return {"X-Gahk-Timestamp": timestamp, "X-Gahk-Signature": sign(key, timestamp, method, path, body)}


def _json_body(value: Any) -> bytes:  # noqa: ANN401 — any JSON value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class GahkWebClient:
    def __init__(
        self,
        base_url: str | None = None,
        key: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.GAHK_WEB_URL).rstrip("/")
        self.key = key if key is not None else settings.GAHK_WEB_KEY
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.key and self.base_url)

    # ---------------------------------------------------------------------------- transport
    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        content_type: str = "application/json",
        signed: bool = True,
    ) -> requests.Response:
        """Send one request and return the 2xx response; raise a GahkWebError otherwise."""
        if signed and not self.enabled:
            raise GahkWebDisabled("GAHK_WEB_KEY is not set")
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if body or method in ("POST", "PUT"):
            headers["Content-Type"] = content_type
        if signed:
            headers.update(signed_headers(self.key, method, urlsplit(url).path, body))
        try:
            response = self.session.request(
                method, url, data=body, headers=headers, timeout=TIMEOUT, allow_redirects=False
            )
        except requests.RequestException as exc:
            raise GahkWebUnavailable(f"{method} {path}: {exc.__class__.__name__}: {exc}") from exc
        # 501 is the webhost saying "this release cannot do that" (no self-updater yet): an answer,
        # not an outage.
        if response.status_code >= 500 and response.status_code != 501:
            raise GahkWebUnavailable(f"{method} {path}: gahk.dk answered {response.status_code}")
        if response.status_code >= 300:
            payload = self._payload(response)
            message = str(payload.get("error") or response.reason or "error")
            if response.status_code == 409 and isinstance(payload.get("missing"), list):
                raise MissingImages(response.status_code, message, payload)
            raise GahkWebRejected(response.status_code, f"{method} {path}: {message}", payload)
        return response

    @staticmethod
    def _payload(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _json(self, method: str, path: str, value: Any = None) -> dict[str, Any]:  # noqa: ANN401 — any JSON value
        body = b"" if value is None else _json_body(value)
        response = self.request(method, path, body)
        try:
            payload = response.json() if response.content else {}
        except ValueError as exc:
            raise GahkWebUnavailable(f"{method} {path}: the answer is not JSON") from exc
        if not isinstance(payload, dict):
            raise GahkWebUnavailable(f"{method} {path}: the answer is not a JSON object")
        return payload

    # -------------------------------------------------------------------------------- calls
    def health(self) -> dict[str, Any]:
        """GET /api/health (unsigned): {"ok", "version", "revision"}."""
        response = self.request("GET", "/api/health", signed=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GahkWebUnavailable("GET /api/health: the answer is not JSON") from exc
        return payload if isinstance(payload, dict) else {}

    def status(self) -> dict[str, Any]:
        """GET /api/status: version, revision, published_at, rendered_at, outbox, update."""
        return self._json("GET", "/api/status")

    def images_missing(self, sha256s: list[str]) -> list[str]:
        payload = self._json("POST", "/api/images/missing", {"sha256": sha256s})
        return [sha for sha in payload.get("missing", []) if isinstance(sha, str)]

    def put_image(self, sha256: str, data: bytes, content_type: str) -> None:
        self.request("PUT", f"/api/images/{sha256}", data, content_type=content_type)

    def publish(self, snapshot_body: bytes) -> dict[str, Any]:
        """POST /api/publish with the snapshot's canonical JSON. Raises MissingImages on a 409."""
        response = self.request("POST", "/api/publish", snapshot_body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GahkWebUnavailable("POST /api/publish: the answer is not JSON") from exc
        return payload if isinstance(payload, dict) else {}

    def preview(self, request_body: dict[str, Any]) -> str:
        """POST /api/preview: the item rendered as gahk.dk would show it (HTML, never stored)."""
        response = self.request("POST", "/api/preview", _json_body(request_body))
        return response.content.decode("utf-8", errors="replace")

    def outbox(self) -> dict[str, Any]:
        """GET /api/outbox: {"applications": [...], "visits": [...]} waiting to be pulled."""
        return self._json("GET", "/api/outbox")

    def ack(self, application_ids: list[str], visits_through: str | None) -> dict[str, Any]:
        """POST /api/outbox/ack: let gahk.dk delete what intern has stored."""
        return self._json(
            "POST", "/api/outbox/ack", {"applications": application_ids, "visits_through": visits_through}
        )

    def rebuild(self) -> dict[str, Any]:
        """POST /api/rebuild: re-render from the cached snapshot (so "upcoming" stays right)."""
        return self._json("POST", "/api/rebuild", {})

    def update(self) -> dict[str, Any]:
        """POST /api/update: the webhost's self-updater runs now (its only trigger)."""
        return self._json("POST", "/api/update", {})
