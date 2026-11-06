"""Отдельный процесс SHADOW LAB (shadow.bat / `python -m tradinglab shadow`).

Цикл (1 с): heartbeat → команды из очереди shadow_lab.db → закрытые свечи 4H → Risk Monitor на каждой новой котировке
основного бота → синхронная оценка → периодическая сверка целостности. Сам SHADOW в сеть не ходит: данные собирает только
основной бот, SHADOW читает их из trading_lab.db через read-only соединение. Ошибка цикла логируется в data/logs/shadow.log
и не останавливает ни процесс SHADOW, ни (тем более) основной бот — это разные процессы и разные базы.
Один процесс SHADOW на папку данных: блокировка файла data/shadow.lock (снимается ОС при завершении процесса).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from pathlib import Path

from ..candles import GRACE_SEC, TF_SEC
from ..forward import ControlError
from ..live import config as LC
from ..live.marketdb import MarketDB
from ..store import Store
from . import strategy as P2
from .readonly import ReadOnlyStore
from .runner import TF, ShadowRunner

log = logging.getLogger("tradinglab.shadow")
DB_NAME, MAIN_DB, LOCK_NAME, LOG_NAME = "shadow_lab.db", "trading_lab.db", "shadow.lock", "shadow.log"


class InstanceLock:
    def __init__(self, path: Path):
        self.path, self.fh = Path(path), None

    def acquire(self) -> bool:
        import sys
        self.fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def release(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


def _t(ts) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if ts else "—"


def readiness(runner: ShadowRunner, main: MarketDB, now: int) -> dict:
    """Проверка готовности перед активацией SHADOW (обязательна для START)."""
    checks = []

    def chk(name, ok, detail, level="required"):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "level": level})

    w, _ = main.kv("worker")
    chk("Основной бот работает (heartbeat)", bool(w) and w.get("status") == "running" and now - w["ts"] <= LC.WORKER_STALE_SEC,
        f"heartbeat {now - w['ts']} с назад, FORWARD {w.get('forward')}" if w else "heartbeat нет — SHADOW не получит свежих данных")
    pool, ds = main.last_quote("pool"), main.last_quote("ds")
    chk("Ончейн-котировка StateView свежая", pool is not None and now - pool["ts"] <= LC.POOL_MAX_AGE,
        f"{pool['mid']:.6g} USDG, {now - pool['ts']} с назад" if pool else "нет")
    if pool and ds and now - ds["ts"] <= LC.QUOTE_MAX_AGE:
        div = abs(ds["mid"] / pool["mid"] - 1)
        chk("Согласие StateView и DexScreener", div <= LC.SOURCE_MAX_DIVERGENCE, f"расхождение {div * 100:.2f}%")
    pt, _ = main.kv("pool_ticks")
    chk("Тики CLMM-пула свежие", bool(pt) and pool is not None and now - pt.get("fetched_at", 0) <= LC.TICKS_MAX_AGE
        and pt["known"][0] < pool["tick"] < pt["known"][1], f"известная область {pt['known']}, {now - pt.get('fetched_at', 0)} с назад" if pt else "нет")
    fees = main.recent_fees()
    chk("Комиссия hook из свопов пула", bool(fees.get("BUY") and fees.get("SELL")),
        f"BUY {fees.get('BUY')} ppm, SELL {fees.get('SELL')} ppm (n={fees.get('BUY_n')}/{fees.get('SELL_n')})" if fees.get("BUY") else "свопов нет")
    gas, _ = main.kv("gas", {})
    chk("Газ (USD за своп)", bool((gas or {}).get("usd_per_swap")), f"{(gas or {}).get('usd_per_swap')}$" if gas else "нет")
    for tf in (TF, "1d"):
        d = TF_SEC[tf]
        exp = (now - GRACE_SEC) // d * d - d
        last = main.last_closed_ts(tf)
        late = now - (exp + d) > 1800
        chk(f"Закрытые свечи {tf.upper()}", last is not None and (last >= exp or not late),
            f"{main.count(tf)} свечей, последняя открыта {_t(last)}" if last else "свечей нет")
    s4, _ = main.candles(TF, now)
    hist = P2._history(s4) if len(s4) else None
    chk("История 4H с запуска пула (нужна структуре v3)", hist == "launch",
        f"первая свеча {_t(s4.candles[0].ts)}, режим истории: {hist}" if len(s4) else "свечей нет")
    chk(f"Правила {P2.STRATEGY['id']} {P2.VERSION} совпадают с зафиксированными", runner.version_ok,
        runner.version_issue or f"хэш {P2.rules_hash()[:12]}")
    integ = runner.integrity()
    chk("Целостность shadow_lab.db", integ["ok"], "; ".join(integ["issues"]) or "состояние совпадает с журналом")
    failed = [c for c in checks if not c["ok"] and c["level"] == "required"]
    return {"ok": not failed, "ts": now, "checks": checks, "failed": [c["name"] for c in failed]}


class ShadowProcess:
    def __init__(self, data: Path | str, clock=time.time, durable: bool = True):
        self.data, self.clock = Path(data), clock
        self.store = Store(self.data / DB_NAME, durable=durable)
        self.own = MarketDB(self.store)
        self.main_path = self.data / MAIN_DB
        self.ro: ReadOnlyStore | None = None
        self.runner: ShadowRunner | None = None
        self.started_at = int(clock())
        self.ticks, self.errors = 0, []
        self.last_quote_ts, self.last_quote_check, self.last_bars, self.last_mark, self.last_integrity = None, -1e18, -1e18, -1e18, -1e18
        self.lock = threading.RLock()
        self._connect()

    def _connect(self) -> bool:
        if self.runner is not None:
            return True
        try:
            self.ro = ReadOnlyStore(self.main_path)
            self.runner = ShadowRunner(self.store, MarketDB(self.ro), clock=self.clock)
            self.check_integrity(int(self.clock()))
            return True
        except Exception as e:  # noqa: BLE001
            self._error("connect", e)
            return False

    def _error(self, where: str, e: Exception) -> None:
        msg = f"{type(e).__name__}: {e}"[:500]
        log.error("shadow %s failed: %s\n%s", where, msg, traceback.format_exc())
        self.errors = (self.errors + [{"ts": int(self.clock()), "where": where, "error": msg}])[-10:]
        try:
            self.own.event(int(self.clock()), "shadow_error", {"where": where, "error": msg})
        except Exception:  # noqa: BLE001
            pass

    def heartbeat(self, now: int, status: str = "running") -> None:
        r = self.runner
        self.own.kv_set("shadow_worker", {"ts": now, "pid": os.getpid(), "started_at": self.started_at, "status": status, "ticks": self.ticks,
                                          "shadow": r.status if r else "NO_MAIN_DB", "experiment": r.experiment_id if r else None,
                                          "errors": self.errors[-5:], "main_db": str(self.main_path)}, now)

    def check_integrity(self, now: int) -> dict:
        self.last_integrity = now
        res = self.runner.integrity()
        self.own.kv_set("integrity", {**res, "ts": now}, now)
        if not res["ok"]:
            log.error("shadow integrity failed: %s", res["issues"])
            self.own.event(now, "integrity_error", {"issues": res["issues"]})
        return res

    def process_commands(self, now: int) -> list[dict]:
        done = []
        for cmd in self.own.pending_commands():
            if now - cmd["ts"] > LC.COMMAND_MAX_AGE:
                self.own.finish_command(cmd["id"], "EXPIRED", {"error": "команда устарела и не исполнена"}, now)
                continue
            done.append(self.execute(cmd["id"], cmd["command"], cmd["args"], now))
        return done

    def execute(self, cid: int, command: str, args, now: int) -> dict:
        a = json.loads(args) if isinstance(args, str) else (args or {})
        try:
            if self.runner is None:
                raise ControlError("нет доступа к базе основного бота", "NO_MAIN_DB")
            if command == "START":
                pf = readiness(self.runner, self.runner.main, now)
                self.own.kv_set("readiness", pf, now)
                if not pf["ok"]:
                    raise ControlError("START отклонён проверкой готовности: " + "; ".join(
                        f"{c['name']} — {c['detail']}" for c in pf["checks"] if not c["ok"] and c["level"] == "required"), self.runner.status)
            res = self.runner.execute(command, a)
            log.info("shadow command %s -> %s", command, res["status"])
            self.own.finish_command(cid, "DONE", {"shadow_status": res["status"]}, now)
            return {"id": cid, "status": "DONE", "result": res}
        except ControlError as e:
            log.warning("shadow command %s rejected: %s", command, e)
            self.own.finish_command(cid, "ERROR", {"error": str(e), "shadow_status": e.status}, now)
            return {"id": cid, "status": "ERROR", "error": str(e)}

    def tick(self, now: float | None = None) -> dict:
        now = int(now if now is not None else self.clock())
        out = {"bars": 0, "quote": False}
        with self.lock:
            self.ticks += 1
            if not self._connect():
                self.heartbeat(now, "waiting_main_db")
                self.process_commands(now)
                return out
            for name, fn in (("commands", lambda: self.process_commands(now)), ("bars", lambda: self._bars(now, out)),
                             ("quote", lambda: self._quote(now, out)), ("mark", lambda: self._mark(now)),
                             ("integrity", lambda: now - self.last_integrity >= 600 and self.check_integrity(now))):
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — ошибка одной линии не останавливает остальные
                    self._error(name, e)
            self.heartbeat(now)
        return out

    def _bars(self, now: int, out: dict) -> None:
        if now - self.last_bars >= 10:
            self.last_bars = now
            out["bars"] = self.runner.on_bars(now)

    def _quote(self, now: int, out: dict) -> None:
        """Сопровождение позиции на каждой новой котировке основного бота (и не реже раза в минуту — для статуса защиты)."""
        q = self.runner.main.last_quote("pool")
        ds = self.runner.main.last_quote("ds")
        key = ((q or {}).get("ts"), (ds or {}).get("ts"))
        if key != self.last_quote_ts or now - self.last_quote_check >= 60:
            self.last_quote_ts, self.last_quote_check = key, now
            self.runner.on_quote(now)
            out["quote"] = True

    def _mark(self, now: int) -> None:
        if now - self.last_mark >= LC.SCHEDULE["mark"] and self.runner.mark(now) is not None:
            self.last_mark = now

    def run_forever(self, stop: threading.Event, interval: float = 1.0) -> None:
        log.info("shadow started pid=%s db=%s main=%s (read-only)", os.getpid(), self.store.path, self.main_path)
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self._error("tick", e)
            stop.wait(interval)
        try:
            self.heartbeat(int(self.clock()), "stopped")
        except Exception:  # noqa: BLE001
            pass
        log.info("shadow stopped")

    def close(self) -> None:
        self.store.close()
        if self.ro:
            self.ro.close()


def setup_logging(data: Path) -> None:
    import logging.handlers
    (data / "logs").mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(data / "logs" / LOG_NAME, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)


def main(data: str = "data", check: bool = False) -> int:
    d = Path(data)
    d.mkdir(parents=True, exist_ok=True)
    if check:                                   # разовая проверка готовности: без цикла, без команд, без сделок
        p = ShadowProcess(d)
        if p.runner is None:
            print("  База основного бота недоступна:", (p.errors or [{}])[-1].get("error"))
            return 1
        r = readiness(p.runner, p.runner.main, int(time.time()))
        print(f"\n  SHADOW LAB — проверка готовности к активации ({P2.STRATEGY['id']} {P2.VERSION}, хэш {P2.rules_hash()[:12]})")
        for c in r["checks"]:
            print(f"  [{'OK ' if c['ok'] else 'НЕТ'}] {c['name']}: {c['detail']}")
        print("\n  ИТОГ:", "готов к START (команда START — только вручную на странице SHADOW LAB)" if r["ok"] else "НЕ готов: " + ", ".join(r["failed"]))
        p.close()
        return 0 if r["ok"] else 2
    lock = InstanceLock(d / LOCK_NAME)
    if not lock.acquire():
        print(f"\n  SHADOW LAB уже запущен для папки {d.resolve()}. Второй экземпляр не запускается.\n")
        return 3
    setup_logging(d)
    p = ShadowProcess(d)
    print(f"\n  SHADOW LAB — PAPER ({P2.STRATEGY['id']} {P2.VERSION}; реальных транзакций нет)\n  База SHADOW: {(d / DB_NAME).resolve()}"
          f"\n  Данные рынка: {(d / MAIN_DB).resolve()} (только чтение; собирает основной бот start.bat)"
          f"\n  Состояние: {p.runner.status if p.runner else 'ожидание базы основного бота'}"
          f"\n  Управление: страница SHADOW LAB панели (http://127.0.0.1:8770/shadow). START — только вручную."
          f"\n  Остановить процесс: Ctrl+C или закрыть окно (состояние сохраняется; при запуске восстанавливается).\n")
    stop = threading.Event()
    try:
        p.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
        p.heartbeat(int(time.time()), "stopped")
    finally:
        p.close()
        lock.release()
    return 0
