"""SHADOW-P2-4H 1.0.0 — ЗАМОРОЖЕННЫЕ правила P2 (подтверждаемый разворот после нарушения major protected high).

Дословная копия правил исследования research-0.1 (tradinglab/research/structure_strategies.py, P2), вынесенная
в отдельный модуль, чтобы последующие изменения исследовательского кода не меняли работающий эксперимент. Хэш этого
файла (rules_hash) записывается в неизменяемые параметры эксперимента при START; изменение правил = новая версия
и новый эксперимент. Решения — только по закрытым свечам 4H (L2: внутрисвечные структурные сигналы не используются).
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from .. import structure_v3 as S3
from ..execution import CostModel, max_trade_usd, quote_buy, quote_sell
from ..risk import RISK
from ..strategies import C, Ctx, decision

VERSION = "1.0.0"
STRATEGY = {"id": "SHADOW-P2-4H", "version": VERSION, "tf": "4h", "htf": "1d",
            "params": {"min_rr": 1.5, "stop_buf_atr": 0.25, "stop_atr": [0.5, 3.0], "t1_frac": 0.5, "trail_atr": 2.5, "time_stop": 40,
                       "structure": f"{S3.VERSION} k_major={S3.K_MAJOR} k_int={S3.K_INT} k_senior={S3.K_SENIOR}", "warmup_bars": 30,
                       "risk": "risk-v1 4H (риск 1% на сделку, экспозиция 0,8, лимит периода 4%/нед, Max DD 18%) с min_rr 1,5"}}
LAUNCH_TS = (1792500000, 1792530000)        # окно свечи запуска FABLES/USDG (история «с запуска» для v3)
MIN_RR = 1.5
STOP_BUF = 0.25
STOP_MIN, STOP_MAX = 0.5, 3.0
T1_FRAC, TRAIL_ATR, TIME_STOP = 0.5, 2.5, 40


def rules_hash() -> str:
    """SHA-256 этого файла и модуля структуры v3 (правила стратегии и структурной модели вместе)."""
    h = hashlib.sha256(Path(__file__).read_bytes())
    h.update(Path(S3.__file__).read_bytes())
    return h.hexdigest()


def risk_cfg():
    return replace(RISK["4h"], min_rr=MIN_RR)


def plan_size(tf: str, entry: float, stop: float, cost: CostModel, equity: float = 1000.0) -> float:
    cfg = RISK[tf]
    cap = min(equity * float(cfg.max_exposure), float(max_trade_usd(cost)), equity - 2 * float(cost.gas_usd))
    usd = cap
    for _ in range(3):
        q = quote_buy(usd, entry, cost)
        out = quote_sell(q.qty, stop, cost, cost.stop_slippage)
        loss = float(q.usd - out.usd) + 2 * float(cost.gas_usd)
        per = loss / usd if usd else 1
        usd = min(cap, equity * float(cfg.risk_per_trade) / per) if per > 0 else cap
    return max(usd, 0.0)


def net_rr(tf: str, entry: float, stop: float, target: float, cost: CostModel) -> tuple[float | None, float, float]:
    """(чистый R:R до цели, плановый размер $, круг издержек % при этом размере): комиссии, импакт, проскальзывание, газ."""
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


def _history(series) -> str:
    first = series.candles[0].ts if series.candles else 0
    return "launch" if LAUNCH_TS[0] <= first <= LAUNCH_TS[1] else "truncated"


def struct_cache(series, history: str) -> dict:
    e = S3.Engine(series.tf, history=history)
    out = {"major": [], "sh": [], "ievents": [], "hist": [], "conflict": []}
    for k in series.candles:
        e.step(k)
        n = len(e.ts)
        out["major"].append(e.major.states[-1])
        out["sh"].append(e.major.target(e, "H", n))
        out["ievents"].append([x["type"] for x in e.internal.events if x["idx"] == n - 1])
        out["hist"].append(e.history_status())
        out["conflict"].append(e.conflict_state[-1])
    out["int_swings"] = list(e.zz["internal"].swings)
    out["events"] = e.major.events
    return out


def _cache(a) -> dict:
    if getattr(a, "_v3s", None) is None:
        a._v3s = struct_cache(a.s, _history(a.s))
    return a._v3s


def _htf_dir(ctx: Ctx) -> str | None:
    if ctx.htf is None or ctx.j is None:
        return None
    c = _cache(ctx.htf)
    st, hs = c["major"][ctx.j], c["hist"][ctx.j]
    return st["trend"] if st["trend"] in ("UP", "DOWN") and hs == "OK" else None


def _last(swings, kind, i, after_idx):
    xs = [w for w in swings if w.kind == kind and w.confirmed_idx <= i and w.idx > after_idx]
    return xs[-1] if xs else None


def decide(ctx: Ctx) -> dict:
    a, i = ctx.a, ctx.i
    if i < 30 or a.v("atr", i) is None:
        return decision(STRATEGY, ctx, "WARMUP", [C("История", False, f"{i + 1} баров", f"{i + 1}", "≥ 30")], None, {}, "")
    c = _cache(a)
    st = c["major"][i]
    if ctx.in_position:
        bad = st["trend"] == "DOWN" or (st["trend"] in ("TRANSITION", "RANGE") and st["bias"] == "down")
        return decision(STRATEGY, ctx, "EXIT" if bad else "HOLD", [C("Нет возврата к DOWN", not bad, st["trend"], st["trend"], "не DOWN")],
                        None, {}, "на закрытии следующей 4H-свечи")
    ch = next((x for x in reversed(c["events"]) if x["idx"] <= i and x["type"] == "CHoCH_UP" and not x.get("restore")), None)
    trans = st["trend"] in ("TRANSITION", "RANGE") and st["bias"] == "up"
    hl = _last(c["int_swings"], "L", i, ch["idx"]) if ch else None
    sh = c["sh"][i]
    t1 = sh["price"] if sh else (max(x.h for x in a.s.candles[ch["idx"]:i + 1]) if ch else None)
    hd = _htf_dir(ctx)
    conds = [
        C("Major TRANSITION вверх (protected high нарушен), history OK", trans and ch is not None and c["hist"][i] == "OK",
          f"{st['trend']}/{st['bias']}" + (f", protected high {st['ph']:.6g}" if st.get("ph") else ""), f"{st['trend']}/{st['bias']}", "TRANSITION/up"),
        C("Нет SCALE_CONFLICT (старший масштаб не держит DOWN)", not c["conflict"][i], "конфликт" if c["conflict"][i] else "нет", "", "нет"),
        C("Триггер: internal iCHoCH_UP / iBOS_UP", any(x in ("iCHoCH_UP", "iBOS_UP") for x in c["ievents"][i]),
          ",".join(c["ievents"][i]) or "нет", "", "на этом баре"),
        C("Higher low после CHoCH выше кандидата protected low", hl is not None and st["pl"] is not None and hl.price > st["pl"],
          f"HL {hl.price:.6g} vs {st['pl']:.6g}" if hl and st["pl"] else "нет", "", "> кандидата"),
        C("Старший ТФ 1D не против (если определён)", hd in (None, "UP"), hd or "не определён (не используется)", hd or "—", "не DOWN"),
    ]
    e, atr = a.s.candles[i].c, a.v("atr", i)
    stop = hl.price - STOP_BUF * atr if hl else e * 0.5
    dist = (e - stop) / atr if atr else None
    rr, usd, rt = net_rr("4h", e, stop, t1, ctx.cost) if t1 else (None, 0.0, 0.0)
    conds += [
        C("Цель впереди цены (структурный уровень)", t1 is not None and t1 > e, f"T1 {t1:.6g} vs close {e:.6g}" if t1 else "нет цели",
          f"{(t1 / e - 1) * 100:+.2f}%" if t1 else "нет", "> close"),
        C(f"Стоп {STOP_MIN}–{STOP_MAX} ATR", dist is not None and STOP_MIN <= dist <= STOP_MAX, f"{dist:.2f} ATR" if dist else "н/д",
          f"{dist:.2f} ATR" if dist else "н/д", f"{STOP_MIN}–{STOP_MAX} ATR"),
        C(f"Чистый R:R до T1 ≥ {MIN_RR} (полные издержки, размер ${usd:.0f})", rr is not None and rr >= MIN_RR,
          f"R:R {rr:.2f}, круг издержек {rt:.2f}%" if rr is not None else "н/д", f"{rr:.2f}" if rr is not None else "н/д", f"≥ {MIN_RR}"),
    ]
    plan = {"entry_ref": e, "stop": stop, "t1": t1, "t1_frac": T1_FRAC, "trail_atr": TRAIL_ATR, "trail_ref": "close",
            "time_stop": TIME_STOP, "rr_net": rr, "plan_usd": usd, "roundtrip_cost_pct": rt}
    ok = all(x["ok"] for x in conds)
    bad = next((x["name"] for x in conds if not x["ok"]), "")
    return decision(STRATEGY, ctx, "ENTER" if ok else "WAIT", conds, plan, {"atr": atr, "close": e},
                    "" if ok else f"на закрытии следующей 4H-свечи; блокирует: {bad}")
