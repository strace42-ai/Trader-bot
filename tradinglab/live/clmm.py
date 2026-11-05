"""Оценка исполнения свопа в пуле Uniswap v4 с концентрированной ликвидностью (FORWARD).

ОЦЕНКА, не подтверждённое исполнение: V4Quoter для этой сети не найден в официальном конфиге, поэтому своп считается
по формулам CLMM в АКТИВНОМ диапазоне (ликвидность L постоянна, тики не пересекаются):
  покупка FABLES (zeroForOne, вход USDG = x):   √P' = L·√P / (L + x·√P),   выход y = L·(√P − √P')
  продажа FABLES (oneForZero, вход FABLES = y): √P' = √P + y / L,          выход x = L·(1/√P − 1/√P')
P = token1/token0 в сырых единицах (USDG 6 знаков, FABLES 18). Комиссия hook списывается со входа (exact input).
Если расчётное смещение цены > SAFE_TICKS (возможен выход из диапазона) — используется худший из результатов CLMM и
консервативной x·y=k-модели, с пометкой в журнале.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, getcontext

from . import config as C
from ..execution import ZERO, CostModel, Fill, dec, q_fables, q_usdg, quote_buy, quote_sell

getcontext().prec = 50
SAFE_TICKS = 20          # ≪ tickSpacing 200: внутри — расчёт в активном диапазоне считается надёжным
LN_1_0001 = Decimal("0.000099995000333308335")  # ln(1.0001)


def _sqrt_raw_from_mid(mid: Decimal) -> Decimal:
    """√P для цены mid (USDG за FABLES): P_raw = 10^(DEC1−DEC0) / mid."""
    return (Decimal(10) ** (C.DEC1 - C.DEC0) / mid).sqrt()


def _ticks(sp0: Decimal, sp1: Decimal) -> float:
    return float((2 * (sp1 / sp0).ln()) / LN_1_0001)


class LiveExecutor:
    """pool: {'liquidity': L, 'mid': ...}; fees: {'BUY': ppm, 'SELL': ppm, 'source': ...}; gas_usd — оценка газа за своп."""

    def __init__(self, fallback: CostModel, pool: dict | None = None, fees: dict | None = None, gas_usd: Decimal | None = None,
                 exec_slippage: str = C.EXEC_SLIPPAGE):
        self.fallback = fallback
        self.pool, self.fees = pool, fees or {}
        self.gas_usd = gas_usd if gas_usd is not None else fallback.gas_usd
        self.exec_slip = Decimal(exec_slippage)
        self.name = "v4-clmm-active-range-estimate"

    def _fee(self, side: str) -> tuple[Decimal, int, str]:
        ppm = self.fees.get(side)
        if ppm:
            return Decimal(ppm) / 1_000_000, int(ppm), self.fees.get("source", "события Swap пула")
        base = self.fallback.buy_fee if side == "BUY" else self.fallback.sell_fee
        return base, int(base * 1_000_000), "начальная выборка 2026-11-05 (свежих событий Swap нет)"

    def _L(self) -> Decimal | None:
        return Decimal(self.pool["liquidity"]) if self.pool and self.pool.get("liquidity") else None

    def buy(self, usd_in, mid, extra_slip: Decimal = ZERO) -> Fill:
        usd_in, mid = q_usdg(dec(usd_in)), dec(mid)
        fee_r, ppm, src = self._fee("BUY")
        L = self._L()
        cons = replace(quote_buy(usd_in, mid, replace(self.fallback, buy_fee=fee_r, slippage=self.exec_slip + extra_slip)),
                       gas_usd=self.gas_usd)
        if L is None:
            return replace(cons, model="conservative-fallback", fee_ppm=ppm, fee_source=src, note="нет состояния пула: консервативная модель")
        fee = usd_in * fee_r
        x = (usd_in - fee) * Decimal(10) ** C.DEC0
        sp = _sqrt_raw_from_mid(mid)
        sp1 = L * sp / (L + x * sp)
        y = L * (sp - sp1) / Decimal(10) ** C.DEC1          # FABLES до задержки исполнения
        ticks = abs(_ticks(sp, sp1))
        slip = self.exec_slip + extra_slip
        out = q_fables(y * (1 - slip))
        ideal = (usd_in - fee) / mid
        f = Fill("BUY", mid, out, usd_in, usd_in / out, fee, (ideal - y) * mid, (y - out) * mid, self.gas_usd,
                 self.name, ppm, src, f"смещение цены {ticks:.2f} тика")
        if ticks > SAFE_TICKS:
            note = f"смещение {ticks:.1f} тика > {SAFE_TICKS}: возможен выход из диапазона, взята худшая оценка"
            if cons.qty < f.qty:
                return replace(cons, model="clmm→conservative", fee_ppm=ppm, fee_source=src, note=note)
            return replace(f, model="v4-clmm-range-risk", note=note)
        return f

    def sell(self, qty, mid, extra_slip: Decimal = ZERO) -> Fill:
        qty, mid = q_fables(dec(qty)), dec(mid)
        fee_r, ppm, src = self._fee("SELL")
        L = self._L()
        cons = replace(quote_sell(qty, mid, replace(self.fallback, sell_fee=fee_r, slippage=self.exec_slip + extra_slip)),
                       gas_usd=self.gas_usd)
        if L is None:
            return replace(cons, model="conservative-fallback", fee_ppm=ppm, fee_source=src, note="нет состояния пула: консервативная модель")
        fee_t = qty * fee_r
        y = (qty - fee_t) * Decimal(10) ** C.DEC1
        sp = _sqrt_raw_from_mid(mid)
        sp1 = sp + y / L
        x = L * (1 / sp - 1 / sp1) / Decimal(10) ** C.DEC0
        ticks = abs(_ticks(sp, sp1))
        slip = self.exec_slip + extra_slip
        usd = q_usdg(x * (1 - slip))
        f = Fill("SELL", mid, qty, usd, usd / qty, fee_t * mid, (qty - fee_t) * mid - x, x - usd, self.gas_usd,
                 self.name, ppm, src, f"смещение цены {ticks:.2f} тика")
        if ticks > SAFE_TICKS:
            note = f"смещение {ticks:.1f} тика > {SAFE_TICKS}: возможен выход из диапазона, взята худшая оценка"
            if cons.usd < f.usd:
                return replace(cons, model="clmm→conservative", fee_ppm=ppm, fee_source=src, note=note)
            return replace(f, model="v4-clmm-range-risk", note=note)
        return f

    def liquidation(self, qty, mid) -> Decimal:
        qty = dec(qty)
        if qty <= 0:
            return ZERO
        f = self.sell(qty, mid)
        return f.usd - f.gas_usd
