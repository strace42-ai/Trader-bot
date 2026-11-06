# Скрипт контрольного пересчёта P2-4H (до/после): python tradinglab/research/p2_recheck_script.py <корень> <вывод.json>
"""Контрольный пересчёт P2-4H: одинаковый код прогона для «до» и «после». Аргументы: корень репозитория, файл вывода."""
import json, sys
root, outp = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
from tradinglab.analysis import analyze
from tradinglab.execution import CONSERVATIVE_V1
from tradinglab.strategies import ANALYSIS_PARAMS
from tradinglab.research import data, structure_strategies as SS
from tradinglab.research.structure_econ import _series
from tradinglab.research.strategy_audit import run_engine
from tradinglab.research.econ import metrics, regimes
meta, fn = SS.STRATS["P2-reversal"]
sets = [("DEV", s, (0.0, 0.6)) for s in data.DEV_ASSETS] + [("HOLDOUT-T", s, (0.6, 1.0)) for s in data.DEV_ASSETS] + \
       [("HOLDOUT-A", s, (0.0, 1.0)) for s in data.HOLDOUT_ASSETS] + [("FABLES", "FABLES", (0.0, 1.0))]
out = []
for grp, sym, fr in sets:
    s, hs = _series(sym, "4h"), _series(sym, "1d")
    a, h = analyze(s, ANALYSIS_PARAMS["4h"]), analyze(hs, ANALYSIS_PARAMS["1d"])
    base = 1.0 if sym == "FABLES" else data.vol_matched_factor(data.binance(sym, "4h"))
    i0, i1 = int(len(s) * fr[0]), int(len(s) * fr[1])
    for cf in (1.0, 2.0):
        cost = data.scaled_cost(CONSERVATIVE_V1, base * cf)
        t = run_engine(a, "4h", h, fn, meta, cost=cost, risk_cfg=SS.risk_cfg("4h"), i0=i0, i1=i1, tag="P2")
        m = metrics(t, s, i0, i1, cost, regimes(s))
        dec = [{"ts": d["bar_ts"], "action": d["action"], "final": d.get("action_final"),
                "fail": next((c["name"] for c in d["conditions"] if c["ok"] is False), None),
                "risk": [c["name"] for c in (d.get("risk") or []) if not c["ok"]]} for d in t.decisions]
        out.append({"group": grp, "sym": sym, "cf": cf, "m": {k: v for k, v in m.items()},
                    "trades": [{k: x[k] for k in ("entry_ts", "exit_ts", "net_pnl", "r_multiple", "exit_reason", "bars_held", "costs")} for x in t.trades],
                    "cancelled": [e for e in t.events if e["type"] == "ENTRY_CANCELLED"], "decisions": dec,
                    "buys": sum(1 for o in t.ops if o["side"] == "BUY")})
    print(grp, sym, flush=True)
json.dump(out, open(outp, "w"))
