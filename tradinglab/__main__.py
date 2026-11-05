"""CLI Trading Lab. Сетевых запросов к рынку и фоновых процессов нет.

  python -m tradinglab serve [--port 8770] [--data data] [--no-browser]   фоновый бот + веб-панель в одном окне (start.bat)
  python -m tradinglab bot   [--data data]                                фоновый бот без интерфейса
  python -m tradinglab web   [--port 8770] [--data data]                  только интерфейс (бот запущен отдельно)
  python -m tradinglab check [--data DIR]                                 разовая проверка источников живых данных
  python -m tradinglab backtest --fixtures datasets/fables_2026-11-05 [--now 2026-11-05T13:27:00Z] [--db PATH] [--json OUT]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
from pathlib import Path

from . import backtest, candles


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _f(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%M")


def load_fixtures(d: Path, now: int) -> dict:
    return {tf: candles.from_gt_payload(json.loads((d / f"gt_ohlcv_{n}.json").read_text(encoding="utf-8")), tf, now)
            for tf, n in (("1h", "h1"), ("4h", "h4"), ("1d", "d1"))}


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m tradinglab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest", help="backtest на записанных закрытых свечах (без сети)")
    b.add_argument("--fixtures", required=True)
    b.add_argument("--now", default=None, help="момент записи данных (ISO, UTC); свечи, не закрытые к нему, отбрасываются")
    b.add_argument("--db", default=None, help="сохранить журнал в отдельную БД Trading Lab")
    b.add_argument("--json", default=None)
    for name, hlp in (("serve", "фоновый бот + локальная панель (http://127.0.0.1:PORT/)"), ("web", "только локальная панель"),
                      ("bot", "только фоновый бот (без интерфейса)"), ("check", "разовая проверка источников живых данных")):
        sv = sub.add_parser(name, help=hlp)
        sv.add_argument("--data", default="data", help="папка для trading_lab.db")
        if name in ("serve", "web"):
            sv.add_argument("--host", default="127.0.0.1")
            sv.add_argument("--port", type=int, default=8770)
            sv.add_argument("--dataset", default=None, help="папка записанного набора данных для BACKTEST")
            sv.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.cmd in ("serve", "web", "bot"):
        run(a)
        return
    if a.cmd == "check":
        check(a)
        return
    d = Path(a.fixtures)
    now = _ts(a.now) if a.now else int(dt.datetime.now(dt.UTC).timestamp())
    series = load_fixtures(d, now)
    store = None
    if a.db:
        from .store import Store
        store = Store(a.db)
    r = backtest.run(series, store=store)
    print(f"Эксперимент {r['experiment']} (BACKTEST, NOT VALIDATED), старт {_f(r['start_ts'])} UTC, модель издержек "
          f"{r['params']['cost_model']['name']}")
    bh = float(r["bh"][-1]["equity"]) if r["bh"] else None
    print(f"Buy & Hold: ${bh:.2f}" if bh else "Buy & Hold: н/д")
    out = {"experiment": r["experiment"], "start_ts": r["start_ts"], "bh_final": bh, "summary": r["summary"], "blockers": {}}
    for tf, sm in r["summary"].items():
        t = r["traders"][tf]
        blk = collections.Counter(x["against"][0] for x in t.decisions if x["against"])
        out["blockers"][tf] = blk.most_common(6)
        print(f"\n{t.name} [{sm['strategy']} v{sm['version']}] состояние {sm['state']}: equity ${sm['equity']:.2f} "
              f"({sm['net_return_pct']:+.2f}%), сделок {sm['closed_trades']}, операций {sm['operations']}, "
              f"vs B&H {sm.get('vs_bh_usd', 0):+.2f}$, выборка {sm['sample']}")
        for name, n in blk.most_common(6):
            print(f"   блокирует: {name} — {n} раз")
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


class InstanceLock:
    """Один сервер на папку данных: блокировка файла ОС, снимается автоматически при завершении процесса."""

    def __init__(self, path: Path):
        self.path, self.fh = path, None

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
            return False
        return True


def _logging(data: Path) -> None:
    import logging
    import logging.handlers
    (data / "logs").mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(data / "logs" / "tradinglab.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)


def run(a) -> None:
    """serve = бот + веб; bot = только бот; web = только интерфейс. Один бот на папку данных (блокировка файла)."""
    import threading
    import webbrowser

    from .store import Store
    data = Path(a.data)
    data.mkdir(parents=True, exist_ok=True)
    _logging(data)
    worker = None
    stop = threading.Event()
    if a.cmd in ("serve", "bot"):
        lock = InstanceLock(data / "bot.lock")
        if not lock.acquire():
            print(f"\n  Бот FABLES Trading Lab уже запущен для папки {data.resolve()}. Второй экземпляр не запускается.\n")
            if a.cmd == "bot":
                return
            print("  Запускаю только интерфейс.")
        else:
            from .live.transport import HttpTransport
            from .live.worker import Worker
            worker = Worker(Store(data / "trading_lab.db"), HttpTransport())
            threading.Thread(target=worker.run_forever, args=(stop,), name="worker", daemon=True).start()
    print(f"\n  FABLES Trading Lab — PAPER (виртуальная торговля, реальных транзакций нет)\n  Данные: {data.resolve()}")
    if worker:
        print("  Фоновый бот: запущен (живые данные: GeckoTerminal, DexScreener, Robinhood RPC, CoinGecko — только чтение)")
    if a.cmd == "bot":
        print("  Интерфейс не запущен. Остановить: Ctrl+C или закрыть окно.\n")
        try:
            while not stop.wait(1):
                pass
        except KeyboardInterrupt:
            stop.set()
        return
    import uvicorn

    from .dataset import DEFAULT_DATASET
    from .web.app import create_app
    app = create_app(data, Path(a.dataset) if a.dataset else DEFAULT_DATASET, worker=worker)
    url = f"http://{a.host}:{a.port}/"
    print(f"  Панель: {url}\n  Закрытие вкладки браузера бота не останавливает. Остановить: Ctrl+C или закрыть это окно.\n")
    if not a.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    finally:
        stop.set()


def check(a) -> None:
    """Разовый цикл сбора (без торговли и без фонового процесса): показывает, что источники отвечают и данные разбираются."""
    import datetime as dt
    import tempfile

    from .live.collector import Collector
    from .live.marketdb import MarketDB
    from .live.transport import HttpTransport
    from .store import Store
    d = Path(a.data) if a.data != "data" else Path(tempfile.mkdtemp(prefix="lab-check-"))
    st = Store(d / "trading_lab.db")
    tr = HttpTransport()
    col = Collector(st, tr, lambda: dt.datetime.now(dt.UTC).timestamp())
    now = int(dt.datetime.now(dt.UTC).timestamp())
    out = col.tick(now)
    col._swaps(now)
    col._gas(now)
    db = MarketDB(st)
    f = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if t else "—"  # noqa: E731
    print(f"\n  Проверка источников ({f(now)}), база: {d}")
    for tf in ("1h", "4h", "1d"):
        s, _ = db.candles(tf, now)
        fm = db.forming(tf)
        print(f"  GT {tf}: закрытых свечей {len(s)}, последняя {f(s.candles[-1].ts) if len(s) else '—'} (close {s.candles[-1].c if len(s) else '—'}); "
              f"формирующаяся {f(fm['ts']) if fm else '—'}")
    pool, ds = db.last_quote("pool"), db.last_quote("ds")
    print(f"  StateView: mid {pool['mid'] if pool else '—'} USDG, блок {pool.get('block') if pool else '—'}, L {pool.get('liquidity') if pool else '—'}")
    print(f"  DexScreener: {ds['mid'] if ds else '—'} USDG, ликвидность {ds.get('liquidity_usd') if ds else '—'}$")
    fees = db.recent_fees()
    print(f"  Комиссия hook (последние Swap): BUY {fees.get('BUY')} ppm (n={fees.get('BUY_n')}), SELL {fees.get('SELL')} ppm (n={fees.get('SELL_n')})")
    gas, _ = db.kv("gas", {})
    ctx = db.last_context("prices")
    print(f"  Газ: {gas}\n  Контекст: {(ctx or {}).get('data')}")
    # контрольная проверка 1: каждая стратегия получает живые данные и формирует решение сама (без торговли)
    import bisect

    from .analysis import analyze
    from .candles import TF_SEC
    from .forward import ForwardRunner
    from .strategies import ANALYSIS_PARAMS, STRATEGIES, Ctx
    r = ForwardRunner(st, clock=lambda: now)
    an = {tf: analyze(db.candles(tf, now)[0], ANALYSIS_PARAMS[tf]) for tf in ("1h", "4h", "1d")}
    print("  Решения стратегий на последней закрытой живой свече (проверка, без торговли):")
    for tf, (strat, fn) in STRATEGIES.items():
        a_ = an[tf]
        if not len(a_.s):
            print(f"    {tf}: нет свечей")
            continue
        i = len(a_.s) - 1
        h = an.get(strat["htf"]) if strat["htf"] else None
        j = bisect.bisect_right([k.ts + TF_SEC[strat["htf"]] for k in h.s.candles], a_.s.candles[i].ts + TF_SEC[tf]) - 1 if h else None
        d = fn(Ctx(a_, i, h, j, r.decision_cost(), False, r.market_ctx(an["1d"].s) if tf == "4h" else None, r.fund_ctx(now) if tf == "1d" else None))
        bad = [c for c in d["conditions"] if c["ok"] is False]
        print(f"    {tf} {strat['id']}: {d['action']} · свеча {f(d['bar_ts'])} · условий PASS {sum(1 for c in d['conditions'] if c['ok'])}, "
              f"FAIL {len(bad)}" + (f" · первый блокер: {bad[0]['name']} ({bad[0]['value']} vs {bad[0]['req']})" if bad else ""))
    print(f"  Ошибки: {col.errors or 'нет'}\n  Здоровье: {tr.health.snapshot()}\n")


if __name__ == "__main__":
    main()
