"""Доступ SHADOW к рыночным данным основного бота: физически только чтение.

Соединение открывается как file:...?mode=ro (SQLite отказывает в любой записи) + PRAGMA query_only. Чтение в WAL-режиме
не блокирует писателя (основной бот) и видит только зафиксированные транзакции. Любая попытка записи через этот объект —
исключение ReadOnlyError ещё до обращения к SQLite.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..live.marketdb import MarketDB
from ..store import Store


class ReadOnlyError(Exception):
    pass


class ReadOnlyStore(Store):
    """Store без права записи: все методы чтения Store (эксперименты, журналы, состояние) работают, tx() — исключение."""

    def __init__(self, path: Path | str):  # noqa: D107 — Store.__init__ не вызывается: он создаёт схему (запись)
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"база основного бота не найдена: {self.path}")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=1")
        self.lock = threading.RLock()
        self.schema_version = None

    @contextmanager
    def tx(self):
        raise ReadOnlyError("SHADOW: запись в базу основного бота запрещена")
        yield  # pragma: no cover



# ключи kv основного бота, которые SHADOW читает как рыночные данные (всё остальное в kv — своё, в shadow_lab.db)
MARKET_KV = ("pool_ticks", "gas", "health", "worker")


class SplitDB:
    """Интерфейс MarketDB для ForwardRunner: рыночные данные — из основной БД (read-only), всё состояние SHADOW
    (последняя проверенная котировка, риск, B&H, оценки, события, команды) — в shadow_lab.db."""

    def __init__(self, main: MarketDB, own: MarketDB):
        self.main, self.own = main, own

    # рыночные данные основного бота (только чтение)
    def candles(self, tf, now):
        return self.main.candles(tf, now)

    def last_closed_ts(self, tf):
        return self.main.last_closed_ts(tf)

    def count(self, tf):
        return self.main.count(tf)

    def forming(self, tf):
        return self.main.forming(tf)

    def last_quote(self, kind):
        return self.main.last_quote(kind)

    def quotes_since(self, kind, since):
        return self.main.quotes_since(kind, since)

    def recent_fees(self, n: int = 30):
        return self.main.recent_fees(n)

    def last_context(self, kind):
        return self.main.last_context(kind)

    def kv(self, key, default=None):
        return (self.main if key in MARKET_KV else self.own).kv(key, default)

    # собственное состояние SHADOW
    def kv_set(self, key, value, now):
        if key in MARKET_KV:
            raise ReadOnlyError(f"SHADOW не пишет рыночный ключ {key}")
        return self.own.kv_set(key, value, now)

    def event(self, ts, kind, payload):
        return self.own.event(ts, kind, payload)

    def events(self, limit: int = 100):
        return self.own.events(limit)

    def enqueue(self, ts, command, args=None):
        return self.own.enqueue(ts, command, args)

    def pending_commands(self):
        return self.own.pending_commands()

    def finish_command(self, cid, status, result, now):
        return self.own.finish_command(cid, status, result, now)

    def command(self, cid):
        return self.own.command(cid)

    def add_mark(self, exp, ts, payload):
        return self.own.add_mark(exp, ts, payload)

    def marks(self, exp):
        return self.own.marks(exp)
