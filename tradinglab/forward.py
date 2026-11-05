"""Управление FORWARD paper trading: START / PAUSE / RESUME / STOP. Только симуляция.

Автомат: NOT_STARTED → RUNNING ⇄ PAUSED → STOPPING → STOPPED; из STOPPED новый START создаёт НОВЫЙ эксперимент.
- START: неизменяемый момент начала (started_at = now), три портфеля по $1 000, параметры и их хеш фиксируются.
- PAUSE: новые входы запрещены, отложенные входы отменяются; защитные стопы и выходы работают (risk.EXEC_POLICY).
- STOP: global kill — новые входы запрещены; открытые позиции закрываются по первой доступной цене (STOPPING),
  после закрытия — STOPPED. Без позиций — сразу STOPPED.
Каждая команда записывается в журнал controls вместе с состояниями трейдеров одной транзакцией.

Источник живых данных в этой версии НЕ подключён (нужно отдельное разрешение на сетевой опрос и фоновый процесс):
step() обрабатывает только переданные ему закрытые свечи и в сервере не вызывается.
"""
from __future__ import annotations

import bisect
import threading
import time

from .analysis import analyze
from .backtest import experiment_params
from .candles import TF_SEC
from .engine import Trader
from .execution import CONSERVATIVE_V1, CostModel
from .risk import params_hash
from .strategies import ANALYSIS_PARAMS, STRATEGIES

TFS = ("1h", "4h", "1d")
TRANSITIONS = {"START": ("NOT_STARTED", "STOPPED"), "PAUSE": ("RUNNING",), "RESUME": ("PAUSED",), "STOP": ("RUNNING", "PAUSED")}
FEED = {"connected": False, "status": "NOT CONNECTED",
        "reason": "источник живых данных не подключён: требуется разрешение на периодический опрос API и фоновый процесс"}


class ControlError(Exception):
    def __init__(self, message: str, status: str):
        super().__init__(message)
        self.status = status


class ForwardController:
    def __init__(self, store, cost: CostModel = CONSERVATIVE_V1, initial: str = "1000", clock=time.time):
        self.store, self.cost, self.initial, self.clock = store, cost, initial, clock
        self.lock = threading.RLock()
        self.traders: dict[str, Trader] = {}
        self.status, self.experiment_id = "NOT_STARTED", None
        last = store.last_control()
        if last:
            self.status, self.experiment_id = last["to_status"], last["experiment_id"]
            for tf in TFS:
                st = store.trader_state(self.experiment_id, tf)
                if st:
                    t = Trader(tf, "FORWARD", self.experiment_id, cost, initial)
                    t.load_state(st)
                    t.hydrate(store)
                    self.traders[tf] = t

    # ------------------------------------------------------------------ состояние
    def allowed(self) -> list[str]:
        return [c for c, src in TRANSITIONS.items() if self.status in src]

    def info(self) -> dict:
        exp = self.store.experiment(self.experiment_id) if self.experiment_id else None
        return {"status": self.status, "experiment": self.experiment_id, "started_at": exp["started_at"] if exp else None,
                "allowed": self.allowed(), "feed": FEED,
                "traders": {tf: {"state": t.state, "paused": t.paused, "last_bar_ts": t.last_bar_ts,
                                 "open_position": t.position is not None} for tf, t in self.traders.items()},
                "controls": self.store.controls(20)}

    def _check(self, cmd: str) -> None:
        if self.status not in TRANSITIONS[cmd]:
            raise ControlError(f"{cmd} недоступна в состоянии {self.status}", self.status)

    # ------------------------------------------------------------------ команды
    def start(self) -> dict:
        with self.lock:
            self._check("START")
            now = int(self.clock())
            params = experiment_params(self.cost, now, self.initial)
            exp = f"FW-{now}"
            self.store.create_experiment(exp, "FORWARD", now, params, params_hash(params), "forward paper trading")
            self.traders = {tf: Trader(tf, "FORWARD", exp, self.cost, self.initial) for tf in TFS}
            prev = self.status
            self.status, self.experiment_id = "RUNNING", exp
            self.store.log_control(exp, now, "START", prev, "RUNNING", "три портфеля по $" + self.initial, list(self.traders.values()))
            return self.info()

    def pause(self) -> dict:
        return self._toggle("PAUSE", True, "PAUSED")

    def resume(self) -> dict:
        return self._toggle("RESUME", False, "RUNNING")

    def _toggle(self, cmd: str, paused: bool, to: str) -> dict:
        with self.lock:
            self._check(cmd)
            for t in self.traders.values():
                t.paused = paused
            prev, self.status = self.status, to
            self.store.log_control(self.experiment_id, int(self.clock()), cmd, prev, to, "", list(self.traders.values()))
            return self.info()

    def stop(self) -> dict:
        with self.lock:
            self._check("STOP")
            open_pos = any(t.position is not None for t in self.traders.values())
            to = "STOPPING" if open_pos else "STOPPED"
            cancelled = []
            for tf, t in self.traders.items():
                if t.pending and t.pending["kind"] == "ENTER":
                    t.pending = None   # отложенный вход при STOP отменяется сразу (фиксируется в журнале команд)
                    cancelled.append(tf.upper())
            prev, self.status = self.status, to
            note = "открытые позиции будут закрыты по первой доступной цене" if open_pos else "позиций нет"
            if cancelled:
                note += "; отменены отложенные входы: " + ", ".join(cancelled)
            self.store.log_control(self.experiment_id, int(self.clock()), "STOP", prev, to, note, list(self.traders.values()))
            return self.info()

    # ------------------------------------------------------------------ обработка новых закрытых свечей
    def step(self, series: dict) -> dict:
        """Обработать закрытые свечи, решение по которым принимается не раньше started_at. Идемпотентно."""
        with self.lock:
            if self.status not in ("RUNNING", "PAUSED", "STOPPING"):
                return {"processed": 0, "status": self.status}
            exp = self.store.experiment(self.experiment_id)
            an = {tf: analyze(series[tf], ANALYSIS_PARAMS[tf]) for tf in series}
            n = 0
            for tf, t in self.traders.items():
                if tf not in an:
                    continue
                htf_tf = STRATEGIES[tf][0]["htf"]
                htf = an.get(htf_tf) if htf_tf else None
                htf_close = [k.ts + TF_SEC[htf_tf] for k in htf.s.candles] if htf else []
                for i, k in enumerate(an[tf].s.candles):
                    if k.ts + TF_SEC[tf] < exp["started_at"] or k.ts <= t.last_bar_ts:
                        continue
                    j = bisect.bisect_right(htf_close, k.ts + TF_SEC[tf]) - 1 if htf else None
                    if t.process_bar(an[tf], i, htf, j if j is not None and j >= 0 else None,
                                     global_kill=self.status == "STOPPING") is not None:
                        self.store.commit_bar(t)
                        n += 1
            if self.status == "STOPPING" and all(t.position is None and t.pending is None for t in self.traders.values()):
                self.store.log_control(self.experiment_id, int(self.clock()), "AUTO", "STOPPING", "STOPPED",
                                       "все позиции закрыты", list(self.traders.values()))
                self.status = "STOPPED"
            return {"processed": n, "status": self.status}
