"""Преобразование объектов ядра (Analysis, Trader, решения) в JSON для панели.

Ничего не вычисляет «для красоты»: все числа — из Analysis, Trader и performance.summarize; интерпретации индикаторов —
детерминированные пороговые правила над этими же числами.
"""
from __future__ import annotations

from decimal import Decimal

from .. import performance, risk as R, structure
from ..analysis import INSUFFICIENT
from ..candles import TF_SEC

IND_SERIES = ("ema_fast", "ema_slow", "bb_upper", "bb_mid", "bb_lower", "macd", "macd_signal", "macd_hist",
              "mfi", "cmf", "obv", "rsi")


def _num(x):
    if isinstance(x, Decimal):
        return float(x)
    return x


def candles_payload(a) -> dict:
    ks = a.s.candles
    out = {"candles": [{"time": k.ts, "open": k.o, "high": k.h, "low": k.l, "close": k.c, "volume": k.v} for k in ks]}
    out["series"] = {n: [{"time": k.ts, "value": v} for k, v in zip(ks, a.arr[n]) if v is not None] for n in IND_SERIES}
    return out


def interpret(a, i: int) -> list[dict]:
    """Значение, статус и интерпретация каждого индикатора на баре i."""
    v = lambda n, j=i: a.v(n, j)  # noqa: E731
    c = a.s.candles[i].c
    mb = a.min_bars()
    rows = []

    def row(key, name, value, text, fmt="{:.4g}"):
        st = "OK" if value is not None else INSUFFICIENT
        rows.append({"key": key, "name": name, "value": value, "display": fmt.format(value) if value is not None else "—",
                     "status": st, "min_bars": mb.get(key), "text": text if value is not None else
                     f"нужно ≥ {mb.get(key)} закрытых баров, есть {i + 1}"})

    ef, es = v("ema_fast"), v("ema_slow")
    row("ema_fast", f"EMA{a.p['ema_fast']}", ef, f"цена {'выше' if ef and c > ef else 'ниже'} EMA{a.p['ema_fast']}" if ef else "", "{:.6g}")
    row("ema_slow", f"EMA{a.p['ema_slow']}", es, (f"EMA{a.p['ema_fast']} {'выше' if ef > es else 'ниже'} EMA{a.p['ema_slow']}"
                                                 if ef and es else f"цена {'выше' if es and c > es else 'ниже'}"), "{:.6g}")
    r = v("rsi")
    row("rsi", f"RSI{a.p['rsi']}", r, "" if r is None else "перекупленность" if r > 70 else "перепроданность" if r < 30
        else "бычья половина диапазона" if r >= 50 else "медвежья половина диапазона", "{:.1f}")
    h, hp = v("macd_hist"), v("macd_hist", i - 1)
    row("macd_hist", "MACD hist", h, "" if h is None else
        f"{'положительная' if h > 0 else 'отрицательная'}, {'растёт' if hp is not None and h > hp else 'снижается'}", "{:.3g}")
    m, sg = v("macd"), v("macd_signal")
    row("macd", "MACD / signal", m, "" if m is None or sg is None else f"MACD {'выше' if m > sg else 'ниже'} сигнальной ({sg:.3g})", "{:.3g}")
    ax, pd, nd = v("adx"), v("pdi"), v("ndi")
    row("adx", "ADX", ax, "" if ax is None else
        f"{'сильный тренд' if ax > 25 else 'формирующийся тренд' if ax >= 18 else 'тренда нет / диапазон'}; "
        f"{'+DI > −DI' if pd > nd else '−DI > +DI'} ({pd:.1f} / {nd:.1f})", "{:.1f}")
    at = v("atr")
    row("atr", "ATR", at, "" if at is None else f"{at / c * 100:.2f}% цены — типичный ход бара", "{:.3g}")
    bw = v("bb_width")
    hist = sorted(x for x in a.arr["bb_width"][max(0, i - 100):i + 1] if x is not None)
    pct = (100 * sum(1 for x in hist if x < bw) / len(hist)) if bw is not None and hist else None
    row("bb_width", "Ширина BB", bw, "" if bw is None else
        f"перцентиль {pct:.0f}% за 100 баров — {'сжатие' if pct < 20 else 'расширение' if pct > 80 else 'обычная'}", "{:.3f}")
    mf = v("mfi")
    row("mfi", f"MFI{a.p['mfi']}", mf, "" if mf is None else "перекупленность по деньгам" if mf > 80 else
        "перепроданность по деньгам" if mf < 20 else "нейтрально", "{:.1f}")
    cm = v("cmf")
    row("cmf", f"CMF{a.p['cmf']}", cm, "" if cm is None else "приток (покупатели закрывают у максимумов)" if cm > 0.05 else
        "отток (закрытия у минимумов)" if cm < -0.05 else "нейтрально", "{:.3f}")
    ob, ob5 = v("obv"), v("obv", i - 5) if i >= 5 else None
    row("obv", "OBV", ob, "" if ob is None or ob5 is None else f"за 5 баров {'растёт' if ob > ob5 else 'снижается'}", "{:.4g}")
    rv = v("relvol")
    row("relvol", "Объём / SMA20", rv, "" if rv is None else "повышенный" if rv >= 1.5 else "пониженный" if rv < 0.7 else "обычный", "{:.2f}")
    return rows


def changes(a, i: int, decisions: list) -> list[str]:
    """Что изменилось с предыдущей закрытой свечи (факты, не прогнозы)."""
    if i < 1:
        return ["предыдущей свечи нет"]
    out = []
    k, p = a.s.candles[i], a.s.candles[i - 1]
    out.append(f"close {p.c:.6g} → {k.c:.6g} ({(k.c / p.c - 1) * 100:+.2f}%)")
    for n, label in (("rsi", "RSI"), ("macd_hist", "MACD hist"), ("cmf", "CMF"), ("mfi", "MFI")):
        x, y = a.v(n, i - 1), a.v(n, i)
        if x is not None and y is not None:
            out.append(f"{label}: {x:.3g} → {y:.3g}")
    r0, r1 = a.regime(i - 1)["regime"], a.regime(i)["regime"]
    out.append(f"режим: {r0} → {r1}" if r0 != r1 else f"режим без изменений: {r1}")
    for e in a.events:
        if e["idx"] == i:
            out.append(f"структура: {e['type']} уровня {e['level']:.6g}")
    for d in a.divs:
        if d["confirmed_idx"] == i:
            out.append(f"подтверждена дивергенция {d['indicator']} {d['type']}")
    for pv in a.pivots:
        if pv.confirmed_idx == i:
            out.append(f"подтверждён swing {'high' if pv.kind == 'H' else 'low'} {pv.price:.6g}")
    if len(decisions) >= 2 and decisions[-1]["action_final"] != decisions[-2]["action_final"]:
        out.append(f"решение: {decisions[-2]['action_final']} → {decisions[-1]['action_final']}")
    return out


def structure_payload(a, i: int) -> dict:
    pv = structure.known_at(a.pivots, i)
    imp = structure.last_impulse_up(a.pivots, i)
    fib = None
    if imp:
        L, Hh = imp
        f = structure.log_fib(L.price, Hh.price)
        fib = {"low": {"time": L.ts, "price": L.price}, "high": {"time": Hh.ts, "price": Hh.price},
               "retracement": f["retracement"], "extension": f["extension"]}
    divs = [d for d in a.divs if d["confirmed_idx"] <= i]
    ks = a.s.candles
    return {
        "pivots": [p.d() for p in pv[-40:]],
        "swings": structure.swing_labels(pv)[-12:],
        "events": [{**e, "time": e["ts"]} for e in a.events if e["idx"] <= i][-30:],
        "divergences": [{**d, "confirmed_time": ks[d["confirmed_idx"]].ts if d["confirmed_idx"] < len(ks) else None} for d in divs][-20:],
        "levels": structure.sr_levels(pv),
        "fvg": [f for f in a.fvgs if f["idx"] <= i][-10:],
        "fib": fib,
        "regime": a.regime(i),
    }


def position_payload(t, mark_mid: float | None) -> dict | None:
    p = t.position
    if not p:
        return None
    qty = float(t.portfolio.qty)
    return {"position_id": p.position_id, "entry_ts": p.entry_ts, "entry_mid": p.entry_mid, "qty": qty,
            "qty_initial": float(p.qty_initial), "stop": p.stop, "initial_stop": p.initial_stop, "t1": p.t1, "t1_done": p.t1_done,
            "trail_atr": p.trail_atr, "time_stop_bars": p.time_stop, "spent": float(p.spent), "risk_usd": float(p.risk_usd),
            "exits": p.exits, "mark_mid": mark_mid,
            "unrealized_vs_entry_pct": (mark_mid / p.entry_mid - 1) * 100 if mark_mid else None}


def risk_payload(t, equity: float) -> dict:
    rs, cfg = t.risk, t.rcfg
    checks = R.entry_checks(rs, cfg, Decimal(str(equity)), 10 ** 9, None)
    return {"version": R.RISK_VERSION, "config": cfg.d(), "killed": rs.killed, "kill_reason": rs.kill_reason,
            "loss_streak": rs.loss_streak, "cooldown_until_idx": rs.cooldown_until_idx, "cooldown_reason": rs.cooldown_reason,
            "peak_equity": float(rs.peak_equity), "period_start_equity": float(rs.period_start_equity),
            "checks": [c for c in checks if "R:R" not in c["name"]], "exec_policy": R.EXEC_POLICY}


def compact_decision(d: dict) -> dict:
    return {"bar_ts": d["bar_ts"], "decided_at": d["decided_at"], "action": d["action"], "action_final": d.get("action_final"),
            "state": d.get("state"), "blocker": d["against"][0] if d.get("against") else None, "explanation": d["explanation"],
            "rr_net": (d.get("plan") or {}).get("rr_net")}


def trader_payload(t, a, mark: dict | None, bh: list | None, extra_diag: dict | None = None) -> dict:
    i = len(a.s) - 1
    sm = performance.summarize(t, bh, TF_SEC[t.tf], mark=mark) if mark and (not t.equity_curve or mark["ts"] >= t.equity_curve[-1]["ts"]) \
        else performance.summarize(t, bh, TF_SEC[t.tf])
    last = t.decisions[-1] if t.decisions else None
    return {
        "tf": t.tf, "mode": t.mode, "experiment": t.experiment, "name": t.name, "state": t.state, "paused": t.paused,
        "strategy": {"id": t.strategy["id"], "version": t.strategy["version"], "params": t.strategy["params"]},
        "summary": sm,
        "position": position_payload(t, mark["mid"] if mark else None),
        "pending": t.pending,
        "ops": t.ops, "trades": t.trades, "events": t.events[-50:],
        "decision": last,
        "decisions": [compact_decision(d) for d in t.decisions[-300:]][::-1],
        "risk": risk_payload(t, sm["equity"]),
        "equity_curve": [{"time": e["ts"], "value": float(e["equity"])} for e in t.equity_curve],
        "analysis": {"as_of_ts": a.s.candles[i].ts if len(a.s) else None, "indicators": interpret(a, i) if len(a.s) else [],
                     "changes": changes(a, i, t.decisions) if len(a.s) else [], "structure": structure_payload(a, i) if len(a.s) else {}},
        "diagnostics": {"quality": a.s.quality(), "analysis_params": a.p, "cost_model": t.cost.d(), **(extra_diag or {})},
    }
