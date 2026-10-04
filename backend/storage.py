"""SQLite keeps chat and full Pydantic AI tool history independently of sandboxes."""

import json
import sqlite3
import uuid
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                messages TEXT NOT NULL DEFAULT '[]',
                history TEXT NOT NULL DEFAULT '[]',
                sandbox_id TEXT, sandbox_created_at REAL, sandbox_last_used_at REAL
            )
        """)
        self.db.commit()

    def list(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM threads ORDER BY updated_at DESC, id")
        ]

    def get(self, thread_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def create(self, now: float) -> dict:
        thread_id = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO threads (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (thread_id, "New conversation", now, now),
            )
        return self.get(thread_id)

    def set_sandbox(self, thread_id: str, sandbox_id: str | None, now: float):
        with self.db:
            self.db.execute(
                "UPDATE threads SET sandbox_id=?, sandbox_created_at=?, sandbox_last_used_at=? "
                "WHERE id=?",
                (sandbox_id, now if sandbox_id else None, now if sandbox_id else None, thread_id),
            )

    def touch(self, thread_id: str, now: float):
        with self.db:
            self.db.execute(
                "UPDATE threads SET sandbox_last_used_at=? WHERE id=?", (now, thread_id)
            )

    def save_turn(self, thread_id: str, message: str, reply: str, history: bytes, now: float):
        row = self.get(thread_id)
        messages = json.loads(row["messages"])
        title = row["title"] if messages else " ".join(message.split())[:60]
        messages.extend(
            [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
        )
        with self.db:
            self.db.execute(
                "UPDATE threads SET title=?, messages=?, history=?, updated_at=? WHERE id=?",
                (title, json.dumps(messages), history.decode(), now, thread_id),
            )

    def delete(self, thread_id: str):
        with self.db:
            self.db.execute("DELETE FROM threads WHERE id=?", (thread_id,))

    def close(self):
        self.db.close()
