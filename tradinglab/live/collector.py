"""Market Data Service: единый сборщик живых данных для всех трёх трейдеров (один запрос — для всех).

Расписание (config.SCHEDULE): DexScreener и StateView — раз в 60 с; события Swap пула — раз в 5 мин; газ — 10 мин;
BTC/ETH/USDG — раз в час; GeckoTerminal — сразу после ожидаемого закрытия свечи (+180 с), повтор каждые 120 с, пока
закрытая свеча не появится, плюс страховочное обновление формирующейся свечи.

Правила данных:
- закрытая свеча сохраняется один раз и не перезаписывается (ретро-правки источника → candle_revisions);
- формирующаяся свеча хранится отдельно (forming) и в расчёт сигналов не попадает;
- available_at — момент получения закрытой свечи системой; свечи, загруженные при старте/после сбоя, помечаются backfill;
- при недоступности источника ничего не придумывается: свеч нет — значит нет; фиксируется событие данных.
"""
from __future__ import annotations

import statistics

from . import config as C, sources as S
from .marketdb import MarketDB
from .transport import SourceError
from ..candles import GRACE_SEC, TF_SEC

TFS = ("1h", "4h", "1d")


class Collector:
    def __init__(self, store, transport, clock):
        self.db, self.tr, self.clock = MarketDB(store), transport, clock
        self.last_run: dict[str, float] = {}
        self.started = False
        self.errors: list[dict] = []

    # ------------------------------------------------------------------ расписание
    def _due(self, key: str, interval: float, now: float) -> bool:
        if now - self.last_run.get(key, -1e18) >= interval:
            self.last_run[key] = now
            return True
        return False

    def _err(self, now: int, where: str, e: Exception) -> None:
        msg = f"{type(e).__name__}: {e}"
        self.errors = (self.errors + [{"ts": now, "where": where, "error": msg[:300]}])[-50:]
        self.db.event(now, "source_error", {"where": where, "error": msg[:300]})

    def tick(self, now: float | None = None) -> dict:
        now = int(now if now is not None else self.clock())
        out = {"new_bars": {}, "quote": False, "now": now}
        if not self.started:
            out["new_bars"] = self.backfill(now)
            self.started = True
        if self._due("pool_state", C.SCHEDULE["pool_state"], now):
            out["quote"] |= self._pool(now)
        if self._due("dexscreener", C.SCHEDULE["dexscreener"], now):
            out["quote"] |= self._ds(now)
        if self._due("swaps", C.SCHEDULE["swaps"], now):
            self._swaps(now)
        if self._due("gas", C.SCHEDULE["gas"], now):
            self._gas(now)
        if self._due("context", C.SCHEDULE["context"], now):
            self._context(now)
        for tf in TFS:
            nb = self._candles(tf, now)
            if nb:
                out["new_bars"].setdefault(tf, []).extend(nb)
        self.db.kv_set("health", self.tr.health.snapshot(), now)
        return out

    # ------------------------------------------------------------------ свечи
    def expected_last_closed(self, tf: str, now: int) -> int:
        d = TF_SEC[tf]
        return (now - GRACE_SEC) // d * d - d

    def backfill(self, now: int) -> dict:
        """Старт/восстановление: загрузить доступную историю. Новые свечи помечаются backfill и available_at=now —
        по ним не будет сделок задним числом (решает Forward Scheduler по окну актуальности)."""
        res = {}
        for tf in TFS:
            try:
                rows = S.gt_ohlcv(self.tr, tf)
            except Exception as e:  # noqa: BLE001 — источник может быть недоступен; это не ошибка программы
                self._err(now, f"backfill {tf}", e)
                continue
            r = self._ingest(tf, rows, now, backfill=True)
            if r["new"]:
                res[tf] = r["new"]
                self.db.event(now, "backfill", {"tf": tf, "new": len(r["new"]), "from": r["new"][0], "to": r["new"][-1]})
            self.last_run[f"gt_{tf}"] = now
        return res

    def _ingest(self, tf: str, rows: list[list], now: int, backfill: bool) -> dict:
        d = TF_SEC[tf]
        closed = [r for r in rows if now >= int(r[0]) + d + GRACE_SEC]
        forming = [r for r in rows if now < int(r[0]) + d + GRACE_SEC]
        before = self.db.last_closed_ts(tf)
        r = self.db.store_closed(tf, closed, now, backfill)
        self.db.set_forming(tf, forming[-1] if forming else None, now)
        if r["revised"]:
            self.db.event(now, "candle_revision", {"tf": tf, "count": r["revised"]})
        if r["new"] and before is not None:
            exp = list(range(before + d, max(r["new"]) + 1, d))
            missing = [t for t in exp if t not in set(r["new"]) and t > before]
            if missing:
                self.db.event(now, "candle_gap", {"tf": tf, "missing": missing[:20], "n": len(missing),
                                                  "note": "источник не вернул свечи; они не заполняются"})
        return r

    def _candles(self, tf: str, now: int) -> list[int]:
        exp = self.expected_last_closed(tf, now)
        have = self.db.last_closed_ts(tf)
        due_close = have is None or have < exp
        key = f"gt_{tf}"
        last = self.last_run.get(key, -1e18)
        interval = C.SCHEDULE["gt_retry"] if due_close else C.SCHEDULE["gt_refresh"][tf]
        if now - last < interval:
            return []
        self.last_run[key] = now
        try:
            rows = S.gt_ohlcv(self.tr, tf, limit=200 if have else 1000)
        except Exception as e:  # noqa: BLE001
            self._err(now, f"gt {tf}", e)
            return []
        r = self._ingest(tf, rows, now, backfill=False)
        if due_close and (self.db.last_closed_ts(tf) or 0) < exp and now - (exp + TF_SEC[tf]) > 1800:
            self.db.event(now, "candle_late", {"tf": tf, "expected": exp, "note": "закрытая свеча не появилась в GeckoTerminal за 30 мин"})
        return r["new"]

    # ------------------------------------------------------------------ котировки
    def _pool(self, now: int) -> bool:
        try:
            ps = S.pool_state(self.tr)
        except Exception as e:  # noqa: BLE001
            self._err(now, "pool_state", e)
            return False
        self.db.add_quote(now, "pool", ps["mid"], ps)
        return True

    def _ds(self, now: int) -> bool:
        try:
            p = S.ds_pair(self.tr)
        except Exception as e:  # noqa: BLE001
            self._err(now, "dexscreener", e)
            return False
        if not p or not p.get("price_usdg"):
            self._err(now, "dexscreener", ValueError("пара не найдена или нет цены"))
            return False
        self.db.add_quote(now, "ds", p["price_usdg"], p)
        return True

    def _swaps(self, now: int) -> None:
        try:
            head = S.head_block(self.tr)
            cur, _ = self.db.kv("swap_cursor")
            start = (cur + 1) if cur else head - C.SWAP_INITIAL_LOOKBACK
            if head - start > 3 * C.SWAP_MAX_RANGE:          # бот долго не работал: старые события не догоняем
                self.db.event(now, "swaps_skipped", {"from": start, "to": head - 3 * C.SWAP_MAX_RANGE})
                start = head - 3 * C.SWAP_MAX_RANGE
            b = start
            while b <= head:
                e = min(b + C.SWAP_MAX_RANGE - 1, head)
                self.db.add_swaps(S.swaps(self.tr, b, e), now)
                self.db.kv_set("swap_cursor", e, now)
                b = e + 1
        except Exception as e:  # noqa: BLE001
            self._err(now, "swaps", e)

    def _gas(self, now: int) -> None:
        try:
            wei = S.gas_price_wei(self.tr)
        except Exception as e:  # noqa: BLE001
            self._err(now, "gas", e)
            return
        ctx = self.db.last_context("prices")
        eth = (ctx or {}).get("data", {}).get("ETH", {}).get("usd")
        usd = wei * C.GAS_UNITS / 1e18 * eth if eth else None
        self.db.kv_set("gas", {"wei": wei, "eth_usd": eth, "usd_per_swap": usd, "gas_units": C.GAS_UNITS}, now)

    def _context(self, now: int) -> None:
        try:
            self.db.add_context(now, "prices", S.cg_prices(self.tr))
            for coin, sym in (("ethereum", "ETH"), ("bitcoin", "BTC")):
                daily = [p for p in S.cg_daily(self.tr, coin) if p[0] + 86400 <= now]   # только закрытые дни
                self.db.add_context(now, f"daily_{sym}", daily)
        except Exception as e:  # noqa: BLE001
            self._err(now, "context", e)

    # ------------------------------------------------------------------ производные для стратегий
    def liquidity_median_24h(self, now: int) -> float | None:
        v = [q.get("liquidity_usd") for q in self.db.quotes_since("ds", now - 86400) if q.get("liquidity_usd")]
        return statistics.median(v) if v else None
