"""Runtime configuration, read from environment variables (or a local .env).

Secrets are `SecretStr`, so they never show up in logs, tracebacks or `repr()`.
The PayPal base URL is locked to the sandbox: PayPilot is a hackathon prototype
and must never be pointed at live money by accident.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PayPal sandbox app (developer.paypal.com > Apps & Credentials > Sandbox)
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: str = SANDBOX_BASE_URL
    paypal_webhook_id: str = ""
    paypal_invoicer_email: str = ""  # sandbox Business account (the merchant)
    paypal_payer_email: str = ""  # sandbox Personal account (pays the demo invoices)
    business_name: str = "PayPilot Demo Studio"

    # Anthropic
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"

    # App
    database_url: str = "sqlite:///./paypilot.db"
    demo_mode: bool = False
    chat_rate_limit_per_minute: int = Field(default=20, ge=1)
    followup_interval_seconds: int = Field(default=300, ge=10)
    http_timeout_seconds: float = 20.0

    @field_validator("paypal_base_url")
    @classmethod
    def _sandbox_only(cls, v: str) -> str:
        v = v.rstrip("/")
        if v != SANDBOX_BASE_URL:
            raise ValueError(
                f"PayPilot is sandbox-only; PAYPAL_BASE_URL must be {SANDBOX_BASE_URL}"
            )
        return v

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret.get_secret_value())

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
