"""Новые стратегии на основе structure-v3 — ИССЛЕДОВАНИЕ (не зарегистрированы в STRATEGIES, в FORWARD не используются).

Правила заданы ЗАРАНЕЕ, одинаковы для 1H и 4H (расстояния в ATR(14), окна в барах) и по результатам не подбирались.
Режим — Major Structure v3 того же ТФ; вход — Internal Structure v3 (iCHoCH); старший ТФ — только вето, и только если
его major-структура определена (UP/DOWN, history OK). Все решения — на закрытии бара i по данным ≤ i.

Исправления проблем аудита (STRATEGY_AUDIT.md):
  T1     цель — реальный структурный уровень ВПЕРЕДИ цены (структурный максимум major-ноги / ближайший major-swing
         сверху); если цена уже у цели или выше — сетапа нет (а не «T1 позади цены»);
  R      R:R считается по полной модели исполнения для ПЛАНОВОГО размера позиции: комиссии hook (покупка/продажа),
         импакт x·y=k, проскальзывание, доп. проскальзывание стопа, газ на вход и выход;
  pull_low  стоп — под СВЕЖИМ внутренним swing low, сформированным после структурного экстремума (не исторический
         минимум с момента импульса);
  риск-слой  min_rr риск-слоя = тот же порог, что в стратегии (1,5 до T1), — без противоречия «цель ближайшая, а R:R ≥ 2».

Обязательные правила безопасности (FAIL → нет входа): стоп 0,5–3 ATR; чистый R:R до T1 ≥ 1,5 с полными издержками;
T1 впереди цены; major history_status = OK (L1); остальные лимиты risk-v1 без изменений. Внутрисвечные структурные
сигналы не используются (L2) — только закрытые свечи.
"""
from __future__ import annotations

import bisect
from dataclasses import replace

from .. import structure_v3 as S3
from ..candles import TF_SEC
from ..execution import CostModel, dec, max_trade_usd, quote_buy, quote_sell
from ..risk import RISK
from ..strategies import C, Ctx, decision

MIN_RR = 1.5
STOP_BUF = 0.25
STOP_MIN, STOP_MAX = 0.5, 3.0
T1_FRAC, TRAIL_ATR, TIME_STOP = 0.5, 2.5, 40


def risk_cfg(tf: str):
    return replace(RISK[tf], min_rr=MIN_RR)


# ------------------------------------------------------------------ издержки на плановый размер
def plan_size(tf: str, entry: float, stop: float, cost: CostModel, equity: float = 1000.0) -> float:
    cfg = RISK[tf]
    cap = min(equity * float(cfg.max_exposure), float(max_trade_usd(cost)), equity - 2 * float(cost.gas_usd))
    usd = cap
    for _ in range(3):                                   # риск на сделку с учётом издержек при этом размере
        q = quote_buy(usd, entry, cost)
        out = quote_sell(q.qty, stop, cost, cost.stop_slippage)
        loss = float(q.usd - out.usd) + 2 * float(cost.gas_usd)
        per = loss / usd if usd else 1
        usd = min(cap, equity * float(cfg.risk_per_trade) / per) if per > 0 else cap
    return max(usd, 0.0)


def net_rr(tf: str, entry: float, stop: float, target: float, cost: CostModel) -> tuple[float | None, float, float]:
    """(чистый R:R до цели, размер позиции $, круг издержек в % при этом размере)."""
    if not (stop < entry < target):
        return None, 0.0, 0.0
    usd = plan_size(tf, entry, stop, cost)
    if usd < 20:
        return None, usd, 0.0
    q = quote_buy(usd, entry, cost)
    win = float(quote_sell(q.qty, target, cost).usd) - usd - 2 * float(cost.gas_usd)
    loss = usd + 2 * float(cost.gas_usd) - float(quote_sell(q.qty, stop, cost, cost.stop_slippage).usd)
    flat = usd + 2 * float(cost.gas_usd) - float(quote_sell(q.qty, entry, cost).usd)
    return (win / loss if loss > 0 else None), usd, flat / usd * 100


# ------------------------------------------------------------------ структура по барам (каузально)
def struct_cache(series, history: str) -> dict:
    """Пошаговый прогон v3; после каждого бара сохраняется то, что известно на его закрытии."""
    e = S3.Engine(series.tf, history=history)
    out = {"major": [], "sh": [], "sl": [], "ievents": [], "mevents": [], "int_lows": [], "maj_highs": [], "hist": [], "conflict": []}
    for i, k in enumerate(series.candles):
        e.step(k)
        n = len(e.ts)
        out["major"].append(e.major.states[-1])
        out["sh"].append(e.major.target(e, "H", n))
        out["sl"].append(e.major.target(e, "L", n))
        out["ievents"].append([x["type"] for x in e.internal.events if x["idx"] == n - 1])
        out["mevents"].append([x for x in e.major.events if x["idx"] == n - 1])
        out["hist"].append(e.history_status())
        out["conflict"].append(e.conflict_state[-1])
    out["int_swings"] = list(e.zz["internal"].swings)
    out["maj_swings"] = list(e.zz["major"].swings)
    out["events"] = e.major.events
    return out


def _cache(ctx: Ctx) -> dict:
    a = ctx.a
    if getattr(a, "_v3s", None) is None:
        first = a.s.candles[0].ts if a.s.candles else 0
        a._v3s = struct_cache(a.s, "launch" if first <= 1792530000 and first >= 1792500000 else "truncated")
    return a._v3s


def _htf_dir(ctx: Ctx) -> str | None:
    """Направление старшего ТФ — только если major определена и история OK; иначе None (не контекст)."""
    if ctx.htf is None or ctx.j is None:
        return None
    if getattr(ctx.htf, "_v3s", None) is None:
        first = ctx.htf.s.candles[0].ts if ctx.htf.s.candles else 0
        ctx.htf._v3s = struct_cache(ctx.htf.s, "launch" if 1792500000 <= first <= 1792530000 else "truncated")
    c = ctx.htf._v3s
    st, hs = c["major"][ctx.j], c["hist"][ctx.j]
    return st["trend"] if st["trend"] in ("UP", "DOWN") and hs == "OK" else None


def _last(swings, kind, i, after_idx):
    xs = [w for w in swings if w.kind == kind and w.confirmed_idx <= i and w.idx > after_idx]
    return xs[-1] if xs else None


def _plan(ctx, stop, t1, extra_conds):
    a, i = ctx.a, ctx.i
    e, atr = a.s.candles[i].c, a.v("atr", i)
    dist = (e - stop) / atr if atr else None
    rr, usd, rt = net_rr(a.s.tf, e, stop, t1, ctx.cost) if t1 else (None, 0.0, 0.0)
    conds = extra_conds + [
        C("Цель впереди цены (структурный уровень)", t1 is not None and t1 > e, f"T1 {t1:.6g} vs close {e:.6g}" if t1 else "нет цели",
          f"{(t1 / e - 1) * 100:+.2f}%" if t1 else "нет", "> close"),
        C(f"Стоп {STOP_MIN}–{STOP_MAX} ATR", dist is not None and STOP_MIN <= dist <= STOP_MAX, f"{dist:.2f} ATR" if dist else "н/д",
          f"{dist:.2f} ATR" if dist else "н/д", f"{STOP_MIN}–{STOP_MAX} ATR"),
        C(f"Чистый R:R до T1 ≥ {MIN_RR} (полные издержки, размер ${usd:.0f})", rr is not None and rr >= MIN_RR,
          f"R:R {rr:.2f}, круг издержек {rt:.2f}%" if rr is not None else "н/д", f"{rr:.2f}" if rr is not None else "н/д", f"≥ {MIN_RR}"),
    ]
    plan = {"entry_ref": e, "stop": stop, "t1": t1, "t1_frac": T1_FRAC, "trail_atr": TRAIL_ATR, "trail_ref": "close",
            "time_stop": TIME_STOP, "rr_net": rr}
    return conds, plan


def _finish(meta, ctx, conds, plan, inputs):
    ok = all(c["ok"] for c in conds) and plan is not None
    bad = next((c["name"] for c in conds if not c["ok"]), "")
    return decision(meta, ctx, "ENTER" if ok else "WAIT", conds, plan, inputs, "" if ok else f"блокирует: {bad}")


def _warm(meta, ctx):
    if ctx.i < 30 or ctx.a.v("atr", ctx.i) is None:
        return decision(meta, ctx, "WARMUP", [C("История", False, f"{ctx.i + 1} баров", f"{ctx.i + 1}", "≥ 30")], None, {}, "")
    return None


# ================================================================== P1: продолжение тренда (откат в Major UP)
P1 = {"id": "V3-P1-trend-pullback", "version": "research-0.1", "tf": "*", "htf": None, "params": {}}


def p1(ctx: Ctx) -> dict:
    w = _warm(P1, ctx)
    if w:
        return w
    c, i = _cache(ctx), ctx.i
    st = c["major"][i]
    if ctx.in_position:
        bad = st["trend"] != "UP"
        return decision(P1, ctx, "EXIT" if bad else "HOLD", [C("Major UP сохраняется", not bad, st["trend"], st["trend"], "UP")], None, {}, "")
    sh = c["sh"][i]
    hd = _htf_dir(ctx)
    low = _last(c["int_swings"], "L", i, sh["idx"]) if sh else None
    conds = [
        C("Major UP (history OK, без SCALE_CONFLICT)", st["trend"] == "UP" and c["hist"][i] == "OK" and not c["conflict"][i],
          f"{st['trend']} / {c['hist'][i]} / конфликт {'да' if c['conflict'][i] else 'нет'}", st["trend"], "UP, OK, без конфликта"),
        C("Фаза PULLBACK: структурный максимум подтверждён", st["phase"] == "PULLBACK" and sh is not None, st["phase"], st["phase"], "PULLBACK"),
        C("Триггер: internal iCHoCH_UP", "iCHoCH_UP" in c["ievents"][i], ",".join(c["ievents"][i]) or "нет", "iCHoCH_UP", "на этом баре"),
        C("Свежий internal low после структурного максимума", low is not None and (st["pl"] is None or low.price > st["pl"]),
          f"{low.price:.6g}" if low else "нет", f"{low.price:.6g}" if low else "нет", "> protected low"),
        C("Старший ТФ не против (если определён)", hd in (None, "UP"), hd or "не определён (не используется)", hd or "—", "не DOWN"),
    ]
    stop = low.price - STOP_BUF * ctx.a.v("atr", i) if low else ctx.a.s.candles[i].c * 0.5
    more, plan = _plan(ctx, stop, sh["price"] if sh else None, conds)
    return _finish(P1, ctx, more, plan, {})


# ================================================================== P2: подтверждаемый разворот после major CHoCH_UP
P2 = {"id": "V3-P2-reversal-after-choch", "version": "research-0.1", "tf": "*", "htf": None, "params": {}}


def p2(ctx: Ctx) -> dict:
    w = _warm(P2, ctx)
    if w:
        return w
    c, i = _cache(ctx), ctx.i
    st = c["major"][i]
    if ctx.in_position:
        bad = st["trend"] == "DOWN" or (st["trend"] in ("TRANSITION", "RANGE") and st["bias"] == "down")
        return decision(P2, ctx, "EXIT" if bad else "HOLD", [C("Нет возврата к DOWN", not bad, st["trend"], st["trend"], "не DOWN")], None, {}, "")
    ch = next((x for x in reversed(c["events"]) if x["idx"] <= i and x["type"] == "CHoCH_UP" and not x.get("restore")), None)
    trans = st["trend"] in ("TRANSITION", "RANGE") and st["bias"] == "up"
    hl = _last(c["int_swings"], "L", i, ch["idx"]) if ch else None
    sh = c["sh"][i]
    t1 = sh["price"] if sh else (max(x.h for x in ctx.a.s.candles[ch["idx"]:i + 1]) if ch else None)
    hd = _htf_dir(ctx)
    conds = [
        C("Major TRANSITION вверх (protected high нарушен), history OK", trans and ch is not None and c["hist"][i] == "OK",
          f"{st['trend']}/{st['bias']}", f"{st['trend']}/{st['bias']}", "TRANSITION/up"),
        C("Нет SCALE_CONFLICT (старший масштаб не держит DOWN)", not c["conflict"][i], "конфликт" if c["conflict"][i] else "нет", "", "нет"),
        C("Триггер: internal iCHoCH_UP / iBOS_UP", any(x in ("iCHoCH_UP", "iBOS_UP") for x in c["ievents"][i]),
          ",".join(c["ievents"][i]) or "нет", "", "на этом баре"),
        C("Higher low после CHoCH выше кандидата protected low", hl is not None and st["pl"] is not None and hl.price > st["pl"],
          f"HL {hl.price:.6g} vs {st['pl']:.6g}" if hl and st["pl"] else "нет", "", "> кандидата"),
        C("Старший ТФ не против (если определён)", hd in (None, "UP"), hd or "не определён (не используется)", hd or "—", "не DOWN"),
    ]
    stop = hl.price - STOP_BUF * ctx.a.v("atr", i) if hl else ctx.a.s.candles[i].c * 0.5
    more, plan = _plan(ctx, stop, t1, conds)
    return _finish(P2, ctx, more, plan, {})


# ================================================================== P3: коррекция внутри Major DOWN после снятия ликвидности (спот)
P3 = {"id": "V3-P3-spot-correction", "version": "research-0.1", "tf": "*", "htf": None, "params": {"window": 12, "time_stop": 24}}


def p3(ctx: Ctx) -> dict:
    w = _warm(P3, ctx)
    if w:
        return w
    c, i = _cache(ctx), ctx.i
    st = c["major"][i]
    k = ctx.a.s.candles
    if ctx.in_position:
        bad = "iCHoCH_DOWN" in c["ievents"][i]
        return decision(P3, ctx, "EXIT" if bad else "HOLD", [C("Внутренняя структура не развернулась вниз", not bad, ",".join(c["ievents"][i]) or "нет", "", "нет iCHoCH_DOWN")], None, {}, "")
    W = P3["params"]["window"]
    ev = [x for q in range(max(0, i - W), i + 1) for x in c["mevents"][q] if x["type"] in ("SWEEP_LOW", "BOS_DOWN")]
    lo_i = min(range(ev[0]["idx"], i + 1), key=lambda q: k[q].l) if ev else None
    e = k[i].c
    highs = sorted(w_.price for w_ in c["maj_swings"] if w_.kind == "H" and w_.confirmed_idx <= i and w_.price > e)
    t1 = min([x for x in highs[:1]] + ([st["ph"]] if st["ph"] else [])) if (highs or st["ph"]) else None
    conds = [
        C("Major DOWN, history OK", st["trend"] == "DOWN" and c["hist"][i] == "OK", f"{st['trend']}/{c['hist'][i]}", st["trend"], "DOWN"),
        C(f"За {W} баров: снятие ликвидности под минимумом (SWEEP_LOW) или новый минимум (BOS_DOWN)", bool(ev),
          ev[-1]["type"] if ev else "нет", ev[-1]["type"] if ev else "нет", "SWEEP_LOW / BOS_DOWN"),
        C("Триггер: internal iCHoCH_UP", "iCHoCH_UP" in c["ievents"][i], ",".join(c["ievents"][i]) or "нет", "", "на этом баре"),
    ]
    stop = k[lo_i].l - STOP_BUF * ctx.a.v("atr", i) if lo_i is not None else e * 0.5
    more, plan = _plan(ctx, stop, t1, conds)
    if plan:
        plan["time_stop"] = P3["params"]["time_stop"]
    return _finish(P3, ctx, more, plan, {})


STRATS = {"P1-trend-pullback": (P1, p1), "P2-reversal": (P2, p2), "P3-spot-correction": (P3, p3)}
