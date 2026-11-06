"""ПРОТОТИП этапа A (исследование, не для торговли): иерархическая рыночная структура v3.

Назначение — проверить предлагаемую спецификацию (docs/STRUCTURE_V3_SPEC.md) на FABLES, BTC/ETH и синтетике до
реализации этапа B. Стратегиями не используется, в БД не пишет.

Модель (один и тот же автомат на двух масштабах: major k_major, internal k_int):
  swings  — каузальный ATR-ZigZag (исправлен дефект v2: после подтверждения swing поиск противоположного экстремума
            начинается с самого swing, а не с бара подтверждения — экстремумы между ними не теряются);
  события — пробой ЗАЩИЩЁННОГО уровня по тени (high > PH / low < PL) = CHoCH (нарушение защиты);
            пробой структурного экстремума для продолжения: тень = факт (BREAK_FACT), close за уровнем = BOS
            (принятие цены); тень с закрытием обратно = SWEEP (protected не переносится);
  protected — назначается только событием major-уровня: после BOS — экстремум отката между пробитым swing и баром
            пробоя; после CHoCH — экстремум прошлой волны (кандидат до подтверждения); внутренние события major
            protected не меняют;
  состояние — UNDEFINED → UP/DOWN; после CHoCH — TRANSITION (с уклоном), подтверждение — BOS в новом направлении
            по закрытию за подтверждённым swing, сформированным после CHoCH; провал — нарушение кандидата → прежний тренд;
            RANGE — TRANSITION, в которой подтверждено ≥ 2 swing каждого вида без подтверждения направления.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..candles import TF_SEC, Series


# ------------------------------------------------------------------ волатильность
def true_range(s: Series) -> list[float]:
    k = s.candles
    return [k[0].h - k[0].l] + [max(k[i].h, k[i - 1].c) - min(k[i].l, k[i - 1].c) for i in range(1, len(k))]


def vol_series(s: Series, mode: str = "atr", n: int = 14, skip: int = 0) -> list[float | None]:
    """atr — Wilder ATR(n) как в ядре; skip — сколько первых баров (аукцион запуска) не учитывать в оценке."""
    tr = true_range(s)
    out: list[float | None] = [None] * len(tr)
    if len(tr) < skip + n:
        return out
    a = sum(tr[skip:skip + n]) / n
    out[skip + n - 1] = a
    for i in range(skip + n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


# ------------------------------------------------------------------ swings
@dataclass
class Swing:
    kind: str
    idx: int
    ts: int
    price: float
    confirmed_idx: int
    label: str = ""
    role: str = ""            # структурная роль (protected / structural / internal) — назначается автоматом
    role_idx: int | None = None

    def d(self) -> dict:
        return asdict(self)


def zigzag(s: Series, vol: list, k: float, r: float = 0.0) -> list[Swing]:
    """Каузальный ATR-ZigZag. Кандидат — экстремум текущей ноги; он подтверждается, когда МАКСИМАЛЬНАЯ реакция после
    него (а не только текущая свеча) достигла k·vol[i] (vol на баре подтверждения — известен на его закрытии).
    Исправления относительно v2: (1) реакция считается по бегущему экстремуму после кандидата; (2) после
    подтверждения новый кандидат ищется начиная с самого swing, экстремумы до бара подтверждения не теряются;
    (3) в начале истории первым подтверждается тот экстремум, который был раньше (например, свеча запуска).
    r > 0 — исследованный и ОТКЛОНЁННЫЙ иерархический критерий Доу (реакция ≥ r от текущей волны)."""
    h, l, ts = s.col("h"), s.col("l"), s.ts
    n = len(h)
    out: list[Swing] = []
    if not n:
        return out
    leg = None
    ch, cl = 0, 0                    # кандидаты H и L
    rl, rh = float("inf"), float("-inf")    # реакция после кандидата H (минимум после ch) и после кандидата L (максимум после cl)
    for i in range(n):
        if leg in (None, "up"):
            if h[i] > h[ch] or (leg == "up" and ch < 0):
                ch, rl = i, float("inf")
            elif i > ch:
                rl = min(rl, l[i])
        if leg in (None, "down"):
            if l[i] < l[cl]:
                cl, rh = i, float("-inf")
            elif i > cl:
                rh = max(rh, h[i])
        v = vol[i]
        if v is None:
            continue
        thr = k * v
        last_l = next((w.price for w in reversed(out) if w.kind == "L"), None)
        last_h = next((w.price for w in reversed(out) if w.kind == "H"), None)
        thr_h = max(thr, r * (h[ch] - last_l)) if r and last_l is not None else thr
        thr_l = max(thr, r * (last_h - l[cl])) if r and last_h is not None else thr
        can_h = leg in (None, "up") and rl < float("inf") and h[ch] - rl >= thr_h
        can_l = leg in (None, "down") and rh > float("-inf") and rh - l[cl] >= thr_l
        if leg is None and can_h and can_l:            # оба кандидата созрели — первым подтверждается более ранний
            can_h, can_l = ch < cl, cl < ch
        if can_h:
            out.append(Swing("H", ch, ts[ch], h[ch], i))
            leg = "down"
            cl = min(range(ch + 1, i + 1), key=lambda x: l[x])
            rh = max((h[x] for x in range(cl + 1, i + 1)), default=float("-inf"))
        elif can_l:
            out.append(Swing("L", cl, ts[cl], l[cl], i))
            leg = "up"
            ch = max(range(cl + 1, i + 1), key=lambda x: h[x])
            rl = min((l[x] for x in range(ch + 1, i + 1)), default=float("inf"))
    prev: dict = {}
    for sw in out:
        q = prev.get(sw.kind)
        sw.label = "" if q is None else ("HH" if sw.price > q.price else "LH") if sw.kind == "H" else ("HL" if sw.price > q.price else "LL")
        prev[sw.kind] = sw
    return out


# ------------------------------------------------------------------ автомат структуры
@dataclass
class State:
    trend: str = "UNDEFINED"          # UP / DOWN / TRANSITION / RANGE / UNDEFINED
    bias: str = ""                    # для TRANSITION/RANGE: up / down
    ph: dict | None = None            # protected high {price, src_idx, set_idx, reason, status}
    pl: dict | None = None            # protected low
    since: int = 0                    # бар последнего события major
    trans_from: str = ""              # тренд до CHoCH
    trans_swings: dict = field(default_factory=lambda: {"H": 0, "L": 0})
    pending_bos: dict | None = None   # тень за структурным уровнем без закрытия — ждём принятия ценой


def run(s: Series, swings: list[Swing], scope: str = "major", vol: list | None = None, k: float = 3.0, init: str = "range",
        rho: float = 0.0, k_abs: float = 6.0) -> dict:
    """Пошаговый автомат. Для бара i используются только swings, подтверждённые на барах < i
    (подтверждение известно на закрытии бара confirmed_idx)."""
    h, l, c, ts = s.col("h"), s.col("l"), s.col("c"), s.ts
    dt = TF_SEC[s.tf]
    st = State()
    events, prot_hist, states = [], [], []
    known: list[Swing] = []
    q = 0

    def ev(i, typ, level, src, extra=None, why=""):
        e = {"type": typ, "scope": scope, "idx": i, "ts": ts[i], "known_ts": ts[i] + dt, "level": level,
             "trigger": "high" if typ.endswith("UP") or typ in ("SWEEP_HIGH", "BREAK_FACT_UP") else "low",
             "trigger_price": h[i] if (typ.endswith("UP") or typ in ("SWEEP_HIGH", "BREAK_FACT_UP")) else l[i],
             "close": c[i], "src": src, "prev": f"{st.trend}{('/' + st.bias) if st.bias else ''}", "why": why, **(extra or {})}
        events.append(e)
        return e

    def set_prot(i, side, price, src_idx, reason, status="PROTECTED"):
        old = st.ph if side == "H" else st.pl
        rec = {"side": side, "price": price, "src_idx": src_idx, "src_ts": ts[src_idx], "set_idx": i, "set_ts": ts[i] + dt,
               "reason": reason, "status": status, "replaced": None if old is None else old["price"]}
        prot_hist.append(rec)
        if side == "H":
            st.ph = rec
        else:
            st.pl = rec

    def clear_prot(i, side, why):
        cur = st.ph if side == "H" else st.pl
        if cur is not None:
            prot_hist.append({**cur, "status": "VIOLATED" if "наруш" in why else "RETIRED", "set_idx": i, "set_ts": ts[i] + dt, "reason": why})
        if side == "H":
            st.ph = None
        else:
            st.pl = None

    def target(kind: str, i: int):
        """Структурный экстремум ноги для BOS — самая крайняя точка ноги (тени и свипы учитываются), после которой
        был откат ≥ k·vol и ещё не было закрытия за ней. Нужен хотя бы один подтверждённый major-swing этого вида
        в ноге. Пробой более низкого LH (более высокого HL) внутри ноги — не BOS. Начало ноги — источник
        противоположного protected (минимум, с которого вырос подъём / максимум, с которого началось падение)."""
        start = (st.pl["src_idx"] if kind == "H" and st.pl else st.ph["src_idx"] if kind == "L" and st.ph else st.since)
        conf = [w for w in known if w.kind == kind and w.idx > start]
        if not conf or i - 1 <= start:
            return None
        v = vol[i - 1] if vol is not None and i >= 1 else None
        if v is None:
            return None
        # кандидаты: точки ноги, после которых был откат ≥ k·vol и ещё не было ЗАКРЫТИЯ за ними; берётся самая крайняя.
        # Тень-sweep без отката не заменяет подтверждённый экстремум; пробитый закрытием уровень повторно не цель.
        best = None
        lo_r, hi_r = float("inf"), float("-inf")          # экстремумы справа от j: low/high и close на (j, i)
        cmax, cmin = float("-inf"), float("inf")
        for j in range(i - 1, start, -1):                    # один проход справа налево (тот же результат, O(ноги))
            if j < i - 1:
                lo_r, hi_r = min(lo_r, l[j + 1]), max(hi_r, h[j + 1])
                cmax, cmin = max(cmax, c[j + 1]), min(cmin, c[j + 1])
                if kind == "H":
                    if h[j] - lo_r >= k * v and cmax <= h[j] and (best is None or h[j] >= h[best]):
                        best = j
                elif hi_r - l[j] >= k * v and cmin >= l[j] and (best is None or l[j] <= l[best]):
                    best = j
        if best is None:
            return None
        j = best
        lw = conf[-1]
        return Swing(kind, j, ts[j], h[j] if kind == "H" else l[j], max(lw.confirmed_idx, j),
                     "структурный " + ("максимум" if kind == "H" else "минимум"))

    for i in range(len(c)):
        while q < len(swings) and swings[q].confirmed_idx < i:
            w = swings[q]
            known.append(w)
            if st.trend in ("TRANSITION", "RANGE") and w.idx > st.since:
                st.trans_swings[w.kind] += 1
            q += 1
        hits = []
        # ---------- 1. нарушение protected (тень)
        if st.ph is not None and h[i] > st.ph["price"]:
            hits.append("PH")
        if st.pl is not None and l[i] < st.pl["price"]:
            hits.append("PL")
        if len(hits) == 2:                                               # неоднозначная свеча: порядок неизвестен
            ev(i, "AMBIGUOUS", None, None, {"levels": {"PH": st.ph["price"], "PL": st.pl["price"]}},
               "свеча нарушила оба protected; порядок внутри свечи без тиков неизвестен")
            clear_prot(i, "H", "нарушен (неоднозначная свеча)")
            clear_prot(i, "L", "нарушен (неоднозначная свеча)")
            st.trend, st.bias, st.since, st.trans_swings = "TRANSITION", "", i, {"H": 0, "L": 0}
            states.append({"trend": st.trend, "bias": st.bias, "ph": None, "pl": None})
            continue
        if hits == ["PH"]:
            lvl = st.ph["price"]
            src = st.ph
            sweep = c[i] <= lvl
            if st.trend == "DOWN" or (st.trend in ("TRANSITION", "RANGE") and st.bias == "down"):
                typ = "CHoCH_UP"
                why = "high выше protected high: нисходящая структура нарушена" + (" (тень, close обратно ниже — sweep)" if sweep else "")
            else:                                                        # провал медвежьей попытки → восстановление UP
                typ = "CHoCH_UP"
                why = "high выше protected high переходной фазы: медвежья попытка не подтвердилась"
            prev_trend = st.trend
            ev(i, typ, lvl, src, {"sweep": sweep, "violation": True}, why)
            clear_prot(i, "H", "нарушен тенью")
            if prev_trend == "TRANSITION" and st.trans_from == "UP":
                # медвежья переходная фаза провалилась: возврат к UP, protected low = минимум переходной фазы
                j = min(range(st.since, i + 1), key=lambda x: l[x])
                set_prot(i, "L", l[j], j, "восстановление UP после неудачной смены: минимум переходной фазы")
                st.trend, st.bias, st.trans_from = "UP", "", ""
            else:
                j = min(range(src["src_idx"], i + 1), key=lambda x: l[x])
                set_prot(i, "L", l[j], j, "кандидат после CHoCH_UP: минимум прошлой нисходящей волны", "CANDIDATE")
                st.trend, st.bias, st.trans_from = "TRANSITION", "up", prev_trend if prev_trend in ("UP", "DOWN") else "DOWN"
                st.trans_swings = {"H": 0, "L": 0}
            st.since, st.pending_bos = i, None
            states.append({"trend": st.trend, "bias": st.bias, "ph": st.ph and st.ph["price"], "pl": st.pl and st.pl["price"]})
            continue
        if hits == ["PL"]:
            lvl = st.pl["price"]
            src = st.pl
            sweep = c[i] >= lvl
            prev_trend = st.trend
            ev(i, "CHoCH_DOWN", lvl, src, {"sweep": sweep, "violation": True},
               "low ниже protected low: восходящая структура нарушена" + (" (тень, close обратно выше — sweep)" if sweep else ""))
            clear_prot(i, "L", "нарушен тенью")
            if prev_trend == "TRANSITION" and st.trans_from == "DOWN":
                j = max(range(st.since, i + 1), key=lambda x: h[x])
                set_prot(i, "H", h[j], j, "возврат к DOWN после неудачной смены: максимум переходной фазы")
                st.trend, st.bias, st.trans_from = "DOWN", "", ""
            else:
                j = max(range(src["src_idx"], i + 1), key=lambda x: h[x])
                set_prot(i, "H", h[j], j, "кандидат после CHoCH_DOWN: максимум прошлой восходящей волны", "CANDIDATE")
                st.trend, st.bias, st.trans_from = "TRANSITION", "down", prev_trend if prev_trend in ("UP", "DOWN") else "UP"
                st.trans_swings = {"H": 0, "L": 0}
            st.since, st.pending_bos = i, None
            states.append({"trend": st.trend, "bias": st.bias, "ph": st.ph and st.ph["price"], "pl": st.pl and st.pl["price"]})
            continue
        # ---------- 2. продолжение / подтверждение: структурный экстремум, тень = факт, close = BOS
        up_side = st.trend == "UP" or (st.trend in ("TRANSITION", "RANGE") and st.bias == "up")
        dn_side = st.trend == "DOWN" or (st.trend in ("TRANSITION", "RANGE") and st.bias == "down")
        done = False
        for kind, active in (("H", up_side), ("L", dn_side)):
            if not active:
                continue
            tg = target(kind, i)
            if tg is None:
                continue
            crossed = h[i] > tg.price if kind == "H" else l[i] < tg.price
            pend = st.pending_bos if st.pending_bos and st.pending_bos["src"] == tg.idx else None
            if not crossed and not pend:
                continue
            accepted = c[i] > tg.price if kind == "H" else c[i] < tg.price
            if not accepted:
                if crossed:
                    ev(i, "SWEEP_HIGH" if kind == "H" else "SWEEP_LOW", tg.price, {"idx": tg.idx, "ts": tg.ts, "label": tg.label},
                       {"violation": False}, "тень за структурным экстремумом, close вернулся — ликвидность снята, BOS нет")
                    st.pending_bos = {"src": tg.idx, "first_idx": i}
                continue
            typ = "BOS_UP" if kind == "H" else "BOS_DOWN"
            confirm = st.trend in ("TRANSITION", "RANGE")
            first = pend["first_idx"] if pend else i
            # protected: экстремум отката между пробитым swing и баром пробоя
            if kind == "H":
                j = min(range(tg.idx, i + 1), key=lambda x: l[x])
                new = (l[j], j)
            else:
                j = max(range(tg.idx, i + 1), key=lambda x: h[x])
                new = (h[j], j)
            ev(i, typ, tg.price, {"idx": tg.idx, "ts": tg.ts, "label": tg.label, "confirmed_ts": ts[tg.confirmed_idx] + dt},
               {"confirmation": confirm, "break_fact_idx": first, "break_fact_ts": ts[first]},
               ("подтверждение нового направления: " if confirm else "продолжение: ") +
               f"close за подтверждённым структурным {'максимумом' if kind == 'H' else 'минимумом'}")
            side = "L" if kind == "H" else "H"
            cur = st.pl if side == "L" else st.ph
            bounce = abs(new[0] - tg.price)                       # амплитуда отката, из которого вырос пробой
            leg = abs((cur["price"] if cur else new[0]) - tg.price)    # текущая major-волна: от protected до пробитого экстремума
            significant = (cur is None or confirm or rho <= 0 or bounce >= rho * leg
                           or (vol is not None and vol[i] is not None and bounce >= k_abs * vol[i]))
            if significant:
                set_prot(i, side, new[0], new[1], f"{typ}: экстремум отката между пробитым экстремумом и пробоем"
                         + ("" if cur is None or confirm or rho <= 0 else f" (откат {bounce / leg:.0%} major-волны)"))
            else:
                events[-1]["protected_kept"] = True
                events[-1]["why"] += (f"; protected НЕ перенесён: откат {bounce / leg:.0%} волны < {rho:.0%} и < {k_abs:g}·ATR"
                                      " — внутренний отскок не становится major-уровнем")
            if confirm:
                other = "H" if kind == "H" else "L"
                clear_prot(i, other, "снят: направление подтверждено")
            st.trend, st.bias, st.trans_from = ("UP" if kind == "H" else "DOWN"), "", ""
            st.since, st.pending_bos = i, None
            done = True
            break
        # ---------- 3. UNDEFINED: начальное направление
        if not done and st.trend == "UNDEFINED":
            if init == "range":
                # v3b: границы major-диапазона — структурные экстремумы ВСЕЙ истории (с откатом ≥ k·vol, без закрытия за
                # ними). Swing внутри диапазона — внутренние; направление задаёт первое закрытие за границей диапазона.
                tH, tL = target("H", i), target("L", i)
                up = tH is not None and c[i] > tH.price
                dn = tL is not None and c[i] < tL.price
                if not up and not dn:
                    if tL is not None and l[i] < tL.price:
                        ev(i, "SWEEP_LOW", tL.price, {"idx": tL.idx, "ts": tL.ts, "label": tL.label}, {"violation": False},
                           "тень за границей начального диапазона, close вернулся")
                    if tH is not None and h[i] > tH.price:
                        ev(i, "SWEEP_HIGH", tH.price, {"idx": tH.idx, "ts": tH.ts, "label": tH.label}, {"violation": False},
                           "тень за границей начального диапазона, close вернулся")
                w = (tH if up else tL) if up != dn else None
                why = "начальное направление: первое закрытие за границей major-диапазона (структурный экстремум истории)"
            else:
                # v3a (этап A): последний подтверждённый swing — может оказаться внутренним (подмена масштаба)
                hs = [w for w in known if w.kind == "H"]
                ls = [w for w in known if w.kind == "L"]
                up = bool(hs and ls) and c[i] > hs[-1].price
                dn = bool(hs and ls) and c[i] < ls[-1].price
                w = (hs[-1] if up else ls[-1]) if up != dn else None
                why = "начальное направление: первое закрытие за последним подтверждённым swing"
            if w is not None:
                ev(i, "BOS_UP" if up else "BOS_DOWN", w.price, {"idx": w.idx, "ts": w.ts, "label": w.label}, {"initial": True}, why)
                if up:
                    j = min(range(w.idx, i + 1), key=lambda x: l[x])
                    set_prot(i, "L", l[j], j, "начальный BOS_UP: минимум между пробитым экстремумом и пробоем")
                else:
                    j = max(range(w.idx, i + 1), key=lambda x: h[x])
                    set_prot(i, "H", h[j], j, "начальный BOS_DOWN: максимум между пробитым экстремумом и пробоем")
                st.trend, st.since = ("UP" if up else "DOWN"), i
        if st.trend == "TRANSITION" and st.trans_swings["H"] >= 2 and st.trans_swings["L"] >= 2:
            st.trend = "RANGE"
        # фаза по структурным уровням: IMPULSE — нога ещё расширяет экстремум (нет подтверждённого отката);
        # PULLBACK — структурный экстремум подтверждён откатом, нового BOS ещё нет
        if st.trend in ("UP", "DOWN"):
            tgt = target("H" if st.trend == "UP" else "L", i + 1) if i + 1 <= len(c) else None
            ph_ = "PULLBACK" if tgt is not None else "IMPULSE"
        else:
            ph_ = {"RANGE": "CONSOLIDATION"}.get(st.trend, "TRANSITION")
        states.append({"trend": st.trend, "bias": st.bias, "ph": st.ph and st.ph["price"], "pl": st.pl and st.pl["price"], "phase": ph_})
    return {"events": events, "protected_history": prot_hist, "state": states, "swings": [w.d() for w in swings]}


def phase(s: Series, swings: list[Swing], state: list[dict]) -> list[str]:
    """IMPULSE / PULLBACK / CONSOLIDATION / TRANSITION по major-тренду и текущей ноге ZigZag (с учётом неподтверждённой)."""
    out = []
    q = 0
    last = None
    for i in range(len(state)):
        while q < len(swings) and swings[q].confirmed_idx <= i:
            last = swings[q]
            q += 1
        leg = None if last is None else ("down" if last.kind == "H" else "up")
        t = state[i]["trend"]
        if t in ("TRANSITION", "UNDEFINED"):
            out.append("TRANSITION")
        elif t == "RANGE":
            out.append("CONSOLIDATION")
        else:
            out.append("IMPULSE" if (leg == "up") == (t == "UP") else "PULLBACK")
    return out


def analyze(s: Series, k_major: float = 3.0, k_int: float = 1.5, skip: int = 0, r_major: float = 0.0, init: str = "range",
            rho: float = 0.0, k_abs: float | None = None) -> dict:
    vol = vol_series(s, "atr", 14, skip)
    sw_m, sw_i = zigzag(s, vol, k_major, r_major), zigzag(s, vol, k_int)
    m, it = run(s, sw_m, "major", vol, k_major, init, rho, float("inf") if k_abs is None else k_abs), run_internal(s, sw_i)
    return {"major": m, "internal": it, "phase": [x.get("phase", "TRANSITION") for x in m["state"]], "k_major": k_major, "k_int": k_int, "skip": skip,
            "r_major": r_major, "init": init, "rho": rho}


def run_internal(s: Series, swings: list[Swing]) -> dict:
    """Internal / minor: ранние признаки. Тренд меняется при ЗАКРЫТИИ за последним подтверждённым внутренним swing
    противоположного вида (iCHoCH), продолжение — закрытие за последним swing по направлению (iBOS). Тень за уровнем
    с закрытием обратно — iSWEEP. Major protected этим слоем не меняется."""
    h, l, c, ts = s.col("h"), s.col("l"), s.col("c"), s.ts
    dt = TF_SEC[s.tf]
    trend, events, states, q = "UNDEFINED", [], [], 0
    lastH = lastL = None
    usedH = usedL = -1
    for i in range(len(c)):
        while q < len(swings) and swings[q].confirmed_idx < i:
            w = swings[q]
            if w.kind == "H":
                lastH = w
            else:
                lastL = w
            q += 1
        typ = None
        if lastH is not None and lastH.idx != usedH and h[i] > lastH.price:
            if c[i] > lastH.price:
                typ, lvl, src = ("iBOS_UP" if trend == "UP" else "iCHoCH_UP"), lastH.price, lastH
                usedH = lastH.idx
            else:
                events.append({"type": "iSWEEP_HIGH", "scope": "internal", "idx": i, "ts": ts[i], "known_ts": ts[i] + dt, "level": lastH.price,
                               "trigger_price": h[i], "close": c[i], "prev": trend, "src": {"idx": lastH.idx, "ts": lastH.ts, "label": lastH.label}})
        if typ is None and lastL is not None and lastL.idx != usedL and l[i] < lastL.price:
            if c[i] < lastL.price:
                typ, lvl, src = ("iBOS_DOWN" if trend == "DOWN" else "iCHoCH_DOWN"), lastL.price, lastL
                usedL = lastL.idx
            else:
                events.append({"type": "iSWEEP_LOW", "scope": "internal", "idx": i, "ts": ts[i], "known_ts": ts[i] + dt, "level": lastL.price,
                               "trigger_price": l[i], "close": c[i], "prev": trend, "src": {"idx": lastL.idx, "ts": lastL.ts, "label": lastL.label}})
        if typ:
            if trend == "UNDEFINED":
                typ = typ.replace("iCHoCH", "iBOS")
            events.append({"type": typ, "scope": "internal", "idx": i, "ts": ts[i], "known_ts": ts[i] + dt, "level": lvl,
                           "trigger_price": h[i] if typ.endswith("UP") else l[i], "close": c[i], "prev": trend,
                           "src": {"idx": src.idx, "ts": src.ts, "label": src.label, "confirmed_ts": ts[src.confirmed_idx] + dt}})
            trend = "UP" if typ.endswith("UP") else "DOWN"
        states.append({"trend": trend, "bias": "", "ph": lastH and lastH.price, "pl": lastL and lastL.price})
    return {"events": events, "state": states, "swings": [w.d() for w in swings]}
