"""Gemini provider: translation to generateContent and back, inside the same agent loop."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.agent.gemini import GeminiCreate
from app.agent.llm import Agent, LLMUnavailable
from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.services.actions import ActionService
from app.services.followups import LLMDrafter

INVOICE_ARGS = {
    "client_name": "Acme Corp",
    "client_email": "ap@acme.example",
    "items": [{"name": "Logo design", "quantity": 1, "unit_amount": "450"}],
    "currency": "USD",
    "due_in_days": 14,
    "note": "",
}


class FakeGemini:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "AIza-test"
        assert request.url.path.endswith("/models/gemini-test:generateContent")
        self.requests.append(json.loads(request.content))
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body)


def model_turn(*parts, finish="STOP"):
    return 200, {
        "candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": finish}]
    }


def gemini(fake: FakeGemini) -> GeminiCreate:
    return GeminiCreate("AIza-test", model="gemini-test", transport=fake.transport)


def agent_with(fake: FakeGemini) -> tuple[Agent, ActionService]:
    engine = make_engine("sqlite://")
    init_db(engine)
    service = ActionService(make_session_factory(engine))  # no PayPal: invoices fail cleanly
    return Agent(gemini(fake), handle_action=service.handle, model="gemini-test"), service


def test_tool_call_round_trip_keeps_thought_signature():
    fake = FakeGemini(
        model_turn(
            {"text": "Creating it."},
            {
                "functionCall": {"id": "fc-1", "name": "create_invoice", "args": INVOICE_ARGS},
                "thoughtSignature": "SIG123",
            },
        ),
        model_turn({"text": "PayPal isn't configured, so nothing was sent."}),
    )
    agent, _ = agent_with(fake)
    reply = agent.respond([], "Invoice Acme Corp $450 for logo design")
    assert reply.text.startswith("PayPal isn't configured")
    assert reply.outcomes[0]["tool"] == "create_invoice"

    first, second = fake.requests
    decl = first["tools"][0]["functionDeclarations"]
    assert {d["name"] for d in decl} >= {"create_invoice", "send_payout"}
    assert "additionalProperties" not in json.dumps(decl)
    assert first["systemInstruction"]["parts"][0]["text"].startswith("You are PayPilot")
    assert first["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"

    # The model turn is replayed verbatim, signature included...
    model_msg = second["contents"][1]
    assert model_msg["role"] == "model"
    assert model_msg["parts"][1]["thoughtSignature"] == "SIG123"
    # ...followed by the function result, matched by id and name.
    fr = second["contents"][2]["parts"][0]["functionResponse"]
    assert fr["id"] == "fc-1" and fr["name"] == "create_invoice"
    assert fr["response"]["result"]["status"] == "failed"


def test_invalid_args_are_retried_like_with_claude():
    bad = {**INVOICE_ARGS, "items": [{"name": "x", "quantity": 1, "unit_amount": "$450"}]}
    fake = FakeGemini(
        model_turn({"functionCall": {"name": "create_invoice", "args": bad}}),
        model_turn({"text": "What amount should I use?"}),
    )
    agent, _ = agent_with(fake)
    agent.respond([], "invoice acme")
    fr = fake.requests[1]["contents"][2]["parts"][0]["functionResponse"]
    assert fr["response"]["result"]["status"] == "invalid"
    assert "id" not in fr  # Gemini sent no id, so none is invented on the way back


def test_quota_exhausted_is_a_friendly_error():
    fake = FakeGemini((429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}))
    agent, _ = agent_with(fake)
    with pytest.raises(LLMUnavailable, match="quota"):
        agent.respond([], "hi")


def test_overloaded_model_falls_back_to_lighter_model():
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path.rsplit("/", 1)[-1])
        if "gemini-busy" in request.url.path:
            return httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}})
        return httpx.Response(200, json=model_turn({"text": "Hi!"})[1])

    create = GeminiCreate(
        "AIza-test",
        model="gemini-busy",
        fallback_model="gemini-lite",
        transport=httpx.MockTransport(handle),
    )
    agent = Agent(create, handle_action=lambda *a, **k: None, model="gemini-busy")
    assert agent.respond([], "hi").text == "Hi!"
    assert paths == ["gemini-busy:generateContent", "gemini-lite:generateContent"]


def test_quota_exhausted_model_falls_back_to_lighter_model():
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path.rsplit("/", 1)[-1])
        if "gemini-spent" in request.url.path:
            return httpx.Response(
                429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}
            )
        return httpx.Response(200, json=model_turn({"text": "Hi!"})[1])

    create = GeminiCreate(
        "AIza-test",
        model="gemini-spent",
        fallback_model="gemini-lite",
        transport=httpx.MockTransport(handle),
    )
    agent = Agent(create, handle_action=lambda *a, **k: None, model="gemini-spent")
    assert agent.respond([], "hi").text == "Hi!"
    assert paths == ["gemini-spent:generateContent", "gemini-lite:generateContent"]


def test_overloaded_without_fallback_is_unavailable():
    fake = FakeGemini((503, {"error": {"code": 503, "status": "UNAVAILABLE"}}))
    agent, _ = agent_with(fake)
    with pytest.raises(LLMUnavailable, match="503"):
        agent.respond([], "hi")


def test_safety_block_maps_to_refusal():
    fake = FakeGemini((200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    agent, _ = agent_with(fake)
    assert agent.respond([], "...").refused


def test_reminder_drafter_uses_json_mode():
    fake = FakeGemini(model_turn({"text": json.dumps({"subject": "Nudge", "note": "Hi"})}))
    subject, note = LLMDrafter(gemini(fake), model="gemini-test").draft({"client": "A"}, "friendly")
    assert (subject, note) == ("Nudge", "Hi")
    gen = fake.requests[0]["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert "additionalProperties" not in json.dumps(gen["responseSchema"])


@pytest.mark.parametrize(
    ("provider", "anthropic", "gemini_key", "expected"),
    [
        ("auto", "", "", None),
        ("auto", "", "g", "gemini"),
        ("auto", "a", "g", "anthropic"),
        ("gemini", "a", "g", "gemini"),
        ("anthropic", "", "g", None),
    ],
)
def test_provider_selection(provider, anthropic, gemini_key, expected):
    s = Settings(
        _env_file=None,
        llm_provider=provider,
        anthropic_api_key=SecretStr(anthropic),
        gemini_api_key=SecretStr(gemini_key),
    )
    assert s.active_llm == expected
