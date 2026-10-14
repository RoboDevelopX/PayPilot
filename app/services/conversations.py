"""Chat histories, kept in memory per conversation id.

Histories are append-only: Claude's thinking blocks are only valid if earlier
turns are never edited, so instead of trimming old turns we cap the number of
turns and ask the user to start a new chat.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Any

MAX_CONVERSATIONS = 200
MAX_USER_TURNS = 30


class ConversationStore:
    def __init__(self) -> None:
        self._convs: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, conversation_id: str | None) -> tuple[str, list[dict[str, Any]], threading.Lock]:
        with self._guard:
            if not conversation_id or conversation_id not in self._convs:
                conversation_id = str(uuid.uuid4())
                self._convs[conversation_id] = []
                self._locks[conversation_id] = threading.Lock()
                while len(self._convs) > MAX_CONVERSATIONS:
                    old, _ = self._convs.popitem(last=False)
                    self._locks.pop(old, None)
            self._convs.move_to_end(conversation_id)
            return conversation_id, self._convs[conversation_id], self._locks[conversation_id]

    @staticmethod
    def user_turns(history: list[dict[str, Any]]) -> int:
        return sum(1 for m in history if m["role"] == "user" and isinstance(m["content"], str))
