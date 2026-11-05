"""Релиз для первого запуска: preflight перед START, сверка состояния с журналом, самопроверка рестарта, диагностика."""
import json
import types

from fastapi.testclient import TestClient

from replay import Clock, Market, run, ts
from test_live_integration import with_position
from tradinglab.live import config as C
from tradinglab.live.health import integrity, preflight, restart_selftest
from tradinglab.live.worker import Worker
from tradinglab.store import Store
from tradinglab.web.app import create_app


def ready(tmp_path, start="2026-11-04T20:05:00Z"):
    clock = Clock(ts(start))
    m = Market(clock)
    st = Store(tmp_path / "trading_lab.db", durable=False)
    w = Worker(st, m.transport(), clock)
    w.tick(clock.t)
    return clock, m, st, w


def test_preflight_passes_on_healthy_sources_and_start_goes_through_queue(tmp_path):
    clock, m, st, w = ready(tmp_path)
    pf = preflight(st, clock.t)
    assert pf["ok"], pf
    names = {c["name"] for c in pf["checks"]}
    assert {"GeckoTerminal OHLCV", "DexScreener", "Robinhood RPC (StateView)", "Ончейн-котировка StateView", "Данные тиков пула",
            "Закрытые свечи 1H", "Закрытые свечи 4H", "Закрытые свечи 1D", "Целостность состояния"} <= names
    cid = w.db.enqueue(clock.t, "START")
    w.tick(clock.t)
    assert w.db.command(cid)["status"] == "DONE" and w.runner.status == "RUNNING"


def test_start_rejected_when_stateview_is_down(tmp_path):
    clock, m, st, w = ready(tmp_path)
    m.rpc_fail += [(C.SEL_GET_SLOT0, clock.t, 10 ** 12)]
    clock.t += 300
    run(w, clock, clock.t + 300)
    cid = w.db.enqueue(clock.t, "START")
    w.tick(clock.t)
    c = w.db.command(cid)
    assert c["status"] == "ERROR" and "проверкой готовности" in json.loads(c["result"])["error"]
    assert w.runner.status == "NOT_STARTED" and st.experiments() == []


def test_integrity_detects_tampered_state_and_blocks_start(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    assert integrity(st)["ok"]
    exp = w.runner.experiment_id
    s = st.trader_state(exp, "1h")
    s["portfolio"]["cash"] = str(float(s["portfolio"]["cash"]) + 100)      # «правка» состояния мимо журнала
    st.conn.execute("UPDATE trader_state SET state_json=? WHERE experiment_id=? AND trader='1h'", (json.dumps(s), exp))
    res = w.check_integrity(clock.t)
    assert not res["ok"] and "кеш в состоянии" in res["issues"][0]
    assert any(e["kind"] == "integrity_error" for e in w.db.events(50))
    assert not preflight(st, clock.t)["ok"]


def test_restart_selftest(tmp_path):
    r = restart_selftest(tmp_path)
    assert r["ok"] and r["status_restored"] == "PAUSED" and r["journal_append_only"]


def test_doctor_offline_writes_report_without_token(tmp_path, capsys):
    from tradinglab.__main__ import doctor
    create_app(tmp_path)                                    # создаёт control_token
    token = (tmp_path / "control_token").read_text()
    doctor(types.SimpleNamespace(data=str(tmp_path), port=1, offline=True))
    reps = list((tmp_path / "logs").glob("diagnostics-*.txt"))
    assert len(reps) == 1
    txt = reps[0].read_text(encoding="utf-8")
    assert "Итог:" in txt and "самопроверка рестарта" in txt and "целостность состояния FORWARD" in txt
    assert token not in txt


def test_terminal_shows_last_decision_and_stop_check(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    app = create_app(tmp_path, worker=w, clock=clock)
    c = TestClient(app, base_url="http://127.0.0.1:8770")
    mon = c.get("/api/terminal/1h?mode=FORWARD").json()["trader"]["monitor"]
    assert mon["protection"] == "FULL" and mon["last_stop_check"]["ts"] >= clock.t - 120 and mon["last_decision_ts"]
    pf = c.get("/api/preflight").json()
    assert pf["checks"][0]["name"] == "Фоновый бот"
    o = c.get("/api/overview").json()
    assert o["live"]["integrity"]["ok"] and o["live"]["last_decisions"]["1h"]["action"]


def test_slow_sources_do_not_block_heartbeat_and_risk_monitor(tmp_path):
    """Регрессия: лимиты GeckoTerminal/CoinGecko задерживали heartbeat и проверку стопов (первый цикл > 1 мин)."""
    import threading
    import time as _t
    clock = Clock(ts("2026-11-04T20:05:00Z"))
    m = Market(clock)
    base = m.handler

    def slow(src, what, params, now):
        if src in ("geckoterminal", "coingecko"):
            _t.sleep(0.5)                                   # «медленный» источник реального времени
        return base(src, what, params, now)
    m.handler = slow
    st = Store(tmp_path / "trading_lab.db", durable=False)
    w = Worker(st, m.transport(), clock)
    stop = threading.Event()
    th = threading.Thread(target=w.run_forever, args=(stop, 0.01, 0.01), daemon=True)
    th.start()
    _t.sleep(1.0)
    fast_ticks = w.ticks
    hb, _ = w.db.kv("worker")
    stop.set()
    th.join(10)
    assert fast_ticks > 20 and hb["status"] == "running"     # быстрая линия работала, пока медленная ждала источники
    assert w.db.last_quote("pool") is not None
