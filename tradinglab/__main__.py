"""CLI Trading Lab. Сетевых запросов к рынку и фоновых процессов нет.

  python -m tradinglab serve [--port 8770] [--data data] [--no-browser]   фоновый бот + веб-панель в одном окне (start.bat)
  python -m tradinglab bot   [--data data]                                фоновый бот без интерфейса
  python -m tradinglab web   [--port 8770] [--data data]                  только интерфейс (бот запущен отдельно)
  python -m tradinglab check [--data DIR]                                 разовая проверка источников живых данных
  python -m tradinglab doctor [--data data] [--port 8770]                 полная диагностика + файл отчёта для поддержки
  python -m tradinglab audit [--tf 1h] [--md FILE]                        аудит индикаторов: ядро vs эталон Pine/TradingView (живые данные)
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
                      ("bot", "только фоновый бот (без интерфейса)"), ("check", "разовая проверка источников живых данных"),
                      ("doctor", "полная диагностика и файл отчёта")):
        sv = sub.add_parser(name, help=hlp)
        sv.add_argument("--data", default="data", help="папка для trading_lab.db")
        if name == "doctor":
            sv.add_argument("--port", type=int, default=8770)
            sv.add_argument("--offline", action="store_true", help="без проверки живых источников")
        if name in ("serve", "web"):
            sv.add_argument("--host", default="127.0.0.1")
            sv.add_argument("--port", type=int, default=8770)
            sv.add_argument("--dataset", default=None, help="папка записанного набора данных для BACKTEST")
            sv.add_argument("--no-browser", action="store_true")
    au = sub.add_parser("audit", help="аудит индикаторов по живым данным (ядро vs эталон Pine/TradingView)")
    au.add_argument("--tf", default="1h", choices=["1h", "4h", "1d"])
    au.add_argument("--md", default=None, help="записать отчёт в Markdown")
    au.add_argument("--n", type=int, default=20)
    a = ap.parse_args()
    if a.cmd == "audit":
        audit_cli(a)
        return
    if a.cmd in ("serve", "web", "bot"):
        run(a)
        return
    if a.cmd == "check":
        check(a)
        return
    if a.cmd == "doctor":
        doctor(a)
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


def doctor(a) -> None:
    """Диагностика первого запуска. Ничего не меняет в рабочей базе (кроме проверки миграций) и не торгует."""
    import datetime as dt
    import importlib.metadata as md
    import json as _json
    import platform
    import sys
    import tempfile
    import traceback
    import urllib.request

    data = Path(a.data)
    out: list[str] = []
    fails: list[str] = []

    def P(line: str = "") -> None:
        print(line)
        out.append(line)

    def res(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        mark = "OK  " if ok else ("WARN" if warn else "FAIL")
        if not ok and not warn:
            fails.append(name)
        P(f"  [{mark}] {name}: {detail}")

    now = int(dt.datetime.now(dt.UTC).timestamp())
    P(f"FABLES Trading Lab — диагностика {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M:%S} UTC")
    P(f"Версия: {__import__('tradinglab').LAB_VERSION}; папка программы: {Path(__file__).resolve().parent.parent}")
    P("\n1. Python и зависимости")
    res("Python", sys.version_info >= (3, 11), f"{platform.python_version()} ({platform.architecture()[0]}), {sys.executable}")
    res("Платформа", True, f"{platform.platform()}")
    req = {}
    for line in (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].split(";")[0].strip()
        if "==" in line:
            n, v = line.split("==")
            req[n.strip()] = v.strip()
    for n, v in req.items():
        try:
            got = md.version(n)
            res(f"пакет {n}", got == v, f"установлено {got}, требуется {v}", warn=got != v)
        except md.PackageNotFoundError:
            if n == "colorama" and sys.platform != "win32":
                continue
            res(f"пакет {n}", False, "не установлен — запустите start.bat (pip install -r requirements.txt)")
    P("\n2. Хранилище")
    try:
        data.mkdir(parents=True, exist_ok=True)
        t = data / ".write_test"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        res("папка данных", True, str(data.resolve()))
    except Exception as e:  # noqa: BLE001
        res("папка данных", False, f"{data.resolve()}: {e}")
    try:
        from .live.health import integrity, restart_selftest
        from .live.marketdb import MarketDB
        from .store import Store
        st = Store(data / "trading_lab.db")
        res("база trading_lab.db", True, f"схема v{st.schema_version}, quick_check {st._one('PRAGMA quick_check')[0]}")
        last = st.last_control()
        P(f"       FORWARD: {last['to_status'] + ' ' + last['experiment_id'] if last else 'не запускался'}; экспериментов: "
          f"{len(st.experiments())}")
        ig = integrity(st)
        res("целостность состояния FORWARD", ig["ok"], "; ".join(ig["issues"]) or f"проверено трейдеров: {len(ig['checked'])}")
        for c in ig["checked"]:
            P(f"       {c['tf']}: операций {c['fills']}, кеш {c['cash']}, FABLES {c['qty']}")
        db = MarketDB(st)
        w, _ = db.kv("worker")
        if w:
            age = now - w["ts"]
            res("фоновый бот", age <= 30 and w.get("status") == "running", f"heartbeat {age} с назад, статус {w.get('status')}, pid {w.get('pid')}",
                warn=True)
        else:
            res("фоновый бот", False, "ещё не запускался в этой папке данных", warn=True)
        events = db.events(60)
    except Exception as e:  # noqa: BLE001
        res("база trading_lab.db", False, f"{type(e).__name__}: {e}")
        P(traceback.format_exc())
        events = []
    with tempfile.TemporaryDirectory() as td:
        try:
            r = restart_selftest(td)
            res("самопроверка рестарта (временная база)", r["ok"], f"статус после перезапуска {r['status_restored']}, журнал неизменяем: "
                f"{r['journal_append_only']}, целостность: {r['integrity']}")
        except Exception as e:  # noqa: BLE001
            res("самопроверка рестарта", False, f"{type(e).__name__}: {e}")
    P("\n3. Панель")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/health", timeout=3) as r:
            h = _json.loads(r.read())
        res("панель", True, f"http://127.0.0.1:{a.port}/ отвечает (версия {h.get('version')}, бот: {'работает' if h['worker']['alive'] else 'нет'})")
    except Exception as e:  # noqa: BLE001
        import socket
        busy = socket.socket().connect_ex(("127.0.0.1", a.port)) == 0
        res("панель", False, f"порт {a.port} {'занят другой программой' if busy else 'свободен — панель не запущена'} ({type(e).__name__})", warn=not busy)
    if not a.offline:
        P("\n4. Живые источники (временная база, без торговли)")
        try:
            from .live.collector import Collector
            from .live.health import preflight
            from .live.transport import HttpTransport
            from .store import Store as _S
            with tempfile.TemporaryDirectory() as td:
                st2 = _S(Path(td) / "probe.db")
                tr = HttpTransport()
                col = Collector(st2, tr, lambda: dt.datetime.now(dt.UTC).timestamp())
                col.tick(now)
                col._swaps(now)
                col._gas(now)
                pf = preflight(st2, int(dt.datetime.now(dt.UTC).timestamp()))
                for c in pf["checks"]:
                    if c["name"] == "Целостность состояния":
                        continue
                    res(c["name"], c["ok"], c["detail"], warn=c["level"] == "warning")
                for e in col.errors:
                    P(f"       ошибка: {e['where']}: {e['error']}")
                st2.close()
                tr.close()
        except Exception as e:  # noqa: BLE001
            res("живые источники", False, f"{type(e).__name__}: {e}")
            P(traceback.format_exc())
    P("\n5. Последние события данных")
    for e in events[:40]:
        P(f"  {dt.datetime.fromtimestamp(e['ts'], dt.UTC):%m-%d %H:%M:%S} {e['kind']}: "
          f"{_json.dumps({k: v for k, v in e.items() if k not in ('ts', 'kind')}, ensure_ascii=False)[:240]}")
    log = data / "logs" / "tradinglab.log"
    P("\n6. Журнал ошибок (последние строки tradinglab.log)")
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = [x for x in lines if " ERROR " in x or " WARNING " in x or "Traceback" in x or x.startswith(("  File", "    "))][-150:]
        P("\n".join(tail) if tail else "  ошибок и предупреждений нет")
    else:
        P("  журнала ещё нет")
    P("\nИтог: " + ("ВСЁ В ПОРЯДКЕ — можно нажимать START" if not fails else "ЕСТЬ ПРОБЛЕМЫ: " + ", ".join(fails)))
    rep = data / "logs" / f"diagnostics-{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}.txt"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nОтчёт сохранён: {rep.resolve()}\n(токен управления в отчёт не включается; файл можно передать для диагностики)")


def audit_cli(a) -> None:
    """Живые свечи GeckoTerminal + котировки пула (read-only) → аудит индикаторов с формирующейся свечой."""
    import datetime as dt
    import tempfile

    from . import audit as AU
    from .analysis import analyze
    from .live.collector import Collector
    from .live.forming import live_bars, live_values
    from .live.marketdb import MarketDB
    from .live.transport import HttpTransport
    from .store import Store
    from .strategies import ANALYSIS_PARAMS
    clk = lambda: dt.datetime.now(dt.UTC).timestamp()  # noqa: E731
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "audit.db")
        col = Collector(st, HttpTransport(), clk)
        now = int(clk())
        col.tick(now)
        db = MarketDB(st)
        s, _ = db.candles(a.tf, now)
        an = analyze(s, ANALYSIS_PARAMS[a.tf])
        bars = live_bars(db, a.tf, now)
        lv = live_values(s, bars, ANALYSIS_PARAMS[a.tf])
        r = AU.audit(an, n=a.n, live=lv)
        q = db.last_quote("pool")
        st.close()
    f = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%M") if t else "LIVE"  # noqa: E731
    g = lambda x, k: "—" if x is None else (f"{x:,.0f}" if k == "obv" else f"{x:.6g}" if abs(x) < 1 else f"{x:.4f}")  # noqa: E731
    out = [f"# Аудит индикаторов {a.tf.upper()} — {dt.datetime.fromtimestamp(now, dt.UTC):%Y-%m-%d %H:%M} UTC", "",
           f"Данные: GeckoTerminal OHLCV (закрытых свечей {r['bars']}, последняя закрытая {f(r['last_closed_ts'])} UTC); "
           f"формирующаяся свеча: open/high/low/close {', '.join(f'{b[k]:.6f}' for b in bars[-1:] for k in ('open', 'high', 'low', 'close'))} "
           f"(close — StateView {q['mid']:.6f} USDG)." if bars and q else "", "",
           f"Эталон — независимая реализация в семантике TradingView/Pine. {r['timestamp_convention']}. {r['value_convention']}.", "",
           "| Индикатор | Статус | max abs diff | max rel diff |", "|---|---|---|---|"]
    out += [f"| {k} | {v['status']} | {v['max_abs_diff']:.2e} | {v['max_rel_diff'] * 100:.2e}% |" for k, v in r["summary"].items()]
    for k in ("rsi", "macd", "macd_signal", "macd_hist", "mfi", "cmf", "obv"):
        out += ["", f"## {k}", "", "| timestamp (UTC) | close | ours | reference | diff | " + ("RSI SMA | RSI EMA | " if k == "rsi" else "") + "note |",
                "|---|---|---|---|---|" + ("---|---|" if k == "rsi" else "") + "---|"]
        for x in r["tables"][k]:
            out.append(f"| {f(x['ts']) if x['close'] is not None else f(x['ts']) + ' LIVE'} | {g(x['close'], '')} | {g(x['ours'], k)} | {g(x['reference'], k)} | "
                       f"{'—' if x['diff'] is None else f'{x['diff']:.1e}'} | " + (f"{g(x.get('rsi_sma'), k)} | {g(x.get('rsi_ema'), k)} | " if k == "rsi" else "") + f"{x['note']} |")
    txt = "\n".join(out) + "\n"
    print(txt)
    if a.md:
        Path(a.md).write_text(txt, encoding="utf-8")
        print(f"Отчёт записан: {a.md}")


if __name__ == "__main__":
    main()
