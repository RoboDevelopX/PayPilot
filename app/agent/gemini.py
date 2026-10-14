"""Google Gemini as an alternative LLM provider (free tier friendly).

The agent loop speaks one format (Anthropic-style messages, tool_use and
tool_result blocks). This adapter translates that format to Gemini's REST
`generateContent` call and back, so the loop, the schema validation and the
policy engine are identical whichever model is behind them.

Gemini specifics handled here:
- tools become `functionDeclarations` (Gemini's schema subset has no
  `additionalProperties`, so it is stripped; Pydantic still forbids extras);
- the model's turn is replayed *verbatim* (it may carry `thoughtSignature`
  parts that Gemini requires back unchanged);
- function results go back as `functionResponse` parts in a user turn.
REST shape checked against ai.google.dev (Oct 2026).
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx

from app.agent.llm import LLMUnavailable

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
OVERLOADED = {500, 502, 503, 504}
REFUSAL_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def _strip_schema(schema: Any) -> Any:
    """Drop JSON-Schema keys Gemini's OpenAPI subset does not accept."""
    if isinstance(schema, dict):
        return {k: _strip_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip_schema(v) for v in schema]
    return schema


def _text_of(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "\n".join(b["text"] for b in system or [] if b.get("type") == "text")


def _block_get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


class GeminiCreate:
    """Callable with the same keyword arguments the agent passes to Claude."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        fallback_model: str = "",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.fallback_model = fallback_model
        self._http = httpx.Client(
            base_url=GEMINI_BASE,
            timeout=timeout,
            transport=transport,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        )

    # ------------------------------------------------------------ request
    def __call__(self, **kw: Any) -> SimpleNamespace:
        body: dict[str, Any] = {"contents": self._contents(kw["messages"])}
        system = _text_of(kw.get("system"))
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if kw.get("tools"):
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": _strip_schema(t["input_schema"]),
                        }
                        for t in kw["tools"]
                    ]
                }
            ]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        gen: dict[str, Any] = {"maxOutputTokens": kw.get("max_tokens", 8192)}
        fmt = (kw.get("output_config") or {}).get("format")
        if fmt and fmt.get("type") == "json_schema":
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = _strip_schema(fmt["schema"])
        body["generationConfig"] = gen

        r = self._post(self.model, body)
        if (r.status_code in OVERLOADED or r.status_code == 429) and self.fallback_model:
            # Free-tier models often answer 503 "high demand", and free quotas are per model
            # (gemini-3.8-flash allows only 20 requests a day), so a lighter model usually has room.
            r = self._post(self.fallback_model, body)
        if r.status_code == 429:
            raise LLMUnavailable("The free AI quota is used up for now. Try again later.")
        if r.status_code in (401, 403):
            raise LLMUnavailable("The AI key was rejected (check GEMINI_API_KEY).")
        if r.status_code >= 400:
            raise LLMUnavailable(f"The AI service returned an error ({r.status_code}).")
        return self._response(r.json())

    def _post(self, model: str, body: dict[str, Any]) -> httpx.Response:
        try:
            return self._http.post(f"/models/{model}:generateContent", json=body)
        except httpx.HTTPError as e:
            raise LLMUnavailable(
                "The AI service is unavailable right now. Try again shortly."
            ) from e

    def _contents(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        names: dict[str, tuple[str, bool]] = (
            {}
        )  # tool_use id -> (function name, id came from Gemini)

        def add(role: str, parts: list[dict[str, Any]]) -> None:
            if contents and contents[-1]["role"] == role and role == "user":
                contents[-1]["parts"].extend(parts)  # merge tool results + next user text
            else:
                contents.append({"role": role, "parts": parts})

        for m in messages:
            if m["role"] == "user":
                if isinstance(m["content"], str):
                    add("user", [{"text": m["content"]}])
                    continue
                parts = []
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        name, real_id = names.get(b["tool_use_id"], ("unknown", False))
                        try:
                            result = json.loads(b["content"])
                        except (TypeError, ValueError):
                            result = {"text": b["content"]}
                        fr: dict[str, Any] = {"name": name, "response": {"result": result}}
                        if real_id:
                            fr["id"] = b["tool_use_id"]
                        parts.append({"functionResponse": fr})
                    elif b.get("type") == "text":
                        parts.append({"text": b["text"]})
                add("user", parts)
            else:
                raw = next(
                    (
                        _block_get(b, "raw")
                        for b in m["content"]
                        if _block_get(b, "type") == "gemini_raw"
                    ),
                    None,
                )
                for b in m["content"]:
                    if _block_get(b, "type") == "tool_use":
                        names[_block_get(b, "id")] = (
                            _block_get(b, "name"),
                            _block_get(b, "gemini_id"),
                        )
                if raw is None:  # history from another provider: rebuild text/function parts
                    raw = {"role": "model", "parts": []}
                    for b in m["content"]:
                        if _block_get(b, "type") == "text":
                            raw["parts"].append({"text": _block_get(b, "text")})
                        elif _block_get(b, "type") == "tool_use":
                            raw["parts"].append(
                                {
                                    "functionCall": {
                                        "name": _block_get(b, "name"),
                                        "args": _block_get(b, "input"),
                                    }
                                }
                            )
                contents.append(copy.deepcopy(raw))
        return contents

    # ----------------------------------------------------------- response
    @staticmethod
    def _response(data: dict[str, Any]) -> SimpleNamespace:
        if (data.get("promptFeedback") or {}).get("blockReason"):
            return SimpleNamespace(content=[], stop_reason="refusal")
        cands = data.get("candidates") or []
        if not cands:
            return SimpleNamespace(content=[], stop_reason="refusal")
        cand = cands[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        blocks: list[Any] = [SimpleNamespace(type="gemini_raw", raw=content)]
        has_call = False
        for part in content.get("parts", []):
            if part.get("thought"):
                continue  # thought summaries are not shown to the user
            if "functionCall" in part:
                fc = part["functionCall"]
                has_call = True
                blocks.append(
                    SimpleNamespace(
                        type="tool_use",
                        id=fc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                        gemini_id=bool(fc.get("id")),
                        name=fc.get("name", ""),
                        input=fc.get("args") or {},
                    )
                )
            elif part.get("text"):
                blocks.append(SimpleNamespace(type="text", text=part["text"]))
        finish = cand.get("finishReason", "STOP")
        if has_call:
            stop = "tool_use"
        elif finish in REFUSAL_REASONS:
            stop = "refusal"
        elif finish == "MAX_TOKENS":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop)
