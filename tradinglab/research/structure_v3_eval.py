"""Этап A: независимая проверка масштаба major-структуры (синтетика с известной разметкой + BTC/ETH)."""
from __future__ import annotations

import gzip
import io
import json
import math
import random
import subprocess

from .. import structure, structure_v2 as SV2
from ..analysis import analyze as core_analyze
from ..candles import Candle, Series
from . import structure_v3_proto as P

SCEN = {   # (баров, дрейф за бар, истинный major-режим)
    "up":            [(900, +0.0025, "UP")],
    "down":          [(900, -0.0025, "DOWN")],
    "range":         [(900, "range", "RANGE")],
    "regime":        [(300, +0.0025, "UP"), (300, "range", "RANGE"), (300, -0.0025, "DOWN")],
    "reversal":      [(450, -0.0025, "DOWN"), (450, +0.0025, "UP")],
    # восстановление внутри падения: откат вверх ~45% предыдущей волны, затем новый минимум — major остаётся DOWN
    "recovery_in_down": [(300, -0.0030, "DOWN"), (120, +0.0030, "DOWN"), (300, -0.0030, "DOWN")],
    # глубокая коррекция внутри роста: ~55% волны, затем новый максимум — major остаётся UP
    "deep_corr_in_up":  [(300, +0.0030, "UP"), (150, -0.0030, "UP"), (300, +0.0030, "UP")],
}


def synth(name: str, seed: int, tf: str = "4h", sigma: float = 0.012) -> tuple[Series, list[str]]:
    rnd, p, out, truth = random.Random(seed), 1.0, [], []
    lvl = None
    i = 0
    for n, drift, lab in SCEN[name]:
        if drift == "range":
            lvl = p
        for _ in range(n):
            d = -0.08 * math.log(p / lvl) if drift == "range" else drift
            o = p
            p = p * math.exp(d + rnd.gauss(0, sigma))
            h = max(o, p) * math.exp(abs(rnd.gauss(0, sigma / 3)))
            lo = min(o, p) * math.exp(-abs(rnd.gauss(0, sigma / 3)))
            out.append(Candle(i * 14400, o, h, lo, p, 1000.0))
            truth.append(lab)
            i += 1
    return Series(tf, out), truth


def binance(sym: str, tf: str) -> Series:
    """BTC/ETH из ветки исследования стратегий (только чтение через git show, без копирования)."""
    raw = subprocess.run(["git", "show", f"claude/strategy-audit-research:datasets/strategy_audit_2026-11-06/{sym}_{tf}.json.gz"],
                         capture_output=True, check=True).stdout
    rows = json.load(gzip.open(io.BytesIO(raw), "rt"))
    return Series(tf, [Candle(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows])


def trends(s: Series, model: str, k: float = 3.0, r: float = 0.0) -> list[str]:
    """Основной тренд по барам: v1 (внутренняя pivots 3/3), v2 (ATR-ZigZag k=3, close), v3 (major, k)."""
    if model == "v1":
        a = core_analyze(s)
        out, cur, by = [], "UNDEFINED", {}
        for e in structure.structure_events(s, a.pivots):
            by[e["idx"]] = "UP" if e["type"].endswith("UP") else "DOWN"
        for i in range(len(s)):
            cur = by.get(i, cur)
            out.append(cur)
        return out
    if model == "v2":
        a = core_analyze(s)
        return [{"up": "UP", "down": "DOWN"}.get(x["trend"], "UNDEFINED") for x in SV2.analyze_structure(a, k)["state"]]
    res = P.analyze(s, k_major=k, r_major=r)
    return [x["trend"] for x in res["major"]["state"]]


def score(tr: list[str], truth: list[str], warm: int = 100) -> dict:
    n = len(tr)
    ok = sum(1 for i in range(warm, n) if (tr[i] == truth[i]) or (truth[i] == "RANGE" and tr[i] in ("RANGE", "TRANSITION")))
    dirs = [x for x in tr if x in ("UP", "DOWN")]
    flips = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
    # смены направления там, где истина НЕ меняется (ложные), и задержка распознавания истинных смен
    false_dir = 0
    prev = None
    for i in range(warm, n):
        if tr[i] in ("UP", "DOWN"):
            if prev is not None and tr[i] != prev and truth[i] == truth[i - 1] and truth[i] != "RANGE":
                false_dir += 1
            prev = tr[i]
    lags = []
    for i in range(1, n):
        if truth[i] != truth[i - 1] and truth[i] in ("UP", "DOWN"):
            j = next((q for q in range(i, n) if tr[q] == truth[i]), None)
            lags.append(None if j is None else j - i)
    in_range = [i for i in range(n) if truth[i] == "RANGE"]
    rflips = 0
    for a, b in zip(in_range, in_range[1:]):
        if b == a + 1 and tr[a] in ("UP", "DOWN") and tr[b] in ("UP", "DOWN") and tr[a] != tr[b]:
            rflips += 1
    return {"acc": round(ok / max(1, n - warm), 3), "flips": flips, "false_dir": false_dir, "range_flips": rflips, "lags": lags}


# ------------------------------------------------------------------ вложенные волны (контроль выбора масштаба)
def nested(seed: int, n_cycles: int = 6, reversal: bool = False, tf: str = "1h", step: float = 0.015, noise: float = 0.012) -> tuple[Series, dict]:
    """Первичный нисходящий тренд из ВТОРИЧНЫХ волн (спад к новому минимуму, восстановление r·волны, r ∈ [0,4; 0,85]),
    каждая нога разбита на ТРЕТИЧНЫЕ подволны (контрдвижение ≤ 0,3 ноги — всегда меньше вторичного восстановления).
    Начало как у FABLES: обвал после всплеска, отскок-LH (H1), внутренний HL, меньший LH, пробой первого минимума.
    Истина: после пробоя первого минимума major DOWN; protected high = вершина последнего ВТОРИЧНОГО восстановления;
    reversal=True — последнее восстановление r = 1,35 проходит эту вершину (настоящая смена характера)."""
    rnd = random.Random(seed)
    lp = [0.0]

    def leg(to, sub=True):
        a = lp[-1]
        d = to - a
        if sub and abs(d) > 0.08:
            p1 = a + 0.6 * d
            p2 = p1 - rnd.uniform(0.15, 0.3) * d             # третичный откат ≤ 0,3 ноги
            lp.extend([p1, p2])
        lp.append(to)
    leg(-0.9, sub=False)                                   # обвал после всплеска (как свеча запуска)
    L1 = lp[-1]
    leg(L1 + 0.45, sub=False); H1 = lp[-1]                 # LH первичной структуры
    leg(L1 + 0.08, sub=False)                              # внутренний HL (выше L1)
    leg(L1 + 0.28, sub=False)                              # меньший внутренний LH (ниже H1)
    leg(L1 - rnd.uniform(0.25, 0.4))                       # пробой L1
    tops = [H1]
    rev_anchor = None
    for cyc in range(n_cycles):
        D = lp[-1]
        if reversal and cyc == n_cycles - 1:
            rev_anchor = len(lp) - 1
        r = 1.35 if (reversal and cyc == n_cycles - 1) else rnd.uniform(0.4, 0.85)
        top = D + r * (tops[-1] - D)
        leg(top)
        if r >= 1:
            leg(top - 0.4 * (top - D)); leg(top + 0.3)
            break
        tops.append(top)
        leg(D - rnd.uniform(0.15, 0.3))
    out, t, anchor_bar = [], 0, [0]
    for a, b in zip(lp, lp[1:]):
        n = max(2, int(abs(b - a) / step))
        for q in range(1, n + 1):
            c = math.exp(a + (b - a) * q / n + rnd.gauss(0, noise / 2))
            o = out[-1].c if out else math.exp(lp[0])
            h = max(o, c) * math.exp(abs(rnd.gauss(0, noise / 2)))
            lo_ = min(o, c) * math.exp(-abs(rnd.gauss(0, noise / 2)))
            out.append(Candle(t * 3600, o, h, lo_, c, 1000.0))
            t += 1
        anchor_bar.append(t - 1)
    return Series(tf, out), {"L1": math.exp(L1), "H1": math.exp(H1), "tops": [math.exp(x) for x in tops], "reversal": reversal,
                             "rev_bar": None if rev_anchor is None else anchor_bar[rev_anchor]}
