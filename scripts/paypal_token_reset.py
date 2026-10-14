"""Revoke the cached PayPal access token so the next one carries the app's current features.

    python -m scripts.paypal_token_reset

Why this works: for client-credentials tokens, PayPal hands back the *same* token until it
expires (about 9 hours). Its scopes are fixed when it is first issued, so if you tick
Invoicing or Payouts on the app afterwards, calls keep failing with 403 NOT_AUTHORIZED.
Terminating the token makes the next request mint a new one with the new scopes.
"""

from __future__ import annotations

import sys

import httpx

from app.config import get_settings
from app.paypal.client import SANDBOX_BASE_URL


def main() -> int:
    s = get_settings()
    if not s.paypal_configured:
        print("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET first.", file=sys.stderr)
        return 1
    auth = (s.paypal_client_id, s.paypal_client_secret.get_secret_value())
    with httpx.Client(base_url=SANDBOX_BASE_URL, timeout=20.0) as http:
        old = http.post("/v1/oauth2/token", auth=auth, data={"grant_type": "client_credentials"})
        old.raise_for_status()
        token = old.json()["access_token"]
        r = http.post(
            "/v1/oauth2/token/terminate",
            auth=auth,
            data={"token": token, "token_type_hint": "ACCESS_TOKEN"},
        )
        r.raise_for_status()
        new = http.post("/v1/oauth2/token", auth=auth, data={"grant_type": "client_credentials"})
        new.raise_for_status()
    scopes = new.json().get("scope", "")
    for feature in ("invoicing", "payouts"):
        print(f"{feature:9s} {'yes' if feature in scopes else 'NO (tick it on the app)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
