"""ShadowRunner — независимый FORWARD-трейдер SHADOW-P2-4H 1.0.0 (paper, $1 000, только FABLES/USDG, только спот).

Наследует ForwardRunner целиком (команды, Forward Scheduler закрытых свечей, Risk Monitor на котировках, уровни защиты
FULL/DEGRADED/NONE, B&H, синхронные оценки) — модель исполнения и политика защиты те же, что у основного бота. Отличия:
  * своё хранилище data/shadow_lab.db: эксперимент, журнал решений/операций/сделок, состояние, команды, оценки, B&H;
  * рыночные данные основного бота читаются только через read-only соединение (SplitDB) — в trading_lab.db SHADOW
    не пишет ничего, второго опроса API нет;
  * один трейдер 4H с ЗАМОРОЖЕННОЙ стратегией shadow.strategy и risk-v1 4H (min_rr 1,5);
  * хэш правил и параметры фиксируются в неизменяемых параметрах эксперимента при START; при несовпадении кода с
    зафиксированным хэшем исполнение блокируется (VERSION_MISMATCH), изменения правил = новая версия и новый эксперимент.
Все операции — моделируемое paper-исполнение (exec_note), не подтверждённые ончейн-свопы.
"""
from __future__ import annotations

import bisect
import time

from ..candles import TF_SEC
from ..engine import Trader
from ..execution import CONSERVATIVE_V1, CostModel
from ..forward import ACTIVE, ControlError, ForwardRunner
from ..live import config as LC
from ..live.clmm import LiveExecutor
from ..live.marketdb import MarketDB
from ..risk import params_hash
from ..strategies import ANALYSIS_PARAMS
from . import strategy as P2
from .readonly import SplitDB

TF, HTF = "4h", "1d"
INITIAL = "1000"
ASSET = {"pair": "FABLES/USDG", "chain": "Robinhood Chain", "kind": "spot, long-only"}
PAPER_NOTE = "PAPER: моделируемое исполнение SHADOW (не ончейн-своп)"
EXP_PREFIX = "SH-P2-4H"


def _paper(ex: LiveExecutor) -> LiveExecutor:
    """Явная пометка paper-исполнения в каждой операции (exec_note)."""
    ex.extra_note = (PAPER_NOTE + ("; " + ex.extra_note if getattr(ex, "extra_note", "") else ""))
    return ex


class ShadowRunner(ForwardRunner):
    def __init__(self, store, main: MarketDB, cost: CostModel = CONSERVATIVE_V1, clock=time.time):
        self.store, self.cost, self.initial, self.clock = store, cost, INITIAL, clock
        self.main = main
        self.db = SplitDB(main, MarketDB(store))
        import threading
        self.lock = threading.RLock()
        self.traders: dict[str, Trader] = {}
        self.status, self.experiment_id = "NOT_STARTED", None
        self.quote: dict | None = None
        self.risk: dict = {}
        self._jump = None
        self._an_cache: dict = {}
        self.version_ok, self.version_issue = True, ""
        last = store.last_control()
        if last and last["experiment_id"]:
            self.status, self.experiment_id = last["to_status"], last["experiment_id"]
            exp = store.experiment(self.experiment_id) or {}
            import json
            fixed = json.loads(exp.get("params_json") or "{}").get("shadow", {})
            if fixed.get("rules_hash") != P2.rules_hash() or fixed.get("version") != P2.VERSION:
                self.version_ok = False
                self.version_issue = (f"код стратегии не совпадает с зафиксированным при START: версия {fixed.get('version')} / "
                                      f"хэш {str(fixed.get('rules_hash'))[:12]} ≠ {P2.VERSION} / {P2.rules_hash()[:12]}")
            st = store.trader_state(self.experiment_id, TF)
            if st:
                self.traders[TF] = self._trader(self.experiment_id)
                self.traders[TF].load_state(st)
        r, _ = self.db.kv("risk", {})
        self.risk = r or {}

    # ------------------------------------------------------------------ трейдер и эксперимент
    def _trader(self, exp: str) -> Trader:
        t = Trader(TF, "FORWARD", exp, self.cost, self.initial, risk_cfg=P2.risk_cfg())
        t.strategy, t.fn = P2.STRATEGY, P2.decide
        t.name = "SHADOW P2 4H"
        return t

    def params(self, now: int) -> dict:
        return {"shadow": {"kind": "SHADOW", "strategy": P2.STRATEGY["id"], "version": P2.VERSION, "rules_hash": P2.rules_hash(),
                           "params": P2.STRATEGY["params"], "asset": ASSET, "initial": self.initial, "tf": TF, "htf": HTF,
                           "execution": "paper/simulated (модель v4-clmm по данным пула основного бота); не ончейн-свопы",
                           "excluded_from": "общий капитал основного FORWARD ($3 000) — SHADOW не входит"},
                "start_ts": now, "cost_model": self.cost.d(), "risk": P2.risk_cfg().d(), "analysis_params": ANALYSIS_PARAMS[TF],
                "forward": {"late_limit": LC.LATE_LIMIT[TF], "entry_ttl": LC.ENTRY_TTL[TF], "quote_max_age": LC.QUOTE_MAX_AGE,
                            "pool_max_age": LC.POOL_MAX_AGE, "ticks_max_age": LC.TICKS_MAX_AGE, "jump_confirm": LC.JUMP_CONFIRM,
                            "source_max_divergence": LC.SOURCE_MAX_DIVERGENCE, "exec_slippage": LC.EXEC_SLIPPAGE}}

    def info(self) -> dict:
        out = super().info()
        out.update(version_ok=self.version_ok, version_issue=self.version_issue, strategy=P2.STRATEGY, rules_hash=P2.rules_hash())
        return out

    def _check(self, cmd: str) -> None:
        if not self.version_ok and cmd in ("START", "RESUME"):
            raise ControlError(f"{cmd} запрещена: {self.version_issue}", self.status)
        super()._check(cmd)

    def execute(self, command: str, args: dict | None = None) -> dict:
        if command == "CLOSE_POSITION":
            args = {"tf": TF}
        return super().execute(command, args)

    def start(self) -> dict:
        with self.lock:
            self._check("START")
            now = int(self.clock())
            params = self.params(now)
            exp, n = f"{EXP_PREFIX}-{now}", 1
            while self.store.experiment(exp) is not None:
                n += 1
                exp = f"{EXP_PREFIX}-{now}-{n}"
            self.store.create_experiment(exp, "FORWARD", now, params, params_hash(params),
                                         f"SHADOW {P2.STRATEGY['id']} {P2.VERSION} — paper, ${self.initial}, FABLES/USDG 4H")
            t = self._trader(exp)
            last = self.db.last_closed_ts(TF)
            t.last_bar_ts = last if last is not None and last + TF_SEC[TF] <= now else -1   # история до START — без решений
            t.state = "FLAT"
            self.traders = {TF: t}
            prev = self.status
            self.status, self.experiment_id = "RUNNING", exp
            self._jump = None
            self._log("START", prev, "RUNNING", f"SHADOW ${self.initial}; правила {P2.VERSION} хэш {P2.rules_hash()[:12]}; "
                                                f"B&H — по первой проверенной котировке")
            return self.info()

    # ------------------------------------------------------------------ исполнение
    def executor(self) -> LiveExecutor:
        from ..forward import executor_from_db
        return _paper(executor_from_db(self.db, self.cost, int(self.clock())))

    def _degraded_executor(self, note: str) -> LiveExecutor:
        return _paper(super()._degraded_executor(note))

    # ------------------------------------------------------------------ закрытые свечи 4H
    def on_bars(self, now: int | None = None) -> int:
        with self.lock:
            if self.status not in ACTIVE or TF not in self.traders or not self.version_ok:
                return 0
            now = int(now if now is not None else self.clock())
            started = self.started_at()
            a, avail = self._analysis(TF, now)
            h, _ = self._analysis(HTF, now)
            hclose = [k.ts + TF_SEC[HTF] for k in h.s.candles]
            cost = self.decision_cost()
            t, n = self.traders[TF], 0
            for i, k in enumerate(a.s.candles):
                if k.ts <= t.last_bar_ts:
                    continue
                close = k.ts + TF_SEC[TF]
                if close < started:
                    t.last_bar_ts = k.ts
                    continue
                t.cost = cost
                got = avail.get(k.ts, now)
                # опоздание — по моменту решения SHADOW (не только получения свечи основным ботом): если SHADOW был выключен,
                # свеча, вовремя полученная основным ботом, всё равно не решается задним числом
                lag = max(got, now) - close
                if lag > LC.LATE_LIMIT[TF]:
                    who = "основной бот не работал или источник был недоступен" if got - close > LC.LATE_LIMIT[TF] else "процесс SHADOW не работал"
                    t.skip_bar(a, i, f"свеча обработана через {lag // 60} мин после закрытия (> {LC.LATE_LIMIT[TF] // 60} мин): "
                                     f"{who} — решения задним числом нет", now)
                else:
                    j = bisect.bisect_right(hclose, close) - 1
                    t.on_live_bar(a, i, h, j if j >= 0 else None, received_ts=got, decided_ts=now)
                self.store.commit_bar(t)
                n += 1
            return n

    def on_quote(self, now: int | None = None) -> dict:
        if not self.version_ok:
            with self.lock:     # без исполнения: только котировка для отображения
                self.quote = self.build_quote(int(now if now is not None else self.clock()))
                return self.quote
        return super().on_quote(now)

    # ------------------------------------------------------------------ целостность и сравнение
    def integrity(self) -> dict:
        from ..live.health import integrity
        return integrity(self.store, self.experiment_id)

    def bh(self) -> dict | None:
        return self.db.kv(f"bh:{self.experiment_id}")[0] if self.experiment_id else None


def compare(shadow_marks: list[dict], main_marks: list[dict], start: int | None) -> dict:
    """SHADOW P2 vs S2 (4H основного FORWARD), Buy & Hold и Cash — с момента активации SHADOW, на общих отметках.
    S2 нормируется к $1 000 по первой оценке основного FORWARD не раньше первой оценки SHADOW. SHADOW в $3 000 не входит."""
    if not shadow_marks or start is None:
        return {"rows": [], "note": "SHADOW ещё не активирован или нет оценок"}
    t0 = shadow_marks[0]["ts"]
    s2 = [m for m in main_marks if m["ts"] >= t0 and (m.get("traders") or {}).get("4h")]
    base = s2[0]["traders"]["4h"]["equity"] if s2 else None
    s2_ts = [m["ts"] for m in s2]
    rows = []
    for m in shadow_marks:
        r = {"ts": m["ts"], "shadow": m["traders"][TF]["equity"], "bh": (m.get("bh") or {}).get("equity"), "cash": float(INITIAL)}
        if base:
            k = bisect.bisect_right(s2_ts, m["ts"]) - 1
            if k >= 0 and m["ts"] - s2_ts[k] <= 3600:
                r["s2"] = s2[k]["traders"]["4h"]["equity"] / base * float(INITIAL)
        rows.append(r)
    last = rows[-1]
    summ = {k: (last.get(k) / float(INITIAL) - 1) * 100 if last.get(k) is not None else None for k in ("shadow", "s2", "bh", "cash")}
    return {"rows": rows, "since": t0, "return_pct": summ,
            "note": "S2 нормирован к $1 000 на момент первой оценки SHADOW; SHADOW не входит в $3 000 основного эксперимента"}
