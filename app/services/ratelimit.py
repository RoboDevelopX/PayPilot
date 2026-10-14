"""In-memory sliding-window rate limiter for the chat endpoint.

Each chat message can cost real LLM tokens and propose payments, so one client
gets at most N messages per minute. In-memory is fine for a single Render
instance; several instances would need a shared store such as Redis.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0, clock=time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next slot frees."""
        now = self.clock()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return round(hits[0] + self.window_s - now, 1)
            hits.append(now)
            return None
