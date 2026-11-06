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
    from .. import structure_v2
    v2 = structure_v2.analyze_structure(a)
    st_v2 = v2["state"][i] if v2["state"] else {"trend": "none", "protected": None}
    last_ext = [e for e in v2["external"] if e["idx"] <= i]
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
        "v2": {"version": v2["version"], "k_atr": v2["k_atr"],
               "external": [e for e in v2["external"] if e["idx"] <= i][-40:],
               "internal": [e for e in v2["internal"] if e["idx"] <= i][-60:],
               "swings": [sw for sw in v2["swings"] if sw["confirmed_idx"] <= i][-30:],
               "trend": st_v2["trend"], "protected": st_v2["protected"],
               "protected_src": last_ext[-1]["protected_src"] if last_ext else None,
               "note": "BOS/CHoCH — внешняя swing-структура v2, ИССЛЕДОВАНИЕ: торговыми стратегиями не используется; k — исследовательский параметр, не окончательный. iBOS/iCHoCH — внутренняя структура v1, именно её использует S2 v1.0.0."},
    }


HTF_OF = {"1h": "4h", "4h": "1d", "1d": None}


def structure_v3_payload(s, htf_s=None) -> dict:
    """Структура v3 (исследование и визуализация; стратегиями не используется). s — закрытые свечи ТФ,
    htf_s — закрытые свечи старшего ТФ (контекст: при UNDEFINED подтверждения не даёт)."""
    from .. import structure_v3 as S3
    if s is None or not len(s):
        return {"version": S3.VERSION, "empty": True}
    e = S3.Engine(s.tf).run(s)
    r = e.result()
    st = e.states()
    tl, prev = [], None
    for j, x in enumerate(st):
        key = (x["major"]["trend"], x["major"]["bias"], x["major"]["phase"], x["internal"], x["conflict"])
        if key != prev:
            tl.append({"ts": e.ts[j], "major": x["major"]["trend"], "bias": x["major"]["bias"], "phase": x["major"]["phase"],
                       "internal": x["internal"], "senior": x["senior"], "conflict": x["conflict"]})
            prev = key
    htf = None
    if htf_s is not None:
        he = S3.Engine(htf_s.tf).run(htf_s) if len(htf_s) else None
        hst = he.major.states[-1] if he and he.major.states else None
        hdir = hst["trend"] if hst and hst["trend"] in ("UP", "DOWN") else None
        m = r["major"]["state"] or {}
        mdir = m.get("trend") if m.get("trend") in ("UP", "DOWN") else (m.get("bias") or "").upper() or None
        htf = {"tf": htf_s.tf, "bars": len(htf_s), "state": hst,
               "conflict": bool(hdir and mdir and hdir != mdir),
               "text": ("старший ТФ не определён (UNDEFINED/TRANSITION) — подтверждения не даёт" if not hdir else
                        f"{htf_s.tf.upper()} {hdir}" + (f", protected {'high' if hdir == 'DOWN' else 'low'} "
                        f"{(hst['ph'] if hdir == 'DOWN' else hst['pl']):.6g}" if (hst['ph'] if hdir == 'DOWN' else hst['pl']) else "")
                        + (f" — HTF_CONFLICT с major {s.tf.upper()} {mdir}" if mdir and hdir != mdir else ""))}
    m = r["major"]
    rng = {"top": m["state"]["ph"] if m["state"] and m["state"]["ph"] else None,
           "bottom": m["state"]["pl"] if m["state"] and m["state"]["pl"] else None}
    for kind in ("H", "L"):
        t = e.major.target(e, kind, len(e.ts))
        if t:
            rng["structural_high" if kind == "H" else "structural_low"] = t
    return {**r, "timeline": tl, "htf": htf, "range": rng,
            "note": "Структура v3 — ИССЛЕДОВАНИЕ и визуализация: торговыми стратегиями не используется. Major: правила S1/S2 "
                    "(вложенность в major-диапазон; protected — экстремум между пробитой major-границей и пробоем); нарушение "
                    "protected — по тени (CHoCH ≠ подтверждение). SCALE_CONFLICT — расхождение со старшим масштабом "
                    f"(k={e.k['senior']}·ATR), HTF_CONFLICT — со старшим ТФ; конфликты показываются, но не разрешаются. "
                    f"k_major={e.k['major']} и k_int={e.k['internal']} — исследовательские параметры."}


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


# ====================================================================== FORWARD (этап E)
def next_close(tf: str, now: int) -> int:
    d = TF_SEC[tf]
    return (now // d + 1) * d


def best_strategy(traders: dict) -> dict:
    if all(v["closed_trades"] == 0 for v in traders.values()):
        return {"by_return": None, "by_risk": None, "note": "сделок нет ни у одной стратегии — сравнивать нечего"}
    by_ret = max(traders, key=lambda k: traders[k]["net_return_pct"])
    by_risk = max(traders, key=lambda k: traders[k]["net_return_pct"] / max(traders[k]["max_drawdown_pct"], 1.0))
    return {"by_return": by_ret, "by_risk": by_risk, "note": "доходность / max(просадка, 1%)"}


def rejection_stats(decisions: list[dict]) -> dict:
    """Какие условия чаще всего мешают войти: по всем отклонённым решениям (WAIT/NO_TRADE), а не только по первому блокеру."""
    rej = [d for d in decisions if d.get("action_final") in ("WAIT", "NO_TRADE", "WARMUP", "SKIPPED")]
    any_fail, first, risk = {}, {}, {}
    for d in rej:
        fails = [c["name"] for c in d.get("conditions") or [] if c.get("ok") is False]
        for n in fails:
            any_fail[n] = any_fail.get(n, 0) + 1
        if fails:
            first[fails[0]] = first.get(fails[0], 0) + 1
        for c in d.get("risk") or []:
            if c.get("ok") is False:
                risk[c["name"]] = risk.get(c["name"], 0) + 1
    srt = lambda m: [{"name": k, "count": v, "pct": v / len(rej) * 100 if rej else 0} for k, v in sorted(m.items(), key=lambda x: -x[1])]  # noqa: E731
    by_action = {}
    for d in decisions:
        by_action[d.get("action_final")] = by_action.get(d.get("action_final"), 0) + 1
    return {"decisions": len(decisions), "rejected": len(rej), "by_action": by_action,
            "any_fail": srt(any_fail), "first_blocker": srt(first), "risk_fail": srt(risk)}


def analysis_only(a) -> dict:
    i = len(a.s) - 1
    return {"as_of_ts": a.s.candles[i].ts, "indicators": interpret(a, i), "changes": changes(a, i, []), "structure": structure_payload(a, i)}


def sources(live: dict) -> list[dict]:
    names = {"geckoterminal": "GeckoTerminal OHLCV", "dexscreener": "DexScreener", "rpc": "Robinhood RPC", "coingecko": "CoinGecko BTC/ETH"}
    out = []
    h = live.get("health") or {}
    for k, n in names.items():
        x = h.get(k)
        if not x:
            out.append({"key": k, "name": n, "status": "NO DATA", "ts": None, "note": "запросов ещё не было (бот не запущен?)"})
            continue
        st = "OK" if x.get("consecutive_fail", 0) == 0 else "DEGRADED" if x.get("consecutive_fail", 0) < 3 else "DOWN"
        out.append({"key": k, "name": n, "status": st, "ts": x.get("last_ok"), "age": x.get("age"), "latency_ms": x.get("latency_ms"),
                    "note": x.get("last_error") or f"вызовов {x.get('calls')}, ошибок {x.get('errors')}"})
    return out


def forward_totals(r, marks: list[dict], live: dict) -> dict:
    last = marks[-1] if marks else None
    init = sum(float(t.portfolio.initial) for t in r.traders.values()) or 3000.0
    if not last:
        return {"initial_total": init, "total_equity": None, "net_pnl": None, "bh_total": None, "mark_ts": None,
                "note": "оценок ещё нет: ждём первую проверенную котировку после START"}
    return {"initial_total": init, "total_equity": last["total"], "net_pnl": last["total"] - init,
            "net_return_pct": (last["total"] / init - 1) * 100, "bh_total": last.get("bh_total"),
            "vs_bh_usd": last["total"] - last["bh_total"] if last.get("bh_total") else None,
            "bh_return_pct": (last["bh_total"] / init - 1) * 100 if last.get("bh_total") else None,
            "mark_ts": last["ts"], "mark_mid": last["mid"], "mark_source": last["source"],
            "traders": {tf: v["equity"] for tf, v in last["traders"].items()}}


def forward_performance(r, marks: list[dict]) -> dict:
    """Кривые и метрики FORWARD на ОБЩИХ timestamps синхронных оценок (fw_marks): один момент, одна котировка, одна модель."""
    curves = {tf: [{"time": m["ts"], "value": m["traders"][tf]["equity"]} for m in marks if tf in m["traders"]] for tf in r.traders}
    bh = [{"time": m["ts"], "value": m["bh"]["equity"]} for m in marks if m.get("bh")]
    table = {}
    last = marks[-1] if marks else None
    for tf, t in r.traders.items():
        eq = [p["value"] for p in curves[tf]]
        mark = {"ts": last["ts"], "mid": last["mid"], "source": last["source"]} if last and (not t.equity_curve or last["ts"] >= t.equity_curve[-1]["ts"]) else None
        sm = performance.summarize(t, None, TF_SEC[tf], mark=mark) if mark else performance.summarize(t, None, TF_SEC[tf])
        in_pos = [m for m in marks if m["traders"].get(tf, {}).get("qty", 0) > 0]
        b = last["bh"]["equity"] if last and last.get("bh") else None
        table[tf] = {**{k: sm[k] for k in ("equity", "net_pnl", "net_return_pct", "realized_pnl", "unrealized_pnl", "costs_total", "fees",
                                          "closed_trades", "operations", "win_rate", "profit_factor", "expectancy_usd", "expectancy_r",
                                          "avg_holding_h", "max_loss_streak", "slippage_bps_mean", "sample", "strategy", "version")},
                     "max_drawdown_pct": performance.max_drawdown(eq) if eq else 0.0,
                     "time_in_fables_pct": len(in_pos) / len(marks) * 100 if marks else 0.0,
                     "bh_equity": b, "vs_bh_usd": sm["equity"] - b if b else None, "vs_bh_ratio": sm["equity"] / b if b else None}
    bh_eq = [p["value"] for p in bh]
    return {"curves": curves, "bh": bh, "table": table, "marks": len(marks),
            "bh_summary": {"equity": bh_eq[-1], "net_pnl": bh_eq[-1] - 1000, "max_drawdown_pct": performance.max_drawdown(bh_eq)} if bh_eq else None,
            "mark": {k: last[k] for k in ("ts", "mid", "source")} if last else None}



def add_live_column(rows: list[dict], live: dict | None) -> None:
    """Колонка LIVE (формирующаяся свеча) рядом со значением закрытой свечи — тот же формат."""
    fmt = {"ema_fast": "{:.6g}", "ema_slow": "{:.6g}", "rsi": "{:.1f}", "macd_hist": "{:.3g}", "macd": "{:.3g}", "adx": "{:.1f}",
           "atr": "{:.3g}", "bb_width": "{:.3f}", "mfi": "{:.1f}", "cmf": "{:.3f}", "obv": "{:.4g}", "relvol": "{:.2f}"}
    for r in rows:
        v = (live or {}).get(r["key"])
        r["live"] = v
        r["live_display"] = fmt.get(r["key"], "{:.4g}").format(v) if v is not None else "—"
