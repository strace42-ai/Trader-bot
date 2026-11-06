"""Структурный анализ v3 (ИССЛЕДОВАНИЕ и визуализация, стратегиями не используется).

Иерархическая модель рынка, пошаговая по построению: каждый бар обрабатывается только по свечам, закрытым к его
закрытию (`Engine.step`). Пакетный прогон — тот же цикл, поэтому результат на обрезанной истории совпадает с префиксом
полного прогона, а состояние можно сохранить и продолжить (`to_state` / `from_state`).

Масштабы:
  internal — ZigZag k_int·ATR; тренд меняется при закрытии за последним внутренним swing (iBOS/iCHoCH);
  major    — ZigZag k_major·ATR + автомат структурного диапазона (правила S1/S2, docs/STRUCTURE_V3_SCALE_REVIEW.md);
  senior   — тот же автомат на старшем масштабе k_senior (объявленный масштаб, не порог значимости): источник
             диагностики SCALE_CONFLICT; конфликт показывается, но автоматически не разрешается.

Правила major (кратко):
  S1  Swing внутри действующего major-диапазона — не major. До первого события границы диапазона — структурные
      экстремумы ИЗВЕСТНОЙ на этот бар истории (с откатом ≥ k·ATR и без закрытия за ними); направление задаёт первое
      закрытие за границей.
  S2  Protected после BOS — экстремум между пробитой major-границей и свечой пробоя.
  Нарушение protected — по ТЕНИ (CHoCH, без допусков; флаг sweep, если close вернулся). Это факт нарушения, а не
  подтверждение нового направления: состояние TRANSITION; подтверждение — BOS (закрытие за структурным экстремумом
  переходной фазы). Тень за структурным экстремумом без закрытия — SWEEP (один раз на уровень). Свеча, пересекающая
  уровни с обеих сторон, — AMBIGUOUS (порядок внутри свечи неизвестен; направление не выводится).

Зафиксированные ограничения (для будущего использования):
  L1  ИСТОРИЯ. Major-диапазон инициализируется по доступной истории. Если ряд начинается не с запуска торгов
      (history != "launch"), начальное направление помечается INCOMPLETE_HISTORY, пока его не подтвердит рынок
      (не начальный BOS/подтверждение после старта ряда). До ATR(14) — INSUFFICIENT_HISTORY и UNDEFINED. Даже после
      снятия пометки результат на усечённой истории может зависеть от точки старта (tests/test_structure_v3.py).
  L2  ЖИВАЯ СВЕЧА. Нарушения protected внутри формирующейся свечи v3 НЕ обрабатывает: все события строятся по
      закрытым свечам. Это обязательное требование, которое нужно реализовать и проверить отдельно, прежде чем
      использовать внутрисвечные структурные сигналы в торговле.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .candles import TF_SEC, Candle, Series

VERSION = "structure-v3.0-research"
K_MAJOR, K_INT, K_SENIOR = 3.0, 1.5, 6.0
ATR_N = 14


@dataclass
class Swing:
    kind: str
    idx: int
    ts: int
    price: float
    confirmed_idx: int
    confirmed_ts: int
    label: str = ""


# ====================================================================== ZigZag (пошаговый)
class ZigZag:
    """Кандидат — экстремум текущей ноги; подтверждается, когда максимальная реакция после него ≥ k·ATR (ATR на баре
    подтверждения). После подтверждения поиск противоположного кандидата начинается с самого swing."""

    def __init__(self, k: float):
        self.k = k
        self.leg = None
        self.ch = self.cl = 0
        self.rl, self.rh = float("inf"), float("-inf")
        self.swings: list[Swing] = []

    def step(self, e: "Engine", i: int) -> Swing | None:
        h, l = e.h, e.l
        if self.leg in (None, "up"):
            if h[i] > h[self.ch]:
                self.ch, self.rl = i, float("inf")
            elif i > self.ch:
                self.rl = min(self.rl, l[i])
        if self.leg in (None, "down"):
            if l[i] < l[self.cl]:
                self.cl, self.rh = i, float("-inf")
            elif i > self.cl:
                self.rh = max(self.rh, h[i])
        v = e.vol[i]
        if v is None:
            return None
        thr = self.k * v
        can_h = self.leg in (None, "up") and self.rl < float("inf") and h[self.ch] - self.rl >= thr
        can_l = self.leg in (None, "down") and self.rh > float("-inf") and self.rh - l[self.cl] >= thr
        if self.leg is None and can_h and can_l:
            can_h, can_l = self.ch < self.cl, self.cl < self.ch
        sw = None
        if can_h:
            sw = self._emit(e, "H", self.ch, i)
            self.leg = "down"
            self.cl = min(range(self.ch + 1, i + 1), key=lambda x: l[x])
            self.rh = max((h[x] for x in range(self.cl + 1, i + 1)), default=float("-inf"))
        elif can_l:
            sw = self._emit(e, "L", self.cl, i)
            self.leg = "up"
            self.ch = max(range(self.cl + 1, i + 1), key=lambda x: h[x])
            self.rl = min((l[x] for x in range(self.ch + 1, i + 1)), default=float("inf"))
        return sw

    def _emit(self, e, kind, idx, i) -> Swing:
        prev = next((w for w in reversed(self.swings) if w.kind == kind), None)
        price = e.h[idx] if kind == "H" else e.l[idx]
        label = "" if prev is None else ("HH" if price > prev.price else "LH") if kind == "H" else ("HL" if price > prev.price else "LL")
        sw = Swing(kind, idx, e.ts[idx], price, i, e.ts[i] + e.dt, label)
        self.swings.append(sw)
        return sw


# ====================================================================== major / senior автомат
class Major:
    def __init__(self, scope: str, zz: ZigZag):
        self.scope = scope
        self.zz = zz
        self.trend, self.bias, self.trans_from = "UNDEFINED", "", ""
        self.ph: dict | None = None
        self.pl: dict | None = None
        self.since = -1
        self.trans_sw = {"H": 0, "L": 0}
        self.pending: dict | None = None       # тень за структурным экстремумом без закрытия: {src, first_idx}
        self.swept: list = []                  # дедупликация SWEEP: (вид, индекс экстремума)
        self.events: list[dict] = []
        self.prot_hist: list[dict] = []
        self.roles: dict = {}                  # индекс экстремума → роль и время назначения
        self.states: list[dict] = []
        self.nknown = 0

    # ------------------------------------------------------------------ helpers
    def _known(self, i):
        return [w for w in self.zz.swings if w.confirmed_idx < i]

    def _role(self, e, idx, role, i, price):
        r = self.roles.setdefault(str(idx), {"idx": idx, "ts": e.ts[idx], "price": price, "roles": []})
        if not r["roles"] or r["roles"][-1]["role"] != role:
            r["roles"].append({"role": role, "assigned_ts": e.ts[i] + e.dt if i >= 0 else None})

    def target(self, e: "Engine", kind: str, i: int):
        """Структурный экстремум ноги: самая крайняя точка (тени учитываются) после начала ноги, после которой был
        откат ≥ k·ATR и не было закрытия за ней; в ноге нужен хотя бы один подтверждённый swing этого вида.
        Используются только бары < i (известно на закрытии бара i−1)."""
        h, l, c = e.h, e.l, e.c
        start = (self.pl["src_idx"] if kind == "H" and self.pl else self.ph["src_idx"] if kind == "L" and self.ph else self.since)
        if i - 1 <= start or not any(w.kind == kind and w.idx > start and w.confirmed_idx < i for w in self.zz.swings):
            return None
        v = e.vol[i - 1]
        if v is None:
            return None
        thr = self.zz.k * v
        best = None
        lo_r, hi_r, cmax, cmin = float("inf"), float("-inf"), float("-inf"), float("inf")
        for j in range(i - 1, start, -1):
            if j < i - 1:
                lo_r, hi_r = min(lo_r, l[j + 1]), max(hi_r, h[j + 1])
                cmax, cmin = max(cmax, c[j + 1]), min(cmin, c[j + 1])
                if kind == "H":
                    if h[j] - lo_r >= thr and cmax <= h[j] and (best is None or h[j] >= h[best]):
                        best = j
                elif hi_r - l[j] >= thr and cmin >= l[j] and (best is None or l[j] <= l[best]):
                    best = j
        if best is None:
            return None
        return {"kind": kind, "idx": best, "ts": e.ts[best], "price": h[best] if kind == "H" else l[best]}

    def _ev(self, e, i, typ, level, src, why, **extra):
        up = typ.endswith("UP") or typ == "SWEEP_HIGH"
        d = {"type": typ, "scope": self.scope, "idx": i, "ts": e.ts[i], "known_ts": e.ts[i] + e.dt, "level": level,
             "trigger": None if typ == "AMBIGUOUS" else ("high" if up else "low"),
             "trigger_price": None if typ == "AMBIGUOUS" else (e.h[i] if up else e.l[i]), "close": e.c[i], "src": src,
             "prev": self.trend + (f"/{self.bias}" if self.bias else ""), "why": why, "gap": e.gap[i], **extra}
        self.events.append(d)
        return d

    def _set_prot(self, e, i, side, price, src_idx, reason, status="PROTECTED"):
        old = self.ph if side == "H" else self.pl
        rec = {"side": side, "price": price, "src_idx": src_idx, "src_ts": e.ts[src_idx], "set_idx": i, "set_ts": e.ts[i] + e.dt,
               "reason": reason, "status": status, "replaced": None if old is None else old["price"], "scope": self.scope}
        self.prot_hist.append(rec)
        if side == "H":
            self.ph = rec
        else:
            self.pl = rec
        self._role(e, src_idx, "protected high" if side == "H" else "protected low", i, price)

    def _clear_prot(self, e, i, side, status, reason):
        cur = self.ph if side == "H" else self.pl
        if cur is not None:
            self.prot_hist.append({**cur, "status": status, "set_idx": i, "set_ts": e.ts[i] + e.dt, "reason": reason})
        if side == "H":
            self.ph = None
        else:
            self.pl = None

    # ------------------------------------------------------------------ шаг
    def step(self, e: "Engine", i: int) -> None:
        h, l, c = e.h, e.l, e.c
        for w in self.zz.swings[self.nknown:]:
            if w.confirmed_idx < i:
                if self.trend in ("TRANSITION", "RANGE") and w.idx > self.since:
                    self.trans_sw[w.kind] += 1
                self.nknown += 1
        neutral = self.trend == "UNDEFINED" or (self.trend in ("TRANSITION", "RANGE") and not self.bias)
        up_side = neutral or self.trend == "UP" or (self.trend in ("TRANSITION", "RANGE") and self.bias == "up")
        dn_side = neutral or self.trend == "DOWN" or (self.trend in ("TRANSITION", "RANGE") and self.bias == "down")
        tH = self.target(e, "H", i) if up_side else None
        tL = self.target(e, "L", i) if dn_side else None
        for t in (tH, tL):
            if t:
                self._role(e, t["idx"], "structural high" if t["kind"] == "H" else "structural low", i - 1, t["price"])
        upP = self.ph is not None and h[i] > self.ph["price"]
        dnP = self.pl is not None and l[i] < self.pl["price"]
        upT = tH is not None and h[i] > tH["price"]
        dnT = tL is not None and l[i] < tL["price"]
        # ---------- неоднозначная свеча: нарушены/пересечены уровни с обеих сторон
        if (upP or upT) and (dnP or dnT):
            lv = {k: v for k, v in (("protected_high", self.ph and upP and self.ph["price"]), ("protected_low", self.pl and dnP and self.pl["price"]),
                                    ("structural_high", tH and upT and tH["price"]), ("structural_low", tL and dnT and tL["price"])) if v}
            self._ev(e, i, "AMBIGUOUS", None, None, "одна свеча пересекла уровни с обеих сторон; порядок внутри свечи без тиков "
                     "неизвестен — направление не выводится, разрешение следующими свечами", levels=lv)
            if upP:
                self._clear_prot(e, i, "H", "VIOLATED", "нарушен тенью (неоднозначная свеча)")
            if dnP:
                self._clear_prot(e, i, "L", "VIOLATED", "нарушен тенью (неоднозначная свеча)")
            if upP or dnP:
                self._clear_prot(e, i, "H", "RETIRED", "снят: неоднозначная свеча")
                self._clear_prot(e, i, "L", "RETIRED", "снят: неоднозначная свеча")
                self.trend, self.bias, self.trans_from = "TRANSITION", "", ""
                self.since, self.trans_sw, self.pending = i - 1, {"H": 0, "L": 0}, None
            self._state(e, i, "TRANSITION")
            return
        # ---------- нарушение protected (тень)
        if upP or dnP:
            side = "H" if upP else "L"
            prot = self.ph if upP else self.pl
            lvl = prot["price"]
            sweep = (c[i] <= lvl) if upP else (c[i] >= lvl)
            prev = self.trend
            typ = "CHoCH_UP" if upP else "CHoCH_DOWN"
            restore = prev in ("TRANSITION", "RANGE") and self.trans_from == ("UP" if upP else "DOWN")
            why = (("high выше" if upP else "low ниже") + f" protected {'high' if upP else 'low'} {lvl:.6g}: "
                   + ("переходная фаза не подтвердилась — возврат к прежнему тренду" if restore else
                      ("нисходящая" if upP else "восходящая") + " структура нарушена; новое направление НЕ подтверждено")
                   + (" (тень, close вернулся — sweep)" if sweep else ""))
            self._ev(e, i, typ, lvl, {"idx": prot["src_idx"], "ts": prot["src_ts"], "label": "protected"}, why,
                     sweep=sweep, violation=True, restore=restore)
            self._clear_prot(e, i, side, "VIOLATED", "нарушен тенью")
            if restore:
                if upP:
                    j = min(range(max(self.since, 0), i + 1), key=lambda x: l[x])
                    self._set_prot(e, i, "L", l[j], j, "восстановление UP: минимум переходной фазы")
                else:
                    j = max(range(max(self.since, 0), i + 1), key=lambda x: h[x])
                    self._set_prot(e, i, "H", h[j], j, "восстановление DOWN: максимум переходной фазы")
                self.trend, self.bias, self.trans_from = ("UP" if upP else "DOWN"), "", ""
            else:
                if upP:
                    j = min(range(prot["src_idx"], i + 1), key=lambda x: l[x])
                    self._set_prot(e, i, "L", l[j], j, "кандидат после CHoCH_UP: минимум прошлой нисходящей волны", "CANDIDATE")
                else:
                    j = max(range(prot["src_idx"], i + 1), key=lambda x: h[x])
                    self._set_prot(e, i, "H", h[j], j, "кандидат после CHoCH_DOWN: максимум прошлой восходящей волны", "CANDIDATE")
                self.trans_from = prev if prev in ("UP", "DOWN") else ("DOWN" if upP else "UP")
                self.trend, self.bias = "TRANSITION", ("up" if upP else "down")
                self.trans_sw = {"H": 0, "L": 0}
            self.since, self.pending, self.swept = i, None, []
            self._state(e, i, "TRANSITION")
            return
        # ---------- BOS / SWEEP / начальное направление
        done = False
        for t, up in ((tH, True), (tL, False)):
            if t is None:
                continue
            crossed = h[i] > t["price"] if up else l[i] < t["price"]
            pend = self.pending if self.pending and self.pending["src"] == t["idx"] else None
            if not crossed and not pend:
                continue
            accepted = c[i] > t["price"] if up else c[i] < t["price"]
            if not accepted:
                key = ["H" if up else "L", t["idx"]]
                if crossed and key not in self.swept:          # один SWEEP на уровень
                    self._ev(e, i, "SWEEP_HIGH" if up else "SWEEP_LOW", t["price"], {"idx": t["idx"], "ts": t["ts"], "label": "структурный"},
                             "тень за структурным экстремумом, close вернулся — ликвидность снята, BOS нет", violation=False)
                    self.swept.append(key)
                if crossed:
                    self.pending = {"src": t["idx"], "first_idx": i}
                continue
            first = pend["first_idx"] if pend else i
            initial = self.trend == "UNDEFINED"
            confirm = self.trend in ("TRANSITION", "RANGE")
            typ = "BOS_UP" if up else "BOS_DOWN"
            why = ("начальное направление: первое закрытие за границей major-диапазона (S1)" if initial else
                   ("подтверждение нового направления после " + ("неоднозначной свечи" if not self.bias else "нарушения protected") + ": ")
                   if confirm else "продолжение: ") + ("" if initial else f"закрытие за структурным {'максимумом' if up else 'минимумом'} {t['price']:.6g}")
            self._ev(e, i, typ, t["price"], {"idx": t["idx"], "ts": t["ts"], "label": "структурный"}, why,
                     initial=initial, confirmation=confirm, break_fact_idx=first, break_fact_ts=e.ts[first])
            self._role(e, t["idx"], "broken structural " + ("high" if up else "low"), i, t["price"])
            rng = range(t["idx"], i + 1)
            j = min(rng, key=lambda x: l[x]) if up else max(rng, key=lambda x: h[x])
            side = "L" if up else "H"
            self._set_prot(e, i, side, l[j] if up else h[j], j, f"{typ}: экстремум между пробитой major-границей и пробоем (S2)")
            if confirm or initial:
                self._clear_prot(e, i, "H" if up else "L", "RETIRED", "снят: направление подтверждено")
            self.trend, self.bias, self.trans_from = ("UP" if up else "DOWN"), "", ""
            self.since, self.pending, self.swept = i, None, []
            done = True
            break
        if not done and self.trend == "TRANSITION" and self.trans_sw["H"] >= 2 and self.trans_sw["L"] >= 2:
            self.trend = "RANGE"
        if self.trend in ("UP", "DOWN"):
            ph = "PULLBACK" if self.target(e, "H" if self.trend == "UP" else "L", i + 1) is not None else "IMPULSE"
        else:
            ph = {"RANGE": "CONSOLIDATION", "UNDEFINED": "UNDEFINED"}.get(self.trend, "TRANSITION")
        self._state(e, i, ph)

    def _state(self, e, i, phase):
        self.states.append({"trend": self.trend, "bias": self.bias, "phase": phase,
                            "ph": self.ph and self.ph["price"], "ph_status": self.ph and self.ph["status"],
                            "pl": self.pl and self.pl["price"], "pl_status": self.pl and self.pl["status"]})


# ====================================================================== internal
class Internal:
    def __init__(self, zz: ZigZag):
        self.zz = zz
        self.trend = "UNDEFINED"
        self.lastH = self.lastL = None
        self.usedH = self.usedL = -1
        self.swept: list = []
        self.events: list[dict] = []
        self.states: list[str] = []
        self.nknown = 0

    def step(self, e: "Engine", i: int) -> None:
        h, l, c = e.h, e.l, e.c
        for w in self.zz.swings[self.nknown:]:
            if w.confirmed_idx < i:
                if w.kind == "H":
                    self.lastH = w
                else:
                    self.lastL = w
                self.nknown += 1
        typ = None
        for w, up in ((self.lastH, True), (self.lastL, False)):
            if w is None or typ or w.idx == (self.usedH if up else self.usedL):
                continue
            crossed = h[i] > w.price if up else l[i] < w.price
            if not crossed:
                continue
            if (c[i] > w.price) if up else (c[i] < w.price):
                typ = ("iBOS_" if self.trend == ("UP" if up else "DOWN") or self.trend == "UNDEFINED" else "iCHoCH_") + ("UP" if up else "DOWN")
                src = w
                if up:
                    self.usedH = w.idx
                else:
                    self.usedL = w.idx
            elif [w.kind, w.idx] not in self.swept:
                self.swept.append([w.kind, w.idx])
                self.events.append({"type": "iSWEEP_HIGH" if up else "iSWEEP_LOW", "scope": "internal", "idx": i, "ts": e.ts[i],
                                    "known_ts": e.ts[i] + e.dt, "level": w.price, "trigger_price": h[i] if up else l[i], "close": c[i],
                                    "prev": self.trend, "gap": e.gap[i], "src": asdict(w),
                                    "why": "тень за внутренним swing, close вернулся"})
        if typ:
            self.events.append({"type": typ, "scope": "internal", "idx": i, "ts": e.ts[i], "known_ts": e.ts[i] + e.dt, "level": src.price,
                                "trigger_price": h[i] if typ.endswith("UP") else l[i], "close": c[i], "prev": self.trend, "gap": e.gap[i],
                                "src": asdict(src), "why": ("смена" if "CHoCH" in typ else "продолжение") +
                                " внутренней структуры: закрытие за последним внутренним swing " + src.label})
            self.trend = "UP" if typ.endswith("UP") else "DOWN"
        self.states.append(self.trend)


# ====================================================================== двигатель
class Engine:
    def __init__(self, tf: str, k_major: float = K_MAJOR, k_int: float = K_INT, k_senior: float = K_SENIOR,
                 history: str = "unknown"):
        """history: "launch" — ряд начинается со свечи запуска торгов (полная история); "truncated"/"unknown" —
        начало ряда произвольное (L1)."""
        self.tf, self.dt = tf, TF_SEC[tf]
        self.history = history
        self.k = {"major": k_major, "internal": k_int, "senior": k_senior}
        self.ts, self.o, self.h, self.l, self.c = [], [], [], [], []
        self.vol: list[float | None] = []
        self.tr: list[float] = []
        self.gap: list[bool] = []
        self.rejected: list[dict] = []
        self.zz = {"major": ZigZag(k_major), "internal": ZigZag(k_int), "senior": ZigZag(k_senior)}
        self.major = Major("major", self.zz["major"])
        self.senior = Major("senior", self.zz["senior"])
        self.internal = Internal(self.zz["internal"])
        self.conflicts: list[dict] = []
        self.conflict_state: list[dict | None] = []

    # ------------------------------------------------------------------ данные
    def step(self, k: Candle) -> None:
        if self.ts and k.ts <= self.ts[-1]:
            return                                          # дубликат/старый бар: идемпотентно
        if not (k.h >= max(k.o, k.c) and min(k.o, k.c) >= k.l and k.l > 0):
            self.rejected.append({"ts": k.ts, "why": "невалидная свеча (OHLC): пропущена, как пропуск данных"})
            return
        i = len(self.ts)
        self.gap.append(bool(self.ts) and k.ts - self.ts[-1] > self.dt)
        self.ts.append(k.ts); self.o.append(k.o); self.h.append(k.h); self.l.append(k.l); self.c.append(k.c)
        self.tr.append(k.h - k.l if i == 0 else max(k.h, self.c[i - 1]) - min(k.l, self.c[i - 1]))
        if i + 1 < ATR_N:
            self.vol.append(None)
        elif i + 1 == ATR_N:
            self.vol.append(sum(self.tr) / ATR_N)
        else:
            self.vol.append((self.vol[-1] * (ATR_N - 1) + self.tr[-1]) / ATR_N)
        # решения по бару i используют swings, подтверждённые на барах < i; затем ZigZag учитывает бар i
        self.major.step(self, i)
        self.senior.step(self, i)
        self.internal.step(self, i)
        for z in self.zz.values():
            z.step(self, i)
        self._conflict(i)

    def _conflict(self, i):
        """SCALE_CONFLICT: major-структура ушла от направления, которое на старшем масштабе ещё защищено непробитым
        уровнем. Конфликт показывается и сохраняется, но не разрешается порогом значимости."""
        m, s = self.major.states[-1], self.senior.states[-1]
        mdir = m["trend"] if m["trend"] in ("UP", "DOWN") else (m["bias"].upper() if m["bias"] else None)
        sdir = s["trend"] if s["trend"] in ("UP", "DOWN") else None
        c = None
        if mdir and sdir and mdir != sdir:
            lvl = s["ph"] if sdir == "DOWN" else s["pl"]
            c = {"major": m["trend"] + (f"/{m['bias']}" if m["bias"] else ""), "senior": sdir, "senior_level": lvl,
                 "text": f"major {mdir} против старшего масштаба {sdir}: старший protected {'high' if sdir == 'DOWN' else 'low'} "
                         f"{lvl:.6g} не нарушен" if lvl is not None else f"major {mdir} против старшего масштаба {sdir}"}
        prev = self.conflict_state[-1] if self.conflict_state else None
        if c and not prev:
            self.conflicts.append({"type": "SCALE_CONFLICT", "idx": i, "ts": self.ts[i], "known_ts": self.ts[i] + self.dt, **c})
        if prev and not c:
            self.conflicts.append({"type": "SCALE_CONFLICT_END", "idx": i, "ts": self.ts[i], "known_ts": self.ts[i] + self.dt,
                                   "text": "направления major и старшего масштаба снова совпадают или старший не определён"})
        self.conflict_state.append(c)
        if c:
            for ev in reversed(self.major.events):
                if ev["idx"] == i and ev["type"].startswith(("CHoCH", "BOS")):
                    ev["scale_conflict"] = c
                    break

    def run(self, s: Series) -> "Engine":
        for k in s.candles:
            self.step(k)
        return self

    # ------------------------------------------------------------------ сохранение / восстановление
    def to_state(self) -> dict:
        def enc(o):
            if isinstance(o, Swing):
                return {"__swing__": asdict(o)}
            if isinstance(o, (ZigZag, Major, Internal)):
                d = {}
                for k, v in vars(o).items():
                    if k == "zz":
                        continue
                    d[k] = enc(v)
                return d
            if isinstance(o, list):
                return [enc(x) for x in o]
            if isinstance(o, dict):
                return {k: enc(v) for k, v in o.items()}
            if isinstance(o, float) and o in (float("inf"), float("-inf")):
                return {"__inf__": 1 if o > 0 else -1}
            return o
        st = {k: enc(v) for k, v in vars(self).items() if k not in ("zz", "major", "senior", "internal")}
        st["zz"] = {k: enc(z) for k, z in self.zz.items()}
        st["major"], st["senior"], st["internal"] = enc(self.major), enc(self.senior), enc(self.internal)
        st["version"] = VERSION
        return json.loads(json.dumps(st))

    @classmethod
    def from_state(cls, st: dict) -> "Engine":
        def dec(o):
            if isinstance(o, dict):
                if "__swing__" in o:
                    return Swing(**o["__swing__"])
                if "__inf__" in o:
                    return float("inf") if o["__inf__"] > 0 else float("-inf")
                return {k: dec(v) for k, v in o.items()}
            if isinstance(o, list):
                return [dec(x) for x in o]
            return o
        e = cls(st["tf"], st["k"]["major"], st["k"]["internal"], st["k"]["senior"], st.get("history", "unknown"))
        for k, v in st.items():
            if k not in ("zz", "major", "senior", "internal", "version"):
                setattr(e, k, dec(v))
        for name, z in e.zz.items():
            for k, v in st["zz"][name].items():
                setattr(z, k, dec(v))
        for name in ("major", "senior", "internal"):
            obj = getattr(e, name)
            for k, v in st[name].items():
                setattr(obj, k, dec(v))
        return e

    # ------------------------------------------------------------------ результат
    def swings(self, scope: str) -> list[dict]:
        roles = self.major.roles if scope == "major" else self.senior.roles if scope == "senior" else {}
        out = []
        for w in self.zz[scope].swings:
            r = roles.get(str(w.idx))
            out.append({**asdict(w), "scope": scope,
                        "tier": "major" if r else ("intermediate" if scope == "major" else scope),
                        "roles": r["roles"] if r else []})
        return out

    def history_status(self) -> str:
        """L1: INSUFFICIENT_HISTORY — нет ATR(14); INCOMPLETE_HISTORY — ряд не с запуска и направление держится
        только на начальном диапазоне; OK — иначе."""
        if len(self.ts) < ATR_N:
            return "INSUFFICIENT_HISTORY"
        if self.history == "launch":
            return "OK"
        confirmed = any(x["type"].startswith(("BOS", "CHoCH")) and not x.get("initial") for x in self.major.events)
        return "OK" if confirmed or self.major.trend == "UNDEFINED" else "INCOMPLETE_HISTORY"

    def result(self) -> dict:
        m = self.major
        last = m.states[-1] if m.states else None
        i = len(self.ts) - 1
        ev = lambda xs, pfx: next((x for x in reversed(xs) if x["type"].startswith(pfx)), None)  # noqa: E731
        cond = []
        if last:
            if m.ph:
                cond.append(f"нарушение защиты вверх: любой high > {m.ph['price']:.6g}" + (" (кандидат)" if m.ph["status"] == "CANDIDATE" else ""))
            if m.pl:
                cond.append(f"нарушение защиты вниз: любой low < {m.pl['price']:.6g}" + (" (кандидат)" if m.pl["status"] == "CANDIDATE" else ""))
            for kind in ("H", "L"):
                t = m.target(self, kind, i + 1) if i >= 0 else None
                if t and ((kind == "H" and last["trend"] in ("UP", "TRANSITION", "RANGE", "UNDEFINED")) or
                          (kind == "L" and last["trend"] in ("DOWN", "TRANSITION", "RANGE", "UNDEFINED"))):
                    cond.append(f"{'BOS_UP' if kind == 'H' else 'BOS_DOWN'}: закрытие {'выше' if kind == 'H' else 'ниже'} структурного "
                                f"{'максимума' if kind == 'H' else 'минимума'} {t['price']:.6g}")
        return {
            "version": VERSION, "tf": self.tf, "k": self.k, "bars": len(self.ts),
            "major": {"events": m.events, "protected_history": m.prot_hist, "swings": self.swings("major"), "state": last},
            "senior": {"events": self.senior.events, "protected_history": self.senior.prot_hist, "state": self.senior.states[-1] if self.senior.states else None},
            "internal": {"events": self.internal.events, "swings": self.swings("internal"),
                         "state": self.internal.states[-1] if self.internal.states else "UNDEFINED"},
            "scale_conflicts": self.conflicts, "scale_conflict_now": self.conflict_state[-1] if self.conflict_state else None,
            "last_major_bos": ev(m.events, "BOS"), "last_major_choch": ev(m.events, "CHoCH"),
            "last_internal_bos": ev(self.internal.events, "iBOS"), "last_internal_choch": ev(self.internal.events, "iCHoCH"),
            "conditions": cond,
            "gaps": [{"idx": j, "ts": self.ts[j]} for j, g in enumerate(self.gap) if g], "rejected": self.rejected,
            "insufficient_history": len(self.ts) < ATR_N,
            "history": self.history, "history_status": self.history_status(),
            "intrabar_live": "NOT_SUPPORTED",          # L2: события только по закрытым свечам
        }

    def states(self) -> list[dict]:
        """Состояние по барам (для сравнения префиксов / таймлайна)."""
        return [{"major": m, "senior": s["trend"], "internal": it, "conflict": bool(cf)}
                for m, s, it, cf in zip(self.major.states, self.senior.states, self.internal.states, self.conflict_state)]


def analyze(s: Series, **kw) -> dict:
    return Engine(s.tf, **kw).run(s).result()


def aggregate(s: Series, tf: str) -> Series:
    """Агрегация младшего ТФ в старший (для синтетики/исследований). Неполная последняя свеча отбрасывается."""
    d = TF_SEC[tf]
    groups: dict[int, list[Candle]] = {}
    for k in s.candles:
        groups.setdefault(k.ts - k.ts % d, []).append(k)
    n = d // TF_SEC[s.tf]
    out = [Candle(t, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g))
           for t, g in sorted(groups.items()) if len(g) == n]
    return Series(tf, out)
