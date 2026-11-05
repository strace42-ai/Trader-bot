"""Веб-терминал FABLES Trading Lab (FastAPI) — клиент системы. Слушает только 127.0.0.1.

- BACKTEST: офлайн-бэктест на записанном наборе данных (пересчитывается при запуске, сверяется с журналом в БД).
- FORWARD: живой эксперимент. Состояние, сделки, решения и рыночные данные читаются из trading_lab.db, куда их пишет
  фоновый бот (tradinglab.live.worker). Веб ничего не исполняет сам: команды ставятся в очередь и исполняются ботом.
- Защита: запросы принимаются только с Host 127.0.0.1/localhost (защита от DNS rebinding); управляющие POST требуют
  заголовок X-Lab-Token (токен установки в data/control_token) и Origin этого же адреса (защита от CSRF).
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import LAB_VERSION, backtest, dataset as ds_mod
from ..analysis import analyze
from ..candles import GRACE_SEC, TF_SEC
from ..forward import ControlError, ForwardRunner, executor_from_db
from ..live import config as LC
from ..live.forming import live_bars, live_values
from ..live.health import preflight
from ..live.marketdb import MarketDB
from ..store import Store
from ..strategies import ANALYSIS_PARAMS
from . import views

STATIC = Path(__file__).resolve().parent / "static"
TFS = ("1h", "4h", "1d")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
COMMANDS = {"start": "START", "pause": "PAUSE", "resume": "RESUME", "stop": "STOP", "close": "CLOSE_POSITION",
            "emergency": "EMERGENCY_EXIT"}


def load_token(data_dir: Path) -> str:
    p = data_dir / "control_token"
    if p.exists():
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    t = secrets.token_urlsafe(32)
    p.write_text(t, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return t


class Lab:
    def __init__(self, data_dir: Path, dataset_path: Path, worker=None, clock=time.time):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock, self.worker = clock, worker
        self.store = worker.store if worker else Store(self.data_dir / "trading_lab.db")
        self.db = MarketDB(self.store)
        self.token = load_token(self.data_dir)
        self.ds = ds_mod.load(dataset_path)
        self.bt = backtest.run(self.ds["series"], quote=self.ds["quote"])
        self.persist = self._persist_backtest()
        self.an = self.bt["analyses"]
        self._live_cache: dict = {}

    def _persist_backtest(self) -> dict:
        exp = self.bt["experiment"]
        if self.store.experiment(exp) is None:
            from ..risk import params_hash
            self.store.create_experiment(exp, "BACKTEST", self.bt["start_ts"], self.bt["params"], params_hash(self.bt["params"]),
                                         f"backtest на наборе {self.ds['id']}")
            for t in self.bt["traders"].values():
                self.store.commit_bar(t)
            return {"experiment": exp, "status": "SAVED", "detail": "журнал бэктеста сохранён в trading_lab.db"}
        mism = [tf for tf, t in self.bt["traders"].items()
                if [f["op_id"] for f in self.store.fills(exp, tf)] != [o["op_id"] for o in t.ops]
                or len(self.store.decisions(exp, tf, 10 ** 6)) != len(t.decisions)]
        return {"experiment": exp, "status": "MISMATCH" if mism else "VERIFIED", "mismatch": mism,
                "detail": "пересчёт совпал с сохранённым журналом" if not mism else "пересчёт НЕ совпал с журналом: " + ", ".join(mism)}

    # ------------------------------------------------------------------ живые данные
    def now(self) -> int:
        return int(self.clock())

    def runner(self) -> ForwardRunner:
        """Свежий read-only срез FORWARD из базы (источник истины — БД, которую пишет бот)."""
        r = ForwardRunner(self.store, clock=self.clock)
        ex = executor_from_db(self.db, r.cost, self.now())
        for t in r.traders.values():
            t.hydrate(self.store)
            t.ex = ex
        return r

    def worker_status(self) -> dict:
        w, _ = self.db.kv("worker")
        now = self.now()
        alive = bool(w) and w.get("status") == "running" and now - w["ts"] <= LC.WORKER_STALE_SEC
        return {"alive": alive, "age": now - w["ts"] if w else None, **(w or {})}

    def live_market(self) -> dict:
        now = self.now()
        pool, ds = self.db.last_quote("pool"), self.db.last_quote("ds")
        lv, _ = self.db.kv("last_valid_quote")
        risk, _ = self.db.kv("risk", {})
        health, _ = self.db.kv("health", {})
        gas, _ = self.db.kv("gas", {})
        ctx = self.db.last_context("prices")
        age = lambda q: now - q["ts"] if q else None  # noqa: E731
        return {
            "now": now,
            "quote": lv, "quote_age": age(lv), "quote_fresh": bool(lv) and now - lv["ts"] <= LC.QUOTE_MAX_AGE,
            "pool": {k: pool.get(k) for k in ("ts", "mid", "block", "tick", "liquidity")} if pool else None, "pool_age": age(pool),
            "ds": {k: ds.get(k) for k in ("ts", "mid", "price_usd", "liquidity_usd", "fdv", "market_cap", "volume", "txns", "price_change")} if ds else None,
            "ds_age": age(ds), "risk": risk, "fees": self.db.recent_fees(), "gas": gas,
            "context": (ctx or {}).get("data"), "context_ts": (ctx or {}).get("ts"),
            "health": {k: {**v, "age": now - v["last_ok"] if v.get("last_ok") else None} for k, v in (health or {}).items()},
            "candles": {tf: {"count": self.db.count(tf), "last_closed": self.db.last_closed_ts(tf), "forming": self.db.forming(tf),
                             "next_close": views.next_close(tf, now), "next_available": views.next_close(tf, now) + GRACE_SEC}
                        for tf in TFS},
            "events": self.db.events(30), "worker": self.worker_status(),
            "integrity": self.db.kv("integrity", {})[0], "preflight": self.db.kv("preflight", None)[0],
            "last_decisions": self.last_decisions(),
        }

    def last_decisions(self) -> dict:
        last = self.store.last_control()
        exp = last["experiment_id"] if last else None
        out = {}
        for tf in TFS:
            d = self.store.decisions(exp, tf, 1) if exp else []
            out[tf] = {"decided_ts": d[0].get("decided_ts"), "bar_ts": d[0]["bar_ts"], "action": d[0]["action_final"]} if d else None
        return out

    def live_block(self, tf: str, a=None) -> dict:
        """Формирующаяся (и, если GT ещё не отдал закрытую, предварительная) свеча + значения индикаторов:
        на последней ЗАКРЫТОЙ свече (как у стратегий) и LIVE на формирующейся (только отображение)."""
        now = self.now()
        a = a if a is not None else self.live_analysis(tf)
        bars = live_bars(self.db, tf, now)
        vals = live_values(a.s, bars, a.p) if a is not None else {"closed": None, "live": None}
        lv, _ = self.db.kv("last_valid_quote")
        return {"tf": tf, "now": now, "bars": bars, "values": vals, "quote": lv, "next_close": views.next_close(tf, now),
                "note": "формирующаяся свеча и LIVE-значения — только отображение; решения стратегий — по закрытым свечам"}

    def audit(self, tf: str, mode: str) -> dict:
        from .. import audit as AU
        if mode == "BACKTEST":
            a = self.an[tf]
            res = AU.audit(a)
            res["source"] = f"записанный набор {self.ds['id']}"
            return res
        a = self.live_analysis(tf)
        if a is None:
            return {"tf": tf, "error": "живых свечей ещё нет — бот не загрузил историю"}
        rev = {r[0]: r[1] for r in self.store._all("SELECT ts, COUNT(*) FROM candle_revisions WHERE tf=? GROUP BY ts", (tf,))}
        res = AU.audit(a, live=self.live_block(tf, a)["values"], revisions=rev)
        res["source"] = "живые свечи GeckoTerminal из trading_lab.db (как их видят стратегии)"
        res["revisions_total"] = sum(rev.values())
        return res

    def live_analysis(self, tf: str):
        now = self.now()
        last = self.db.last_closed_ts(tf)
        key = (self.db.count(tf), last)
        hit = self._live_cache.get(tf)
        if hit and hit[0] == key:
            return hit[1]
        s, _ = self.db.candles(tf, now)
        a = analyze(s, ANALYSIS_PARAMS[tf]) if len(s) else None
        self._live_cache[tf] = (key, a)
        return a

    # ------------------------------------------------------------------ payloads
    def overview(self) -> dict:
        snap, val, sm = self.ds["snapshot"], self.bt["valuation"], self.bt["summary"]
        last1h = self.ds["series"]["1h"].candles[-1]
        initial_total = sum(float(t.portfolio.initial) for t in self.bt["traders"].values())
        traders = {tf: {"equity": val["traders"][tf]["equity"], "net_pnl": sm[tf]["net_pnl"], "net_return_pct": sm[tf]["net_return_pct"],
                        "closed_trades": sm[tf]["closed_trades"], "max_drawdown_pct": sm[tf]["max_drawdown_pct"],
                        "state": self.bt["traders"][tf].state, "strategy": sm[tf]["strategy"]} for tf in TFS}
        best = views.best_strategy(traders)
        r = self.runner()
        fw = r.info()
        live = self.live_market()
        marks = self.db.marks(r.experiment_id) if r.experiment_id else []
        fwt = views.forward_totals(r, marks, live) if r.experiment_id else None
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
            "forward": {k: fw[k] for k in ("status", "experiment", "started_at", "allowed")} | {"totals": fwt},
            "live": live,
            "sources": views.sources(live),
            "engine": {"backtest": "DONE", "forward": fw["status"], "worker": live["worker"]},
        }

    def terminal(self, tf: str, mode: str) -> dict:
        if mode == "BACKTEST":
            a = self.an[tf]
            base = {"tf": tf, "mode": mode, **views.candles_payload(a)}
            t = self.bt["traders"][tf]
            base["trader"] = views.trader_payload(t, a, self.bt["mark"], self.bt["bh"],
                                                  {"dataset": self.ds["id"], "experiment": self.bt["experiment"], "persist": self.persist,
                                                   "start_ts": self.bt["start_ts"]})
            return base
        now = self.now()
        a = self.live_analysis(tf)
        base = {"tf": tf, "mode": mode, "live": True, **(views.candles_payload(a) if a else {"candles": [], "series": {}})}
        base["live"] = self.live_block(tf, a) if a is not None else {"bars": [], "values": {"closed": None, "live": None}}
        base["next_close"] = views.next_close(tf, now)
        base["next_available"] = base["next_close"] + GRACE_SEC
        r = self.runner()
        fw = r.info()
        base["forward"] = {k: fw[k] for k in ("status", "experiment", "started_at", "allowed")}
        base["worker"] = self.worker_status()
        lv, _ = self.db.kv("last_valid_quote")
        base["quote"] = lv
        t = r.traders.get(tf)
        if fw["status"] == "NOT_STARTED" or t is None or a is None:
            base["trader"] = None
            base["preview"] = views.analysis_only(a) if a else None
            if base["preview"]:
                views.add_live_column(base["preview"]["indicators"], base["live"]["values"].get("live"))
            return base
        mark = {"ts": lv["ts"], "mid": lv["mid"], "source": lv["source"]} if lv and (not t.equity_curve or lv["ts"] >= t.equity_curve[-1]["ts"]) else None
        bh, _ = self.db.kv(f"bh:{r.experiment_id}")
        base["trader"] = views.trader_payload(t, a, mark, None, {"experiment": r.experiment_id, "dataset": "LIVE",
                                                                 "exec_model": t.ex.name if getattr(t.ex, "pool", None) else "conservative-fallback"})
        if bh and mark:
            from ..execution import dec
            b = float(t.ex.liquidation(dec(bh["qty"]), mark["mid"]) + dec(bh["cash"]))
            sm = base["trader"]["summary"]
            sm["bh_equity_same_time"], sm["vs_bh_usd"] = b, sm["equity"] - b
            sm["vs_bh_ratio"] = sm["equity"] / b if b else None
        base["trader"]["rejections"] = views.rejection_stats(t.decisions)
        views.add_live_column(base["trader"]["analysis"]["indicators"], base["live"]["values"].get("live"))
        risk, _ = self.db.kv("risk", {})
        base["trader"]["monitor"] = {"last_decision_ts": t.decisions[-1].get("decided_ts") if t.decisions else None,
                                     **((risk or {}).get("traders") or {}).get(tf, {})}
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
        r = self.runner()
        fw = r.info()
        fwd = {"status": fw["status"], "experiment": fw["experiment"], "started_at": fw["started_at"]}
        if r.experiment_id:
            fwd.update(views.forward_performance(r, self.db.marks(r.experiment_id)))
        return {"backtest": {"experiment": bt["experiment"], "start_ts": start, "mark": mark, "curves": curves, "bh": bh,
                             "cash": [{"time": start, "value": 1000.0}, {"time": mark["ts"], "value": 1000.0}],
                             "summary": bt["summary"], "valuation": bt["valuation"], "validation": "NOT VALIDATED"},
                "forward": fwd}

    # ------------------------------------------------------------------ команды
    def command(self, command: str, args: dict) -> dict:
        now = self.now()
        if self.worker is not None:                  # бот в этом процессе (serve): та же очередь, исполнение сразу
            cid = self.db.enqueue(now, command, args)
            res = self.worker.execute(cid, command, args, now)
        else:                                        # отдельный бот: команда в очередь, ждём исполнения
            if not self.worker_status()["alive"]:
                raise HTTPException(503, {"error": "фоновый бот не запущен — команда не поставлена в очередь"})
            cid = self.db.enqueue(now, command, args)
            res = None
            for _ in range(100):
                c = self.db.command(cid)
                if c["status"] != "PENDING":
                    import json
                    res = {"id": cid, "status": c["status"], **(json.loads(c["result"]) if c["result"] else {})}
                    break
                time.sleep(0.1)
            if res is None:
                raise HTTPException(504, {"error": "бот не ответил за 10 с; команда истечёт и не будет исполнена"})
        if res["status"] != "DONE":
            raise HTTPException(409, {"error": res.get("error") or res["status"]})
        return self.runner().info()


def create_app(data_dir: Path | str = "data", dataset_path: Path | str = ds_mod.DEFAULT_DATASET, worker=None, clock=time.time) -> FastAPI:
    lab = Lab(Path(data_dir), Path(dataset_path), worker=worker, clock=clock)
    app = FastAPI(title="FABLES Trading Lab", version=LAB_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.lab = lab

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "разрешён только локальный доступ (127.0.0.1)"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and (urlparse(origin).hostname or "") not in ALLOWED_HOSTS:
                return JSONResponse({"detail": "запрос с чужого сайта отклонён"}, status_code=403)
            if not secrets.compare_digest(request.headers.get("x-lab-token") or "", lab.token):
                return JSONResponse({"detail": "нет токена управления"}, status_code=403)
        return await call_next(request)

    def _tf(tf: str) -> str:
        if tf not in TFS:
            raise HTTPException(404, f"неизвестный таймфрейм {tf}")
        return tf

    def _page(name: str) -> HTMLResponse:
        html = (STATIC / name).read_text(encoding="utf-8").replace("__LAB_TOKEN__", lab.token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Content-Security-Policy": "frame-ancestors 'none'"})

    @app.get("/")
    def index():
        return _page("index.html")

    @app.get("/performance")
    def perf_page():
        return _page("performance.html")

    import logging
    log = logging.getLogger("tradinglab.web")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        import traceback
        log.error("API %s %s failed: %s\n%s", request.method, request.url.path, exc, traceback.format_exc())
        return JSONResponse({"detail": f"внутренняя ошибка: {type(exc).__name__}: {exc} (подробности — data/logs/tradinglab.log)"}, status_code=500)

    @app.get("/api/preflight")
    def preflight_api():
        pf = preflight(lab.store, lab.now())
        w = lab.worker_status()
        pf["checks"].insert(0, {"name": "Фоновый бот", "ok": w["alive"], "detail": f"heartbeat {w['age']} с назад" if w.get("age") is not None
                                else "не запущен", "level": "required"})
        pf["ok"] = pf["ok"] and w["alive"]
        return pf

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": LAB_VERSION, "worker": lab.worker_status()}

    @app.get("/api/overview")
    def overview():
        return lab.overview()

    @app.get("/api/terminal/{tf}")
    def terminal(tf: str, mode: str = "BACKTEST"):
        mode = mode.upper()
        if mode not in ("BACKTEST", "FORWARD"):
            raise HTTPException(400, "mode: BACKTEST или FORWARD")
        return lab.terminal(_tf(tf), mode)

    @app.get("/api/live/{tf}")
    def live(tf: str):
        return lab.live_block(_tf(tf))

    @app.get("/api/audit/{tf}")
    def audit(tf: str, mode: str = "FORWARD"):
        mode = mode.upper()
        if mode not in ("BACKTEST", "FORWARD"):
            raise HTTPException(400, "mode: BACKTEST или FORWARD")
        return lab.audit(_tf(tf), mode)

    @app.get("/api/performance")
    def performance():
        return lab.performance()

    @app.get("/api/forward")
    def forward_info():
        return lab.runner().info()

    @app.post("/api/forward/{cmd}")
    def forward_cmd(cmd: str, tf: str | None = None):
        command = COMMANDS.get(cmd)
        if command is None:
            raise HTTPException(404, "команда: start | pause | resume | stop | close?tf=1h | emergency")
        try:
            return lab.command(command, {"tf": tf} if tf else {})
        except ControlError as e:
            raise HTTPException(409, {"error": str(e), "status": e.status})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


__all__ = ["create_app", "Lab", "TF_SEC", "FileResponse"]
