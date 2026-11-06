"""Экономический прогон стратегий на structure-v3 против S1/S2 v1.0.0, A-reversal (лучшая прошлая альтернатива) и B&H.
Методика как в econ.py: исполнение — реальный engine (open следующей свечи), издержки Uniswap v4 (для независимых
активов — масштаб по ATR к FABLES), стресс ×0/×1,5/×2; DEV (первые 60% BTC/ETH/SOL/DOGE) — только поиск ошибок,
HOLDOUT — последние 40% тех же и 6 других активов; старший ТФ — 4H для 1H и 1D для 4H."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from .. import structure_v3 as S3
from ..analysis import analyze
from ..execution import CONSERVATIVE_V1
from ..strategies import ANALYSIS_PARAMS
from . import alt_strategies as AL
from . import data, structure_strategies as SS
from .econ import metrics, regimes
from .strategy_audit import run_engine

HTF = {"1h": "4h", "4h": "1d"}
COST_F = (0.0, 1.0, 1.5, 2.0)
NAMES = ("S-orig", "A-reversal", *SS.STRATS)


def _series(sym, tf):
    if sym == "FABLES":
        import json
        from ..candles import normalize
        d = json.loads((data.ROOT / "structure_v3_2026-11-06" / "fables_gt_full.json").read_text())
        return normalize(d["rows"][tf], tf, d["fetched_at"])
    if sym.startswith("synth"):
        from .structure_v3_eval import synth
        kind, seed = sym.split(":")[1], int(sym.split(":")[2])
        from ..candles import Candle, Series
        s4, _ = synth(kind, seed, tf="4h")
        s1 = Series("1h", [Candle(q * 3600, k.o, k.h, k.l, k.c, k.v) for q, k in enumerate(s4.candles)])   # та же траектория на 1H-шкале
        return s1 if tf == "1h" else S3.aggregate(s1, tf)
    return data.binance(sym, tf)


def _job(args):
    name, sym, tf, fr, base_f, cf = args
    s = _series(sym, tf)
    hs = _series(sym, HTF[tf]) if not (sym.startswith("synth") and HTF[tf] == "1d") else None
    a = analyze(s, ANALYSIS_PARAMS[tf])
    h = analyze(hs, ANALYSIS_PARAMS[HTF[tf]]) if hs is not None and len(hs) else None
    i0, i1 = int(len(s) * fr[0]), int(len(s) * fr[1])
    cost = data.scaled_cost(CONSERVATIVE_V1, base_f * cf)
    if name == "S-orig":
        t = run_engine(a, tf, h if tf == "1h" else None, cost=cost, i0=i0, i1=i1, tag="orig")
    elif name == "A-reversal":
        meta, fn = AL.ALTS[name]
        t = run_engine(a, tf, None, fn, meta, cost=cost, risk_cfg=AL.risk_cfg(tf), i0=i0, i1=i1, tag=name)
    else:
        meta, fn = SS.STRATS[name]
        t = run_engine(a, tf, h, fn, meta, cost=cost, risk_cfg=SS.risk_cfg(tf), i0=i0, i1=i1, tag=name)
    m = metrics(t, s, i0, i1, cost, regimes(s))
    m["trade_list"] = [{k: x[k] for k in ("entry_ts", "exit_ts", "net_pnl", "r_multiple", "exit_reason", "bars_held", "costs")} for x in t.trades]
    return (name, sym, tf, tuple(fr), cf), m


def plan_jobs():
    sets = []
    for sym in data.DEV_ASSETS:
        sets += [("DEV", sym, (0.0, 0.6)), ("HOLDOUT-T", sym, (0.6, 1.0))]
    sets += [("HOLDOUT-A", sym, (0.0, 1.0)) for sym in data.HOLDOUT_ASSETS]
    sets += [("FABLES", "FABLES", (0.0, 1.0))]
    sets += [("SYNTH", f"synth:{k}:{q}", (0.0, 1.0)) for k in ("up", "down", "range", "regime", "reversal", "recovery_in_down", "deep_corr_in_up") for q in (1, 2)]
    jobs = []
    for grp, sym, fr in sets:
        for tf in ("1h", "4h"):
            if sym == "FABLES":
                base = 1.0
            elif sym.startswith("synth"):
                base = min(1.0, 1.5 / data.FABLES_ATR_PCT[tf])
            else:
                base = data.vol_matched_factor(data.binance(sym, tf))
            for name in NAMES:
                for cf in COST_F:
                    jobs.append((grp, (name, sym, tf, fr, base, cf)))
    return jobs


def run_all(workers=4):
    jobs = plan_jobs()
    with ProcessPoolExecutor(workers) as ex:
        res = list(ex.map(_job, [j for _, j in jobs], chunksize=2))
    return [{"group": g, "key": k, "m": m} for (g, _), (k, m) in zip(jobs, res)]
