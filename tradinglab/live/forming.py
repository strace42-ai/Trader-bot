"""Формирующаяся свеча FORWARD / LIVE и live-значения индикаторов — ТОЛЬКО для отображения.

Стратегии и Risk Monitor эти данные не используют: решения по-прежнему принимаются только по закрытым свечам
(live_candles), исполнение — по проверенным котировкам.

Формирующаяся свеча текущего периода [start, start+Δ) таймфрейма (UTC):
  open  — open формирующейся свечи GeckoTerminal этого периода; если её нет — первая котировка периода;
  high/low — экстремумы с начала периода: high/low формирующейся свечи GeckoTerminal и всех котировок пула за период;
  close — последняя котировка (StateView, иначе DexScreener), если она не старше QUOTE_MAX_AGE; иначе close GeckoTerminal;
  volume — объём формирующейся свечи GeckoTerminal (частичный; если её нет — неизвестен).
Если GeckoTerminal ещё не отдал только что закрытый период (≈ первые минуты после закрытия), он показывается
«предварительно» (status=CLOSING) из котировок, пока не придёт закрытая свеча источника.
Цены свечей GeckoTerminal — USD, котировки пула — USDG (≈ USD; при отклонении USDG > 0,5% входы блокируются).
"""
from __future__ import annotations

from . import config as C
from .marketdb import MarketDB
from ..analysis import analyze
from ..candles import TF_SEC, Candle, Series

LIVE_KEYS = ("rsi", "macd", "macd_signal", "macd_hist", "mfi", "cmf", "obv", "ema_fast", "ema_slow", "bb_upper", "bb_mid",
             "bb_lower", "atr", "adx", "pdi", "ndi")


def _quotes(db: MarketDB, start: int, end: int) -> tuple[list[dict], str]:
    for kind, name in (("pool", "StateView"), ("ds", "DexScreener")):
        qs = [q for q in db.quotes_since(kind, start) if q["ts"] < end and q.get("mid")]
        if qs:
            return qs, name
    return [], ""


def build_bar(db: MarketDB, tf: str, start: int, now: int, status: str) -> dict | None:
    d = TF_SEC[tf]
    end = start + d
    gt = db.forming(tf)
    gt = gt if gt and gt["ts"] == start else None
    qs, src = _quotes(db, start, end)
    prices = [q["mid"] for q in qs]
    o = gt["o"] if gt else (prices[0] if prices else None)
    if o is None:
        return None
    hs = [o] + prices + ([gt["h"]] if gt else [])
    ls_ = [o] + prices + ([gt["l"]] if gt else [])
    last = qs[-1] if qs else None
    fresh = last is not None and (status == "CLOSING" or now - last["ts"] <= C.QUOTE_MAX_AGE)
    c = last["mid"] if fresh else (gt["c"] if gt else o)
    return {"time": start, "open": o, "high": max(hs), "low": min(ls_), "close": c, "volume": gt["v"] if gt else None,
            "status": status, "end": end, "close_source": f"{src} {now - last['ts']} с назад" if fresh else "GeckoTerminal (формирующаяся)",
            "open_source": "GeckoTerminal" if gt else src, "quotes": len(prices), "quotes_from": qs[0]["ts"] if qs else None,
            "gt_fetched_at": gt["fetched_at"] if gt else None, "updated_at": now}


def live_bars(db: MarketDB, tf: str, now: int) -> list[dict]:
    d = TF_SEC[tf]
    start = now // d * d
    last_closed = db.last_closed_ts(tf)
    out = []
    if last_closed is not None and last_closed < start - d:            # период закрыт, но GeckoTerminal его ещё не отдал
        prev = build_bar(db, tf, start - d, now, "CLOSING")
        if prev and prev["quotes"]:
            out.append(prev)
    cur = build_bar(db, tf, start, now, "FORMING")
    if cur:
        out.append(cur)
    return out


def live_values(closed: Series, bars: list[dict], params: dict) -> dict:
    """Значения индикаторов на последней закрытой свече и на формирующейся (закрытые + предварительные + формирующаяся).
    Без объёма формирующейся свечи MFI/CMF/OBV для неё не рассчитываются (None)."""
    out = {"closed": None, "live": None}
    if not len(closed):
        return out
    a = analyze(closed, params)
    i = len(closed) - 1
    out["closed"] = {"time": closed.candles[i].ts, **{k: a.v(k, i) for k in LIVE_KEYS}}
    if not bars:
        return out
    extra = [Candle(b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"] or 0.0) for b in bars]
    s2 = Series(closed.tf, closed.candles + extra)
    a2 = analyze(s2, params)
    j = len(s2) - 1
    vals = {k: a2.v(k, j) for k in LIVE_KEYS}
    if any(b["volume"] is None for b in bars):
        for k in ("mfi", "cmf", "obv"):
            vals[k] = None
    out["live"] = {"time": bars[-1]["time"], "status": bars[-1]["status"], **vals}
    return out
