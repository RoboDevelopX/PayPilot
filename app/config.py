"""Runtime configuration, read once from environment variables (or a local .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret: str = os.getenv("PAYPAL_CLIENT_SECRET", "")
    paypal_base_url: str = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
    paypal_webhook_id: str = os.getenv("PAYPAL_WEBHOOK_ID", "")
    # Optional: the sandbox business account email shown as the invoicer.
    paypal_invoicer_email: str = os.getenv("PAYPAL_INVOICER_EMAIL", "")
    business_name: str = os.getenv("PAYPILOT_BUSINESS_NAME", "PayPilot Demo Studio")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    anthropic_effort: str = os.getenv("ANTHROPIC_EFFORT", "medium")

    # Force mock modes even when keys are present (handy for judges and tests).
    force_mock_paypal: bool = _flag("PAYPILOT_MOCK_PAYPAL")
    force_mock_ai: bool = _flag("PAYPILOT_MOCK_AI")

    # Empty means: in-memory for mock PayPal (whose fake data is in memory too), paypal-sandbox.db otherwise.
    db_path: str = os.getenv("PAYPILOT_DB", "")
    reminder_check_seconds: int = int(os.getenv("PAYPILOT_REMINDER_CHECK_SECONDS", "60"))

    @property
    def paypal_mode(self) -> str:
        if self.force_mock_paypal or not (self.paypal_client_id and self.paypal_client_secret):
            return "mock"
        return "sandbox"

    @property
    def ai_mode(self) -> str:
        if self.force_mock_ai or not self.anthropic_api_key:
            return "mock"
        return "claude"


settings = Settings()
