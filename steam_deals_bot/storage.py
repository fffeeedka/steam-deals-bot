"""SQLite: подписчики, их настройки и что уже отправлено."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "mode": "free",  # free — только бесплатное, new — все новинки, deals — скидки
    "min_discount": 50,
    "priority_genres": [],
    "blocked_genres": [],
}

MODES = ("free", "new", "deals")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id   INTEGER PRIMARY KEY,
    settings  TEXT NOT NULL,
    added_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    chat_id INTEGER NOT NULL,
    appid   INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, appid)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Chat:
    chat_id: int
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))

    @property
    def priority(self) -> list[str]:
        return [g.lower() for g in self.settings.get("priority_genres", [])]

    @property
    def blocked(self) -> list[str]:
        return [g.lower() for g in self.settings.get("blocked_genres", [])]


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- подписчики ---

    def add_chat(self, chat_id: int) -> Chat:
        row = self._db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        if row:
            return self.get_chat(chat_id)
        settings = dict(DEFAULT_SETTINGS)
        self._db.execute(
            "INSERT INTO chats (chat_id, settings, added_at) VALUES (?, ?, strftime('%s','now'))",
            (chat_id, json.dumps(settings, ensure_ascii=False)),
        )
        self._db.commit()
        return Chat(chat_id, settings)

    def get_chat(self, chat_id: int) -> Chat | None:
        row = self._db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            return None
        settings = dict(DEFAULT_SETTINGS)
        try:
            settings.update(json.loads(row["settings"]))
        except json.JSONDecodeError:
            pass
        return Chat(chat_id, settings)

    def update_setting(self, chat_id: int, key: str, value: Any) -> Chat | None:
        chat = self.get_chat(chat_id)
        if chat is None:
            return None
        chat.settings[key] = value
        self._db.execute(
            "UPDATE chats SET settings = ? WHERE chat_id = ?",
            (json.dumps(chat.settings, ensure_ascii=False), chat_id),
        )
        self._db.commit()
        return chat

    def all_chats(self) -> list[Chat]:
        chats = []
        for row in self._db.execute("SELECT chat_id FROM chats ORDER BY chat_id"):
            chat = self.get_chat(row["chat_id"])
            if chat is not None:
                chats.append(chat)
        return chats

    def remove_chat(self, chat_id: int) -> None:
        self._db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        self._db.execute("DELETE FROM sent WHERE chat_id = ?", (chat_id,))
        self._db.commit()

    # --- отправленное ---

    def was_sent(self, chat_id: int, appid: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM sent WHERE chat_id = ? AND appid = ?", (chat_id, appid)
        ).fetchone()
        return row is not None

    def mark_sent(self, chat_id: int, appid: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO sent (chat_id, appid, sent_at) "
            "VALUES (?, ?, strftime('%s','now'))",
            (chat_id, appid),
        )
        self._db.commit()

    # --- служебное ---

    def meta_get(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()
