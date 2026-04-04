from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional


def _json_default(o: Any) -> Any:
    if is_dataclass(o):
        return asdict(o)
    return str(o)


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def set(self, key: str, value: Any) -> None:
        value_json = json.dumps(value, default=_json_default)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kv(key, value_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, value_json, now),
            )
            conn.commit()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT value_json, updated_at FROM kv WHERE key=?", (key,))
            row = cur.fetchone()
            if not row:
                return None
            value_json, updated_at = row
            return {"value": json.loads(value_json), "updated_at": float(updated_at)}

