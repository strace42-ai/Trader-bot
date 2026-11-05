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
                res = self.runner.execute(command, a)
            self.db.finish_command(cid, "DONE", {"status": res["status"]}, now)
            return {"id": cid, "status": "DONE", "result": res}
        except ControlError as e:
            self.db.finish_command(cid, "ERROR", {"error": str(e), "status": e.status}, now)
            return {"id": cid, "status": "ERROR", "error": str(e)}

    def tick(self, now: float | None = None) -> dict:
        now = int(now if now is not None else self.clock())
        with self.lock:
            self.ticks += 1
            self.process_commands(now)
            out = self.collector.tick(now)
            bars = 0
            if out["new_bars"] or now - self.last_bars >= 10:
                self.last_bars = now
                bars = self.runner.on_bars(now)
            if out["quote"] or now - self.last_quote_check >= 60:
                self.last_quote_check = now
                self.runner.on_quote(now)
            if now - self.last_mark >= C.SCHEDULE["mark"]:
                if self.runner.mark(now) is not None:
                    self.last_mark = now
            self.heartbeat(now)
            return {**out, "bars_processed": bars}

    def run_forever(self, stop: threading.Event, interval: float = 1.0) -> None:
        log.info("worker started pid=%s", os.getpid())
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — процесс не должен умирать от ошибки одного цикла
                log.error("tick failed: %s\n%s", e, traceback.format_exc())
                try:
                    self.db.event(int(self.clock()), "worker_error", {"error": f"{type(e).__name__}: {e}"[:500]})
                except Exception:  # noqa: BLE001
                    pass
            stop.wait(interval)
        try:
            self.heartbeat(int(self.clock()), "stopped")
        except Exception:  # noqa: BLE001
            pass
        log.info("worker stopped")
