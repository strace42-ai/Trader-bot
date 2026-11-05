"""Аудит индикаторов: сравнение ядра (tradinglab.indicators) с независимыми эталонными реализациями в семантике
TradingView / Pine Script — именно эту методику использует график DexScreener (TradingView Charting Library).

Эталоны написаны заново по определениям Pine (ta.rsi, ta.ema, ta.rma, ta.macd, ta.mfi, ta.obv, ta.atr, ta.dmi,
ta.bb/stdev, Chaikin Money Flow) и НЕ используют код ядра. Отличия методик, которые ожидаемо дают расхождение:
- EMA: ядро засевает EMA средним первых n значений (SMA seed), Pine — первым значением. Влияние затухает как (1−2/(n+1))^k:
  на 1H (сотни баров) — 1e-10 и меньше; на коротких рядах (4H, 1D) в первых барах заметно.
- ATR: Pine ta.atr использует TR первого бара = high−low и стартует на бар раньше; влияние затухает как (13/14)^k.
- RSI у обоих — Wilder (RMA, засев SMA). Для объяснения расхождений с внешними графиками дополнительно считаются
  варианты: SMA-RSI (Cutler) и EMA-RSI, а также RSI с текущей формирующейся свечой.
Все расчёты — по одним и тем же закрытым свечам GeckoTerminal (время свечи = начало периода, UTC).
"""
from __future__ import annotations

import math

from . import indicators as I
from .candles import Series

RSI_N, MFI_N, CMF_N, BB_N, ATR_N, ADX_N = 14, 14, 20, 20, 14, 14


# ====================================================================== эталоны (семантика Pine)
def pine_sma(x, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if all(v is not None for v in w):
            out[i] = sum(w) / n
    return out


def pine_ema(x, n):
    """ta.ema: alpha = 2/(n+1); первое значение = первому src (не SMA)."""
    a, out, prev = 2 / (n + 1), [None] * len(x), None
    for i, v in enumerate(x):
        if v is None:
            continue
        prev = v if prev is None else a * v + (1 - a) * prev
        out[i] = prev
    return out


def pine_rma(x, n):
    """ta.rma: первое значение — SMA первых n значений, далее alpha = 1/n."""
    out, prev, buf = [None] * len(x), None, []
    for i, v in enumerate(x):
        if v is None:
            continue
        if prev is None:
            buf.append(v)
            if len(buf) == n:
                prev = sum(buf) / n
                out[i] = prev
            continue
        prev = (v + (n - 1) * prev) / n
        out[i] = prev
    return out


def pine_rsi(c, n=RSI_N):
    ch = [None] + [c[i] - c[i - 1] for i in range(1, len(c))]
    up = pine_rma([None if d is None else max(d, 0.0) for d in ch], n)
    dn = pine_rma([None if d is None else max(-d, 0.0) for d in ch], n)
    out = []
    for u, d in zip(up, dn):
        out.append(None if u is None or d is None else 100.0 if d == 0 and u > 0 else 50.0 if d == 0 else 100 - 100 / (1 + u / d))
    return out


def rsi_sma(c, n=RSI_N):
    """SMA-RSI (Cutler): средние приросты/потери — простые средние за n. НЕ используется ядром — для объяснения."""
    out = [None] * len(c)
    for i in range(n, len(c)):
        g = sum(max(c[j] - c[j - 1], 0) for j in range(i - n + 1, i + 1)) / n
        l_ = sum(max(c[j - 1] - c[j], 0) for j in range(i - n + 1, i + 1)) / n
        out[i] = 100.0 if l_ == 0 and g > 0 else 50.0 if l_ == 0 else 100 - 100 / (1 + g / l_)
    return out


def rsi_ema(c, n=RSI_N):
    ch = [None] + [c[i] - c[i - 1] for i in range(1, len(c))]
    up = pine_ema([None if d is None else max(d, 0.0) for d in ch], n)
    dn = pine_ema([None if d is None else max(-d, 0.0) for d in ch], n)
    return [None if u is None or d is None or (u == 0 and d == 0) else 100.0 if d == 0 else 100 - 100 / (1 + u / d) for u, d in zip(up, dn)]


def pine_macd(c, f=12, s=26, sig=9):
    line = [a - b for a, b in zip(pine_ema(c, f), pine_ema(c, s))]
    sg = pine_ema(line, sig)
    return line, sg, [a - b for a, b in zip(line, sg)]


def pine_mfi(h, l, c, v, n=MFI_N):
    src = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
    out = [None] * len(c)
    for i in range(n, len(c)):
        up = sum(v[j] * src[j] for j in range(i - n + 1, i + 1) if src[j] - src[j - 1] > 0)
        lo = sum(v[j] * src[j] for j in range(i - n + 1, i + 1) if src[j] - src[j - 1] < 0)
        out[i] = 100.0 if lo == 0 and up > 0 else 50.0 if lo == 0 else 100 - 100 / (1 + up / lo)
    return out


def pine_cmf(h, l, c, v, n=CMF_N):
    ad = [0.0 if (cc == hh and cc == ll) or hh == ll else ((2 * cc - ll - hh) / (hh - ll)) * vv for hh, ll, cc, vv in zip(h, l, c, v)]
    out = [None] * len(c)
    for i in range(n - 1, len(c)):
        vs = sum(v[i - n + 1:i + 1])
        out[i] = sum(ad[i - n + 1:i + 1]) / vs if vs else 0.0
    return out


def pine_obv(c, v):
    out, acc = [], 0.0
    for i in range(len(c)):
        if i > 0:
            acc += math.copysign(1, c[i] - c[i - 1]) * v[i] if c[i] != c[i - 1] else 0.0
        out.append(acc)
    return out


def pine_bb(c, n=BB_N, k=2.0):
    mid = pine_sma(c, n)
    up, lo = [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - mid[i]) ** 2 for x in w) / n)       # ta.stdev(biased=true)
        up[i], lo[i] = mid[i] + k * sd, mid[i] - k * sd
    return up, mid, lo


def pine_atr(h, l, c, n=ATR_N):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return pine_rma(tr, n)


def pine_dmi(h, l, c, n=ADX_N):
    up = [None] + [h[i] - h[i - 1] for i in range(1, len(c))]
    dn = [None] + [l[i - 1] - l[i] for i in range(1, len(c))]
    pdm = [None if u is None else (u if u > d and u > 0 else 0.0) for u, d in zip(up, dn)]
    ndm = [None if d is None else (d if d > u and d > 0 else 0.0) for u, d in zip(up, dn)]
    tr = [None] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    trur, sp, sn = pine_rma(tr, n), pine_rma(pdm, n), pine_rma(ndm, n)
    plus = [None if t is None or not t else 100 * a / t for a, t in zip(sp, trur)]
    minus = [None if t is None or not t else 100 * b / t for b, t in zip(sn, trur)]
    dx = [None if p is None or m is None else abs(p - m) / ((p + m) or 1) for p, m in zip(plus, minus)]
    adx = [None if x is None else 100 * x for x in pine_rma(dx, n)]
    return adx, plus, minus


# ====================================================================== сравнение
def _note(name: str, i: int, diff: float | None, tol: float) -> str:
    if diff is None:
        return "нет значения (warmup)"
    if abs(diff) <= tol:
        return "совпадает"
    if name in ("macd", "macd_signal", "macd_hist", "ema_fast", "ema_slow"):
        return "разная инициализация EMA (ядро: SMA первых n; Pine: первое значение) — затухает"
    if name in ("atr", "adx", "pdi", "ndi"):
        return "разный первый бар TR (Pine: high−low) — затухает"
    return "РАСХОЖДЕНИЕ — требует проверки"


def references(s: Series, a) -> dict:
    """Эталонные ряды по тем же свечам, ключи как в ядре."""
    h, l, c, v = s.col("h"), s.col("l"), s.col("c"), s.col("v")
    m, sg, hist = pine_macd(c)
    bu, bm, bl = pine_bb(c, a.p["bb"], a.p["bb_k"])
    adx, pdi, ndi = pine_dmi(h, l, c, a.p["adx"])
    return {"rsi": pine_rsi(c, a.p["rsi"]), "macd": m, "macd_signal": sg, "macd_hist": hist,
            "mfi": pine_mfi(h, l, c, v, a.p["mfi"]), "cmf": pine_cmf(h, l, c, v, a.p["cmf"]), "obv": pine_obv(c, v),
            "ema_fast": pine_ema(c, a.p["ema_fast"]), "ema_slow": pine_ema(c, a.p["ema_slow"]),
            "bb_upper": bu, "bb_mid": bm, "bb_lower": bl, "atr": pine_atr(h, l, c, a.p["atr"]), "adx": adx, "pdi": pdi, "ndi": ndi}


TOL = {"rsi": 1e-9, "mfi": 1e-9, "cmf": 1e-12, "obv": 1e-6, "bb_upper": 1e-12, "bb_mid": 1e-12, "bb_lower": 1e-12, "macd": 1e-12,
       "macd_signal": 1e-12, "macd_hist": 1e-12, "ema_fast": 1e-12, "ema_slow": 1e-12, "atr": 1e-12, "adx": 1e-9, "pdi": 1e-9, "ndi": 1e-9}
AUDIT_KEYS = ("rsi", "macd", "macd_signal", "macd_hist", "mfi", "cmf", "obv", "ema_fast", "ema_slow", "bb_upper", "bb_mid", "bb_lower",
              "atr", "adx", "pdi", "ndi")


def audit(a, n: int = 20, live: dict | None = None, revisions: dict | None = None) -> dict:
    """Таблицы `timestamp | close | ours | reference | diff | note` по последним n закрытым барам + строка LIVE."""
    s = a.s
    ref = references(s, a)
    c = s.col("c")
    rs_sma, rs_ema = rsi_sma(c, a.p["rsi"]), rsi_ema(c, a.p["rsi"])
    idx = list(range(max(0, len(s) - n), len(s)))
    tables, summary = {}, {}
    for k in AUDIT_KEYS:
        rows, worst = [], 0.0
        for i in idx:
            ours, r = a.v(k, i), ref[k][i]
            diff = None if ours is None or r is None else ours - r
            if diff is not None:
                worst = max(worst, abs(diff))
            row = {"ts": s.candles[i].ts, "close": c[i], "ours": ours, "reference": r, "diff": diff, "note": _note(k, i, diff, TOL[k])}
            if revisions and s.candles[i].ts in revisions:
                row["note"] += f"; источник исправил свечу после фиксации ({revisions[s.candles[i].ts]} раз)"
            if k == "rsi":
                row["rsi_sma"], row["rsi_ema"] = rs_sma[i], rs_ema[i]
            rows.append(row)
        if live and live.get("live") and live["live"].get(k) is not None:
            rows.append({"ts": live["live"]["time"], "close": None, "ours": live["live"][k], "reference": None, "diff": None,
                         "note": f"LIVE — формирующаяся свеча ({live['live']['status']}), меняется с ценой; стратегиями не используется"})
        tables[k] = rows
        bad = [r for r in rows if r["note"].startswith("РАСХОЖДЕНИЕ")]
        have = [r for r in rows if r["ours"] is not None and r["reference"] is not None]
        if not have:
            for r in rows:
                if r["ours"] is None and r["reference"] is not None:
                    r["note"] = "ядро: INSUFFICIENT_HISTORY (не показывает до минимальной истории); Pine рисует с первого бара"
        rel = max((abs(r["diff"]) / max(abs(r["reference"]), 1e-12) for r in have), default=0.0)
        summary[k] = {"max_abs_diff": worst, "max_rel_diff": rel, "rows": len(rows),
                      "status": "WARMUP" if not have else "BUG?" if bad else "OK" if worst <= TOL[k] else "METHOD"}
    return {"tf": s.tf, "bars": len(s), "last_closed_ts": s.candles[-1].ts if len(s) else None, "tables": tables, "summary": summary,
            "timestamp_convention": "время свечи = НАЧАЛО периода, UTC (как GeckoTerminal); DexScreener подписывает свечи началом периода в часовом поясе браузера",
            "value_convention": "значения на графике, в легенде, таблицах и API — по ЗАКРЫТЫМ свечам; LIVE — отдельная строка по формирующейся"}
