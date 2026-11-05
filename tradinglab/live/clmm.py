"""Оценка исполнения свопа в пуле Uniswap v4 (FORWARD): локальный пошаговый расчёт по состоянию StateView.

ЭТО ОЦЕНКА, А НЕ ТОЧНЫЙ QUOTE И НЕ ПОДТВЕРЖДЁННОЕ ИСПОЛНЕНИЕ:
- V4Quoter для Robinhood Chain в официальном конфиге не найден, поэтому своп считается локально;
- состояние пула (√P, L, тики) читается раз в минуту и может измениться до момента виртуального исполнения;
- hook учитывается только как комиссия (наблюдаемая в событиях Swap). Проверка на 74 реальных свопах пула
  (evidence/fables_swaps): формулы ниже воспроизводят фактический выход с медианной ошибкой ~1e-8 — то есть hook
  кроме комиссии дельты не меняет, — но это эмпирический факт, а не гарантия на будущее.

Модель (exact input, комиссия списывается со входа, как в v4):
  внутри диапазона с постоянной L:
    покупка FABLES (zeroForOne, вход USDG x): √P' = L·√P / (L + x·√P),  выход y = L·(√P − √P')
    продажа FABLES (oneForZero, вход FABLES y): √P' = √P + y / L,        выход x = L·(1/√P − 1/√P')
  на инициализированном тике t (√P_t = 1.0001^(t/2)): движение вниз L −= liquidityNet(t), вверх L += liquidityNet(t).
Тики и liquidityNet загружаются вокруг текущей цены (StateView.getTickBitmap / getTickLiquidity). Если своп доходит до
тика, liquidityNet которого не загружен, или ликвидность обнуляется — расчёт недостоверен: берётся худшая из
частичной оценки и консервативной x·y=k-модели, с пометкой exec_confidence=range-unknown.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, getcontext

from . import config as C
from ..execution import ZERO, CostModel, Fill, dec, q_fables, q_usdg, quote_buy, quote_sell

getcontext().prec = 60
Q96 = Decimal(2) ** 96
BASE = Decimal("1.0001")
MODEL = "v4-local-quote-estimate"
DISCLAIMER = "оценка по состоянию StateView, не V4Quoter и не подтверждённый своп"


def sqrt_at_tick(t: int) -> Decimal:
    return BASE ** (Decimal(t) / 2)


def simulate(sqrt_p: Decimal, L: Decimal, tick: int, ticks: dict[int, int], known: tuple[int, int] | None, amount_in: Decimal,
             zero_for_one: bool) -> dict:
    """Пошаговый своп. sqrt_p, amount_in — в сырых единицах (√(token1/token0), wei). ticks: {tick: liquidityNet}.
    known: (нижняя, верхняя) границы, в пределах которых все инициализированные тики известны (с liquidityNet)."""
    out, rem, crossed = Decimal(0), amount_in, []
    sp, L = sqrt_p, Decimal(L)
    lo, hi = known if known else (tick, tick)
    while rem > 0:
        if L <= 0:
            return {"ok": False, "why": "активная ликвидность обнулилась", "out": out, "crossed": crossed, "sqrt": sp}
        if zero_for_one:
            nxt = max((t for t in ticks if t <= tick), default=None)
            if nxt is None or nxt < lo:
                bound = sqrt_at_tick(lo)                     # дальше знаний нет
                need = L * (sp - bound) / (sp * bound) if sp > bound else Decimal(0)
                if rem <= need:
                    sp1 = L * sp / (L + rem * sp)
                    out += L * (sp - sp1)
                    return {"ok": True, "out": out, "crossed": crossed, "sqrt": sp1}
                return {"ok": False, "why": f"своп выходит ниже известной области тиков ({lo})", "out": out, "crossed": crossed, "sqrt": sp}
            spt = sqrt_at_tick(nxt)
            need = L * (sp - spt) / (sp * spt)
            if rem <= need:
                sp1 = L * sp / (L + rem * sp)
                out += L * (sp - sp1)
                return {"ok": True, "out": out, "crossed": crossed, "sqrt": sp1}
            out += L * (sp - spt)
            rem -= need
            sp, L = spt, L - Decimal(ticks[nxt])
            crossed.append(nxt)
            tick = nxt - 1
        else:
            nxt = min((t for t in ticks if t > tick), default=None)
            if nxt is None or nxt > hi:
                bound = sqrt_at_tick(hi)
                need = L * (bound - sp) if bound > sp else Decimal(0)
                if rem <= need:
                    sp1 = sp + rem / L
                    out += L * (1 / sp - 1 / sp1)
                    return {"ok": True, "out": out, "crossed": crossed, "sqrt": sp1}
                return {"ok": False, "why": f"своп выходит выше известной области тиков ({hi})", "out": out, "crossed": crossed, "sqrt": sp}
            spt = sqrt_at_tick(nxt)
            need = L * (spt - sp)
            if rem <= need:
                sp1 = sp + rem / L
                out += L * (1 / sp - 1 / sp1)
                return {"ok": True, "out": out, "crossed": crossed, "sqrt": sp1}
            out += L * (1 / sp - 1 / spt)
            rem -= need
            sp, L = spt, L + Decimal(ticks[nxt])
            crossed.append(nxt)
            tick = nxt
    return {"ok": True, "out": out, "crossed": crossed, "sqrt": sp}


class LiveExecutor:
    """pool: состояние StateView {'sqrt_price_x96','tick','liquidity','ticks':{tick: net},'known':[lo,hi]} или None.
    fees: {'BUY': ppm, 'SELL': ppm, 'source': ...}; gas_usd — оценка газа за своп."""

    def __init__(self, fallback: CostModel, pool: dict | None = None, fees: dict | None = None, gas_usd: Decimal | None = None,
                 exec_slippage: str = C.EXEC_SLIPPAGE, extra_note: str = ""):
        self.fallback = fallback
        self.pool, self.fees = pool, fees or {}
        self.gas_usd = gas_usd if gas_usd is not None else fallback.gas_usd
        self.exec_slip = Decimal(exec_slippage)
        self.extra_note = extra_note
        self.name = MODEL if pool and pool.get("sqrt_price_x96") else "conservative-fallback"

    def _fee(self, side: str) -> tuple[Decimal, int, str]:
        ppm = self.fees.get(side)
        if ppm:
            return Decimal(ppm) / 1_000_000, int(ppm), self.fees.get("source", "события Swap пула")
        base = self.fallback.buy_fee if side == "BUY" else self.fallback.sell_fee
        return base, int(base * 1_000_000), "начальная выборка 2026-11-05 (свежих событий Swap нет)"

    def _state(self):
        p = self.pool
        if not p or not p.get("sqrt_price_x96") or not p.get("liquidity"):
            return None
        ticks = {int(k): int(v) for k, v in (p.get("ticks") or {}).items()}
        known = tuple(p["known"]) if p.get("known") else None
        return Decimal(int(p["sqrt_price_x96"])) / Q96, Decimal(int(p["liquidity"])), int(p["tick"]), ticks, known

    def _cons(self, side: str, amount, mid, fee_r, extra_slip):
        m = replace(self.fallback, buy_fee=fee_r, sell_fee=fee_r, slippage=self.exec_slip + extra_slip)
        f = quote_buy(amount, mid, m) if side == "BUY" else quote_sell(amount, mid, m)
        return replace(f, gas_usd=self.gas_usd)

    def _note(self, base: str) -> str:
        return "; ".join(x for x in (base, self.extra_note, DISCLAIMER) if x)

    def buy(self, usd_in, mid, extra_slip: Decimal = ZERO) -> Fill:
        usd_in, mid = q_usdg(dec(usd_in)), dec(mid)
        fee_r, ppm, src = self._fee("BUY")
        cons = self._cons("BUY", usd_in, mid, fee_r, extra_slip)
        st = self._state()
        if st is None:
            return replace(cons, model="conservative-fallback", fee_ppm=ppm, fee_source=src,
                           note=self._note("нет свежего состояния пула: консервативная x·y=k-модель"))
        sp, L, tick, ticks, known = st
        fee = usd_in * fee_r
        r = simulate(sp, L, tick, ticks, known, (usd_in - fee) * Decimal(10) ** C.DEC0, True)
        y = r["out"] / Decimal(10) ** C.DEC1
        slip = self.exec_slip + extra_slip
        ref_mid = Decimal(10) ** (C.DEC1 - C.DEC0) / (sp * sp)     # цена пула, по которой считается своп
        if not r["ok"]:
            worse = cons if cons.qty < q_fables(y * (1 - slip)) or y <= 0 else None
            f = worse or self._mk("BUY", ref_mid, q_fables(y * (1 - slip)), usd_in, fee, y, slip, ppm, src)
            return replace(f, model="range-unknown→worst", fee_ppm=ppm, fee_source=src,
                           note=self._note(f"exec_confidence=range-unknown: {r['why']}; взята худшая оценка"))
        f = self._mk("BUY", ref_mid, q_fables(y * (1 - slip)), usd_in, fee, y, slip, ppm, src)
        return replace(f, note=self._note(f"exec_confidence=state-exact; пересечено тиков: {len(r['crossed'])}"))

    def _mk(self, side, mid, out, usd_in, fee, y, slip, ppm, src) -> Fill:
        ideal = (usd_in - fee) / mid
        return Fill(side, mid, out, usd_in, usd_in / out, fee, (ideal - y) * mid, (y - out) * mid, self.gas_usd, MODEL, ppm, src, "")

    def sell(self, qty, mid, extra_slip: Decimal = ZERO) -> Fill:
        qty, mid = q_fables(dec(qty)), dec(mid)
        fee_r, ppm, src = self._fee("SELL")
        cons = self._cons("SELL", qty, mid, fee_r, extra_slip)
        st = self._state()
        if st is None:
            return replace(cons, model="conservative-fallback", fee_ppm=ppm, fee_source=src,
                           note=self._note("нет свежего состояния пула: консервативная x·y=k-модель"))
        sp, L, tick, ticks, known = st
        fee_t = qty * fee_r
        r = simulate(sp, L, tick, ticks, known, (qty - fee_t) * Decimal(10) ** C.DEC1, False)
        x = r["out"] / Decimal(10) ** C.DEC0
        slip = self.exec_slip + extra_slip
        ref_mid = Decimal(10) ** (C.DEC1 - C.DEC0) / (sp * sp)
        usd = q_usdg(x * (1 - slip))
        f = Fill("SELL", ref_mid, qty, usd, usd / qty, fee_t * ref_mid, (qty - fee_t) * ref_mid - x, x - usd, self.gas_usd, MODEL, ppm, src, "")
        if not r["ok"]:
            if cons.usd < f.usd:
                f = cons
            return replace(f, model="range-unknown→worst", fee_ppm=ppm, fee_source=src,
                           note=self._note(f"exec_confidence=range-unknown: {r['why']}; взята худшая оценка"))
        return replace(f, note=self._note(f"exec_confidence=state-exact; пересечено тиков: {len(r['crossed'])}"))

    def liquidation(self, qty, mid) -> Decimal:
        qty = dec(qty)
        if qty <= 0:
            return ZERO
        f = self.sell(qty, mid)
        return f.usd - f.gas_usd
