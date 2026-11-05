"""Запросы к источникам и разбор ответов. Только FABLES/USDG-пул и контекст BTC/ETH/USDG; всё read-only."""
from __future__ import annotations

from decimal import Decimal

from . import config as C

GT_TF = {"1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}
GT_HDR = {"Accept": "application/json;version=20230302"}
CG_IDS = {"bitcoin": "BTC", "ethereum": "ETH", "global-dollar": "USDG"}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ GeckoTerminal
def gt_ohlcv(tr, tf: str, limit: int = 1000) -> list[list]:
    """[[ts, o, h, l, c, v_usd], ...] по возрастанию; последняя строка обычно формирующаяся свеча."""
    period, agg = GT_TF[tf]
    data = tr.get("geckoterminal", f"{C.GT_BASE}/networks/{C.CHAIN}/pools/{C.POOL_ID}/ohlcv/{period}",
                  params={"aggregate": agg, "limit": limit, "currency": "usd", "token": C.TOKEN}, headers=GT_HDR)
    meta = (data or {}).get("meta") or {}
    base = (meta.get("base") or {}).get("address", "").lower()
    if base and base != C.TOKEN:
        raise ValueError(f"GeckoTerminal вернул свечи не для FABLES: {base}")
    lst = ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    return sorted(lst, key=lambda r: r[0])


# ------------------------------------------------------------------ DexScreener
def ds_pair(tr) -> dict | None:
    data = tr.get("dexscreener", f"{C.DS_BASE}/latest/dex/pairs/{C.CHAIN}/{C.POOL_ID}")
    pairs = (data or {}).get("pairs") or []
    p = next((x for x in pairs if (x.get("pairAddress") or "").lower() == C.POOL_ID), None)
    if not p:
        return None
    if (p.get("baseToken") or {}).get("address", "").lower() != C.TOKEN:
        raise ValueError("DexScreener: базовый токен пары не FABLES")
    liq = p.get("liquidity") or {}
    return {"price_usd": _f(p.get("priceUsd")), "price_usdg": _f(p.get("priceNative")), "liquidity_usd": _f(liq.get("usd")),
            "liquidity_base": _f(liq.get("base")), "liquidity_quote": _f(liq.get("quote")), "fdv": _f(p.get("fdv")),
            "market_cap": _f(p.get("marketCap")), "volume": {k: _f(v) for k, v in (p.get("volume") or {}).items()},
            "txns": p.get("txns") or {}, "price_change": {k: _f(v) for k, v in (p.get("priceChange") or {}).items()}}


# ------------------------------------------------------------------ RPC (StateView, Swap, газ)
def _word(h: str, i: int) -> int:
    return int(h[2 + 64 * i: 2 + 64 * (i + 1)], 16)


def _signed(v: int, bits: int = 256) -> int:
    return v - (1 << bits) if v >> (bits - 1) else v


def pool_state(tr) -> dict:
    slot0 = tr.rpc("eth_call", [{"to": C.STATE_VIEW, "data": "0x" + C.SEL_GET_SLOT0 + C.POOL_ID[2:]}, "latest"])
    liq = tr.rpc("eth_call", [{"to": C.STATE_VIEW, "data": "0x" + C.SEL_GET_LIQUIDITY + C.POOL_ID[2:]}, "latest"])
    block = int(tr.rpc("eth_blockNumber", []), 16)
    sp = _word(slot0, 0)
    tick = _signed(_word(slot0, 1))
    L = int(liq, 16)
    if sp == 0 or L == 0:
        raise ValueError("пул не инициализирован или ликвидность 0")
    return {"block": block, "sqrt_price_x96": str(sp), "tick": tick, "protocol_fee": _word(slot0, 2), "lp_fee_stored": _word(slot0, 3),
            "liquidity": str(L), "mid": mid_from_sqrt(sp)}


def mid_from_sqrt(sqrt_price_x96: int) -> float:
    """Цена FABLES в USDG. P = token1/token0 (сырые единицы); USDG за FABLES = 10^(DEC1-DEC0) / P."""
    p = (Decimal(sqrt_price_x96) / Decimal(2 ** 96)) ** 2
    return float(Decimal(10) ** (C.DEC1 - C.DEC0) / p)


def swaps(tr, from_block: int, to_block: int) -> list[dict]:
    """События Swap ТОЛЬКО основного пула (topic1 = poolId). amount0 — USDG с точки зрения вызывающего (минус = платит)."""
    logs = tr.rpc("eth_getLogs", [{"address": C.POOL_MANAGER, "fromBlock": hex(from_block), "toBlock": hex(to_block),
                                   "topics": [C.TOPIC_SWAP, C.POOL_ID]}]) or []
    out = []
    for l in logs:
        d = l["data"]
        a0, a1 = _signed(_word(d, 0)), _signed(_word(d, 1))
        out.append({"block": int(l["blockNumber"], 16), "log_index": int(l["logIndex"], 16), "tx": l["transactionHash"],
                    "side": "BUY" if a0 < 0 else "SELL", "usdg": abs(a0) / 10 ** C.DEC0, "fables": abs(a1) / 10 ** C.DEC1,
                    "fee_ppm": _word(d, 5), "tick": _signed(_word(d, 4)), "sqrt_price_x96": str(_word(d, 2))})
    return out


def head_block(tr) -> int:
    return int(tr.rpc("eth_blockNumber", []), 16)


def gas_price_wei(tr) -> int:
    return int(tr.rpc("eth_gasPrice", []), 16)


# ------------------------------------------------------------------ CoinGecko: контекст
def cg_prices(tr) -> dict:
    data = tr.get("coingecko", f"{C.CG_BASE}/simple/price", params={"ids": ",".join(CG_IDS), "vs_currencies": "usd",
                                                                     "include_24hr_change": "true"}) or {}
    return {CG_IDS[k]: {"usd": _f(v.get("usd")), "chg24": _f(v.get("usd_24h_change"))} for k, v in data.items() if k in CG_IDS}


def cg_daily(tr, coin: str, days: int = 60) -> list[tuple[int, float]]:
    """Дневные цены [(ts, usd)]; последняя точка CoinGecko — текущая (не закрытая), её отбрасывает потребитель."""
    data = tr.get("coingecko", f"{C.CG_BASE}/coins/{coin}/market_chart", params={"vs_currency": "usd", "days": days, "interval": "daily"}) or {}
    return [(int(ts // 1000), float(p)) for ts, p in data.get("prices") or []]
