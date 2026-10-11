"""Tiny SQLite-backed document store for rules, invoices, payouts, approvals and the audit log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Store:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            " kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
            " PRIMARY KEY (kind, id))"
        )
        self._db.commit()

    def put(self, kind: str, id: str, data: dict[str, Any]) -> dict[str, Any]:
        ts = now()
        data = {**data, "id": id}
        data.setdefault("created_at", ts)
        data["updated_at"] = ts
        with self._lock:
            self._db.execute(
                "INSERT INTO records (kind, id, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (kind, id, json.dumps(data), data["created_at"], ts),
            )
            self._db.commit()
        return data

    def get(self, kind: str, id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM records WHERE kind = ? AND id = ?", (kind, id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM records WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def update(self, kind: str, id: str, **fields: Any) -> dict[str, Any]:
        current = self.get(kind, id)
        if current is None:
            raise KeyError(f"{kind} {id} not found")
        current.update(fields)
        return self.put(kind, id, current)

    def log(self, event: str, detail: dict[str, Any]) -> dict[str, Any]:
        """Append an entry to the audit trail. Every proposal and decision lands here."""
        return self.put("activity", new_id("evt"), {"event": event, "detail": detail})

    def reset(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM records")
            self._db.commit()
