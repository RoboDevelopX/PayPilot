"""Live checks against the real PayPal sandbox. Skipped unless credentials are set."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.paypal import client_from_settings

settings = get_settings()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.paypal_configured, reason="PayPal sandbox keys not set"),
]


def test_oauth_token():
    client = client_from_settings(settings)
    assert client.access_token()
