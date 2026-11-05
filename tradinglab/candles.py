"""Свечи: нормализация, только закрытые, дедупликация, проверка целостности и пропусков.

Свеча [ts, ts+Δ) считается закрытой, если now ≥ ts + Δ + GRACE. Незакрытые свечи отбрасываются ВСЕГДА.
Пропуски не заполняются (никаких выдуманных свечей) — они только фиксируются.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TF_SEC = {"1h": 3600, "4h": 4 * 3600, "1d": 86400}
GT_PARAMS = {"1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}
GRACE_SEC = 180  # задержка агрегации GeckoTerminal после закрытия свечи


@dataclass(frozen=True)
class Candle:
    ts: int        # начало свечи, UTC секунды
    o: float
    h: float
    l: float
    c: float
    v: float       # объём в USD (так отдаёт GeckoTerminal)

    def close_time(self, tf: str) -> int:
        return self.ts + TF_SEC[tf]


@dataclass
class Series:
    tf: str
    candles: list[Candle]
    dropped_unclosed: int = 0
    duplicates: int = 0
    conflicts: list[dict] = field(default_factory=list)   # одинаковый ts, разные значения
    invalid: list[dict] = field(default_factory=list)     # нарушение h ≥ max(o,c) ≥ min(o,c) ≥ l > 0
    gaps: list[dict] = field(default_factory=list)        # {after_ts, missing}

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def ts(self) -> list[int]:
        return [k.ts for k in self.candles]

    def col(self, name: str) -> list[float]:
        return [getattr(k, name) for k in self.candles]

    def upto(self, t_decision: int) -> "Series":
        """Только свечи, закрытые к моменту t_decision (для HTF-контекста и backtest)."""
        d = TF_SEC[self.tf]
        return Series(self.tf, [k for k in self.candles if k.ts + d <= t_decision])

    def quality_at(self, i: int, lookback: int = 50) -> dict:
        """Качество данных, известное на закрытии бара i: учитываются только пропуски, конфликты и невалидные строки
        с временем не позже бара i (будущие дефекты ряда не влияют на прошлые решения — нет look-ahead)."""
        if not self.candles:
            return {"bars": 0, "gaps_recent": 0, "invalid_recent": 0, "conflicts_recent": 0, "ok": False, "as_of_ts": None}
        i = max(0, min(i, len(self.candles) - 1))
        ts_i = self.candles[i].ts
        lo = self.candles[max(0, i - lookback)].ts
        gaps = [g for g in self.gaps if lo <= g["after_ts"] < ts_i]
        inv = [x for x in self.invalid if lo <= x["ts"] <= ts_i]
        conf = [x for x in self.conflicts if lo <= x["ts"] <= ts_i]
        return {"bars": i + 1, "as_of_ts": ts_i, "gaps_recent": len(gaps), "invalid_recent": len(inv),
                "conflicts_recent": len(conf), "gaps": gaps, "ok": not gaps and not inv}

    def quality(self, lookback: int = 50) -> dict:
        """Качество на последнем закрытом баре + общие счётчики ряда (для диагностики)."""
        q = self.quality_at(len(self.candles) - 1, lookback)
        q.update({"bars": len(self.candles), "gaps_total": len(self.gaps), "conflicts": len(self.conflicts),
                  "invalid": len(self.invalid), "dropped_unclosed": self.dropped_unclosed})
        return q


def is_closed(ts: int, tf: str, now: int, grace: int = GRACE_SEC) -> bool:
    return now >= ts + TF_SEC[tf] + grace


def normalize(rows: list, tf: str, now: int, grace: int = GRACE_SEC) -> Series:
    """rows: [[ts, o, h, l, c, v], ...] в любом порядке (формат GeckoTerminal ohlcv_list)."""
    d = TF_SEC[tf]
    by_ts: dict[int, Candle] = {}
    s = Series(tf, [])
    for r in rows:
        ts = int(r[0])
        k = Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0.0))
        if ts % d != 0:  # GT: 1H/4H/1D выровнены по UTC (4H: 00,04,…; 1D: 00:00)
            s.invalid.append({"ts": ts, "reason": "не выровнено по таймфрейму"})
            continue
        if not (k.h >= max(k.o, k.c) and min(k.o, k.c) >= k.l and k.l > 0 and k.v >= 0):
            s.invalid.append({"ts": ts, "reason": "нарушены соотношения OHLC"})
            continue
        if not is_closed(ts, tf, now, grace):
            s.dropped_unclosed += 1
            continue
        if ts in by_ts:
            s.duplicates += 1
            if by_ts[ts] != k:
                s.conflicts.append({"ts": ts, "a": by_ts[ts].__dict__, "b": k.__dict__})
            continue  # первая запись выигрывает: источник ретроактивно не переписывает уже принятые данные
        by_ts[ts] = k
    s.candles = [by_ts[t] for t in sorted(by_ts)]
    for a, b in zip(s.candles, s.candles[1:]):
        if b.ts - a.ts != d:
            s.gaps.append({"after_ts": a.ts, "missing": (b.ts - a.ts) // d - 1})
    return s


def from_gt_payload(payload: dict, tf: str, now: int) -> Series:
    lst = ((payload or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    return normalize(lst, tf, now)
