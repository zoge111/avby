"""Слой хранения: SQLite (только stdlib). Весь SQL находится в этом файле —
для PostgreSQL достаточно переписать класс Database (asyncpg), остальной код не затрагивается.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    created_at    INTEGER NOT NULL,
    premium_until INTEGER NOT NULL DEFAULT 0,
    is_blocked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS monitors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL DEFAULT 'filter',      -- filter | seller
    title      TEXT    NOT NULL,
    filters    TEXT    NOT NULL,                       -- JSON, см. matcher.py
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitors_user   ON monitors(user_id);
CREATE INDEX IF NOT EXISTS idx_monitors_active ON monitors(is_active);
-- история отправленных уведомлений: защита от дублей
CREATE TABLE IF NOT EXISTS sent_adverts (
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    advert_id  INTEGER NOT NULL,
    sent_at    INTEGER NOT NULL,
    PRIMARY KEY (monitor_id, advert_id)
);
-- все объявления, которые бот уже видел в ленте (из них складывается «первый проход»)
CREATE TABLE IF NOT EXISTS seen_adverts (
    advert_id     INTEGER PRIMARY KEY,
    first_seen_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> int:
    return int(time.time())


def _monitor(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is not None:
        row["filters"] = json.loads(row["filters"])
    return row


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # --- служебное -------------------------------------------------------------------

    async def connect(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)  # autocommit
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _exec(self, sql: str, params: Any, mode: str) -> Any:
        conn = self._conn
        assert conn is not None, "Database.connect() не вызван"
        if mode == "many":  # пакетная вставка одной транзакцией
            conn.execute("BEGIN")
            try:
                conn.executemany(sql, params)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return None
        cur = conn.execute(sql, params)
        if mode == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        if mode == "all":
            return [dict(r) for r in cur.fetchall()]
        return cur.lastrowid if mode == "id" else cur.rowcount

    async def _run(self, sql: str, params: Any = (), mode: str = "rc") -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._exec, sql, params, mode)

    # --- пользователи ----------------------------------------------------------------

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        await self._run(
            "INSERT INTO users(user_id, username, first_name, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "first_name=excluded.first_name, is_blocked=0",
            (user_id, username, first_name, _now()),
        )

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self._run("SELECT * FROM users WHERE user_id=?", (user_id,), "one")

    @staticmethod
    def is_premium(user: dict[str, Any] | None) -> bool:
        return bool(user and user["premium_until"] > _now())

    async def set_premium(self, user_id: int, until: int) -> bool:
        return await self._run("UPDATE users SET premium_until=? WHERE user_id=?", (until, user_id)) > 0

    async def mark_blocked(self, user_id: int) -> None:
        await self._run("UPDATE users SET is_blocked=1 WHERE user_id=?", (user_id,))

    async def all_user_ids(self) -> list[int]:
        rows = await self._run("SELECT user_id FROM users WHERE is_blocked=0", (), "all")
        return [r["user_id"] for r in rows]

    # --- мониторинги -----------------------------------------------------------------

    async def add_monitor(self, user_id: int, kind: str, title: str, filters: dict[str, Any]) -> int:
        await self._run("INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?,?)", (user_id, _now()))
        return await self._run(
            "INSERT INTO monitors(user_id, kind, title, filters, created_at) VALUES(?,?,?,?,?)",
            (user_id, kind, title, json.dumps(filters, ensure_ascii=False), _now()),
            "id",
        )

    async def list_monitors(self, user_id: int) -> list[dict[str, Any]]:
        rows = await self._run("SELECT * FROM monitors WHERE user_id=? ORDER BY id", (user_id,), "all")
        return [_monitor(r) for r in rows]  # type: ignore[misc]

    async def get_monitor(self, user_id: int, monitor_id: int) -> dict[str, Any] | None:
        return _monitor(
            await self._run("SELECT * FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id), "one")
        )

    async def count_active(self, user_id: int) -> int:
        row = await self._run(
            "SELECT COUNT(*) AS n FROM monitors WHERE user_id=? AND is_active=1", (user_id,), "one"
        )
        return row["n"]

    async def set_active(self, user_id: int, monitor_id: int, active: bool) -> None:
        await self._run(
            "UPDATE monitors SET is_active=? WHERE id=? AND user_id=?", (int(active), monitor_id, user_id)
        )

    async def stop_all(self, user_id: int) -> int:
        return await self._run("UPDATE monitors SET is_active=0 WHERE user_id=? AND is_active=1", (user_id,))

    async def delete_monitor(self, user_id: int, monitor_id: int) -> bool:
        return await self._run("DELETE FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id)) > 0

    async def active_monitors(self) -> list[dict[str, Any]]:
        """Активные мониторинги пользователей, которые не заблокировали бота."""
        rows = await self._run(
            "SELECT m.id, m.user_id, m.kind, m.filters, m.created_at FROM monitors m "
            "JOIN users u ON u.user_id = m.user_id WHERE m.is_active=1 AND u.is_blocked=0",
            (), "all",
        )
        return [_monitor(r) for r in rows]  # type: ignore[misc]

    # --- объявления ------------------------------------------------------------------

    async def has_seen(self) -> bool:
        return await self._run("SELECT 1 AS x FROM seen_adverts LIMIT 1", (), "one") is not None

    async def known_ids(self, ids: Iterable[int]) -> set[int]:
        ids = list(ids)
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        rows = await self._run(f"SELECT advert_id FROM seen_adverts WHERE advert_id IN ({marks})", ids, "all")
        return {r["advert_id"] for r in rows}

    async def mark_seen(self, ids: Iterable[int]) -> None:
        now = _now()
        await self._run(
            "INSERT OR IGNORE INTO seen_adverts(advert_id, first_seen_at) VALUES(?,?)",
            [(i, now) for i in ids], "many",
        )

    async def claim(self, monitor_id: int, advert_id: int) -> bool:
        """Атомарно «занимает» пару (мониторинг, объявление). False — уведомление уже отправлялось."""
        n = await self._run(
            "INSERT OR IGNORE INTO sent_adverts(monitor_id, advert_id, sent_at) VALUES(?,?,?)",
            (monitor_id, advert_id, _now()),
        )
        return n == 1

    async def cleanup(self, seen_days: int = 30, sent_days: int = 60) -> None:
        now = _now()
        await self._run("DELETE FROM seen_adverts WHERE first_seen_at < ?", (now - seen_days * 86400,))
        await self._run("DELETE FROM sent_adverts WHERE sent_at < ?", (now - sent_days * 86400,))

    # --- статистика ------------------------------------------------------------------

    async def incr(self, key: str, n: int = 1) -> None:
        if n:
            await self._run(
                "INSERT INTO counters(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
                (key, n),
            )

    async def set_counter(self, key: str, value: int) -> None:
        await self._run(
            "INSERT INTO counters(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def stats(self) -> dict[str, Any]:
        one = lambda sql, p=(): self._run(sql, p, "one")  # noqa: E731
        users = await one("SELECT COUNT(*) AS n FROM users")
        blocked = await one("SELECT COUNT(*) AS n FROM users WHERE is_blocked=1")
        premium = await one("SELECT COUNT(*) AS n FROM users WHERE premium_until > ?", (_now(),))
        total = await one("SELECT COUNT(*) AS n FROM monitors")
        active = await one("SELECT COUNT(*) AS n FROM monitors WHERE is_active=1")
        rows = await self._run("SELECT key, value FROM counters", (), "all")
        return {
            "users": users["n"], "blocked": blocked["n"], "premium": premium["n"],
            "monitors": total["n"], "active": active["n"],
            "counters": {r["key"]: r["value"] for r in rows},
        }
