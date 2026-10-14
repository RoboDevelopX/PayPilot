"""Optional password protection for a public demo deployment.

PayPilot has no user accounts: it is a single-user sandbox prototype. When
APP_PASSWORD is set, every page and API call needs HTTP Basic credentials,
except the health check (Render polls it), PayPal webhooks (PayPal can't log in;
their signature is verified instead) and static files.
"""

from __future__ import annotations

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

OPEN_PATHS = ("/healthz", "/webhooks/paypal")
OPEN_PREFIXES = ("/static/",)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, username: str, password: str) -> None:
        super().__init__(app)
        self._expected = (username.encode(), password.encode())

    def _authorized(self, header: str) -> bool:
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            user, _, pw = base64.b64decode(encoded, validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        # compare_digest on both parts, so timing doesn't reveal which one was wrong
        ok_user = secrets.compare_digest(user, self._expected[0])
        ok_pw = secrets.compare_digest(pw, self._expected[1])
        return ok_user and ok_pw

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        if self._authorized(request.headers.get("authorization", "")):
            return await call_next(request)
        return PlainTextResponse(
            "PayPilot demo: sign in with the username and password you were given.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="PayPilot (sandbox)", charset="UTF-8"'},
        )
