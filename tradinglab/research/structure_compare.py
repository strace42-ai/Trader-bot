"""Сравнение структурного анализа v1 (внутренняя структура, используется S2 v1.0.0) и v2 (внешняя swing-структура),
и НЕЗАВИСИМЫЙ исследовательский прогон S2 с внешней структурой. Ничего не пишет в базу, не меняет S1/S2/S3,
их FORWARD-историю и портфели; вариант S2 не регистрируется в STRATEGIES.
"""
from __future__ import annotations

import json
from collections import Counter
import math
import random
from pathlib import Path

from .. import structure_v2 as SV2
from ..analysis import analyze
from ..candles import TF_SEC, Candle, Series, normalize
from ..engine import Trader
from ..execution import CONSERVATIVE_V1
from ..strategies import ANALYSIS_PARAMS, C, S2, decision, s2

ROOT = Path(__file__).resolve().parents[2] / "datasets" / "structure_research_2026-11-06"
COINS = {"ethereum": "ETH", "bitcoin": "BTC"}
S2_V2 = {**S2, "id": "S2-4H-structure-pullback-RESEARCH-swingv2", "version": "research-0.1"}


def load_sets() -> dict[str, Series]:
    d = json.loads((ROOT / "fables_gt_live.json").read_text())
    out = {f"FABLES {tf}": normalize(d["rows"][tf], tf, d["fetched_at"]) for tf in ("1h", "4h", "1d")}
    cg = json.loads((ROOT / "cg_eth_btc_ohlc_30d.json").read_text())
    for coin, rows in cg.items():   # CoinGecko: время = закрытие свечи → начало = закрытие − 4ч; объёма нет
        out[f"{COINS.get(coin, coin.upper())} 4h"] = Series("4h", [Candle(int(r[0] // 1000) - 14400, r[1], r[2], r[3], r[4], 0.0) for r in rows])
    return out


def synthetic(kind: str, n: int = 900, seed: int = 1, tf: str = "4h") -> Series:
    rnd, p, out = random.Random(seed), 1.0, []
    d = TF_SEC[tf]
    for i in range(n):
        if kind == "range":
            drift = -0.08 * math.log(p)
        elif kind == "trend":
            drift = 0.0025
        else:                                                   # regime: вверх 300 / боковик 300 / вниз 300
            drift = 0.0025 if i < 300 else (-0.08 * math.log(p / math.exp(0.75)) if i < 600 else -0.0025)
        o = p
        p = p * math.exp(drift + rnd.gauss(0, 0.012))
        h = max(o, p) * math.exp(abs(rnd.gauss(0, 0.004)))
        lo = min(o, p) * math.exp(-abs(rnd.gauss(0, 0.004)))
        out.append(Candle(i * d, o, h, lo, p, 1000.0 * (1 + rnd.random())))
    return Series(tf, out)


def compare_events(s: Series, k: float = SV2.K_ATR) -> dict:
    a = analyze(s, ANALYSIS_PARAMS.get(s.tf, {}))
    r = SV2.analyze_structure(a, k)
    ext, inn = r["external"], r["internal"]
    key = lambda e: (e["idx"], e["type"].lstrip("i").split("_")[1])   # бар и направление  # noqa: E731
    ek, ik = {key(e) for e in ext}, {key(e) for e in inn}
    rng = lambda i: a.v("adx", i) is not None and a.v("adx", i) < 18   # noqa: E731
    v1_choch = [e for e in inn if e["type"].startswith("iCHoCH")]
    v2_choch = [e for e in ext if e["type"].startswith("CHoCH")]
    return {"bars": len(s), "k_atr": k, "swings": len(r["swings"]), "v1_events": len(inn), "v2_events": len(ext),
            "v1_choch": len(v1_choch), "v2_choch": len(v2_choch),
            "v1_choch_in_range": sum(1 for e in v1_choch if rng(e["idx"])), "v2_choch_in_range": sum(1 for e in v2_choch if rng(e["idx"])),
            "both": sorted(ek & ik), "only_v1": [e for e in inn if key(e) not in ek], "only_v2": [e for e in ext if key(e) not in ik],
            "external": ext, "internal": inn, "trend_now": r["state"][-1]["trend"] if r["state"] else "none",
            "protected_now": r["state"][-1]["protected"] if r["state"] else None}


def s2_with_swing_structure(ext_events: list[dict]):
    """Вариант S2: условие «структура вверх» и выход по медвежьему слому — по ВНЕШНЕЙ структуре v2; остальное как S2 v1.0.0."""
    P = S2["params"]

    def fn(ctx):
        d = s2(ctx)
        if d["action"] == "WARMUP":
            return {**d, "strategy": S2_V2["id"], "version": S2_V2["version"]}
        i = ctx.i
        ev = [e for e in ext_events if e["idx"] <= i]
        if ctx.in_position:
            brk = [e for e in ev if e["idx"] == i and e["type"] in ("CHoCH_DOWN", "BOS_DOWN")]
            c = C("Медвежий слом внешней структуры", bool(brk), brk[0]["type"] if brk else "нет", brk[0]["type"] if brk else "нет",
                  "внешний CHoCH_DOWN на этом баре → выход")
            return decision(S2_V2, ctx, "EXIT" if brk else "HOLD", [c], None, d["inputs"], "на закрытии следующего 4H-бара")
        last = ev[-1] if ev else None
        up = last is not None and last["type"] in ("BOS_UP", "CHoCH_UP")
        recent = any(e["type"] in ("BOS_UP", "CHoCH_UP") and i - e["idx"] <= P["choch_lookback"] for e in ev)
        c = C("Внешняя структура вверх (swing v2)", up and recent,
              f"последнее внешнее событие {last['type'] if last else 'нет'}; BOS/CHoCH вверх за {P['choch_lookback']} баров: {'да' if recent else 'нет'}",
              last["type"] if last else "нет событий", f"внешний тренд вверх и BOS/CHoCH вверх за {P['choch_lookback']} баров")
        conds = [c if x["name"].startswith("Структура вверх") else x for x in d["conditions"]]
        ok = all(x["ok"] is not False for x in conds) and any(x["ok"] for x in conds) and d["plan"] is not None
        bad = next((x["name"] for x in conds if x["ok"] is False), "")
        return decision(S2_V2, ctx, "ENTER" if ok else "WAIT", conds, d["plan"], d["inputs"], "" if ok else f"блокирует: {bad}")
    return fn


def run_s2(s: Series, fn=None, tag: str = "v1") -> Trader:
    """Пошаговый прогон S2 (исполнение по open следующей свечи, консервативные издержки) — в памяти, без записи в БД."""
    a = analyze(s, ANALYSIS_PARAMS["4h"])
    t = Trader("4h", "BACKTEST", f"RESEARCH-{tag}", CONSERVATIVE_V1, "1000")
    if fn is not None:
        t.fn = fn
    for i in range(len(s)):
        t.process_bar(a, i, None, None)
    return t


def s2_research(s: Series, k: float = SV2.K_ATR) -> dict:
    a = analyze(s, ANALYSIS_PARAMS["4h"])
    ext = SV2.analyze_structure(a, k)["external"]
    t1 = run_s2(s, None, "v1")
    t2 = run_s2(s, s2_with_swing_structure(ext), "swingv2")
    rows = []
    for d1, d2 in zip(t1.decisions, t2.decisions):
        c1 = next((c for c in d1["conditions"] if c["name"].startswith("Структура вверх")), None)
        c2 = next((c for c in d2["conditions"] if c["name"].startswith("Внешняя структура")), None)
        if (d1["action_final"] != d2["action_final"]) or (c1 and c2 and c1["ok"] != c2["ok"]):
            rows.append({"bar_ts": d1["bar_ts"], "v1_action": d1["action_final"], "v2_action": d2["action_final"],
                         "v1_structure": None if c1 is None else c1["ok"], "v2_structure": None if c2 is None else c2["ok"],
                         "v1_blocker": d1["against"][0] if d1["against"] else None, "v2_blocker": d2["against"][0] if d2["against"] else None})
    is_struct = lambda c: c["name"].startswith("Структура вверх") or c["name"].startswith("Внешняя структура")  # noqa: E731

    def summ(t):
        live = [d for d in t.decisions if d["action_final"] not in ("WARMUP",) and not d.get("in_position")]
        fails = [sum(1 for c in d["conditions"] if not is_struct(c) and c["ok"] is False) for d in live]
        sok = [any(is_struct(c) and c["ok"] for c in d["conditions"]) for d in live]
        return {"decisions": len(t.decisions), "enters": sum(1 for d in t.decisions if d["action_final"] == "ENTER"),
                "trades": len(t.trades), "net_pnl": float(sum(float(x["net_pnl"]) for x in t.trades)),
                "struct_pass": sum(sok),
                "others_all_ok": sum(1 for f in fails if f == 0),                       # прочие условия выполнены
                "near_miss": sum(1 for f, ok in zip(fails, sok) if f == 1 and ok),      # структура ок + ровно 1 прочий блокер
                "top_blocker": Counter(c["name"] for d in live for c in d["conditions"] if not is_struct(c) and c["ok"] is False).most_common(1)}
    return {"bars": len(s), "v1": summ(t1), "v2": summ(t2), "differences": rows, "trades_v1": t1.trades, "trades_v2": t2.trades}


def report(k: float = SV2.K_ATR) -> dict:
    sets = load_sets()
    for kind in ("range", "trend", "regime"):
        for seed in (1, 2, 3):
            sets[f"synth {kind} #{seed} 4h"] = synthetic(kind, seed=seed)
    comp = {n: compare_events(s, k) for n, s in sets.items()}
    research = {n: s2_research(s, k) for n, s in sets.items() if s.tf == "4h"}
    return {"k_atr": k, "compare": comp, "s2": research}


def sensitivity(ks=(2.0, 2.5, 3.0, 4.0), seeds=(1, 2, 3)) -> dict:
    """Чувствительность к k на данных, где «правильный» ответ известен (синтетика), и частота событий на реальных рядах.
    Выбор k не опирается на график FABLES: FABLES/ETH/BTC показаны только как контроль частоты событий."""
    out = {}
    real = load_sets()
    for k in ks:
        row = {"range_false_choch": [], "trend_acc": [], "regime_acc": [], "down_lag": [], "real": {}}
        for seed in seeds:
            r = SV2.analyze_structure(analyze(synthetic("range", seed=seed), ANALYSIS_PARAMS["4h"]), k)
            row["range_false_choch"].append(sum(1 for e in r["external"] if e["type"].startswith("CHoCH")))
            st = SV2.analyze_structure(analyze(synthetic("trend", seed=seed), ANALYSIS_PARAMS["4h"]), k)["state"]
            row["trend_acc"].append(round(sum(1 for x in st[100:] if x["trend"] == "up") / len(st[100:]), 3))
            st = SV2.analyze_structure(analyze(synthetic("regime", seed=seed), ANALYSIS_PARAMS["4h"]), k)["state"]
            ok = sum(1 for x in st[100:300] if x["trend"] == "up") + sum(1 for x in st[650:] if x["trend"] == "down")
            row["regime_acc"].append(round(ok / (200 + len(st[650:])), 3))
            row["down_lag"].append(next((i - 600 for i in range(600, len(st)) if all(x["trend"] == "down" for x in st[i:])), None))
        for n, s in real.items():
            r = SV2.analyze_structure(analyze(s, ANALYSIS_PARAMS.get(s.tf, {})), k)
            row["real"][n] = {"ext_per100": round(100 * len(r["external"]) / len(s), 1),
                              "choch_per100": round(100 * sum(1 for e in r["external"] if e["type"].startswith("CHoCH")) / len(s), 1)}
        out[k] = row
    v1 = [sum(1 for e in SV2.analyze_structure(analyze(synthetic("range", seed=q), ANALYSIS_PARAMS["4h"]))["internal"]
              if e["type"].startswith("iCHoCH")) for q in seeds]
    return {"k": out, "v1_range_false_choch": v1}
