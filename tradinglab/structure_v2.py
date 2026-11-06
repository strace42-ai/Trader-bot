"""Структурный анализ v2 (ИССЛЕДОВАНИЕ, стратегиями не используется): внутренняя и внешняя (swing) структура.

v1 (`structure.structure_events`, используется S2 v1.0.0) — это ВНУТРЕННЯЯ структура: pivots 3/3, направление меняется
при пробое ПОСЛЕДНЕГО известного pivot. В v2 она сохраняется без изменений как iBOS / iCHoCH.

ВНЕШНЯЯ swing-структура (BOS / CHoCH):
1. Swings — ATR-zigzag: на восходящем отрезке отслеживается максимум; он подтверждается как swing high, когда low
   откатывается от него на ≥ k·ATR(14) (ATR — на баре подтверждения). Симметрично для swing low. Порог в единицах ATR
   масштабно-инвариантен (одинаков для 1H/4H/1D и разных активов). k = 3,0 — ИССЛЕДОВАТЕЛЬСКИЙ параметр (проверен на
   синтетике с известным режимом, не подобран по графику FABLES; окончательно оптимальным не считается).
   Подтверждение известно только на баре, где откат достиг порога (confirmed_idx) — без look-ahead.
2. Состояние: тренд up/down/none, «сильный» уровень для продолжения и protected-уровень для смены:
   - up: BOS_UP — close выше СИЛЬНОГО максимума волны (самый высокий подтверждённый swing high, сформированный после последнего события;
     пробой более низкого промежуточного максимума — внутренняя структура). protected low = минимум отката от пробитого
     swing high до бара пробоя (HL, из которого вырос пробой). CHoCH_DOWN — ТОЛЬКО close ниже protected low.
     Пробой промежуточных минимумов выше protected — внутренняя структура, тренд не меняется.
   - down: симметрично (BOS_DOWN, protected high, CHoCH_UP). После CHoCH protected = экстремум прошлой волны.
   - none: первый close за последним подтверждённым swing high/low — BOS (initial) задаёт направление.
3. Все уровни берутся из swings с confirmed_idx < i; событие на баре i использует только бары ≤ i.
4. Время фактического появления: known_ts события = закрытие свечи пробоя; known_ts pivot = закрытие свечи, на которой он
   подтвердился (для protected — закрытие свечи события, установившего protected). Ретроспективных меток нет.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from . import structure
from .candles import TF_SEC, Series

VERSION = "structure-v2.0-research"
K_ATR = 3.0


@dataclass(frozen=True)
class Swing:
    kind: str           # "H" | "L"
    idx: int
    ts: int
    price: float
    confirmed_idx: int
    label: str = ""     # HH/LH/HL/LL относительно предыдущего swing того же вида

    def d(self) -> dict:
        return asdict(self)


def atr_swings(s: Series, atr: list, k: float = K_ATR) -> list[Swing]:
    """Каузальный ATR-zigzag по high/low. Бары без ATR (warmup) не подтверждают свинги."""
    h, l, ts = s.col("h"), s.col("l"), s.ts
    out: list[Swing] = []
    if not h:
        return out
    leg = None                       # "up" — ищем swing high, "down" — ищем swing low
    hi_i = lo_i = 0
    for i in range(len(h)):
        if h[i] > h[hi_i]:
            hi_i = i
        if l[i] < l[lo_i]:
            lo_i = i
        a = atr[i] if i < len(atr) else None
        if a is None:
            continue
        thr = k * a
        if leg in (None, "up") and hi_i < i and h[hi_i] - l[i] >= thr and (leg == "up" or hi_i > lo_i):
            out.append(Swing("H", hi_i, ts[hi_i], h[hi_i], i))
            leg, lo_i = "down", i
            continue
        if leg in (None, "down") and lo_i < i and h[i] - l[lo_i] >= thr and (leg == "down" or lo_i > hi_i):
            out.append(Swing("L", lo_i, ts[lo_i], l[lo_i], i))
            leg, hi_i = "up", i
    lab, prev = [], {}
    for sw in out:
        q = prev.get(sw.kind)
        label = "" if q is None else ("HH" if sw.price > q.price else "LH") if sw.kind == "H" else ("HL" if sw.price > q.price else "LL")
        lab.append(Swing(sw.kind, sw.idx, sw.ts, sw.price, sw.confirmed_idx, label))
        prev[sw.kind] = sw
    return lab


def external_structure(s: Series, swings: list[Swing]) -> dict:
    """События внешней структуры и протокол состояния (trend, protected, target) по барам."""
    h, l, c, ts = s.col("h"), s.col("l"), s.col("c"), s.ts
    dt = TF_SEC[s.tf]
    events, state = [], []
    trend, protected, prot_src = "none", None, None
    prot_set = 0                    # бар события, на котором установлен текущий protected
    last_ev = 0                     # бар последнего события: цели BOS — только swings, сформированные не раньше него
    k = 0                           # swings отсортированы по confirmed_idx — добавляем по мере подтверждения
    known: list[Swing] = []
    for i in range(len(c)):
        while k < len(swings) and swings[k].confirmed_idx < i:
            known.append(swings[k])
            k += 1
        tgt = None
        if trend == "up":           # сильный максимум волны: самый высокий подтверждённый swing high после последнего события
            tgt = max((w for w in known if w.kind == "H" and w.idx >= last_ev), key=lambda w: w.price, default=None)
        elif trend == "down":
            tgt = min((w for w in known if w.kind == "L" and w.idx >= last_ev), key=lambda w: w.price, default=None)
        ev = None
        if trend == "down" and c[i] > protected:
            ev = ("CHoCH_UP", protected, None)
        elif trend == "up" and c[i] < protected:
            ev = ("CHoCH_DOWN", protected, None)
        elif trend == "up" and tgt and c[i] > tgt.price:
            ev = ("BOS_UP", tgt.price, tgt)
        elif trend == "down" and tgt and c[i] < tgt.price:
            ev = ("BOS_DOWN", tgt.price, tgt)
        elif trend == "none":       # начальное направление: первый close за последним подтверждённым swing
            hs = [w for w in known if w.kind == "H"]
            ls = [w for w in known if w.kind == "L"]
            if hs and c[i] > hs[-1].price:
                ev = ("BOS_UP", hs[-1].price, hs[-1])
            elif ls and c[i] < ls[-1].price:
                ev = ("BOS_DOWN", ls[-1].price, ls[-1])
        if ev:
            typ, level, src = ev
            prev = trend
            up = typ.endswith("UP")
            # BOS: protected = экстремум отката между пробитым swing и баром пробоя (HL/LH, из которого вырос пробой).
            # CHoCH: protected = экстремум предыдущей волны с последнего события (начало нового движения).
            start = src.idx if src is not None else last_ev
            if src is None:
                piv = {**prot_src, "confirmed_idx": prot_set, "confirmed_ts": ts[prot_set], "known_ts": ts[prot_set] + dt, "label": "protected"}
            else:
                piv = {"idx": src.idx, "ts": src.ts, "price": src.price, "kind": src.kind, "confirmed_idx": src.confirmed_idx,
                       "confirmed_ts": ts[src.confirmed_idx], "known_ts": ts[src.confirmed_idx] + dt, "label": src.label}
            rng = range(start, i + 1)
            j = min(rng, key=lambda x: l[x]) if up else max(rng, key=lambda x: h[x])
            protected = l[j] if up else h[j]
            prot_src = {"idx": j, "ts": ts[j], "price": protected, "kind": "L" if up else "H"}
            trend = "up" if up else "down"
            last_ev = prot_set = i
            events.append({"type": typ, "scope": "external", "idx": i, "ts": ts[i], "known_ts": ts[i] + dt, "level": level, "close": c[i], "pivot": piv,
                           "prev_trend": prev, "initial": prev == "none", "protected_after": protected, "protected_src": prot_src})
        state.append({"trend": trend, "protected": protected})
    return {"events": events, "state": state}


def internal_events(s: Series, pivots: list) -> list[dict]:
    """v1-события (используются S2 v1.0.0) в формате v2: iBOS / iCHoCH с полными данными pivot."""
    by = {p.idx: p for p in pivots}
    out, prev, dt = [], "none", TF_SEC[s.tf]
    for e in structure.structure_events(s, pivots):
        p = by[e["pivot_idx"]]
        out.append({"type": "i" + e["type"], "v1_type": e["type"], "scope": "internal", "idx": e["idx"], "ts": e["ts"], "known_ts": e["ts"] + dt, "level": e["level"],
                    "close": s.candles[e["idx"]].c, "prev_trend": prev,
                    "pivot": {"idx": p.idx, "ts": p.ts, "price": p.price, "kind": p.kind, "confirmed_idx": p.confirmed_idx,
                              "confirmed_ts": s.candles[p.confirmed_idx].ts, "known_ts": s.candles[p.confirmed_idx].ts + dt}})
        prev = "up" if e["type"].endswith("UP") else "down"
    return out


def analyze_structure(a, k: float = K_ATR) -> dict:
    """a — Analysis ядра. Возвращает внутренние и внешние события, swings и итоговое состояние."""
    sw = atr_swings(a.s, a.arr["atr"], k)
    ext = external_structure(a.s, sw)
    return {"version": VERSION, "k_atr": k, "swings": [x.d() for x in sw], "external": ext["events"], "state": ext["state"],
            "internal": internal_events(a.s, a.pivots)}
