"""CLI Trading Lab. Сетевых запросов к рынку и фоновых процессов нет.

  python -m tradinglab serve [--port 8770] [--data data] [--dataset datasets/fables_2026-11-05] [--no-browser]
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
    sv = sub.add_parser("serve", help="локальная панель FABLES Trading Lab (http://127.0.0.1:PORT/)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8770)
    sv.add_argument("--data", default="data", help="папка для trading_lab.db")
    sv.add_argument("--dataset", default=None, help="папка набора данных (manifest.json)")
    sv.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.cmd == "serve":
        serve(a)
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


def serve(a) -> None:
    import threading
    import webbrowser

    import uvicorn

    from .dataset import DEFAULT_DATASET
    from .web.app import create_app
    data = Path(a.data)
    data.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(data / "server.lock")
    if not lock.acquire():
        print(f"\n  FABLES Trading Lab уже запущен для папки {data.resolve()}. Второй сервер не запускается.\n")
        return
    app = create_app(data, Path(a.dataset) if a.dataset else DEFAULT_DATASET)
    url = f"http://{a.host}:{a.port}/"
    print(f"\n  FABLES Trading Lab: {url}\n  Данные: {data.resolve()}\n  Режим: PAPER (виртуальная торговля). Сеть не опрашивается."
          f"\n  Остановить сервер: Ctrl+C или закрыть это окно. Состояние сохраняется в trading_lab.db.\n")
    if not a.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
