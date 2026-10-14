"""The LLM action layer: Claude turns a chat message into structured tool calls.

The loop is deliberately small and explicit:
  user text -> Claude -> tool_use blocks -> Pydantic validation
            -> ActionService (policy engine, then PayPal or the approval inbox)
            -> tool_result back to Claude -> Claude writes the reply.
Free text is never parsed for actions: only validated tool calls do anything.
Invalid tool input is returned to Claude as an error, at most twice per turn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
from pydantic import ValidationError

from app.agent.tools import TOOLS
from app.schemas import parse_action

log = logging.getLogger("paypilot.agent")

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_SCHEMA_RETRIES = 2
MAX_ROUNDS = 6
MAX_TOKENS = 16000


class LLMUnavailable(Exception):
    """Claude could not be reached or rejected the request; the user sees a friendly message."""


@dataclass
class AgentReply:
    text: str
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    clarification: str | None = None
    refused: bool = False


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def anthropic_create(api_key: str, *, timeout: float = 60.0) -> Callable[..., Any]:
    """Build the function the agent calls for each model turn.

    Server-side `fallbacks: "default"` re-runs a request on Anthropic's recommended
    model if a safety classifier declines it, instead of returning a refusal.
    """
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def create(**kwargs: Any) -> Any:
        return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)

    return create


class Agent:
    def __init__(
        self,
        create_message: Callable[..., Any],
        *,
        handle_action: Callable[[Any], Any],
        model: str,
        effort: str = "medium",
        system_prompt: str | None = None,
    ) -> None:
        self.create_message = create_message
        self.handle_action = handle_action  # ActionService.handle
        self.model = model
        self.effort = effort
        self.system_prompt = system_prompt or load_prompt("system.md")

    def respond(self, history: list[dict[str, Any]], user_text: str) -> AgentReply:
        """Run one user turn. `history` is mutated in place (append-only)."""
        start = len(history)
        history.append({"role": "user", "content": user_text})
        outcomes: list[dict[str, Any]] = []
        schema_failures = 0

        for _ in range(MAX_ROUNDS):
            try:
                resp = self._call(history)
            except LLMUnavailable:
                if not outcomes:
                    del history[start:]  # nothing happened; let the user simply retry
                    raise
                # Actions already ran: keep the record and summarize them ourselves.
                return AgentReply(_fallback_summary(outcomes), outcomes)

            if resp.stop_reason == "refusal":
                return AgentReply("Sorry, I can't help with that request.", outcomes, refused=True)

            # `fallback` blocks only mark where a server-side fallback happened.
            content = [b for b in resp.content if b.type != "fallback"]
            history.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if b.type == "tool_use"]
            if not tool_uses:
                text = "\n".join(b.text for b in content if b.type == "text").strip()
                return AgentReply(text or _fallback_summary(outcomes), outcomes)

            results, clarification, had_invalid = [], None, False
            for tu in tool_uses:
                try:
                    action = parse_action(tu.name, dict(tu.input))
                except KeyError:
                    had_invalid = True
                    results.append(
                        _result(tu.id, {"status": "invalid", "errors": ["unknown tool"]}, True)
                    )
                    continue
                except ValidationError as e:
                    had_invalid = True
                    results.append(
                        _result(tu.id, {"status": "invalid", "errors": _errors(e)}, True)
                    )
                    continue
                if action.kind == "ask_clarification":
                    clarification = action.question
                    results.append(_result(tu.id, {"status": "shown_to_user"}))
                    continue
                outcome = self.handle_action(action)
                outcomes.append({"tool": action.kind, **outcome.to_dict()})
                results.append(_result(tu.id, outcome.to_dict()))

            history.append({"role": "user", "content": results})
            if clarification:
                return AgentReply(clarification, outcomes, clarification=clarification)
            if had_invalid:
                schema_failures += 1
                if schema_failures > MAX_SCHEMA_RETRIES:
                    return AgentReply(
                        "I couldn't turn that into a valid request. Could you rephrase it with "
                        "the recipient's email and the amount?",
                        outcomes,
                    )

        return AgentReply(
            _fallback_summary(outcomes) or "Sorry, that took too many steps.", outcomes
        )

    def _call(self, history: list[dict[str, Any]]) -> Any:
        try:
            return self.create_message(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": self.system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=history,
            )
        except anthropic.AuthenticationError as e:
            raise LLMUnavailable("The AI key was rejected (check ANTHROPIC_API_KEY).") from e
        except anthropic.RateLimitError as e:
            raise LLMUnavailable("The AI service is busy right now. Try again in a minute.") from e
        except anthropic.BadRequestError as e:
            log.error("Claude rejected the request: %s", e)
            raise LLMUnavailable("The AI service rejected the request.") from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMUnavailable(
                "The AI service is unavailable right now. Try again shortly."
            ) from e


def _result(tool_use_id: str, payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(payload, default=str),
    }
    if is_error:
        block["is_error"] = True
    return block


def _errors(e: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()][:5]


def _fallback_summary(outcomes: list[dict[str, Any]]) -> str:
    return " ".join(f"{o['tool']}: {o['status']} ({o['message']})." for o in outcomes)
