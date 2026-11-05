"""Мелкие утилиты (без внешних зависимостей)."""
from __future__ import annotations

import datetime as dt


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
