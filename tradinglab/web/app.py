"""Веб-терминал FABLES Trading Lab (FastAPI). Только локально, только чтение рынка из записанного набора данных.

- BACKTEST: результат офлайн-бэктеста на закрытых свечах набора данных (пересчитывается при запуске детерминированно;
  при первом запуске журнал сохраняется в trading_lab.db, при последующих — сверяется с ним).
- FORWARD: отдельный эксперимент; пока не запущен — NOT_STARTED. Источник живых данных не подключён — сервер не делает
  сетевых запросов и не запускает фоновых процессов.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import LAB_VERSION, backtest, dataset as ds_mod
from ..analysis import analyze
from ..forward import ControlError, ForwardController
from ..store import Store
from ..strategies import ANALYSIS_PARAMS
from . import views

STATIC = Path(__file__).resolve().parent / "static"
TFS = ("1h", "4h", "1d")


class Lab:
    def __init__(self, data_dir: Path, dataset_path: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "trading_lab.db")
        self.ds = ds_mod.load(dataset_path)
        self.bt = backtest.run(self.ds["series"], quote=self.ds["quote"])
        self.persist = self._persist_backtest()
        self.an = self.bt["analyses"]
        self.forward = ForwardController(self.store)

    def _persist_backtest(self) -> dict:
        """Первый запуск — сохранить журнал бэктеста; далее — сверить пересчёт с сохранённым журналом."""
        exp = self.bt["experiment"]
        if self.store.experiment(exp) is None:
            from ..risk import params_hash
            self.store.create_experiment(exp, "BACKTEST", self.bt["start_ts"], self.bt["params"], params_hash(self.bt["params"]),
                                         f"backtest на наборе {self.ds['id']}")
            for t in self.bt["traders"].values():
                self.store.commit_bar(t)
            return {"experiment": exp, "status": "SAVED", "detail": "журнал бэктеста сохранён в trading_lab.db"}
        mism = []
        for tf, t in self.bt["traders"].items():
            db_ops = [f["op_id"] for f in self.store.fills(exp, tf)]
            if db_ops != [o["op_id"] for o in t.ops] or len(self.store.decisions(exp, tf, 10 ** 6)) != len(t.decisions):
                mism.append(tf)
        return {"experiment": exp, "status": "MISMATCH" if mism else "VERIFIED", "mismatch": mism,
                "detail": "пересчёт совпал с сохранённым журналом" if not mism else "пересчёт НЕ совпал с журналом: " + ", ".join(mism)}

    # ------------------------------------------------------------------ payloads
    def overview(self) -> dict:
        snap, val, sm = self.ds["snapshot"], self.bt["valuation"], self.bt["summary"]
        last1h = self.ds["series"]["1h"].candles[-1]
        initial_total = sum(float(t.portfolio.initial) for t in self.bt["traders"].values())
        traders = {tf: {"equity": val["traders"][tf]["equity"], "net_pnl": sm[tf]["net_pnl"], "net_return_pct": sm[tf]["net_return_pct"],
                        "closed_trades": sm[tf]["closed_trades"], "max_drawdown_pct": sm[tf]["max_drawdown_pct"],
                        "state": self.bt["traders"][tf].state, "strategy": sm[tf]["strategy"]} for tf in TFS}
        if all(v["closed_trades"] == 0 for v in traders.values()):
            best = {"by_return": None, "by_risk": None, "note": "сделок нет ни у одной стратегии — сравнивать нечего"}
        else:
            by_ret = max(traders, key=lambda k: traders[k]["net_return_pct"])
            by_risk = max(traders, key=lambda k: traders[k]["net_return_pct"] / max(traders[k]["max_drawdown_pct"], 1.0))
            best = {"by_return": by_ret, "by_risk": by_risk, "note": "доходность / max(просадка, 1%)"}
        fw = self.forward.info()
        return {
            "version": LAB_VERSION,
            "dataset": {"id": self.ds["id"], "recorded_at": self.ds["recorded_at"], "source": self.ds["manifest"]["candles_source"]},
            "price": {"usd": snap["price_usd"] if snap else None, "usdg": snap["price_usdg"] if snap else None,
                      "ts": snap["ts"] if snap else None, "source": snap["source"] if snap else None,
                      "last_candle_close": last1h.c, "last_candle_close_ts": last1h.ts + 3600},
            "market": {k: snap.get(k) for k in ("liquidity_usd", "fdv", "market_cap", "volume", "txns", "price_change")} if snap else None,
            "mark": self.bt["mark"],
            "backtest": {"experiment": self.bt["experiment"], "start_ts": self.bt["start_ts"], "initial_total": initial_total,
                         "total_equity": val["total"], "net_pnl": val["total"] - initial_total,
                         "net_return_pct": (val["total"] / initial_total - 1) * 100,
                         "bh_total": val["bh_total"], "vs_bh_usd": val["total"] - val["bh_total"] if val["bh_total"] else None,
                         "bh_return_pct": (val["bh_total"] / initial_total - 1) * 100 if val["bh_total"] else None,
                         "traders": traders, "best": best, "validation": "NOT VALIDATED", "persist": self.persist,
                         "cost_model": self.bt["params"]["cost_model"]["name"]},
            "forward": {k: fw[k] for k in ("status", "experiment", "started_at", "allowed", "feed")},
            "sources": [
                {"name": "GeckoTerminal OHLCV", "status": "RECORDED", "ts": self.ds["recorded_at"], "note": "записанный набор, сеть не опрашивается"},
                {"name": "DexScreener snapshot", "status": "RECORDED" if snap else "NONE", "ts": snap["ts"] if snap else None,
                 "note": "срез цены/ликвидности на момент записи"},
                {"name": "Живой поток данных", "status": "NOT CONNECTED", "ts": None, "note": fw["feed"]["reason"]},
            ],
            "engine": {"backtest": "DONE", "forward": fw["status"], "background_process": "НЕТ (сервер не опрашивает сеть)"},
        }

    def terminal(self, tf: str, mode: str) -> dict:
        a = self.an[tf]
        base = {"tf": tf, "mode": mode, **views.candles_payload(a)}
        if mode == "BACKTEST":
            t = self.bt["traders"][tf]
            base["trader"] = views.trader_payload(t, a, self.bt["mark"], self.bt["bh"],
                                                  {"dataset": self.ds["id"], "experiment": self.bt["experiment"], "persist": self.persist,
                                                   "start_ts": self.bt["start_ts"]})
            return base
        fw = self.forward.info()
        base["forward"] = {k: fw[k] for k in ("status", "experiment", "started_at", "allowed", "feed")}
        t = self.forward.traders.get(tf)
        base["trader"] = None if fw["status"] == "NOT_STARTED" or t is None else \
            views.trader_payload(t, a, self.bt["mark"] if self.bt["mark"]["ts"] >= (fw["started_at"] or 0) else None, None,
                                 {"dataset": self.ds["id"], "experiment": fw["experiment"], "feed": fw["feed"]})
        if base["trader"] is not None and fw["started_at"] and self.bt["mark"]["ts"] < fw["started_at"]:
            base["trader"]["summary"]["mark_note"] = "после START новых котировок нет — оценка по начальному капиталу"
        return base

    def performance(self) -> dict:
        bt, mark = self.bt, self.bt["mark"]
        curves = {}
        for tf, t in bt["traders"].items():
            pts = [{"time": e["ts"], "value": float(e["equity"])} for e in t.equity_curve]
            pts.append({"time": mark["ts"], "value": bt["valuation"]["traders"][tf]["equity"]})
            curves[tf] = pts
        bh = [{"time": b["ts"], "value": float(b["equity"])} for b in bt["bh"]]
        bh.append({"time": mark["ts"], "value": bt["valuation"]["bh_per_trader"]})
        start = bt["start_ts"]
        fw = self.forward.info()
        fcurves = {tf: [{"time": e["ts"], "value": float(e["equity"])} for e in t.equity_curve] for tf, t in self.forward.traders.items()}
        return {"backtest": {"experiment": bt["experiment"], "start_ts": start, "mark": mark, "curves": curves, "bh": bh,
                             "cash": [{"time": start, "value": 1000.0}, {"time": mark["ts"], "value": 1000.0}],
                             "summary": bt["summary"], "valuation": bt["valuation"], "validation": "NOT VALIDATED"},
                "forward": {"status": fw["status"], "experiment": fw["experiment"], "started_at": fw["started_at"], "curves": fcurves,
                            "feed": fw["feed"]}}


def create_app(data_dir: Path | str = "data", dataset_path: Path | str = ds_mod.DEFAULT_DATASET) -> FastAPI:
    lab = Lab(Path(data_dir), Path(dataset_path))
    app = FastAPI(title="FABLES Trading Lab", version=LAB_VERSION)
    app.state.lab = lab

    def _tf(tf: str) -> str:
        if tf not in TFS:
            raise HTTPException(404, f"неизвестный таймфрейм {tf}")
        return tf

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/performance")
    def perf_page():
        return FileResponse(STATIC / "performance.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": LAB_VERSION, "forward": lab.forward.status}

    @app.get("/api/overview")
    def overview():
        return lab.overview()

    @app.get("/api/terminal/{tf}")
    def terminal(tf: str, mode: str = "BACKTEST"):
        mode = mode.upper()
        if mode not in ("BACKTEST", "FORWARD"):
            raise HTTPException(400, "mode: BACKTEST или FORWARD")
        return lab.terminal(_tf(tf), mode)

    @app.get("/api/performance")
    def performance():
        return lab.performance()

    @app.get("/api/forward")
    def forward_info():
        return lab.forward.info()

    @app.post("/api/forward/{cmd}")
    def forward_cmd(cmd: str):
        fn = {"start": lab.forward.start, "pause": lab.forward.pause, "resume": lab.forward.resume, "stop": lab.forward.stop}.get(cmd)
        if fn is None:
            raise HTTPException(404, "команда: start | pause | resume | stop")
        try:
            return fn()
        except ControlError as e:
            raise HTTPException(409, {"error": str(e), "status": e.status})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


__all__ = ["create_app", "Lab", "ANALYSIS_PARAMS", "analyze"]
