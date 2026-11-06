"""Наборы данных для исследований (только чтение записанных файлов; сеть не используется)."""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

from ..candles import Candle, Series

ROOT = Path(__file__).resolve().parents[2] / "datasets"
AUDIT = ROOT / "strategy_audit_2026-11-06"
DEV_ASSETS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT")                    # проверка правил на ошибки (первые 60% времени)
HOLDOUT_ASSETS = ("AVAXUSDT", "LINKUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT")   # не открывались до финального прогона
ASSETS = DEV_ASSETS + HOLDOUT_ASSETS


@lru_cache(maxsize=None)
def binance(sym: str, tf: str) -> Series:
    rows = json.load(gzip.open(AUDIT / f"{sym}_{tf}.json.gz", "rt"))
    return Series(tf, [Candle(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows])


def fables() -> dict[str, Series]:
    """Самые свежие записанные свечи FABLES (GeckoTerminal) — 1H/4H/1D."""
    from .structure_compare import load_sets
    s = load_sets()
    return {"1h": s["FABLES 1h"], "4h": s["FABLES 4h"], "1d": s["FABLES 1d"]}


def window(s: Series, frac_from: float, frac_to: float) -> tuple[int, int]:
    n = len(s)
    return int(n * frac_from), int(n * frac_to)


def scaled_cost(m, f: float, name: str | None = None):
    """Издержки × f: комиссии, проскальзывание, проскальзывание стопа и газ ×f; импакт ×f (глубина / f)."""
    from dataclasses import replace
    from decimal import Decimal
    F = Decimal(str(f))
    return replace(m, name=name or f"{m.name}×{f:g}", buy_fee=m.buy_fee * F, sell_fee=m.sell_fee * F, slippage=m.slippage * F,
                   stop_slippage=m.stop_slippage * F, gas_usd=m.gas_usd * F,
                   depth_usd=m.depth_usd / F if f > 0 else Decimal("1e15"), label=f"{m.label} ×{f:g}")


FABLES_ATR_PCT = {"1h": 3.57, "4h": 8.60}       # медиана ATR(14)/close FABLES (записанные ряды 2026-11-06)


def vol_matched_factor(s: Series) -> float:
    """Множитель издержек, при котором издержки в единицах ATR такие же, как у FABLES на этом ТФ."""
    from ..analysis import analyze
    import statistics
    a = analyze(s)
    c = s.col("c")
    pct = statistics.median(a.arr["atr"][i] / c[i] * 100 for i in range(len(c)) if a.arr["atr"][i])
    return min(1.0, pct / FABLES_ATR_PCT[s.tf])
