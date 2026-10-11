"""The Claude tool-use loop, with the API replaced by a scripted fake."""
import json
from types import SimpleNamespace as NS

from app.actions import Service
from app.agent import TOOLS, ClaudeAgent
from app.paypal import MockPayPal
from app.store import Store


class FakeMessages:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def make_agent(script):
    svc = Service(Store(":memory:"), MockPayPal())
    agent = ClaudeAgent(svc, api_key="test", model="claude-opus-5-5", effort="medium")
    fake = FakeMessages(script)
    agent.client = NS(beta=NS(messages=fake))
    return agent, fake


def tool_use(name, args, id="tu_1"):
    return NS(stop_reason="tool_use", content=[NS(type="text", text="On it."), NS(type="tool_use", id=id, name=name, input=args)])


def text(t):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=t)])


def test_payout_over_limit_is_queued_not_sent():
    agent, fake = make_agent([
        tool_use("send_payout", {"recipient_email": "carol@example.com", "amount": "400", "currency": "USD"}),
        text("That's above your $50 limit, so it's waiting for your approval."),
    ])
    out = agent.chat("s1", "send carol 400 for the shoot")
    assert out["events"][0]["result"]["status"] == "pending_approval"
    assert agent.service.store.list("payout") == []
    # The verdict went back to the model as a tool_result.
    history = agent._sessions["s1"]  # user, assistant(tool_use), user(tool_result), assistant(text)
    result = json.loads(history[2]["content"][0]["content"])
    assert result["status"] == "pending_approval"
    assert fake.calls[0]["fallbacks"] == "default"
    assert "approval" in out["reply"]


def test_refusal_rolls_back_history():
    agent, _ = make_agent([NS(stop_reason="refusal", content=[])])
    out = agent.chat("s2", "hi")
    assert "can't" in out["reply"]
    assert agent._sessions["s2"] == []


def test_tool_schemas_are_strict():
    for t in TOOLS:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False


def test_reminder_drafting_uses_structured_output_and_falls_back():
    agent, fake = make_agent([NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"subject": "Quick nudge", "note": "Hi Alice..."}))])])
    inv = {"recipient_name": "Alice", "total": "90.00", "currency": "USD", "items": []}
    assert agent.draft_reminder(inv, 1) == ("Quick nudge", "Hi Alice...")
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"
    subject, _ = agent.draft_reminder(inv, 3)  # script exhausted -> template fallback
    assert subject.startswith("Final reminder")
