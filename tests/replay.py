"""Воспроизведение записанного потока реальных данных FABLES для интеграционных тестов (без сети).

Market отдаёт ответы GeckoTerminal / DexScreener / RPC / CoinGecko так, как они выглядели бы в момент now:
- свечи — записанные реальные (datasets/fables_2026-11-05), видимы с задержкой GT_LAG после закрытия;
- цена котировок — внутри часа линейно от open к close записанной 1H-свечи (StateView и DexScreener согласованы);
- сценарии: аварии источников, пропуск свечей, скачки цены, расхождение источников, изменение комиссии.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from tradinglab.live import config as C
from tradinglab.live.transport import ReplayTransport, SourceError

ROOT = Path(__file__).resolve().parent.parent / "datasets" / "fables_2026-11-05"
H = 3600
GT_LAG = 60


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _rows(name: str) -> list[list]:
    return sorted(json.loads((ROOT / name).read_text())["data"]["attributes"]["ohlcv_list"])


class Clock:
    def __init__(self, t: int):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Market:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.rows = {"1h": _rows("gt_ohlcv_h1.json"), "4h": _rows("gt_ohlcv_h4.json"), "1d": _rows("gt_ohlcv_d1.json")}
        self.down: list[tuple[str, int, int]] = []          # (источник, с, по)
        self.missing: set[tuple[str, int]] = set()          # (tf, ts) — GT не возвращает
        self.price_override: list[tuple[int, int, float]] = []
        self.ds_mult: list[tuple[int, int, float]] = []     # расхождение DexScreener
        self.fees = {"BUY": 5000, "SELL": 10000}
        self.L = 20_000_000_000_000_000_000
        self.liquidity_usd = 700_000.0
        self.rpc_fail: list[tuple[str, int, int]] = []         # (селектор eth_call или метод, с, по)

    # ------------------------------------------------------------------ рынок
    def price(self, now: int) -> float:
        for a, b, p in self.price_override:
            if a <= now < b:
                return p
        h = now // H * H
        for r in self.rows["1h"]:
            if r[0] == h:
                frac = (now - h) / H
                return r[1] + (r[4] - r[1]) * frac
        return self.rows["1h"][-1][4]

    def is_down(self, src: str, now: int) -> bool:
        return any(s == src and a <= now < b for s, a, b in self.down)

    def gt(self, tf: str, now: int) -> list[list]:
        d = {"1h": H, "4h": 4 * H, "1d": 86400}[tf]
        out = []
        for r in self.rows[tf]:
            if (tf, r[0]) in self.missing:
                continue
            if r[0] + d + GT_LAG <= now:
                out.append(r)
            elif r[0] <= now < r[0] + d + GT_LAG:
                out.append([r[0], r[1], max(r[1], self.price(now)), min(r[1], self.price(now)), self.price(now), r[5] * 0.5])
        return out

    # ------------------------------------------------------------------ ответы API
    def handler(self, src: str, what: str, params, now: float):
        now = int(now)
        if self.is_down(src, now):
            raise SourceError(src, "unavailable", "сценарий: источник недоступен")
        if src == "geckoterminal":
            tf = "1d" if "/ohlcv/day" in what else ("4h" if params.get("aggregate") == 4 else "1h")
            return {"data": {"attributes": {"ohlcv_list": self.gt(tf, now)}}, "meta": {"base": {"address": C.TOKEN}}}
        if src == "dexscreener":
            p = self.price(now)
            for a, b, m in self.ds_mult:
                if a <= now < b:
                    p *= m
            return {"pairs": [{"pairAddress": C.POOL_ID, "baseToken": {"address": C.TOKEN}, "priceNative": str(p), "priceUsd": str(p),
                               "liquidity": {"usd": self.liquidity_usd, "base": 8e6, "quote": 1.8e5}, "fdv": 9e6, "marketCap": 2.1e6,
                               "volume": {"h24": 450000.0, "h1": 9000.0}, "txns": {"h24": {"buys": 220, "sells": 800}}, "priceChange": {"h24": 1.0}}]}
        if src == "rpc":
            key = params[0]["data"][2:10] if what == "eth_call" else what
            if any(k == key and a <= now < b for k, a, b in self.rpc_fail):
                raise SourceError("rpc", "unavailable", f"сценарий: {key} недоступен")
            if what == "eth_blockNumber":
                return hex(self.block(now))
            if what == "eth_gasPrice":
                return hex(20_000_000)
            if what == "eth_call":
                sel = params[0]["data"][2:10]
                if sel == C.SEL_GET_SLOT0:
                    sp = int((Decimal(10) ** 12 / Decimal(self.price(now))).sqrt() * Decimal(2 ** 96))
                    return "0x" + "".join(hex(x % (1 << 256))[2:].rjust(64, "0") for x in (sp, self.tick(now), 0, 0))
                if sel == C.SEL_GET_TICK_BITMAP:      # инициализированные тики каждые 2000 (liquidityNet = 0 → L постоянна)
                    w = int(params[0]["data"][-64:], 16)
                    w = w - (1 << 256) if w >> 255 else w
                    bm = sum(1 << b for b in range(256) if ((w << 8) + b) % 10 == 0)
                    return "0x" + hex(bm)[2:].rjust(64, "0")
                if sel == C.SEL_GET_TICK_LIQUIDITY:
                    return "0x" + hex(self.L)[2:].rjust(64, "0") + "0" * 64
                if sel == C.SEL_GET_LIQUIDITY:
                    return "0x" + hex(self.L)[2:].rjust(64, "0")
            if what == "eth_getLogs":
                f, t = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
                out = []
                for b in range(f - f % 3000 + 3000, t + 1, 3000):     # синтетические свопы пула (2 на окно)
                    for i, (side, fee) in enumerate((("BUY", self.fees["BUY"]), ("SELL", self.fees["SELL"]))):
                        a0 = -100_000_000 if side == "BUY" else 100_000_000
                        a1 = 1_600 * 10 ** 18 * (1 if side == "BUY" else -1)
                        enc = lambda v: hex(v % (1 << 256))[2:].rjust(64, "0")  # noqa: E731
                        out.append({"blockNumber": hex(b), "logIndex": hex(i), "transactionHash": f"0x{b:064x}",
                                    "data": "0x" + enc(a0) + enc(a1) + enc(1) + enc(self.L) + enc(304295) + enc(fee)})
                return out
        if src == "coingecko":
            if "simple/price" in what:
                return {"bitcoin": {"usd": 79000.0, "usd_24h_change": 0.5}, "ethereum": {"usd": 2400.0, "usd_24h_change": 1.0},
                        "global-dollar": {"usd": 1.0, "usd_24h_change": 0.0}}
            base = 2300.0 if "ethereum" in what else 78000.0
            return {"prices": [[(now - (60 - i) * 86400) * 1000, base + i * 2.0] for i in range(61)]}
        raise AssertionError(f"неожиданный запрос {src} {what}")

    def tick(self, now: int) -> int:
        import math
        return math.floor(math.log(1e12 / self.price(now)) / math.log(1.0001))

    def block(self, now: int) -> int:
        return 106_000_000 + (now - 1_792_000_000) * 10

    def transport(self) -> ReplayTransport:
        return ReplayTransport(self.handler, self.clock)


def run(worker, clock: Clock, until: int, step: int = 60) -> None:
    while clock.t < until:
        worker.tick(clock.t)
        clock.t += step
