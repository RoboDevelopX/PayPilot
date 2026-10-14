"""PayPal sandbox integration. Only `app.services.executor` calls into this package."""

from __future__ import annotations

from app.config import Settings
from app.paypal.client import PayPalClient


def client_from_settings(settings: Settings, **kwargs) -> PayPalClient:
    return PayPalClient(
        settings.paypal_client_id,
        settings.paypal_client_secret.get_secret_value(),
        base_url=settings.paypal_base_url,
        timeout=settings.http_timeout_seconds,
        **kwargs,
    )
