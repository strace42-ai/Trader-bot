"""Альтернативные торговые подходы — ИССЛЕДОВАНИЕ (не зарегистрированы в STRATEGIES, в FORWARD не используются).

Правила заданы ЗАРАНЕЕ, одинаковы для 1H и 4H (все расстояния — в ATR(14), окна — в барах) и не подбирались
по истории. Каждое решение строится только по данным, известным на закрытии бара i; исполнение — engine, по open
следующей свечи, с моделью издержек.

Общие ОБЯЗАТЕЛЬНЫЕ правила безопасности (FAIL любого → нет входа):
  S-1 стоп определён до входа и лежит в 0,5…3,0 ATR от close;
  S-2 расстояние до T1 ≥ 3× круга издержек (комиссии + проскальзывание);
  S-3 чистый R:R до T1 (с издержками и проскальзыванием стопа) ≥ 1,2 — проверяется риск-слоем (risk-v1, min_rr = 1,2);
  S-4 остальные лимиты risk-v1 без изменений: риск на сделку, экспозиция, лимит убытка периода, просадка/kill switch,
      cooldown, аномальные свечи, качество данных.
ФИЛЬТРЫ КАЧЕСТВА — взвешенное подтверждение: 4 независимых признака с весом 1, нужно ≥ 2 (порог задан заранее).

Выход (все три): T1 = entry + 2R (продажа 50%), затем trailing; time stop; стратегический выход по своему правилу.
"""
from __future__ import annotations

from dataclasses import replace

from .. import structure_v2 as SV2
from ..analysis import Analysis
from ..risk import RISK
from ..strategies import C, Ctx, decision, roundtrip

Q_MIN = 2                    # минимум признаков качества из 4
R_T1 = 2.0                   # T1 = entry + 2R
STOP_MIN, STOP_MAX = 0.5, 3.0
MIN_RR = 1.2


def risk_cfg(tf: str):
    return replace(RISK[tf], min_rr=MIN_RR)


def ctx_cache(a: Analysis) -> dict:
    """Внешняя структура v2 (контекст) и внутренние события v1 по барам — один раз на Analysis."""
    if getattr(a, "_alt_cache", None) is None:          # кэш хранится на самом объекте (id() переиспользуется после GC)
        v2 = SV2.analyze_structure(a)
        by_idx: dict[int, list] = {}
        for e in a.events:
            by_idx.setdefault(e["idx"], []).append(e["type"])
        last_int = []
        cur = None
        for i in range(len(a.s)):
            for t in by_idx.get(i, []):
                cur = (t, i)
            last_int.append(cur)
        ext_by_idx = {e["idx"]: e["type"] for e in v2["external"]}
        a._alt_cache = {"state": v2["state"], "int_at": by_idx, "last_int": last_int, "ext_at": ext_by_idx}
    return a._alt_cache


def _plan(ctx: Ctx, stop: float, t_mult: float, trail: float, trail_ref: str, tstop: int) -> tuple[dict, list[dict]]:
    a, i = ctx.a, ctx.i
    e, atr = a.s.candles[i].c, a.v("atr", i)
    risk = e - stop
    t1 = e + R_T1 * risk
    from ..strategies import rr_net
    rr = rr_net(e, stop, t1, ctx.cost)
    dist = risk / atr if atr else None
    rt = roundtrip(ctx.cost)
    safety = [
        C("S-1 Стоп 0,5–3 ATR", dist is not None and STOP_MIN <= dist <= STOP_MAX, f"стоп {stop:.6g}, {dist:.2f} ATR" if dist else "нет ATR",
          f"{dist:.2f} ATR" if dist else "н/д", f"{STOP_MIN}–{STOP_MAX} ATR"),
        C("S-2 Цель ≥ 3× издержек", (t1 / e - 1) >= 3 * rt, f"T1 +{(t1 / e - 1) * 100:.2f}% vs 3×{rt * 100:.2f}%",
          f"+{(t1 / e - 1) * 100:.2f}%", f"≥ {3 * rt * 100:.2f}%"),
        C("S-3 Чистый R:R до T1 ≥ 1,2", rr is not None and rr >= MIN_RR, f"R:R {rr:.2f}" if rr is not None else "н/д",
          f"{rr:.2f}" if rr is not None else "н/д", f"≥ {MIN_RR}"),
    ]
    plan = {"entry_ref": e, "stop": stop, "t1": t1, "t1_frac": 0.5, "trail_atr": trail, "trail_ref": trail_ref,
            "time_stop": tstop, "rr_net": rr}
    return plan, safety


def _quality(items: list[tuple[str, bool, str]]) -> dict:
    n = sum(1 for _, ok, _ in items if ok)
    return C(f"Качество: ≥ {Q_MIN} из {len(items)}", n >= Q_MIN, "; ".join(f"{'✓' if ok else '✗'} {nm} ({d})" for nm, ok, d in items),
             f"{n} из {len(items)}", f"≥ {Q_MIN}")


def _finish(meta: dict, ctx: Ctx, conds: list[dict], plan, inputs) -> dict:
    ok = all(c["ok"] for c in conds) and plan is not None
    bad = next((c["name"] for c in conds if not c["ok"]), "")
    return decision(meta, ctx, "ENTER" if ok else "WAIT", conds, plan if ok else plan, inputs, "" if ok else f"блокирует: {bad}")


def _warm(meta, ctx, need=60):
    if ctx.i < need or ctx.a.v("atr", ctx.i) is None or ctx.a.v("ema_slow", ctx.i) is None or ctx.a.v("adx", ctx.i) is None:
        return decision(meta, ctx, "WARMUP", [C("История", False, f"{ctx.i + 1} из {need}", f"{ctx.i + 1}", f"≥ {need}")], None, {}, "")
    return None


# ------------------------------------------------------------------ A. Разворот: internal CHoCH после свежего минимума
REV = {"id": "R-A-reversal-internal-choch", "version": "research-0.1", "tf": "*", "htf": None,
       "params": {"fall_atr": 4.0, "fresh_low": 15, "low_window": 50, "high_window": 30, "stop_atr": 0.25, "trail_atr": 2.5,
                  "time_stop": 30}}


def rev(ctx: Ctx) -> dict:
    P, a, i = REV["params"], ctx.a, ctx.i
    w = _warm(REV, ctx)
    if w:
        return w
    cc = ctx_cache(a)
    k = a.s.candles
    atr = a.v("atr", i)
    inputs = {x: a.v(x, i) for x in ("rsi", "cmf", "obv", "atr", "relvol")}
    if ctx.in_position:
        bad = "CHoCH_DOWN" in cc["int_at"].get(i, []) or "BOS_DOWN" in cc["int_at"].get(i, [])
        c = C("Внутренний слом вниз", bad, "iCHoCH/iBOS вниз → разворот не состоялся" if bad else "нет", "да" if bad else "нет", "→ выход")
        return decision(REV, ctx, "EXIT" if bad else "HOLD", [c], None, inputs, "")
    lo_i = min(range(i - P["fresh_low"] + 1, i + 1), key=lambda x: k[x].l)
    lo = k[lo_i].l
    fresh = lo <= min(x.l for x in k[i - P["low_window"] + 1:i + 1])
    hi = max(x.h for x in k[i - P["high_window"] + 1:i + 1])
    st = cc["state"][i]["trend"]
    conds = [
        C("Контекст: падение ≥ 4 ATR к свежему 50-барному минимуму", fresh and (hi - lo) / atr >= P["fall_atr"],
          f"high {hi:.6g} → low {lo:.6g} = {(hi - lo) / atr:.1f} ATR; минимум {'свежий' if fresh else 'не свежий'}",
          f"{(hi - lo) / atr:.1f} ATR", "≥ 4 ATR, минимум = минимум 50 баров"),
        C("Контекст: внешняя структура v2 не вверх", st != "up", f"внешний тренд {st}", st, "down/none"),
        C("Триггер: internal CHoCH_UP на этом баре", "CHoCH_UP" in cc["int_at"].get(i, []), "внутренняя структура развернулась вверх"
          if "CHoCH_UP" in cc["int_at"].get(i, []) else "нет", "iCHoCH_UP" if "CHoCH_UP" in cc["int_at"].get(i, []) else "нет", "iCHoCH_UP"),
    ]
    rmin = min((x for x in a.arr["rsi"][i - 9:i + 1] if x is not None), default=None)
    divs = [d for d in a.divs if d["confirmed_idx"] <= i and d["type"] == "regular_bullish" and i - d["p2"]["idx"] <= 10]
    cmf, o, o5, rv = a.v("cmf", i), a.v("obv", i), a.v("obv", i - 5), a.v("relvol", i)
    conds.append(_quality([("RSI был < 35 за 10 баров", rmin is not None and rmin < 35, f"min {rmin:.1f}" if rmin else "н/д"),
                           ("бычья дивергенция ≤ 10 баров", bool(divs), divs[-1]["indicator"] if divs else "нет"),
                           ("CMF > 0 или OBV ↑ 5 баров", (cmf or 0) > 0 or (o is not None and o5 is not None and o > o5), f"CMF {cmf:.3f}" if cmf is not None else "н/д"),
                           ("объём ≥ 1,2× SMA20", (rv or 0) >= 1.2, f"{rv:.2f}×" if rv else "н/д")]))
    plan, safety = _plan(ctx, lo - P["stop_atr"] * atr, R_T1, P["trail_atr"], "close", P["time_stop"])
    return _finish(REV, ctx, conds + safety, plan, inputs)


# ------------------------------------------------------------------ B. Продолжение тренда: откат к EMA20 во внешнем аптренде
CONT = {"id": "R-B-trend-continuation", "version": "research-0.1", "tf": "*", "htf": None,
        "params": {"pb_bars": 6, "pb_min_atr": 1.0, "ema_touch_atr": 0.25, "stop_atr": 0.3, "trail_atr": 3.0, "time_stop": 40}}


def cont(ctx: Ctx) -> dict:
    P, a, i = CONT["params"], ctx.a, ctx.i
    w = _warm(CONT, ctx)
    if w:
        return w
    cc = ctx_cache(a)
    k = a.s.candles
    atr, ef, es = a.v("atr", i), a.v("ema_fast", i), a.v("ema_slow", i)
    inputs = {x: a.v(x, i) for x in ("ema_fast", "ema_slow", "adx", "pdi", "ndi", "macd_hist", "cmf", "rsi", "atr")}
    stt = cc["state"][i]
    if ctx.in_position:
        bad = cc["ext_at"].get(i) == "CHoCH_DOWN"
        c = C("Внешний CHoCH_DOWN", bad, "внешний тренд сломан" if bad else "нет", "да" if bad else "нет", "→ выход")
        return decision(CONT, ctx, "EXIT" if bad else "HOLD", [c], None, inputs, "")
    pb = k[i - P["pb_bars"] + 1:i + 1]
    pb_low = min(x.l for x in pb)
    hi20 = max(x.h for x in k[i - 19:i + 1])
    conds = [
        C("Контекст: внешний тренд v2 вверх, close > protected low", stt["trend"] == "up" and stt["protected"] is not None and k[i].c > stt["protected"],
          f"тренд {stt['trend']}, protected {stt['protected']}", stt["trend"], "up и close > protected"),
        C("Контекст: EMA20 > EMA50", ef > es, f"EMA20 {ef:.6g}, EMA50 {es:.6g}", f"{ef:.6g}/{es:.6g}", "EMA20 > EMA50"),
        C("Сетап: откат ≥ 1 ATR от 20-барного максимума к EMA20", (hi20 - pb_low) / atr >= P["pb_min_atr"] and pb_low <= ef + P["ema_touch_atr"] * atr,
          f"откат {(hi20 - pb_low) / atr:.1f} ATR, минимум {pb_low:.6g} vs EMA20+0,25ATR {ef + P['ema_touch_atr'] * atr:.6g}",
          f"{(hi20 - pb_low) / atr:.1f} ATR", "≥ 1 ATR и касание EMA20"),
        C("Триггер: бычья свеча, close > high[-1] и > EMA20", k[i].c > k[i].o and k[i].c > k[i - 1].h and k[i].c > ef,
          f"close {k[i].c:.6g}, high[-1] {k[i - 1].h:.6g}", f"{k[i].c:.6g}", "> high[-1], > open, > EMA20"),
    ]
    adx, pdi, ndi, h0, h1 = a.v("adx", i), a.v("pdi", i), a.v("ndi", i), a.v("macd_hist", i), a.v("macd_hist", i - 1)
    cmf, rsi = a.v("cmf", i), a.v("rsi", i)
    conds.append(_quality([("ADX ≥ 20 и +DI > −DI", adx >= 20 and pdi > ndi, f"ADX {adx:.1f}"),
                           ("MACD hist растёт", h0 is not None and h1 is not None and h0 > h1, "↑" if (h0 or 0) > (h1 or 0) else "↓"),
                           ("CMF > 0", (cmf or 0) > 0, f"{cmf:.3f}" if cmf is not None else "н/д"),
                           ("RSI 45–70", rsi is not None and 45 <= rsi <= 70, f"{rsi:.1f}" if rsi else "н/д")]))
    plan, safety = _plan(ctx, pb_low - P["stop_atr"] * atr, R_T1, P["trail_atr"], "high", P["time_stop"])
    return _finish(CONT, ctx, conds + safety, plan, inputs)


# ------------------------------------------------------------------ C. Раннее подтверждённое восстановление после падения
RECOV = {"id": "R-C-confirmed-recovery", "version": "research-0.1", "tf": "*", "htf": None,
         "params": {"window": 60, "fall_atr": 6.0, "bottom_age": 25, "stop_atr": 0.3, "trail_atr": 2.5, "time_stop": 30}}


def recov(ctx: Ctx) -> dict:
    P, a, i = RECOV["params"], ctx.a, ctx.i
    w = _warm(RECOV, ctx)
    if w:
        return w
    cc = ctx_cache(a)
    k = a.s.candles
    atr, ef = a.v("atr", i), a.v("ema_fast", i)
    inputs = {x: a.v(x, i) for x in ("ema_fast", "rsi", "macd", "macd_signal", "cmf", "obv", "atr")}
    W = P["window"]
    b = min(range(i - W + 1, i + 1), key=lambda x: k[x].l)
    top = max(range(i - W + 1, b + 1), key=lambda x: k[x].h)
    fall = (k[top].h - k[b].l) / atr
    hl = [p for p in a.pivots if p.kind == "L" and p.idx > b and p.confirmed_idx <= i and p.price > k[b].l]
    if ctx.in_position:
        return decision(RECOV, ctx, "HOLD", [C("Позиция", True, "стоп под HL / T1 / trailing / time stop в engine", "LONG", "")], None, inputs, "")
    li = cc["last_int"][i]
    conds = [
        C("Контекст: падение ≥ 6 ATR, дно ≤ 25 баров назад", fall >= P["fall_atr"] and i - b <= P["bottom_age"] and b > top,
          f"{k[top].h:.6g} → {k[b].l:.6g} = {fall:.1f} ATR, дно {i - b} баров назад", f"{fall:.1f} ATR, {i - b} бар.", "≥ 6 ATR, ≤ 25 баров"),
        C("Подтверждение: подтверждённый higher low после дна", bool(hl), f"HL {hl[-1].price:.6g}" if hl else "нет", f"{hl[-1].price:.6g}" if hl else "нет",
          "pivot low > дна, подтверждён"),
        C("Подтверждение: внутренняя структура вверх после дна", li is not None and li[0] in ("BOS_UP", "CHoCH_UP") and li[1] > b,
          f"последнее внутреннее событие {li[0] if li else 'нет'}", li[0] if li else "нет", "iBOS_UP/iCHoCH_UP после дна"),
        C("Подтверждение: close > EMA20 и > high[-1]", k[i].c > ef and k[i].c > k[i - 1].h, f"close {k[i].c:.6g}, EMA20 {ef:.6g}",
          f"{k[i].c:.6g}", "> EMA20 и > high[-1]"),
    ]
    rsi, m, sg, cmf, o, o10 = a.v("rsi", i), a.v("macd", i), a.v("macd_signal", i), a.v("cmf", i), a.v("obv", i), a.v("obv", i - 10)
    conds.append(_quality([("RSI > 50", (rsi or 0) > 50, f"{rsi:.1f}" if rsi else "н/д"),
                           ("MACD > signal", m is not None and sg is not None and m > sg, ""),
                           ("CMF > 0", (cmf or 0) > 0, f"{cmf:.3f}" if cmf is not None else "н/д"),
                           ("OBV выше, чем 10 баров назад", o is not None and o10 is not None and o > o10, "")]))
    stop = (hl[-1].price if hl else k[b].l) - P["stop_atr"] * atr
    plan, safety = _plan(ctx, stop, R_T1, P["trail_atr"], "close", P["time_stop"])
    return _finish(RECOV, ctx, conds + safety, plan, inputs)


ALTS = {"A-reversal": (REV, rev), "B-continuation": (CONT, cont), "C-recovery": (RECOV, recov)}


# ------------------------------------------------------------------ S2-diag: S2 с устранёнными конструктивными противоречиями
# ДИАГНОСТИКА, не предложение параметров. Все условия и пороги S2 v1.0.0 сохранены, изменены только два места,
# которые аудит определил как конструктивные противоречия:
#  (1) зона отката проверяется по минимуму ПОСЛЕДНИХ 10 баров (а не по историческому минимуму с момента H);
#  (2) T1 = entry + 2R вместо «старого максимума импульса» (вход по подтверждению чаще всего уже выше H).
S2D = {"id": "S2-diag-recent-pullback-RT1", "version": "research-0.1", "tf": "4h", "htf": None,
       "params": {"pb_bars": 10}}


def s2d(ctx: Ctx) -> dict:
    import math
    from .. import structure
    from ..strategies import S2, rr_net, s2
    P, a, i = S2["params"], ctx.a, ctx.i
    d = s2(ctx)
    if d["action"] in ("WARMUP", "HOLD", "EXIT"):
        return {**d, "strategy": S2D["id"], "version": S2D["version"]}
    k = a.s.candles
    imp = structure.last_impulse_up(a.pivots, i)
    conds = [c for c in d["conditions"] if not (c["name"].startswith("Откат") or c["name"].startswith("Чистый R:R")
                                                   or c["name"].startswith("Структура отката"))]
    plan = None
    if imp:
        L, H = imp
        fib = structure.log_fib(L.price, H.price)
        r_sh, r_dp, r_inv = (fib["retracement"][P[x]] for x in ("fib_shallow", "fib_deep", "fib_invalid"))
        pl = min(x.l for x in k[max(H.idx + 1, i - S2D["params"]["pb_bars"] + 1):i + 1]) if i > H.idx else None
        ema = a.v("ema_fast", i)
        zone = pl is not None and (r_dp * 0.99 <= pl <= r_sh or (ema is not None and pl <= ema * P["ema_touch"] and pl >= r_inv))
        conds.append(C("Откат (последние 10 баров) в зону Fib 0,382–0,618 / EMA20", zone, f"минимум {pl}", pl, "зона"))
        conds.append(C("Структура отката не сломана (close > 0,786)", k[i].c > r_inv, "", k[i].c, r_inv))
        if pl is not None:
            atr = a.v("atr", i)
            stop = pl - P["stop_atr"] * atr
            e = k[i].c
            t1 = e + 2 * (e - stop)
            rr = rr_net(e, stop, t1, ctx.cost)
            conds.append(C("Чистый R:R до T1(2R) ≥ 1,2", rr is not None and rr >= MIN_RR, f"{rr}", rr, "≥ 1,2"))
            plan = {"entry_ref": e, "stop": stop, "t1": t1, "t1_frac": P["t1_frac"], "trail_atr": P["trail_atr"], "trail_ref": "high",
                    "time_stop": P["time_stop"], "rr_net": rr}
    ok = all(c["ok"] is not False for c in conds) and any(c["ok"] for c in conds) and plan is not None
    bad = next((c["name"] for c in conds if c["ok"] is False), "")
    return decision(S2D, ctx, "ENTER" if ok else "WAIT", conds, plan, d["inputs"], "" if ok else f"блокирует: {bad}")


ALTS["S2-diag"] = (S2D, s2d)


# ------------------------------------------------------------------ S1-diag: S1 с целью в R вместо ближайшего сопротивления
# ДИАГНОСТИКА. Все условия S1 v1.0.0 (sweep, подтверждение, поток, RSI, BB, контекст 4H) сохранены; заменено только
# противоречие «цель = БЛИЖАЙШЕЕ сопротивление ≥ 2 ATR, а риск-слой требует R:R ≥ 2 до неё»: T1 = entry + 2R
# с общими правилами безопасности S-1…S-3 (стоп ≤ 3 ATR, цель ≥ 3× издержек, чистый R:R ≥ 1,2).
S1D = {"id": "S1-diag-RT1", "version": "research-0.1", "tf": "1h", "htf": "4h", "params": {}}


def s1d(ctx: Ctx) -> dict:
    from ..strategies import S1, s1
    d = s1(ctx)
    if d["action"] in ("WARMUP", "HOLD") or d["plan"] is None:
        return {**d, "strategy": S1D["id"], "version": S1D["version"]}
    conds = [c for c in d["conditions"] if not c["name"].startswith("Цель")]
    plan, safety = _plan(ctx, d["plan"]["stop"], R_T1, S1["params"]["trail_atr"], "close", S1["params"]["time_stop"])
    return _finish(S1D, ctx, conds + safety, plan, d["inputs"])


ALTS["S1-diag"] = (S1D, s1d)
