"""Подтверждающий прогон P2-4H на 12 активах, не использованных ни в одном предыдущем исследовании
(datasets/p2_confirm_2026-11-06). Правила, параметры, риск и модель исполнения — без изменений (research-0.1).
Один прогон, без настройки. Сравнение с S2 v1.0.0 и Buy & Hold за тот же период."""
from __future__ import annotations

import gzip
import json
import random
import statistics as st
from concurrent.futures import ProcessPoolExecutor

from ..analysis import analyze
from ..candles import Candle, Series
from ..execution import CONSERVATIVE_V1
from ..strategies import ANALYSIS_PARAMS
from . import data, structure_strategies as SS
from .econ import metrics, regimes
from .strategy_audit import run_engine

DIR = data.ROOT / "p2_confirm_2026-11-06"
SYMS = ("SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "NEARUSDT", "INJUSDT", "TIAUSDT", "SEIUSDT", "JUPUSDT", "WLDUSDT", "FETUSDT", "RENDERUSDT")


def load(sym, tf) -> Series:
    rows = json.load(gzip.open(DIR / f"{sym}_{tf}.json.gz", "rt"))
    return Series(tf, [Candle(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows])


def _job(args):
    name, sym, cf = args
    s, hs = load(sym, "4h"), load(sym, "1d")
    a, h = analyze(s, ANALYSIS_PARAMS["4h"]), analyze(hs, ANALYSIS_PARAMS["1d"])
    cost = data.scaled_cost(CONSERVATIVE_V1, data.vol_matched_factor(s) * cf)
    if name == "P2":
        meta, fn = SS.STRATS["P2-reversal"]
        t = run_engine(a, "4h", h, fn, meta, cost=cost, risk_cfg=SS.risk_cfg("4h"), tag="P2c")
    else:
        t = run_engine(a, "4h", None, cost=cost, tag="S2c")
    m = metrics(t, s, 0, len(s), cost, regimes(s))
    return {"name": name, "sym": sym, "cf": cf, "m": {k: v for k, v in m.items() if k != "trade_list"},
            "trades": [{k: x[k] for k in ("entry_ts", "exit_ts", "net_pnl", "r_multiple", "exit_reason", "bars_held", "costs")} for x in t.trades],
            "signals": sum(1 for d in t.decisions if d["action"] == "ENTER"), "executed": sum(1 for o in t.ops if o["side"] == "BUY"),
            "cancelled": sum(1 for e in t.events if e["type"] == "ENTRY_CANCELLED")}


def run(workers=4):
    jobs = [(n, s, cf) for n in ("P2", "S2") for s in SYMS for cf in (1.0, 2.0)]
    with ProcessPoolExecutor(workers) as ex:
        return list(ex.map(_job, jobs))


def summary(res, name, cf):
    rs = [r for r in res if r["name"] == name and r["cf"] == cf]
    tr = [t for r in rs for t in r["trades"]]
    R = [t["r_multiple"] for t in tr if t["r_multiple"] is not None]
    pn = [float(t["net_pnl"]) for t in tr]
    gw, gl = sum(p for p in pn if p > 0), -sum(p for p in pn if p < 0)
    ci = None
    if len(R) >= 3:
        rnd = random.Random(1)
        bs = sorted(st.mean(rnd.choices(R, k=len(R))) for _ in range(2000))
        ci = (bs[50], bs[1949])
    return {"signals": sum(r["signals"] for r in rs), "executed": sum(r["executed"] for r in rs), "cancelled": sum(r["cancelled"] for r in rs),
            "trades": len(tr), "net": sum(pn), "E": st.mean(R) if R else 0.0, "ci": ci, "pf": gw / gl if gl else (float("inf") if gw else 0.0),
            "mdd": max((r["m"]["mdd"] for r in rs), default=0.0), "E_wo_best": st.mean(sorted(R)[:-1]) if len(R) > 1 else None,
            "bh": sum(r["m"]["bh"] for r in rs), "win": sum(1 for p in pn if p > 0) / len(pn) if pn else 0.0,
            "by_regime": {g: (sum(r["m"]["by_regime"][g]["n"] for r in rs), sum(r["m"]["by_regime"][g]["net"] for r in rs)) for g in ("up", "range", "down")}}
