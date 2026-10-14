"""A thin, typed PayPal REST client (sandbox only).

Why this works (OAuth2 client credentials): our app proves who it is by sending
its client id + secret to /v1/oauth2/token and gets a short-lived bearer token
(about 9 hours in sandbox). We cache the token and refresh it 60 s before it
expires, so most API calls cost one HTTP request, not two.

Why this works (idempotency): every POST that creates something carries a
`PayPal-Request-Id`. If the network drops after PayPal processed the request,
retrying with the *same* id returns the original result instead of doing it
twice. That is what makes the automatic retries below safe for money.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import SANDBOX_BASE_URL
from app.paypal.errors import (
    PayPalAuthError,
    PayPalError,
    PayPalUnavailable,
    error_for_status,
)

log = logging.getLogger("paypilot.paypal")

TOKEN_REFRESH_MARGIN_S = 60
MAX_RETRIES = 2


@dataclass(frozen=True)
class PayPalResponse:
    status: int
    data: dict[str, Any]
    debug_id: str | None
    request_id: str | None
    headers: dict[str, str]


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = SANDBOX_BASE_URL,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        if base_url.rstrip("/") != SANDBOX_BASE_URL:
            raise ValueError("PayPilot only talks to the PayPal sandbox")
        self._auth = (client_id, client_secret)
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()
        self._sleep = sleep

    # ------------------------------------------------------------------ auth
    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if (
                not force_refresh
                and self._token
                and time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S
            ):
                return self._token
            try:
                r = self._http.post(
                    "/v1/oauth2/token",
                    auth=self._auth,
                    data={"grant_type": "client_credentials"},
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as e:
                raise PayPalUnavailable(f"could not reach PayPal for a token: {e}") from e
            if r.status_code != 200:
                raise PayPalAuthError(
                    "PayPal rejected the client id/secret (check PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)",
                    status=r.status_code,
                    debug_id=r.headers.get("paypal-debug-id"),
                )
            body = r.json()
            self._token = body["access_token"]
            self._token_expires_at = time.monotonic() + int(body.get("expires_in", 3000))
            return self._token

    # --------------------------------------------------------------- request
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
        prefer_representation: bool = True,
    ) -> PayPalResponse:
        """Send one API call. Retries only when it is safe (GET, or POST with a request id)."""
        safe_to_retry = method.upper() == "GET" or request_id is not None
        refreshed = False
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self.access_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if prefer_representation:
                headers["Prefer"] = "return=representation"
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            try:
                r = self._http.request(method, path, json=json, params=params, headers=headers)
            except httpx.HTTPError as e:
                err: PayPalError = PayPalUnavailable(f"could not reach PayPal: {e}")
            else:
                if r.status_code == 401 and not refreshed:
                    # Token revoked or expired early: refresh once and retry.
                    self.access_token(force_refresh=True)
                    refreshed = True
                    continue
                if r.status_code < 400:
                    return PayPalResponse(
                        status=r.status_code,
                        data=_json_or_empty(r),
                        debug_id=r.headers.get("paypal-debug-id"),
                        request_id=request_id,
                        headers=dict(r.headers),
                    )
                err = _map_error(r)

            if err.retryable and safe_to_retry and attempt < MAX_RETRIES:
                attempt += 1
                log.warning("PayPal %s %s failed (%s); retry %d", method, path, err, attempt)
                self._sleep(0.5 * 2**attempt)
                continue
            raise err

    def close(self) -> None:
        self._http.close()


def _json_or_empty(r: httpx.Response) -> dict[str, Any]:
    if not r.content:
        return {}
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"items": data}


def _map_error(r: httpx.Response) -> PayPalError:
    body = _json_or_empty(r)
    details = body.get("details") or []
    message = body.get("message") or body.get("error_description") or r.reason_phrase or "error"
    if details:
        message += ": " + "; ".join(
            f"{d.get('field', '')} {d.get('issue', '')} {d.get('description', '')}".strip()
            for d in details
        )
    cls = error_for_status(r.status_code)
    return cls(
        message,
        status=r.status_code,
        name=body.get("name"),
        details=details,
        debug_id=body.get("debug_id") or r.headers.get("paypal-debug-id"),
    )
