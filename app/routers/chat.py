"""POST /api/chat: one user message in, the agent's reply and any actions out."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.agent.llm import LLMUnavailable
from app.container import Container
from app.schemas import ChatIn
from app.services.conversations import MAX_USER_TURNS

router = APIRouter()


class ChatError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def client_key(request: Request) -> str:
    # Render sits behind a proxy; the first X-Forwarded-For hop is the real client.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


async def run_chat(request: Request, body: ChatIn) -> dict[str, Any]:
    """Shared by the JSON API and the HTMX chat page. Raises ChatError."""
    c: Container = request.app.state.container
    retry_after = c.chat_limiter.check(client_key(request))
    if retry_after is not None:
        raise ChatError(429, f"Too many messages. Try again in {retry_after:.0f}s.", retry_after)
    if c.agent is None:
        raise ChatError(
            503, "The AI assistant is not configured (set GEMINI_API_KEY or ANTHROPIC_API_KEY)."
        )

    conversation_id, history, lock = c.conversations.get(body.conversation_id)
    if c.conversations.user_turns(history) >= MAX_USER_TURNS:
        raise ChatError(409, "This chat is full. Start a new chat to continue.")

    def run():
        with lock:  # one turn at a time per conversation
            return c.agent.respond(history, body.message)

    try:
        reply = await run_in_threadpool(run)
    except LLMUnavailable as e:
        raise ChatError(503, str(e)) from e
    return {
        "conversation_id": conversation_id,
        "reply": reply.text,
        "clarification": reply.clarification,
        "actions": reply.outcomes,
    }


@router.post("/api/chat")
async def chat(body: ChatIn, request: Request) -> dict[str, Any]:
    try:
        return await run_chat(request, body)
    except ChatError as e:
        headers = (
            {"Retry-After": str(int(e.retry_after) + 1)} if e.retry_after is not None else None
        )
        raise HTTPException(e.status_code, str(e), headers=headers) from e
