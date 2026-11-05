"""Фоновый процесс FORWARD: сбор данных → решения на закрытых свечах → Risk Monitor на котировках → оценки.

Единственный писатель торгового состояния. Веб-интерфейс только читает базу и ставит команды в очередь (commands);
команды исполняются здесь же, последовательно. Работает без браузера и без Claude, пока запущен процесс.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback

from . import config as C
from .collector import Collector
from .health import integrity, preflight
from .marketdb import MarketDB
from ..forward import ControlError, ForwardRunner

log = logging.getLogger("tradinglab.worker")


class Worker:
    def __init__(self, store, transport, clock=time.time):
        self.store, self.clock = store, clock
        self.db = MarketDB(store)
        self.collector = Collector(store, transport, clock)
        self.runner = ForwardRunner(store, clock=clock)
        self.last_mark = -1e18
        self.last_quote_check = -1e18
        self.last_bars = -1e18
        self.started_at = int(clock())
        self.ticks = 0
        self.lock = threading.RLock()
        self.last_integrity = -1e18
        self._bars_pending = False
        self.check_integrity(self.started_at)

    def check_integrity(self, now: int) -> dict:
        """Сверка состояния трейдеров с журналом операций — при запуске и каждые 10 мин."""
        self.last_integrity = now
        res = integrity(self.store)
        self.db.kv_set("integrity", {**res, "ts": now}, now)
        if not res["ok"]:
            log.error("integrity check failed: %s", res["issues"])
            self.db.event(now, "integrity_error", {"issues": res["issues"]})
        return res

    def heartbeat(self, now: int, status: str = "running") -> None:
        self.db.kv_set("worker", {"ts": now, "pid": os.getpid(), "started_at": self.started_at, "status": status, "ticks": self.ticks,
                                  "forward": self.runner.status, "errors": self.collector.errors[-5:]}, now)

    def process_commands(self, now: int) -> list[dict]:
        done = []
        for cmd in self.db.pending_commands():
            if now - cmd["ts"] > C.COMMAND_MAX_AGE:
                self.db.finish_command(cmd["id"], "EXPIRED", {"error": "команда устарела и не исполнена"}, now)
                continue
            done.append(self.execute(cmd["id"], cmd["command"], cmd["args"], now))
        return done

    def execute(self, cid: int, command: str, args, now: int) -> dict:
        import json
        a = json.loads(args) if isinstance(args, str) else (args or {})
        try:
            with self.lock:
                if command == "START":
                    pf = preflight(self.store, now)
                    self.db.kv_set("preflight", pf, now)
                    if not pf["ok"]:
                        raise ControlError("START отклонён проверкой готовности: " + "; ".join(
                            f"{c['name']} — {c['detail']}" for c in pf["checks"] if not c["ok"] and c["level"] == "required"), self.runner.status)
                res = self.runner.execute(command, a)
                log.info("command %s %s -> %s", command, a, res["status"])
            self.db.finish_command(cid, "DONE", {"forward_status": res["status"]}, now)
            return {"id": cid, "status": "DONE", "result": res}
        except ControlError as e:
            log.warning("command %s rejected: %s", command, e)
            self.db.finish_command(cid, "ERROR", {"error": str(e), "forward_status": e.status}, now)
            return {"id": cid, "status": "ERROR", "error": str(e)}

    def tick_fast(self, now: float | None = None) -> dict:
        """Быстрая линия: heartbeat, команды, котировки, Risk Monitor, решения по уже полученным свечам, оценки."""
        now = int(now if now is not None else self.clock())
        with self.lock:
            self.ticks += 1
            self.heartbeat(now)
            self.process_commands(now)
            out = self.collector.tick_fast(now)
            bars = 0
            if self._bars_pending or now - self.last_bars >= 10:
                self._bars_pending = False
                self.last_bars = now
                bars = self.runner.on_bars(now)
            if out["quote"] or now - self.last_quote_check >= 60:
                self.last_quote_check = now
                self.runner.on_quote(now)
            if now - self.last_integrity >= 600:
                self.check_integrity(now)
            if now - self.last_mark >= C.SCHEDULE["mark"]:
                if self.runner.mark(now) is not None:
                    self.last_mark = now
            self.heartbeat(now)
            return {**out, "bars_processed": bars}

    def tick_slow(self, now: float | None = None) -> dict:
        """Медленная линия (свой поток в run_forever): сбор свечей и редких данных. Торговое состояние не трогает."""
        now = int(now if now is not None else self.clock())
        out = self.collector.tick_slow(now)
        if out["new_bars"]:
            self._bars_pending = True
        return out

    def tick(self, now: float | None = None) -> dict:
        """Один полный цикл (тесты, воспроизведение): медленная линия, затем быстрая."""
        now = int(now if now is not None else self.clock())
        slow = self.tick_slow(now)
        fast = self.tick_fast(now)
        self.collector._ticks(now)          # медленная линия в run_forever подхватывает тики сама через ≤ 5 с
        return {**slow, **fast}

    def run_forever(self, stop: threading.Event, interval: float = 1.0, slow_interval: float = 5.0) -> None:
        log.info("worker started pid=%s", os.getpid())

        def slow_loop():
            while not stop.is_set():
                try:
                    self.tick_slow()
                except Exception as e:  # noqa: BLE001
                    log.error("slow tick failed: %s\n%s", e, traceback.format_exc())
                    try:
                        self.db.event(int(self.clock()), "worker_error", {"lane": "slow", "error": f"{type(e).__name__}: {e}"[:500]})
                    except Exception:  # noqa: BLE001
                        pass
                stop.wait(slow_interval)

        th = threading.Thread(target=slow_loop, name="collector-slow", daemon=True)
        th.start()
        while not stop.is_set():
            try:
                self.tick_fast()
            except Exception as e:  # noqa: BLE001 — процесс не должен умирать от ошибки одного цикла
                log.error("tick failed: %s\n%s", e, traceback.format_exc())
                try:
                    self.db.event(int(self.clock()), "worker_error", {"lane": "fast", "error": f"{type(e).__name__}: {e}"[:500]})
                except Exception:  # noqa: BLE001
                    pass
            stop.wait(interval)
        th.join(timeout=30)
        try:
            self.heartbeat(int(self.clock()), "stopped")
        except Exception:  # noqa: BLE001
            pass
        log.info("worker stopped")
