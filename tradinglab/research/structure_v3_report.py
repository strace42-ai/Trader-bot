"""Сравнение v1 / v2 / v3 (реализованный модуль). Точность и ложные события — только на синтетике с известной разметкой;
устойчивость к точке старта и частоты — только на реальных BTC/ETH. Показатели не смешиваются."""
from __future__ import annotations

import statistics as st

from .. import structure, structure_v2 as SV2
from .. import structure_v3 as S
from ..analysis import analyze as CA
from ..candles import Series
from . import structure_v3_eval as E


def trend_v(s: Series, model: str) -> list[str]:
    if model == "v3":
        return [x["trend"] for x in S.Engine(s.tf).run(s).major.states]
    return E.trends(s, model, 3.0)


def synthetic_table(seeds=range(1, 6)) -> list[str]:
    rows = []
    for sc in E.SCEN:
        cells = []
        for m in ("v1", "v2", "v3"):
            rs = []
            for q in seeds:
                s, t = E.synth(sc, q)
                rs.append(E.score(trend_v(s, m), t))
            lags = [x for r in rs for x in r["lags"]]
            lag = "—" if not lags else ("∞" if any(x is None for x in lags) else str(int(st.median(lags))))
            cells.append(f"{st.mean(r['acc'] for r in rs):.2f} / {sum(r['false_dir'] for r in rs)} / {sum(r['range_flips'] for r in rs)} / {lag}")
        rows.append(f"| {sc} | " + " | ".join(cells) + " |")
    return rows


def nested_table(n=20) -> list[str]:
    out = []
    for m in ("v2", "v3"):
        fch = tr = conf = 0
        init_ok = 0
        for rev in (False, True):
            for seed in range(1, n + 1):
                s, mk = E.nested(seed, reversal=rev)
                h = s.col("h")
                cross = next((i for i in range(mk["rev_bar"], len(h)) if h[i] > mk["tops"][-1]), len(h)) if rev else len(h)
                if m == "v2":
                    ev = [e for e in SV2.analyze_structure(CA(s))["external"] if e["type"] == "CHoCH_UP"]
                else:
                    e3 = S.Engine("1h").run(s)
                    ev = [e for e in e3.major.events if e["type"] == "CHoCH_UP"]
                    if not rev:
                        ph0 = next(p for p in e3.major.prot_hist if p["side"] == "H")
                        init_ok += abs(ph0["price"] / mk["H1"] - 1) < 0.03
                fch += sum(1 for e in ev if e["idx"] < cross)
                tr += sum(1 for e in ev if e["idx"] >= cross)
                conf += sum(1 for e in ev if e["idx"] < cross and e.get("scale_conflict"))
        out.append(f"| {m} | {fch} | {conf if m == 'v3' else '—'} | {tr} | {init_ok if m == 'v3' else '—'} |")
    return out


def real_table(window=1500, step=500) -> list[str]:
    out = []
    for sym in ("BTCUSDT", "ETHUSDT"):
        for tf in ("1h", "4h"):
            s = E.binance(sym, tf)
            n = len(s)
            a = CA(s)
            v1n = len(structure.structure_events(s, a.pivots))
            v2n = len(SV2.analyze_structure(a)["external"])
            e3 = S.Engine(tf).run(s)
            v3b = sum(1 for e in e3.major.events if e["type"].startswith("BOS"))
            v3c = sum(1 for e in e3.major.events if e["type"].startswith("CHoCH"))
            conf = sum(1 for c in e3.conflicts if c["type"] == "SCALE_CONFLICT")
            agree = {}
            for m in ("v1", "v2", "v3"):
                full = trend_v(s, m)
                full = [x.upper() for x in full]
                ag = []
                for off in range(step, n - window, step):
                    t = [x.upper() for x in trend_v(Series(tf, s.candles[off:off + window]), m)]
                    f0 = next((i for i, x in enumerate(t) if x not in ("UNDEFINED", "NONE")), None)
                    if f0 is not None:
                        ag.append(sum(1 for i in range(f0, len(t)) if t[i] == full[off + i]) / (len(t) - f0))
                agree[m] = st.mean(ag) if ag else float("nan")
            out.append(f"| {sym[:3]} {tf.upper()} | {1000 * v1n / n:.0f} | {1000 * v2n / n:.1f} | {1000 * v3b / n:.1f} / {1000 * v3c / n:.1f} | "
                       f"{1000 * conf / n:.1f} | {agree['v1']:.2f} / {agree['v2']:.2f} / {agree['v3']:.2f} |")
    return out
