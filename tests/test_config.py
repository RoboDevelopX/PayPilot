import pytest
from pydantic import ValidationError

from app.config import Settings


def test_live_paypal_url_is_refused():
    with pytest.raises(ValidationError, match="sandbox-only"):
        Settings(_env_file=None, paypal_base_url="https://api-m.paypal.com")


def test_secrets_are_masked_in_repr(settings):
    assert "test-secret" not in repr(settings)
    assert settings.paypal_configured
    assert not settings.anthropic_configured
