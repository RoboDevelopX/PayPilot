"""Runtime configuration, read from environment variables (or a local .env).

Secrets are `SecretStr`, so they never show up in logs, tracebacks or `repr()`.
The PayPal base URL is locked to the sandbox: PayPilot is a hackathon prototype
and must never be pointed at live money by accident.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    # LLM provider: "auto" picks Anthropic if its key is set, else Gemini if its key is set.
    llm_provider: Literal["auto", "anthropic", "gemini"] = "auto"

    # Anthropic
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"  # low/medium/high: thinking depth vs. latency and cost

    # Google Gemini (free tier key from aistudio.google.com)
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"  # used when the main model is overloaded

    # App
    database_url: str = "sqlite:///./paypilot.db"
    demo_mode: bool = False
    chat_rate_limit_per_minute: int = Field(default=20, ge=1)
    followups_enabled: bool = True  # background job: poll open invoices, send due reminders
    followup_interval_seconds: int = Field(default=300, ge=10)
    http_timeout_seconds: float = 20.0
    ml_enabled: bool = True  # load ml/artifacts/*.joblib (advisory signals only)
    # Optional HTTP Basic password for a public deployment (webhooks and /healthz stay open).
    app_username: str = "paypilot"
    app_password: SecretStr = SecretStr("")

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
    def paypal_simulated(self) -> bool:
        """Demo mode without keys: every PayPal call is answered from recordings."""
        return self.demo_mode and not self.paypal_configured

    @property
    def assistant_label(self) -> str:
        if self.active_llm:
            return f"{self.active_llm}: {self.llm_model}"
        return "demo script (no AI key)" if self.demo_mode else "not configured"

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value())

    @property
    def active_llm(self) -> str | None:
        """Which provider the agent will use, or None if no key is set."""
        if self.llm_provider == "anthropic" or (
            self.llm_provider == "auto" and self.anthropic_configured
        ):
            return "anthropic" if self.anthropic_configured else None
        if self.llm_provider in ("gemini", "auto") and self.gemini_configured:
            return "gemini"
        return None

    @property
    def llm_model(self) -> str | None:
        return {"anthropic": self.anthropic_model, "gemini": self.gemini_model}.get(
            self.active_llm or ""
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
