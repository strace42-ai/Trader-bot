"""Количественный аудит S1 (1H) и S2 (4H) v1.0.0: независимые частоты условий, воронка входов, пересечения FAIL,
разбор R:R/стопов/целей и проверка логической совместимости. Стратегии вызываются КАК ЕСТЬ (код strategies.py
не меняется); аудит лишь вызывает их на каждом баре «как будто позиции нет» и дополнительно разбирает их входные данные.
Ничего не пишет в БД.
"""
from __future__ import annotations

import bisect
import math
from collections import Counter
from itertools import combinations

from .. import structure
from ..analysis import Analysis, analyze
from ..candles import TF_SEC, Series
from ..engine import Trader
from ..execution import CONSERVATIVE_V1, CostModel
from ..risk import RISK
from ..strategies import ANALYSIS_PARAMS, S1, S2, Ctx, roundtrip, rr_net, s1, s2


def htf_index(a: Analysis, htf: Analysis | None) -> list[int | None]:
    """Индекс последней ЗАКРЫТОЙ старшей свечи на момент решения по каждому бару (как в backtest.run)."""
    if htf is None:
        return [None] * len(a.s)
    close = [k.ts + TF_SEC[htf.s.tf] for k in htf.s.candles]
    out = []
    for k in a.s.candles:
        j = bisect.bisect_right(close, k.ts + TF_SEC[a.s.tf]) - 1
        out.append(j if j >= 0 else None)
    return out


def _ok(d: dict, prefix: str):
    c = next((c for c in d["conditions"] if c["name"].startswith(prefix)), None)
    return None if c is None else c["ok"]


# ============================================================================ S2
def s2_rows(a: Analysis, cost: CostModel = CONSERVATIVE_V1, i0: int = 0, i1: int | None = None) -> list[dict]:
    """По каждому бару: условия S2 (как в коде) + разбор импульса, отката и R:R."""
    P = S2["params"]
    rows = []
    k = a.s.candles
    i1 = len(a.s) if i1 is None else i1
    for i in range(max(i0, 1), i1):
        d = s2(Ctx(a, i, None, None, cost, False))
        if d["action"] == "WARMUP":
            continue
        r = {"i": i, "ts": k[i].ts, "action": d["action"], "c": k[i].c}
        r["structure"] = _ok(d, "Структура вверх")
        r["zone"] = _ok(d, "Откат в зону")
        r["not_broken"] = _ok(d, "Структура отката не сломана")
        r["impulse"] = _ok(d, "Импульс") is not False
        r["rr_ok"] = _ok(d, "Чистый R:R до T1")
        r["trigger"] = _ok(d, "Триггер")
        r["adx"] = _ok(d, "ADX")
        r["cmf"] = _ok(d, "CMF")
        r["market"] = _ok(d, "Рынок")                    # None: ETH не подключён — в коде S2 считается «не FAIL»
        # компоненты триггера и ADX отдельно
        hist, hp = a.v("macd_hist", i), a.v("macd_hist", i - 1)
        r["candle"] = k[i].c > k[i].o and k[i].c > k[i - 1].h
        r["macd_up"] = hist is not None and hp is not None and hist > hp
        adx, pdi, ndi = a.v("adx", i), a.v("pdi", i), a.v("ndi", i)
        r["adx18"] = adx is not None and adx >= P["adx_min"]
        r["di_up"] = pdi is not None and ndi is not None and pdi > ndi
        # импульс / откат / R
        imp = structure.last_impulse_up(a.pivots, i)
        r["rr_cat"] = None
        if imp:
            L, H = imp
            r["imp_age"] = i - H.idx
            r["imp_size_atr"] = (H.price - L.price) / a.v("atr", i)
            pull = [x.l for x in k[H.idx + 1:i + 1]]
            pl = min(pull) if pull else None
            r["pull_low_age"] = None if pl is None else i - (H.idx + 1 + pull.index(pl))
            r["above_H"] = max(x.h for x in k[H.idx + 1:i + 1]) > H.price if i > H.idx else False
            # события структуры после минимума отката — минимум отката мог устареть
            if pl is not None:
                pl_idx = H.idx + 1 + pull.index(pl)
                r["events_after_pull"] = [e["type"] for e in a.events if pl_idx < e["idx"] <= i]
                fib = structure.log_fib(L.price, H.price)
                r["below_786"] = pl < fib["retracement"][P["fib_invalid"]]
                atr = a.v("atr", i)
                stop = pl - P["stop_atr"] * atr
                e = k[i].c
                r["stop_atr"] = (e - stop) / atr
                r["t1_atr"] = (H.price - e) / atr
                if e >= H.price:
                    r["rr_cat"] = "entry_ge_T1"
                else:
                    g = (H.price - e) / (e - stop)
                    n = rr_net(e, stop, H.price, cost)
                    r["rr_gross"], r["rr_net"] = g, n
                    if n is not None and n >= P["min_rr_t1"]:
                        r["rr_cat"] = "ok"
                    elif g >= P["min_rr_t1"]:
                        r["rr_cat"] = "costs"                    # геометрия ≥ 1, издержки опускают ниже 1
                    elif r["stop_atr"] > 2.0 and r["t1_atr"] >= 1.0:
                        r["rr_cat"] = "stop_far"                 # стоп далеко (> 2 ATR) при нормальном расстоянии до T1
                    else:
                        r["rr_cat"] = "t1_near"                  # T1 слишком близко
                    t2 = pl * math.exp(1.618 * math.log(H.price / L.price))
                    r["rr2_net"] = rr_net(e, stop, t2, cost)
        r["stale_impulse"] = bool(imp) and (r.get("imp_age", 0) > P["choch_lookback"] or r.get("above_H") or r.get("below_786"))
        r["base"] = r["impulse"] and bool(r["zone"]) and bool(r["not_broken"])
        rows.append(r)
    return rows


S2_FUNNEL = [
    ("Бары после WARMUP", lambda r: True),
    ("Базовый сетап: импульс + откат в зону Fib/EMA20, не ниже 0,786", lambda r: r["base"]),
    ("Подтверждение ценой: бычья свеча, close > high[-1]", lambda r: r["candle"]),
    ("Структура: последнее событие вверх + CHoCH_UP ≤ 40 баров", lambda r: r["structure"]),
    ("Индикаторы: MACD hist ↑, ADX ≥ 18 и +DI > −DI, CMF ≥ −0,05", lambda r: r["macd_up"] and r["adx"] and r["cmf"]),
    ("Рынок (ETH; в backtest не подключён → не блокирует)", lambda r: r["market"] is not False),
    ("R:R до T1 ≥ 1 (net) и риск-слой: R:R плана ≥ 2", lambda r: bool(r["rr_ok"]) and (r.get("rr2_net") or 0) >= RISK["4h"].min_rr),
]

S2_CONDS = {"base": "Сетап (импульс+зона)", "candle": "Свеча-подтверждение", "structure": "Структура", "macd_up": "MACD hist ↑",
            "adx18": "ADX ≥ 18", "di_up": "+DI > −DI", "cmf": "CMF ≥ −0,05", "rr_ok": "R:R T1 ≥ 1"}


# ============================================================================ S1
def s1_rows(a: Analysis, htf: Analysis, cost: CostModel = CONSERVATIVE_V1, i0: int = 0, i1: int | None = None) -> list[dict]:
    P = S1["params"]
    jj = htf_index(a, htf)
    rows = []
    k = a.s.candles
    i1 = len(a.s) if i1 is None else i1
    for i in range(i0, i1):
        d = s1(Ctx(a, i, htf, jj[i], cost, False))
        if d["action"] == "WARMUP":
            continue
        r = {"i": i, "ts": k[i].ts, "action": d["action"], "c": k[i].c}
        r["sweep"] = bool(_ok(d, "Liquidity sweep"))
        r["confirm"] = _ok(d, "Подтверждение: закрытие")
        r["flow"] = _ok(d, "Подтверждение денежным")
        r["htf"] = _ok(d, "Контекст старшего")
        r["rsi"] = _ok(d, "RSI")
        r["bbw"] = _ok(d, "Ширина BB")
        r["target"] = _ok(d, "Цель")
        # независимые версии условий, зависящих от sweep в коде (для частоты на произвольных барах)
        cmf, o, o5 = a.v("cmf", i), a.v("obv", i), a.v("obv", i - 5)
        r["flow_any"] = (cmf is not None and cmf > 0) or (o is not None and o5 is not None and o > o5)
        # «сырой» sweep без требования объёма
        sw = None
        for kk in range(i - 1, i - 1 - P["confirm_bars"], -1):
            sw = structure.liquidity_sweep_low(a.s, kk, a.pivots, a.v("atr", kk - 1), P["sweep_tol_atr"])
            if sw:
                break
        r["sweep_novol"] = sw is not None
        plan = d["plan"]
        r["rr_t1"] = plan["rr_net"] if plan else None
        r["risk_rr"] = plan is not None and plan["rr_net"] is not None and plan["rr_net"] >= RISK["1h"].min_rr
        if plan:
            atr = a.v("atr", i)
            r["stop_atr"] = (plan["entry_ref"] - plan["stop"]) / atr
            r["t1_atr"] = (plan["t1"] - plan["entry_ref"]) / atr if plan["t1"] else None
            r["t1_pct"] = (plan["t1"] / plan["entry_ref"] - 1) * 100 if plan["t1"] else None
        rows.append(r)
    return rows


S1_FUNNEL = [
    ("Бары после WARMUP", lambda r: True),
    ("Базовый сетап: sweep swing low с объёмом ≥ 1,3× за 3 бара", lambda r: r["sweep"]),
    ("Подтверждение ценой: close > high sweep-свечи", lambda r: bool(r["confirm"])),
    ("Структура / волатильность: ширина BB не в нижних 10%", lambda r: bool(r["bbw"])),
    ("Индикаторы: денежный поток (див./CMF/OBV), RSI ≤ 70", lambda r: bool(r["flow"]) and bool(r["rsi"])),
    ("Рыночный контекст: 4H не сильный downtrend, RSI 4H > 35", lambda r: bool(r["htf"])),
    ("Цель ≥ 2 ATR и ≥ 3× издержек; риск-слой: R:R до T1 ≥ 2", lambda r: bool(r["target"]) and r["risk_rr"]),
]

S1_CONDS = {"sweep": "Sweep+объём", "confirm": "Close > high sweep", "bbw": "BB width", "flow": "Денежный поток", "rsi": "RSI ≤ 70",
            "htf": "Контекст 4H", "target": "Цель ≥2ATR/3×изд.", "risk_rr": "R:R T1 ≥ 2 (риск)"}


# ============================================================================ общие расчёты
def funnel(rows: list[dict], stages) -> list[dict]:
    """Последовательная воронка + независимая частота PASS каждого этапа + условная доля отказа на этапе."""
    out, cur = [], rows
    n = len(rows)
    for name, f in stages:
        nxt = [r for r in cur if f(r)]
        indep = sum(1 for r in rows if f(r))
        out.append({"stage": name, "remain": len(nxt), "indep_pass": indep, "indep_pct": 100 * indep / n if n else 0,
                    "cond_reject_pct": 100 * (1 - len(nxt) / len(cur)) if cur else None, "entered": len(cur)})
        cur = nxt
    return out


def fail_intersections(rows: list[dict], conds: dict, base_key: str) -> dict:
    """Среди баров с базовым сетапом: частота FAIL, единственный блокер, попарные совместные FAIL, число FAIL на бар."""
    base = [r for r in rows if r[base_key]]
    keys = [k for k in conds if k != base_key]
    fails = {k: sum(1 for r in base if not r.get(k)) for k in keys}
    sole = Counter()
    nfail = Counter()
    for r in base:
        f = [k for k in keys if not r.get(k)]
        nfail[len(f)] += 1
        if len(f) == 1:
            sole[f[0]] += 1
    pair = {f"{conds[x]} & {conds[y]}": sum(1 for r in base if not r.get(x) and not r.get(y)) for x, y in combinations(keys, 2)}
    copass = {f"{conds[x]} & {conds[y]}": sum(1 for r in base if r.get(x) and r.get(y)) for x, y in combinations(keys, 2)}
    return {"base_bars": len(base), "fail": {conds[k]: v for k, v in fails.items()}, "sole_blocker": {conds[k]: v for k, v in sole.items()},
            "n_fail_hist": dict(sorted(nfail.items())), "pair_fail": pair, "pair_pass": copass,
            "all_pass": nfail.get(0, 0)}


def s2_r_breakdown(rows: list[dict]) -> dict:
    with_r = [r for r in rows if r["rr_cat"] is not None]
    cand = [r for r in with_r if r["base"] and r["candle"]]
    cnt = lambda xs: dict(Counter(r["rr_cat"] for r in xs))  # noqa: E731
    stale = sum(1 for r in rows if r["stale_impulse"])
    no_imp = sum(1 for r in rows if not r["impulse"])
    # минимум отката «устарел»: (а) ушёл ниже 0,786 → импульс мёртв, но после него структура снова развернулась вверх;
    # (б) условие зоны выполнено по минимуму, случившемуся > 10 баров назад (откат давно закончился).
    pull_stale = [r for r in with_r if r.get("below_786") and any(e.endswith("UP") for e in r.get("events_after_pull", []))]
    zone_old = [r for r in with_r if r["zone"] and (r.get("pull_low_age") or 0) > 10]
    return {"bars": len(rows), "no_impulse": no_imp, "stale_impulse": stale, "valid_setup": sum(1 for r in rows if r["base"]),
            "r_all_bars": cnt(with_r), "r_at_candidates": cnt(cand), "candidates": len(cand),
            "dead_impulse_after_up_event": len(pull_stale), "zone_by_old_low": len(zone_old),
            "zone_by_old_low_at_trigger": sum(1 for r in zone_old if r["candle"]),
            "rr_at_trigger_by_age": {"≤10": dict(Counter(r["rr_cat"] for r in cand if (r.get("pull_low_age") or 0) <= 10)),
                                     ">10": dict(Counter(r["rr_cat"] for r in cand if (r.get("pull_low_age") or 0) > 10))},
            "below_786": sum(1 for r in with_r if r.get("below_786")),
            "above_H": sum(1 for r in with_r if r.get("above_H"))}


def run_engine(a: Analysis, tf: str, htf: Analysis | None = None, fn=None, strategy=None, cost: CostModel = CONSERVATIVE_V1,
               risk_cfg=None, i0: int = 0, i1: int | None = None, tag: str = "AUDIT") -> Trader:
    """Пошаговый прогон через реальный engine (исполнение по open следующей свечи) — в памяти."""
    t = Trader(tf, "BACKTEST", f"RESEARCH-{tag}", cost, "1000", risk_cfg=risk_cfg)
    if fn is not None:
        t.fn = fn
    if strategy is not None:
        t.strategy = strategy
    jj = htf_index(a, htf)
    i1 = len(a.s) if i1 is None else i1
    for i in range(i0, i1):
        t.process_bar(a, i, htf, jj[i])
    return t


def audit_s2(s4: Series, cost: CostModel = CONSERVATIVE_V1) -> dict:
    a = analyze(s4, ANALYSIS_PARAMS["4h"])
    rows = s2_rows(a, cost)
    t = run_engine(a, "4h", cost=cost)
    return {"rows": rows, "funnel": funnel(rows, S2_FUNNEL), "fails": fail_intersections(rows, S2_CONDS, "base"),
            "r": s2_r_breakdown(rows), "enters": sum(1 for d in t.decisions if d["action_final"] == "ENTER"), "trades": t.trades,
            "would_enter": sum(1 for r in rows if r["action"] == "ENTER")}


def audit_s1(s1h: Series, s4: Series, cost: CostModel = CONSERVATIVE_V1) -> dict:
    a, h = analyze(s1h, ANALYSIS_PARAMS["1h"]), analyze(s4, ANALYSIS_PARAMS["4h"])
    rows = s1_rows(a, h, cost)
    t = run_engine(a, "1h", h, cost=cost)
    return {"rows": rows, "funnel": funnel(rows, S1_FUNNEL), "fails": fail_intersections(rows, S1_CONDS, "sweep"),
            "enters": sum(1 for d in t.decisions if d["action_final"] == "ENTER"), "trades": t.trades,
            "would_enter": sum(1 for r in rows if r["action"] == "ENTER")}


def combine(parts: list[dict]) -> dict:
    """Суммирование воронок/пересечений по нескольким рядам."""
    f0 = parts[0]["funnel"]
    fun = []
    for k, st in enumerate(f0):
        remain = sum(p["funnel"][k]["remain"] for p in parts)
        entered = sum(p["funnel"][k]["entered"] for p in parts)
        indep = sum(p["funnel"][k]["indep_pass"] for p in parts)
        tot = sum(p["funnel"][0]["remain"] for p in parts)
        fun.append({"stage": st["stage"], "remain": remain, "entered": entered, "indep_pass": indep, "indep_pct": 100 * indep / tot,
                    "cond_reject_pct": 100 * (1 - remain / entered) if entered else None})
    fl = {}
    for key in ("fail", "sole_blocker", "pair_fail", "pair_pass", "n_fail_hist"):
        c = Counter()
        for p in parts:
            c.update(p["fails"][key])
        fl[key] = dict(c)
    fl["base_bars"] = sum(p["fails"]["base_bars"] for p in parts)
    fl["all_pass"] = sum(p["fails"]["all_pass"] for p in parts)
    return {"funnel": fun, "fails": fl, "enters": sum(p["enters"] for p in parts), "trades": sum(len(p["trades"]) for p in parts),
            "would_enter": sum(p["would_enter"] for p in parts)}
