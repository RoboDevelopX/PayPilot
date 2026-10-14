from __future__ import annotations

import pytest
import respx
from pydantic import SecretStr

from app.config import SANDBOX_BASE_URL, Settings
from app.paypal.client import PayPalClient

BASE = SANDBOX_BASE_URL


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        paypal_client_id="test-client",
        paypal_client_secret="test-secret",
        paypal_invoicer_email="merchant@example.com",
        paypal_payer_email="payer@example.com",
        anthropic_api_key="",
    )


@pytest.fixture
def paypal_mock():
    """respx router that intercepts httpx calls to the sandbox and answers OAuth."""
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.post("/v1/oauth2/token").respond(
            200, json={"access_token": "A21AA-test-token", "expires_in": 32400}
        )
        yield router


@pytest.fixture
def pp_client() -> PayPalClient:
    return PayPalClient("test-client", "test-secret", sleep=lambda _s: None)


@pytest.fixture
def sandbox():
    from tests.fakes import FakeSandbox

    return FakeSandbox()


@pytest.fixture
def make_app(settings, sandbox):
    """Build an isolated app: in-memory DB, fake PayPal sandbox, scripted Claude."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    def _make(llm=None, **overrides):
        s = settings.model_copy(update=overrides) if overrides else settings
        if llm is not None:
            s = s.model_copy(update={"anthropic_api_key": SecretStr("test-key")})
        app = create_app(s, paypal_transport=sandbox.transport, llm_create=llm)
        return TestClient(app), app

    return _make
