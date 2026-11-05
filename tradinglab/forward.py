"""FORWARD paper trading: управление экспериментом, Forward Scheduler и Risk Monitor. Только симуляция.

Состояния эксперимента: NOT_STARTED → RUNNING ⇄ PAUSED → STOPPED (из STOPPED новый START создаёт новый эксперимент).

Команды:
  START           новый эксперимент: неизменяемый started_at, три портфеля по $1 000; свечи до START — только история.
  PAUSE           новые входы запрещены, отложенный вход отменяется; стопы, trailing и выходы продолжают защищать позиции.
  RESUME          снятие паузы.
  STOP            эксперимент завершается БЕЗ автоматической продажи: отложенные ордера отменяются, решения и стопы больше
                  не исполняются, открытые позиции остаются и оцениваются по рынку. Продать можно только вручную.
  CLOSE_POSITION  виртуальная продажа позиции одного трейдера по первой проверенной котировке.
  EMERGENCY_EXIT  подтверждаемое закрытие всех позиций по первой проверенной котировке; RUNNING → PAUSED (без повторных входов).

Время: решение — только по закрытой свече, полученной в пределах окна актуальности (LATE_LIMIT); иначе SKIPPED (бот не
работал — сделок задним числом нет). Исполнение — по первой проверенной котировке, полученной ПОСЛЕ решения.
"""
from __future__ import annotations

import bisect
import statistics
import threading
import time
from dataclasses import replace
from decimal import Decimal

from .analysis import analyze
from .backtest import experiment_params
from .candles import TF_SEC
from .engine import Trader
from .execution import CONSERVATIVE_V1, CostModel, dec
from .indicators import ema
from .live import config as LC
from .live.clmm import LiveExecutor
from .live.marketdb import MarketDB
from .risk import params_hash
from .strategies import ANALYSIS_PARAMS, STRATEGIES

TFS = ("1h", "4h", "1d")
ACTIVE = ("RUNNING", "PAUSED")
TRANSITIONS = {"START": ("NOT_STARTED", "STOPPED"), "PAUSE": ("RUNNING",), "RESUME": ("PAUSED",), "STOP": ("RUNNING", "PAUSED"),
               "CLOSE_POSITION": ("RUNNING", "PAUSED", "STOPPED"), "EMERGENCY_EXIT": ("RUNNING", "PAUSED", "STOPPED")}


class ControlError(Exception):
    def __init__(self, message: str, status: str):
        super().__init__(message)
        self.status = status


def executor_from_db(db: MarketDB, cost: CostModel, now: int) -> LiveExecutor:
    """Одна и та же модель исполнения для сделок, оценки портфелей и B&H: свежее состояние пула, комиссии, газ."""
    pool = db.last_quote("pool")
    pool = pool if pool and now - pool["ts"] <= LC.QUOTE_MAX_AGE else None
    gas, _ = db.kv("gas", {})
    g = (gas or {}).get("usd_per_swap")
    return LiveExecutor(cost, pool, db.recent_fees(), Decimal(str(round(g, 6))) if g else None)


class ForwardRunner:
    def __init__(self, store, cost: CostModel = CONSERVATIVE_V1, initial: str = "1000", clock=time.time):
        self.store, self.cost, self.initial, self.clock = store, cost, initial, clock
        self.db = MarketDB(store)
        self.lock = threading.RLock()
        self.traders: dict[str, Trader] = {}
        self.status, self.experiment_id = "NOT_STARTED", None
        self.quote: dict | None = None
        self.risk: dict = {}
        self._jump: tuple[float, int] | None = None   # (цена, ts) неподтверждённого скачка
        self._an_cache: dict = {}
        last = store.last_control()
        if last and last["experiment_id"]:
            self.status, self.experiment_id = last["to_status"], last["experiment_id"]
            for tf in TFS:
                st = store.trader_state(self.experiment_id, tf)
                if st:
                    t = Trader(tf, "FORWARD", self.experiment_id, cost, initial)
                    t.load_state(st)
                    self.traders[tf] = t

    # ------------------------------------------------------------------ состояние
    def allowed(self) -> list[str]:
        out = [c for c, src in TRANSITIONS.items() if self.status in src]
        if not any(t.position for t in self.traders.values()):
            out = [c for c in out if c not in ("CLOSE_POSITION", "EMERGENCY_EXIT")]
        return out

    def started_at(self) -> int | None:
        e = self.store.experiment(self.experiment_id) if self.experiment_id else None
        return e["started_at"] if e else None

    def info(self) -> dict:
        return {"status": self.status, "experiment": self.experiment_id, "started_at": self.started_at(), "allowed": self.allowed(),
                "traders": {tf: {"state": t.state, "paused": t.paused, "last_bar_ts": t.last_bar_ts, "open_position": t.position is not None,
                                 "pending": (t.pending or {}).get("kind")} for tf, t in self.traders.items()},
                "controls": self.store.controls(30), "quote": self.quote, "risk": self.risk}

    def _check(self, cmd: str) -> None:
        if self.status not in TRANSITIONS[cmd]:
            raise ControlError(f"{cmd} недоступна в состоянии {self.status}", self.status)
        if cmd not in self.allowed():
            raise ControlError(f"{cmd}: нет открытых позиций", self.status)

    def _log(self, cmd: str, prev: str, to: str, note: str = "") -> None:
        self.store.log_control(self.experiment_id, int(self.clock()), cmd, prev, to, note, list(self.traders.values()))

    def _flush(self) -> None:
        for t in self.traders.values():
            r = t.pending_records()
            if any(r.values()):
                self.store.commit_bar(t)

    # ------------------------------------------------------------------ команды
    def execute(self, command: str, args: dict | None = None) -> dict:
        fn = {"START": self.start, "PAUSE": self.pause, "RESUME": self.resume, "STOP": self.stop,
              "CLOSE_POSITION": lambda: self.close_position((args or {}).get("tf")), "EMERGENCY_EXIT": self.emergency_exit}.get(command)
        if fn is None:
            raise ControlError(f"неизвестная команда {command}", self.status)
        return fn()

    def start(self) -> dict:
        with self.lock:
            self._check("START")
            now = int(self.clock())
            params = experiment_params(self.cost, now, self.initial)
            params["forward"] = {"late_limit": LC.LATE_LIMIT, "entry_ttl": LC.ENTRY_TTL, "quote_max_age": LC.QUOTE_MAX_AGE,
                                 "source_max_divergence": LC.SOURCE_MAX_DIVERGENCE, "exec_model": "v4-clmm-active-range-estimate",
                                 "exec_slippage": LC.EXEC_SLIPPAGE}
            exp, n = f"FW-{now}", 1
            while self.store.experiment(exp) is not None:      # START сразу после STOP в ту же секунду
                n += 1
                exp = f"FW-{now}-{n}"
            self.store.create_experiment(exp, "FORWARD", now, params, params_hash(params), "forward paper trading (live)")
            self.traders = {}
            for tf in TFS:
                t = Trader(tf, "FORWARD", exp, self.cost, self.initial)
                last = self.db.last_closed_ts(tf)
                # свечи, закрытые до START, — только история: решений по ним в FORWARD нет
                closed_before = [x for x in ([last] if last is not None else []) if x + TF_SEC[tf] <= now]
                t.last_bar_ts = closed_before[0] if closed_before else -1
                t.state = "FLAT"
                self.traders[tf] = t
            prev = self.status
            self.status, self.experiment_id = "RUNNING", exp
            self._jump = None
            self._log("START", prev, "RUNNING", f"три портфеля по ${self.initial}; Buy & Hold — по первой проверенной котировке")
            return self.info()

    def pause(self) -> dict:
        with self.lock:
            self._check("PAUSE")
            for t in self.traders.values():
                t.paused = True
                if t.pending and t.pending["kind"] == "ENTER":
                    t.pending = None
                    t._push("events", {"ts": int(self.clock()), "type": "ENTRY_CANCELLED", "why": "PAUSE", "policy": "pause"})
            self._flush()
            prev, self.status = self.status, "PAUSED"
            self._log("PAUSE", prev, "PAUSED", "новые входы запрещены; защитные стопы и выходы активны")
            return self.info()

    def resume(self) -> dict:
        with self.lock:
            self._check("RESUME")
            for t in self.traders.values():
                t.paused = False
            prev, self.status = self.status, "RUNNING"
            self._log("RESUME", prev, "RUNNING")
            return self.info()

    def stop(self) -> dict:
        with self.lock:
            self._check("STOP")
            cancelled, kept = [], []
            for tf, t in self.traders.items():
                if t.pending:
                    cancelled.append(f"{tf.upper()}:{t.pending['kind']}")
                    t._push("events", {"ts": int(self.clock()), "type": f"{t.pending['kind']}_CANCELLED", "why": "STOP эксперимента",
                                       "policy": "stop"})
                    t.pending = None
                if t.position:
                    kept.append(tf.upper())
                    t.state = "LONG"
                t.paused = True
            self._flush()
            prev, self.status = self.status, "STOPPED"
            note = "автоматической продажи нет"
            note += f"; позиции сохранены: {', '.join(kept)}" if kept else "; открытых позиций нет"
            if cancelled:
                note += f"; отменены отложенные ордера: {', '.join(cancelled)}"
            self._log("STOP", prev, "STOPPED", note)
            return self.info()

    def close_position(self, tf: str | None) -> dict:
        with self.lock:
            self._check("CLOSE_POSITION")
            t = self.traders.get(tf or "")
            if t is None or t.position is None:
                raise ControlError(f"у трейдера {tf} нет открытой позиции", self.status)
            t.request_close(int(self.clock()), "CLOSE POSITION (вручную)")
            self._flush()
            self._log("CLOSE_POSITION", self.status, self.status, f"{tf.upper()}: продажа по первой проверенной котировке")
            return self.info()

    def emergency_exit(self) -> dict:
        with self.lock:
            self._check("EMERGENCY_EXIT")
            now, closing = int(self.clock()), []
            for tf, t in self.traders.items():
                if t.pending and t.pending["kind"] == "ENTER":
                    t.pending = None
                    t._push("events", {"ts": now, "type": "ENTRY_CANCELLED", "why": "EMERGENCY EXIT", "policy": "emergency"})
                if t.position:
                    t.request_close(now, "EMERGENCY EXIT (вручную)")
                    closing.append(tf.upper())
                if self.status == "RUNNING":
                    t.paused = True
            self._flush()
            prev = self.status
            if self.status == "RUNNING":
                self.status = "PAUSED"
            self._log("EMERGENCY_EXIT", prev, self.status, f"закрываются: {', '.join(closing) or 'нет'}; новые входы остановлены")
            return self.info()

    # ------------------------------------------------------------------ контекст и модель исполнения
    def executor(self) -> LiveExecutor:
        return executor_from_db(self.db, self.cost, int(self.clock()))

    def decision_cost(self) -> CostModel:
        """Допущения для решения стратегии и размера: консервативные slippage как в backtest, но фактическая комиссия
        hook, ликвидность и газ (параметры стратегии не меняются)."""
        fees = self.db.recent_fees()
        ds = self.db.last_quote("ds") or {}
        gas, _ = self.db.kv("gas", {})
        c = self.cost
        return replace(c, name="forward-decision-v1",
                       buy_fee=Decimal(fees["BUY"]) / 1_000_000 if fees.get("BUY") else c.buy_fee,
                       sell_fee=Decimal(fees["SELL"]) / 1_000_000 if fees.get("SELL") else c.sell_fee,
                       liquidity_usd=Decimal(str(ds["liquidity_usd"])) if ds.get("liquidity_usd") else c.liquidity_usd,
                       gas_usd=max(Decimal(str(round(gas.get("usd_per_swap") or 0, 6))), c.gas_usd))

    def market_ctx(self, fables_1d) -> dict | None:
        eth = self.db.last_context("daily_ETH")
        closes = [p for _, p in (eth or {}).get("data", [])]
        if len(closes) < 21:
            return None
        e20 = ema(closes, 20)[-1]
        out = {"eth_above_ema20": closes[-1] > e20, "eth_close": closes[-1], "eth_ema20": e20}
        fc = [k.c for k in fables_1d.candles] if fables_1d else []
        if len(fc) >= 8 and len(closes) >= 8:
            out["rs_vs_eth_7d"] = (fc[-1] / fc[-8] - 1) - (closes[-1] / closes[-8] - 1)
        return out

    def fund_ctx(self, now: int) -> dict:
        out = {}
        ds = self.db.last_quote("ds")
        if ds and now - ds["ts"] <= 3600:
            h = (ds.get("txns") or {}).get("h24") or {}
            if h.get("buys"):
                out["sell_buy_h24"] = h["sells"] / h["buys"]
            old = self.db.quotes_since("ds", now - 7 * 86400 - 3600)
            if old and old[0]["ts"] <= now - 7 * 86400 + 3600 and old[0].get("liquidity_usd") and ds.get("liquidity_usd"):
                out["liq_change_7d"] = ds["liquidity_usd"] / old[0]["liquidity_usd"] - 1
        return out

    # ------------------------------------------------------------------ Forward Scheduler: закрытые свечи
    def _analysis(self, tf: str, now: int):
        s, avail = self.db.candles(tf, now)
        key = (tf, len(s), s.candles[-1].ts if len(s) else None)
        if self._an_cache.get(tf, (None,))[0] != key:
            self._an_cache[tf] = (key, analyze(s, ANALYSIS_PARAMS[tf]), avail)
        return self._an_cache[tf][1], self._an_cache[tf][2]

    def on_bars(self, now: int | None = None) -> int:
        """Обработать новые закрытые свечи каждого трейдера (идемпотентно, по last_bar_ts)."""
        with self.lock:
            if self.status not in ACTIVE:
                return 0
            now = int(now if now is not None else self.clock())
            started = self.started_at()
            n = 0
            an = {tf: self._analysis(tf, now) for tf in TFS}
            cost = self.decision_cost()
            for tf, t in self.traders.items():
                a, avail = an[tf]
                htf_tf = STRATEGIES[tf][0]["htf"]
                h = an[htf_tf][0] if htf_tf else None
                hclose = [k.ts + TF_SEC[htf_tf] for k in h.s.candles] if h else []
                for i, k in enumerate(a.s.candles):
                    if k.ts <= t.last_bar_ts:
                        continue
                    close = k.ts + TF_SEC[tf]
                    if close < started:
                        t.last_bar_ts = k.ts          # история до START: без решений
                        continue
                    t.cost = cost
                    got = avail.get(k.ts, now)
                    lag = got - close
                    if lag > LC.LATE_LIMIT[tf]:
                        t.skip_bar(a, i, f"свеча получена через {lag // 60} мин после закрытия (> {LC.LATE_LIMIT[tf] // 60} мин): "
                                         f"бот не работал или источник был недоступен", now)
                    else:
                        j = bisect.bisect_right(hclose, close) - 1 if h else None
                        t.on_live_bar(a, i, h, j if j is not None and j >= 0 else None, received_ts=got, decided_ts=now,
                                      market=self.market_ctx(an["1d"][0].s) if tf == "4h" else None,
                                      fund=self.fund_ctx(now) if tf == "1d" else None)
                    self.store.commit_bar(t)
                    n += 1
            return n

    # ------------------------------------------------------------------ Risk Monitor: котировка
    def build_quote(self, now: int) -> dict:
        pool, ds = self.db.last_quote("pool"), self.db.last_quote("ds")
        fresh = lambda q: q is not None and now - q["ts"] <= LC.QUOTE_MAX_AGE  # noqa: E731
        issues, q = [], {"ts": None, "mid": None, "source": None, "valid": False}
        if fresh(pool):
            q.update(ts=pool["ts"], mid=pool["mid"], source="StateView (on-chain)", block=pool.get("block"))
            if fresh(ds):
                div = abs(ds["mid"] / pool["mid"] - 1)
                q["divergence"] = div
                if div > LC.SOURCE_MAX_DIVERGENCE:
                    issues.append(f"расхождение пул/DexScreener {div * 100:.2f}% > {LC.SOURCE_MAX_DIVERGENCE * 100:.0f}%")
            else:
                q["single_source"] = True
        elif fresh(ds):
            q.update(ts=ds["ts"], mid=ds["mid"], source="DexScreener (нет свежего состояния пула)", single_source=True)
        else:
            issues.append("нет свежей котировки (старше %d с)" % LC.QUOTE_MAX_AGE)
        last_valid, _ = self.db.kv("last_valid_quote")
        if q["mid"] and last_valid and not issues:
            jump = q["mid"] / last_valid["mid"] - 1
            if abs(jump) > LC.JUMP_CONFIRM:
                if self._jump is not None and q["ts"] > self._jump[1] and abs(q["mid"] / self._jump[0] - 1) < 0.02:
                    q["jump_confirmed"] = jump           # следующая котировка подтвердила новый уровень
                else:
                    if self._jump is None or abs(q["mid"] / self._jump[0] - 1) >= 0.02:
                        self._jump = (q["mid"], q["ts"])
                    issues.append(f"скачок цены {jump * 100:+.1f}% ждёт подтверждения следующей котировкой")
        q["issues"] = issues
        q["valid"] = bool(q["mid"]) and not issues
        q["age"] = now - q["ts"] if q["ts"] else None
        if q["valid"]:
            self._jump = None
            self.db.kv_set("last_valid_quote", {"ts": q["ts"], "mid": q["mid"], "source": q["source"]}, now)
        return q

    def entry_blockers(self, q: dict, now: int) -> list[str]:
        b = [] if q["valid"] else ["котировка недостоверна: " + "; ".join(q["issues"])]
        ds = self.db.last_quote("ds")
        med = statistics.median([x["liquidity_usd"] for x in self.db.quotes_since("ds", now - 86400) if x.get("liquidity_usd")] or [0]) or None
        if ds and med and ds.get("liquidity_usd") and ds["liquidity_usd"] < (1 - LC.LIQ_DROP_RISK_OFF) * med:
            b.append(f"ликвидность {ds['liquidity_usd']:,.0f}$ < {(1 - LC.LIQ_DROP_RISK_OFF) * 100:.0f}% медианы 24 ч ({med:,.0f}$)")
        prices = (self.db.last_context("prices") or {}).get("data", {})
        usdg = (prices.get("USDG") or {}).get("usd")
        if usdg and abs(usdg - 1) > LC.USDG_DEPEG:
            b.append(f"USDG = {usdg:.4f}$ — отклонение от привязки > {LC.USDG_DEPEG * 100:.1f}%")
        fees = self.db.recent_fees()
        for side in ("BUY", "SELL"):
            if fees.get(side) and fees[side] > LC.FEE_MAX_PPM:
                b.append(f"комиссия hook {side} {fees[side] / 1e4:.2f}% > {LC.FEE_MAX_PPM / 1e4:.0f}%")
        return b

    def on_quote(self, now: int | None = None) -> dict:
        """Каждая новая котировка: исполнение отложенных ордеров и защита позиций (стоп/trailing/цель) — независимо от ТФ."""
        with self.lock:
            now = int(now if now is not None else self.clock())
            q = self.build_quote(now)
            self.quote = q
            if self.status == "NOT_STARTED" or not self.traders:
                return q
            ex = self.executor()
            blockers = self.entry_blockers(q, now)
            self.risk = {"ts": now, "quote_valid": q["valid"], "issues": q["issues"], "entry_blockers": blockers,
                         "exec_model": ex.name if ex.pool else "conservative-fallback", "fees": self.db.recent_fees()}
            unprotected = []
            for tf, t in self.traders.items():
                t.ex = ex
                idx = self.db.count(tf)
                p = t.pending
                handled = False
                if p and p["kind"] == "ENTER":
                    if self.status != "RUNNING":
                        t.pending = None
                        t._push("events", {"ts": now, "type": "ENTRY_CANCELLED", "why": self.status, "policy": "pause"})
                    elif blockers:
                        if now - (p.get("decided_ts") or now) > LC.ENTRY_TTL[tf]:
                            t.pending = None
                            t._push("events", {"ts": now, "type": "ENTRY_CANCELLED", "policy": "entry_ttl",
                                               "why": f"нет условий для входа {LC.ENTRY_TTL[tf] // 60} мин: " + "; ".join(blockers)})
                            t.state = "FLAT"
                    elif q["ts"] > (p.get("decided_ts") or 0):
                        t.on_quote(q["mid"], q["ts"], idx, price_ok=True, quote_source=q["source"])
                        handled = True
                elif p and p["kind"] != "ENTER":
                    if q["valid"] and q["ts"] > (p.get("decided_ts") or 0):
                        t.on_quote(q["mid"], q["ts"], idx, price_ok=True, quote_source=q["source"])
                        handled = True
                    elif not q["valid"]:
                        unprotected.append(tf)
                if t.position and self.status in ACTIVE and t.pending is None and not handled:
                    if q["valid"]:
                        t.intrabar(q["mid"], q["mid"], q["mid"], q["ts"], idx, price_ok=True, quote_source=q["source"])
                    else:
                        unprotected.append(tf)
                if any(t.pending_records().values()):
                    self.store.commit_bar(t)
            if unprotected:
                self.risk["unprotected"] = sorted(set(unprotected))
                self.db.event(now, "risk_unverified_quote", {"traders": self.risk["unprotected"], "issues": q["issues"]})
            self.db.kv_set("risk", self.risk, now)
            self._benchmark(q, ex, now)
            return q

    # ------------------------------------------------------------------ Buy & Hold и синхронные оценки
    def _benchmark(self, q: dict, ex: LiveExecutor, now: int) -> None:
        if not self.experiment_id or self.status == "NOT_STARTED":
            return
        key = f"bh:{self.experiment_id}"
        bh, _ = self.db.kv(key)
        if bh is None and q["valid"] and q["ts"] >= (self.started_at() or 0):
            f = ex.buy(dec(self.initial) - ex.gas_usd, q["mid"])
            self.db.kv_set(key, {"ts": q["ts"], "qty": str(f.qty), "usd": str(f.usd), "gas": str(f.gas_usd), "price": str(f.exec_price),
                                 "mid": q["mid"], "model": f.model, "fee_ppm": f.fee_ppm, "cash": str(dec(self.initial) - f.usd - f.gas_usd)}, now)

    def mark(self, now: int | None = None) -> dict | None:
        """Синхронная оценка всех портфелей и B&H по одной проверенной котировке и одной модели (общий timestamp)."""
        with self.lock:
            now = int(now if now is not None else self.clock())
            q = self.quote
            if not self.experiment_id or not q or not q["valid"] or self.status == "NOT_STARTED":
                return None
            ex = self.executor()
            bh, _ = self.db.kv(f"bh:{self.experiment_id}")
            if bh is None:          # сравнение начинается с момента, когда B&H куплен: у всех кривых общие timestamps
                return None
            out = {"mid": q["mid"], "quote_ts": q["ts"], "source": q["source"], "status": self.status, "traders": {}}
            for tf, t in self.traders.items():
                liq = ex.liquidation(t.portfolio.qty, q["mid"])
                out["traders"][tf] = {"equity": float(t.portfolio.cash + liq), "cash": float(t.portfolio.cash), "qty": float(t.portfolio.qty),
                                      "value": float(liq), "realized": float(t.portfolio.realized), "costs": float(t.portfolio.costs)}
            out["total"] = sum(v["equity"] for v in out["traders"].values())
            if bh:
                v = float(ex.liquidation(dec(bh["qty"]), q["mid"]) + dec(bh["cash"]))
                out["bh"] = {"equity": v, "qty": float(bh["qty"])}
                out["bh_total"] = v * len(self.traders)
            self.db.add_mark(self.experiment_id, now, out)
            return out


ForwardController = ForwardRunner  # совместимость с этапом D
