"""Веб-терминал: страницы, API связан с ядром (без выдуманных данных), FORWARD-управление, отсутствие сетевых запросов."""
import socket

import pytest
from fastapi.testclient import TestClient

from tradinglab import backtest, dataset
from tradinglab.web.app import create_app


@pytest.fixture()
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("сетевой запрос из сервера панели запрещён")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@pytest.fixture()
def client(tmp_path, no_network):
    return TestClient(create_app(tmp_path))


def test_pages_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>FABLES Trading Lab</title>" in r.text
    assert client.get("/performance").status_code == 200
    v = client.get("/static/vendor/lightweight-charts.standalone.production.js")
    assert v.status_code == 200 and "Lightweight Charts" in v.text[:300]
    assert client.get("/static/app.js").status_code == 200


def test_overview_matches_core_backtest(client):
    o = client.get("/api/overview").json()
    ds = dataset.load()
    ref = backtest.run(ds["series"], quote=ds["quote"])
    bt = o["backtest"]
    assert bt["experiment"] == ref["experiment"] and bt["initial_total"] == 3000
    assert bt["total_equity"] == pytest.approx(ref["valuation"]["total"])
    assert bt["total_equity"] == pytest.approx(sum(v["equity"] for v in bt["traders"].values()))
    assert bt["bh_total"] == pytest.approx(ref["valuation"]["bh_total"])
    assert o["mark"] == ref["mark"] and o["price"]["usdg"] == ds["snapshot"]["price_usdg"]
    assert bt["validation"] == "NOT VALIDATED" and o["forward"]["status"] == "NOT_STARTED"
    assert any(s["status"] == "NOT CONNECTED" for s in o["sources"])


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_terminal_backtest_payload_is_core_data(client, tf):
    d = client.get(f"/api/terminal/{tf}").json()
    ds = dataset.load()
    ref = backtest.run(ds["series"], quote=ds["quote"])
    t = ref["traders"][tf]
    assert len(d["candles"]) == len(ds["series"][tf]) and d["candles"][-1]["time"] == ds["series"][tf].candles[-1].ts
    tr = d["trader"]
    assert [o["op_id"] for o in tr["ops"]] == [o["op_id"] for o in t.ops]
    assert len(tr["trades"]) == len(t.trades)
    assert tr["decision"]["explanation"] == t.decisions[-1]["explanation"]
    assert tr["summary"]["equity"] == pytest.approx(ref["summary"][tf]["equity"])
    assert tr["summary"]["mark"] == ref["mark"]
    names = {r["key"]: r for r in tr["analysis"]["indicators"]}
    if tf == "1d":
        assert names["macd_hist"]["status"] == "INSUFFICIENT_HISTORY" and tr["state"] == "WARMUP"
    else:
        assert names["rsi"]["status"] == "OK"
    for k in ("macd_hist", "mfi", "cmf", "obv"):
        assert k in d["series"]


def test_forward_not_started_shows_no_trader(client):
    d = client.get("/api/terminal/1h?mode=FORWARD").json()
    assert d["forward"]["status"] == "NOT_STARTED" and d["trader"] is None
    p = client.get("/api/performance").json()
    assert p["forward"]["status"] == "NOT_STARTED" and p["forward"]["curves"] == {}


def test_forward_controls_and_persistence(tmp_path, no_network):
    c = TestClient(create_app(tmp_path))
    assert c.post("/api/forward/pause").status_code == 409
    assert c.post("/api/forward/start").json()["status"] == "RUNNING"
    assert c.post("/api/forward/start").status_code == 409
    d = c.get("/api/terminal/4h?mode=FORWARD").json()
    assert d["trader"]["mode"] == "FORWARD" and d["trader"]["ops"] == [] and d["trader"]["summary"]["equity"] == pytest.approx(1000)
    assert c.post("/api/forward/pause").json()["status"] == "PAUSED"
    c2 = TestClient(create_app(tmp_path))                 # перезапуск сервера
    assert c2.get("/api/forward").json()["status"] == "PAUSED"
    assert c2.get("/api/overview").json()["backtest"]["persist"]["status"] == "VERIFIED"
    assert c2.post("/api/forward/resume").json()["status"] == "RUNNING"
    assert c2.post("/api/forward/stop").json()["status"] == "STOPPED"
    assert c2.post("/api/forward/unknown").status_code == 404
    # BACKTEST не изменился от действий FORWARD
    assert c2.get("/api/overview").json()["backtest"]["total_equity"] == pytest.approx(3000)


def test_bad_params(client):
    assert client.get("/api/terminal/5m").status_code == 404
    assert client.get("/api/terminal/1h?mode=LIVE").status_code == 400


def test_concurrent_requests_do_not_break_store(tmp_path, no_network):
    """Регрессия: параллельные запросы из пула потоков FastAPI к одному SQLite-соединению давали HTTP 500."""
    from concurrent.futures import ThreadPoolExecutor
    c = TestClient(create_app(tmp_path))
    c.post("/api/forward/start")
    paths = ["/api/overview", "/api/forward", "/api/terminal/1h?mode=FORWARD", "/api/performance", "/api/terminal/4h"] * 8
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda p: c.get(p).status_code, paths))
        codes += list(ex.map(lambda cmd: c.post(f"/api/forward/{cmd}").status_code, ["pause", "resume"] * 6))
    assert all(x in (200, 409) for x in codes) and codes.count(200) >= len(paths)
