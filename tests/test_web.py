"""Веб-терминал: безопасность управляющих API, связь с ядром и живыми данными бота, команды через очередь."""
import socket
import threading

import pytest
from fastapi.testclient import TestClient

from replay import Clock, Market, run, ts
from tradinglab import backtest, dataset
from tradinglab.live.worker import Worker
from tradinglab.store import Store
from tradinglab.web.app import create_app

BASE = "http://127.0.0.1:8770"


@pytest.fixture()
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("сетевой запрос из веб-интерфейса запрещён (сеть опрашивает только бот)")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def client(app):
    return TestClient(app, base_url=BASE)


def hdr(app):
    return {"X-Lab-Token": app.state.lab.token}


@pytest.fixture()
def live(tmp_path, no_network):
    """Бот в том же процессе (как `serve`) на воспроизведении записанного потока."""
    clock = Clock(ts("2026-11-04T20:05:00Z"))
    m = Market(clock)
    st = Store(tmp_path / "trading_lab.db", durable=False)
    w = Worker(st, m.transport(), clock)
    w.tick(clock.t)
    app = create_app(tmp_path, worker=w, clock=clock)
    return clock, m, w, app


# ------------------------------------------------------------------ безопасность
def test_only_localhost_and_token_protected_controls(tmp_path, no_network):
    app = create_app(tmp_path)
    assert TestClient(app, base_url="http://evil.example").get("/api/overview").status_code == 403   # DNS rebinding
    c = client(app)
    assert c.get("/api/overview").status_code == 200
    assert c.post("/api/forward/start").status_code == 403                                           # без токена
    assert c.post("/api/forward/start", headers={"X-Lab-Token": "x" * 43}).status_code == 403
    assert c.post("/api/forward/start", headers={**hdr(app), "Origin": "https://evil.example"}).status_code == 403
    page = c.get("/").text
    assert app.state.lab.token in page and "__LAB_TOKEN__" not in page
    assert (tmp_path / "control_token").read_text() == app.state.lab.token


def test_web_only_without_bot_refuses_commands(tmp_path, no_network):
    app = create_app(tmp_path)
    r = client(app).post("/api/forward/start", headers=hdr(app))
    assert r.status_code == 503 and "бот не запущен" in r.text
    assert app.state.lab.db.pending_commands() == []                  # команда не поставлена в очередь


def test_web_only_with_separate_bot_process_uses_queue(tmp_path, no_network):
    clock = Clock(ts("2026-11-04T20:05:00Z"))
    m = Market(clock)
    w = Worker(Store(tmp_path / "trading_lab.db", durable=False), m.transport(), clock)
    w.tick(clock.t)
    app = create_app(tmp_path, clock=clock)                           # веб без бота в процессе
    stop = threading.Event()

    def bot():                                                         # «отдельный процесс» бота: исполняет очередь
        while not stop.wait(0.02):
            w.process_commands(int(clock.t))
            w.heartbeat(int(clock.t))
    th = threading.Thread(target=bot, daemon=True)
    th.start()
    try:
        r = client(app).post("/api/forward/start", headers=hdr(app))
        assert r.status_code == 200 and r.json()["status"] == "RUNNING"
    finally:
        stop.set()
        th.join(2)


# ------------------------------------------------------------------ связь с ядром и живыми данными
def test_pages_and_backtest_still_match_core(tmp_path, no_network):
    app = create_app(tmp_path)
    c = client(app)
    assert "<title>FABLES Trading Lab</title>" in c.get("/").text and c.get("/performance").status_code == 200
    assert "Lightweight Charts" in c.get("/static/vendor/lightweight-charts.standalone.production.js").text[:300]
    ds = dataset.load()
    ref = backtest.run(ds["series"], quote=ds["quote"])
    o = c.get("/api/overview").json()
    assert o["backtest"]["total_equity"] == pytest.approx(ref["valuation"]["total"])
    d = c.get("/api/terminal/4h?mode=BACKTEST").json()
    assert d["trader"]["decision"]["explanation"] == ref["traders"]["4h"].decisions[-1]["explanation"]
    assert all({"value", "req", "result"} <= set(x) for x in d["trader"]["decision"]["conditions"])


def test_live_overview_and_terminal_show_actual_bot_data(live):
    clock, m, w, app = live
    c = client(app)
    o = c.get("/api/overview").json()
    assert o["forward"]["status"] == "NOT_STARTED" and o["live"]["worker"]["alive"]
    assert o["live"]["quote"]["mid"] == pytest.approx(w.collector.db.last_quote("pool")["mid"])
    assert {s["key"]: s["status"] for s in o["sources"]}["rpc"] == "OK"
    d = c.get("/api/terminal/1h?mode=FORWARD").json()
    assert d["trader"] is None and d["preview"] and len(d["candles"]) == w.collector.db.count("1h")
    assert d["forming"]["ts"] > d["candles"][-1]["time"] and d["next_close"] > clock.t
    assert c.post("/api/forward/start", headers=hdr(app)).json()["status"] == "RUNNING"
    run(w, clock, ts("2026-11-04T23:10:00Z"))
    d = c.get("/api/terminal/1h?mode=FORWARD").json()
    tr = d["trader"]
    db_dec = w.store.decisions(w.runner.experiment_id, "1h", 10)
    assert tr["mode"] == "FORWARD" and tr["decision"]["bar_ts"] == db_dec[0]["bar_ts"]
    assert tr["rejections"]["decisions"] == len(db_dec) and tr["rejections"]["any_fail"]
    assert tr["summary"]["mark"]["mid"] == pytest.approx(w.db.kv("last_valid_quote")[0]["mid"])
    p = c.get("/api/performance").json()["forward"]
    assert p["status"] == "RUNNING" and len(p["curves"]["1h"]) == len(p["bh"]) == p["marks"] > 10
    assert p["table"]["1h"]["equity"] == pytest.approx(1000.0) and p["table"]["1h"]["vs_bh_ratio"] is not None


def test_all_controls_through_api(live):
    clock, m, w, app = live
    c = client(app)
    post = lambda cmd: c.post(f"/api/forward/{cmd}", headers=hdr(app))  # noqa: E731
    assert post("pause").status_code == 409
    assert post("start").json()["status"] == "RUNNING"
    assert post("pause").json()["status"] == "PAUSED"
    assert post("resume").json()["status"] == "RUNNING"
    assert c.post("/api/forward/close?tf=1h", headers=hdr(app)).status_code == 409      # позиции нет
    assert post("emergency").status_code == 409
    assert post("stop").json()["status"] == "STOPPED"
    assert post("start").json()["status"] == "RUNNING"                                   # новый эксперимент
    assert [x["command"] for x in w.store.controls()][::-1] == ["START", "PAUSE", "RESUME", "STOP", "START"]
    assert c.post("/api/forward/unknown", headers=hdr(app)).status_code == 404


def test_concurrent_requests_do_not_break_store(live):
    from concurrent.futures import ThreadPoolExecutor
    clock, m, w, app = live
    c = client(app)
    c.post("/api/forward/start", headers=hdr(app))
    paths = ["/api/overview", "/api/forward", "/api/terminal/1h?mode=FORWARD", "/api/performance", "/api/terminal/4h"] * 6
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda p: c.get(p).status_code, paths))
        codes += list(ex.map(lambda cmd: c.post(f"/api/forward/{cmd}", headers=hdr(app)).status_code, ["pause", "resume"] * 4))
    assert all(x in (200, 409) for x in codes) and codes.count(200) >= len(paths)


def test_bad_params(tmp_path, no_network):
    c = client(create_app(tmp_path))
    assert c.get("/api/terminal/5m").status_code == 404
    assert c.get("/api/terminal/1h?mode=LIVE").status_code == 400


def test_chart_series_are_exactly_core_arrays_and_hist_is_macd_minus_signal(tmp_path, no_network):
    """Панель получает ряды индикаторов прямо из аналитического ядра (включая RSI(14)); фронтенд ничего не пересчитывает."""
    from tradinglab import analysis, dataset
    from tradinglab.strategies import ANALYSIS_PARAMS
    c = client(create_app(tmp_path))
    ds = dataset.load()
    for tf in ("1h", "4h", "1d"):
        d = c.get(f"/api/terminal/{tf}?mode=BACKTEST").json()
        a = analysis.analyze(ds["series"][tf], ANALYSIS_PARAMS[tf])
        ts = [k.ts for k in a.s.candles]
        for key in ("rsi", "macd", "macd_signal", "macd_hist", "mfi", "cmf", "obv", "ema_fast", "ema_slow", "bb_upper", "bb_lower"):
            core = [{"time": t, "value": v} for t, v in zip(ts, a.arr[key]) if v is not None]
            assert d["series"][key] == core, (tf, key)
        m = {p["time"]: p["value"] for p in d["series"]["macd"]}
        sg = {p["time"]: p["value"] for p in d["series"]["macd_signal"]}
        for p in d["series"]["macd_hist"]:
            assert p["value"] == m[p["time"]] - sg[p["time"]]
        assert a.p["rsi"] == 14 and len(d["series"]["rsi"]) == max(0, len(ts) - 14)    # RSI(14) с 15-й свечи
