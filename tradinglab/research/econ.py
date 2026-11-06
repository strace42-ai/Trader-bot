"""Сравнительный экономический прогон: исходные S1/S2 v1.0.0, три альтернативы и Buy & Hold.

- Исполнение — реальный engine: решение на закрытии бара i, исполнение по open бара i+1, стоп/цель внутри свечи
  (стоп проверяется раньше цели), модель издержек Uniswap v4 (комиссии hook 0,5%/1,0%, slippage, импакт x·y=k, газ).
- Независимые активы (Binance, только чтение): издержки масштабированы так, чтобы в единицах ATR они были как у FABLES
  на этом ТФ («vol-matched»); дополнительно стресс ×1,5 и ×2 и диагностический ×0 (без издержек).
- Периоды: DEV — первые 60% времени BTC/ETH/SOL/DOGE (только поиск ошибок в коде правил; параметры не менялись);
  HOLDOUT-T — последние 40% тех же активов; HOLDOUT-A — 6 других активов целиком; FABLES — реальные издержки.
- Режимы рынка для отчёта (не для торговли): блоки по 200 баров, z = ln(close_end/close_start)/(σ·√n): > 1 — рост,
  < −1 — падение, иначе боковик.
"""
from __future__ import annotations

import bisect
import math
import statistics
from concurrent.futures import ProcessPoolExecutor

from ..analysis import analyze
from ..execution import CONSERVATIVE_V1, liquidation_value, quote_buy
from ..execution import dec
from ..strategies import ANALYSIS_PARAMS
from . import alt_strategies as AL
from . import data
from .strategy_audit import run_engine

COST_F = (0.0, 1.0, 1.5, 2.0)        # множители к базовым (vol-matched / FABLES) издержкам


def regimes(s, block: int = 200) -> list[str]:
    c = s.col("c")
    lab = []
    for b0 in range(0, len(c), block):
        b1 = min(len(c), b0 + block)
        seg = c[b0:b1]
        if len(seg) < 20:
            lab += [lab[-1] if lab else "range"] * len(seg)
            continue
        r = [math.log(seg[j] / seg[j - 1]) for j in range(1, len(seg))]
        z = math.log(seg[-1] / seg[0]) / (statistics.pstdev(r) * math.sqrt(len(r)) or 1)
        lab += ["up" if z > 1 else "down" if z < -1 else "range"] * len(seg)
    return lab


def metrics(t, s, i0, i1, cost, reg) -> dict:
    tr = t.trades
    pnl = [float(x["net_pnl"]) for x in tr]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    eq = [float(e["equity"]) for e in t.equity_curve] or [1000.0]
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    k = s.candles
    f = quote_buy(dec(1000) - cost.gas_usd, k[i0].o, cost)
    bh = float(liquidation_value(f.qty, k[i1 - 1].c, cost)) - 1000
    held = sum(x["bars_held"] for x in tr)
    ts = [c.ts for c in k]
    by_reg = {"up": [], "range": [], "down": []}
    for x in tr:                                   # режим блока, в котором открыта сделка
        j = max(0, bisect.bisect_right(ts, x["entry_ts"]) - 1)
        by_reg[reg[j]].append(float(x["net_pnl"]))
    return {"trades": len(tr), "net": sum(pnl), "exp": sum(pnl) / len(pnl) if pnl else 0.0,
            "exp_r": statistics.mean(x["r_multiple"] for x in tr if x["r_multiple"] is not None) if tr else 0.0,
            "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (float("inf") if wins else 0.0),
            "win": len(wins) / len(pnl) * 100 if pnl else 0.0, "mdd": mdd * 100, "bh": bh, "exposure": held / (i1 - i0) * 100,
            "by_regime": {r: {"n": len(v), "net": sum(v)} for r, v in by_reg.items()},
            "regime_bars": {r: sum(1 for q in range(i0, i1) if reg[q] == r) for r in ("up", "range", "down")}}


def _job(args):
    name, sym, tf, i_frac, base_f, cf = args[:6]
    qmin = args[6] if len(args) > 6 else None
    if qmin is not None:
        AL.Q_MIN = qmin
    if sym == "FABLES":
        sers = data.fables()
    elif sym.startswith("synth"):
        from .structure_compare import synthetic
        kind, seed = sym.split(":")[1], int(sym.split(":")[2])
        sers = {tf: synthetic(kind, n=1200, seed=seed, tf=tf), "4h": synthetic(kind, n=1200, seed=seed, tf="4h")}
    else:
        sers = {tf: data.binance(sym, tf), "4h": data.binance(sym, "4h")}
    s = sers[tf]
    a = analyze(s, ANALYSIS_PARAMS[tf])
    i0, i1 = int(len(s) * i_frac[0]), int(len(s) * i_frac[1])
    cost = data.scaled_cost(CONSERVATIVE_V1, base_f * cf)
    reg = regimes(s)
    if name == "S-orig":
        htf = analyze(sers["4h"], ANALYSIS_PARAMS["4h"]) if tf == "1h" else None
        t = run_engine(a, tf, htf, cost=cost, i0=i0, i1=i1, tag="orig")
    else:
        meta, fn = AL.ALTS[name]
        htf = analyze(sers["4h"], ANALYSIS_PARAMS["4h"]) if name == "S1-diag" else None
        t = run_engine(a, tf, htf, fn, meta, cost=cost, risk_cfg=AL.risk_cfg(tf), i0=i0, i1=i1, tag=name)
    m = metrics(t, s, i0, i1, cost, reg)
    m["trade_list"] = [{k: x[k] for k in ("entry_ts", "exit_ts", "net_pnl", "r_multiple", "exit_reason", "bars_held")} for x in t.trades]
    return (name, sym, tf, tuple(i_frac), cf) + ((qmin,) if qmin is not None else ()), m


def plan_jobs() -> list[tuple]:
    jobs = []
    sets = []
    for sym in data.DEV_ASSETS:
        sets += [("DEV", sym, (0.0, 0.6)), ("HOLDOUT-T", sym, (0.6, 1.0))]
    sets += [("HOLDOUT-A", sym, (0.0, 1.0)) for sym in data.HOLDOUT_ASSETS]
    sets += [("FABLES", "FABLES", (0.0, 1.0))]
    sets += [("SYNTH", f"synth:{k}:{q}", (0.0, 1.0)) for k in ("trend", "range", "regime") for q in (1, 2)]
    for grp, sym, fr in sets:
        for tf in ("1h", "4h"):
            if sym == "FABLES":
                base = 1.0
            elif sym.startswith("synth"):
                base = min(1.0, 1.2 * 1.25 / data.FABLES_ATR_PCT[tf])          # σ 1,2%/бар ≈ ATR 1,5%
            else:
                base = data.vol_matched_factor(data.binance(sym, tf))
            for name in ("S-orig", *AL.ALTS):
                if (name == "S2-diag" and tf != "4h") or (name == "S1-diag" and tf != "1h"):
                    continue
                for cf in COST_F:
                    jobs.append((grp, (name, sym, tf, fr, base, cf)))
    return jobs


def plan_quality_jobs() -> list[tuple]:
    """Ценность фильтра качества: тот же прогон с порогом 0, 2 (основной) и 3 из 4 — только для анализа, не для выбора."""
    out = []
    for grp, j in plan_jobs():
        if j[0] in ("A-reversal", "B-continuation", "C-recovery") and j[5] == 1.0 and grp in ("HOLDOUT-T", "HOLDOUT-A", "DEV"):
            for q in (0, 3):
                out.append((grp, j + (q,)))
    return out


def run_all(workers: int = 4, jobs=None) -> list[dict]:
    jobs = jobs or plan_jobs()
    with ProcessPoolExecutor(workers) as ex:
        res = list(ex.map(_job, [j for _, j in jobs], chunksize=4))
    return [{"group": g, "key": k, "m": m} for (g, _), (k, m) in zip(jobs, res)]
