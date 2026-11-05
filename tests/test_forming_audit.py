"""Формирующаяся свеча FORWARD/LIVE, live-значения индикаторов, аудит индикаторов и единство серий в API/таблицах."""
import math

import pytest
from fastapi.testclient import TestClient

from replay import H, Clock, Market, run, ts
from tradinglab import analysis, audit as AU, dataset, indicators as I
from tradinglab.analysis import analyze
from tradinglab.candles import Candle, Series
from tradinglab.live.forming import live_bars, live_values
from tradinglab.live.worker import Worker
from tradinglab.store import Store
from tradinglab.strategies import ANALYSIS_PARAMS
from tradinglab.web.app import create_app

BASE = "http://127.0.0.1:8770"


def bot(tmp_path, start="2026-11-04T20:02:00Z", until="2026-11-04T20:40:00Z"):
    clock = Clock(ts(start))
    m = Market(clock)
    st = Store(tmp_path / "trading_lab.db", durable=False)
    w = Worker(st, m.transport(), clock)
    run(w, clock, ts(until))
    return clock, m, st, w


# ====================================================================== 1. формирующаяся свеча
def test_forming_bar_open_extremes_close_from_period_start(tmp_path):
    clock = Clock(ts("2026-11-04T20:02:00Z"))
    m = Market(clock)
    spike_t = ts("2026-11-04T20:15:00Z")
    m.price_override = [(spike_t, spike_t + 60, 0.0700), (spike_t + 600, spike_t + 660, 0.0500)]   # всплеск и провал внутри часа
    st = Store(tmp_path / "trading_lab.db", durable=False)
    w = Worker(st, m.transport(), clock)
    run(w, clock, ts("2026-11-04T20:40:00Z"))
    bars = live_bars(w.db, "1h", clock.t)
    assert [b["status"] for b in bars] == ["FORMING"]
    b = bars[0]
    gt = w.db.forming("1h")
    assert b["time"] == ts("2026-11-04T20:00:00Z") and b["open"] == gt["o"]            # open — открытие текущего часа
    assert b["high"] == pytest.approx(0.0700) and b["low"] == pytest.approx(0.0500)      # экстремумы с начала часа (по котировкам)
    assert b["close"] == pytest.approx(w.db.last_quote("pool")["mid"])                  # close — текущая цена
    for tf, start in (("4h", "2026-11-04T20:00:00Z"), ("1d", "2026-11-04T00:00:00Z")):
        fb = live_bars(w.db, tf, clock.t)[-1]
        assert fb["time"] == ts(start) and fb["high"] >= 0.0700 and fb["low"] <= 0.0500 and fb["status"] == "FORMING"


def test_closing_bar_shown_until_source_delivers_then_becomes_closed(tmp_path):
    clock, m, st, w = bot(tmp_path, until="2026-11-04T20:58:00Z")
    m.down = [("geckoterminal", clock.t, ts("2026-11-04T21:20:00Z"))]
    run(w, clock, ts("2026-11-04T21:10:00Z"))                   # час 20:00 закрыт, но GeckoTerminal недоступен
    bars = live_bars(w.db, "1h", clock.t)
    assert [b["status"] for b in bars] == ["CLOSING", "FORMING"]
    assert bars[0]["time"] == ts("2026-11-04T20:00:00Z") and bars[1]["time"] == ts("2026-11-04T21:00:00Z")
    assert w.db.last_closed_ts("1h") < ts("2026-11-04T20:00:00Z")
    run(w, clock, ts("2026-11-04T21:30:00Z"))                   # источник вернулся — свеча стала обычной закрытой
    assert w.db.last_closed_ts("1h") == ts("2026-11-04T20:00:00Z")
    assert [b["status"] for b in live_bars(w.db, "1h", clock.t)] == ["FORMING"]


def test_forming_bar_is_display_only_and_never_reaches_strategies(tmp_path):
    clock, m, st, w = bot(tmp_path, until="2026-11-04T20:05:00Z")
    w.runner.start()
    clock.t += 60
    app = create_app(tmp_path, worker=w, clock=clock)
    c = TestClient(app, base_url=BASE)
    run(w, clock, ts("2026-11-04T22:40:00Z"))
    before = {tf: st.decisions(w.runner.experiment_id, tf, 1000) for tf in ("1h", "4h", "1d")}
    for _ in range(3):
        for tf in ("1h", "4h", "1d"):
            assert c.get(f"/api/live/{tf}").status_code == 200
            c.get(f"/api/terminal/{tf}?mode=FORWARD")
    after = {tf: st.decisions(w.runner.experiment_id, tf, 1000) for tf in ("1h", "4h", "1d")}
    assert before == after                                       # отображение ничего не пишет и не меняет решения
    last_closed = w.db.last_closed_ts("1h")
    assert all(d["bar_ts"] <= last_closed for d in after["1h"])  # решения — только по закрытым свечам


def test_live_values_equal_core_on_closed_plus_forming(tmp_path):
    clock, m, st, w = bot(tmp_path)
    s, _ = w.db.candles("1h", clock.t)
    bars = live_bars(w.db, "1h", clock.t)
    v = live_values(s, bars, ANALYSIS_PARAMS["1h"])
    a = analyze(s, ANALYSIS_PARAMS["1h"])
    assert v["closed"]["rsi"] == a.v("rsi", len(s) - 1) and v["closed"]["time"] == s.candles[-1].ts
    b = bars[-1]
    s2 = Series("1h", s.candles + [Candle(b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"])])
    a2 = analyze(s2, ANALYSIS_PARAMS["1h"])
    for k in ("rsi", "macd_hist", "mfi", "cmf", "obv", "ema_fast", "bb_upper"):
        assert v["live"][k] == a2.v(k, len(s2) - 1)
    assert v["live"]["rsi"] != v["closed"]["rsi"]


# ====================================================================== 2. аудит индикаторов
def test_audit_1h_core_matches_pine_reference_exactly():
    ds = dataset.load()
    a = analysis.analyze(ds["series"]["1h"], ANALYSIS_PARAMS["1h"])
    r = AU.audit(a, n=20)
    for k in ("rsi", "mfi", "cmf", "obv", "bb_upper", "bb_mid", "bb_lower", "adx", "pdi", "ndi", "macd", "macd_signal", "macd_hist",
              "ema_fast", "atr"):
        assert r["summary"][k]["status"] == "OK", (k, r["summary"][k])
    assert r["summary"]["ema_slow"]["max_rel_diff"] < 1e-6       # засев EMA50 — затухающая разница методики
    rows = r["tables"]["rsi"]
    assert len(rows) == 20 and {"ts", "close", "ours", "reference", "diff", "note", "rsi_sma", "rsi_ema"} <= set(rows[0])
    assert all(row["ts"] == k.ts for row, k in zip(rows, a.s.candles[-20:]))
    assert all(abs(row["rsi_sma"] - row["ours"]) > 1 for row in rows[-5:])   # SMA-RSI — другая формула, другие значения


def test_audit_4h_ema_seed_difference_is_method_not_bug():
    ds = dataset.load()
    a = analysis.analyze(ds["series"]["4h"], ANALYSIS_PARAMS["4h"])
    r = AU.audit(a)
    assert all(v["status"] in ("OK", "METHOD", "WARMUP") for v in r["summary"].values())
    assert r["summary"]["rsi"]["status"] == "OK" and r["summary"]["mfi"]["status"] == "OK"
    assert r["summary"]["macd"]["status"] == "METHOD" and "инициализация EMA" in r["tables"]["macd"][-1]["note"]
    # разница затухает: ядро и Pine, рассчитанные на длинном ряду, сходятся
    long = [0.06 + 0.002 * math.sin(i / 7) for i in range(600)]
    assert abs(I.macd(long)["hist"][-1] - AU.pine_macd(long)[2][-1]) < 1e-12


def test_pine_rsi_matches_core_hand_example_and_variants_differ():
    c = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    assert all(abs(x - y) < 1e-12 for x, y in zip(I.rsi(c, 14)[14:], AU.pine_rsi(c, 14)[14:]))
    assert abs(AU.rsi_sma(c, 14)[-1] - I.rsi(c, 14)[-1]) > 0.5


def test_audit_api_forward_has_live_row(tmp_path):
    clock, m, st, w = bot(tmp_path)
    c = TestClient(create_app(tmp_path, worker=w, clock=clock), base_url=BASE)
    r = c.get("/api/audit/1h?mode=FORWARD").json()
    assert r["summary"]["rsi"]["status"] == "OK"
    live = r["tables"]["rsi"][-1]
    assert live["close"] is None and "LIVE" in live["note"] and live["ts"] == ts("2026-11-04T20:00:00Z")
    b = c.get("/api/audit/1h?mode=BACKTEST").json()
    assert len(b["tables"]["macd_hist"]) == 20


# ====================================================================== 3. одна и та же серия везде
@pytest.mark.parametrize("mode", ["BACKTEST", "FORWARD"])
def test_table_values_equal_chart_series_last_closed(tmp_path, mode):
    clock, m, st, w = bot(tmp_path)
    c = TestClient(create_app(tmp_path, worker=w, clock=clock), base_url=BASE)
    for tf in ("1h", "4h"):
        d = c.get(f"/api/terminal/{tf}?mode={mode}").json()
        rows = (d["trader"]["analysis"] if d.get("trader") else d["preview"])["indicators"]
        last_t = d["candles"][-1]["time"]
        for r in rows:
            if r["key"] in d["series"] and r["value"] is not None:
                pts = {p["time"]: p["value"] for p in d["series"][r["key"]]}
                assert pts[last_t] == r["value"], (tf, r["key"])       # таблица = график = API
        if mode == "FORWARD":
            lv = d["live"]["values"]
            assert lv["closed"]["time"] == last_t and lv["closed"]["rsi"] == {p["time"]: p["value"] for p in d["series"]["rsi"]}[last_t]
            assert any(r.get("live") is not None for r in rows)
