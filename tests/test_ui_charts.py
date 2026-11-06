"""UI-тест графиков в реальном браузере (Chromium через Playwright/Node). Пропускается, если Node или Playwright недоступны."""
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "ui" / "chart_check.js"


def _node_env():
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        return None
    root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
    env = {**__import__("os").environ, "NODE_PATH": root}
    ok = subprocess.run([node, "-e", "require('playwright')"], env=env, capture_output=True).returncode == 0
    return (node, env) if ok else None


@pytest.fixture(scope="module")
def ui_result(tmp_path_factory):
    ne = _node_env()
    if ne is None:
        pytest.skip("Node.js + Playwright не установлены — UI-тест графиков пропущен")
    import uvicorn
    from tradinglab.web.app import create_app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(tmp_path_factory.mktemp("ui")), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        r = subprocess.run([ne[0], str(SCRIPT), f"http://127.0.0.1:{port}/"], env=ne[1], capture_output=True, text=True, timeout=180)
    finally:
        srv.should_exit = True
        th.join(10)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if "fatal" in out and "Executable doesn't exist" in out["fatal"]:
        pytest.skip("браузер Playwright не установлен — UI-тест графиков пропущен")
    assert "fatal" not in out, out
    return out


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_rsi_pane_shows_core_values_with_levels_and_zones(ui_result, tf):
    x = ui_result["on"][tf]
    assert x["rsiPane"] == 1 and x["paneMap"]["rsi"] == 1
    assert x["rsi"]["n_chart"] == x["rsi"]["n_api"] > 0 and x["rsi"]["maxdiff"] == 0       # ряд ядра без пересчёта
    assert x["zones"]["maxdiff"] == 0
    assert x["rsiLevels"] == [30, 50, 70] and x["rsiRange"] == {"minValue": 0, "maxValue": 100}
    assert x["zoneBases"] == [30, 70] and x["rsiLastLabel"] is True
    assert x["labels"][0].startswith("RSI(14)")
    h = x["paneHeights"]
    assert h[1] > max(h[2:])                                             # панель RSI выше остальных осцилляторов
    assert abs(x["rsiY100"] - 0.06 * h[1]) <= 3 and abs(x["rsiY0"] - 0.94 * h[1]) <= 3   # шкала ровно 0–100 (отступы 6%)
    assert x["rsiSpan3070"] >= 0.33 * h[1]


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_oscillators_match_core_and_hist_equals_macd_minus_signal(ui_result, tf):
    x = ui_result["on"][tf]
    for k in ("macd", "signal", "hist", "mfi", "cmf", "obv"):
        assert x[k]["n_chart"] == x[k]["n_api"] and x[k]["maxdiff"] == 0, (tf, k, x[k])
    assert x["histVsDiff"] < 1e-15


def test_rsi_toggle_removes_and_restores_pane_and_keeps_zoom(ui_result):
    on, off, back = ui_result["on"]["1h"], ui_result["off"]["1h"], ui_result["back"]["1h"]
    assert off["rsiPane"] is None and off["panes"] == on["panes"] - 1 and "rsi" not in off["paneMap"]
    assert off["paneMap"]["macd"] == 1 and off["hist"]["maxdiff"] == 0       # остальные панели сдвинулись, данные те же
    assert back["rsiPane"] == 1 and back["panes"] == on["panes"] and back["rsi"]["maxdiff"] == 0
    assert abs(off["range"]["from"] - on["range"]["from"]) < 1 and abs(off["range"]["to"] - on["range"]["to"]) < 1
    assert ui_result["off"]["4h"]["rsiPane"] == 1                            # переключатель действует на свой терминал


def test_mobile_and_no_console_errors(ui_result):
    m = ui_result["mobile"]
    assert m["sw"] <= m["cw"] and m["rsiPaneH"] >= 40
    assert ui_result["errs"] == []


@pytest.fixture(scope="module")
def live_ui(tmp_path_factory):
    ne = _node_env()
    if ne is None:
        pytest.skip("Node.js + Playwright не установлены — UI-тест пропущен")
    import uvicorn
    from replay import Clock, Market, run, ts
    from tradinglab.live.worker import Worker
    from tradinglab.store import Store
    from tradinglab.web.app import create_app
    d = tmp_path_factory.mktemp("live")
    clock = Clock(ts("2026-11-04T20:02:00Z"))
    w = Worker(Store(d / "trading_lab.db", durable=False), Market(clock).transport(), clock)
    run(w, clock, ts("2026-11-04T20:40:00Z"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(d, clock=clock), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        r = subprocess.run([ne[0], str(SCRIPT.parent / "live_check.js"), f"http://127.0.0.1:{port}/"], env=ne[1], capture_output=True, text=True, timeout=180)
    finally:
        srv.should_exit = True
        th.join(10)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if "fatal" in out and "Executable doesn't exist" in out["fatal"]:
        pytest.skip("браузер Playwright не установлен")
    assert "fatal" not in out, out
    return out


def test_forming_candle_replaces_live_line(live_ui):
    i = live_ui["info"]
    assert i["lastTime"] == i["formingTime"] > i["closedTime"] and i["formingStatus"] == "FORMING"
    assert ".35" in i["lastColor"] and i["lastClose"] == i["formingClose"]          # полупрозрачная, close = текущая цена
    assert "FORMING" in i["markers"] and "Live" not in i["priceLines"]


def test_tooltips_on_forming_and_closed_bars_all_panes(live_ui):
    tf, tm, i = live_ui["tipForming"], live_ui["tipMacd"], live_ui["info"]
    assert tf["shown"] == "block" and "FORMING" in tf["text"] and "закрытая" in tf["text"] and [x.upper() for x in tf["on"]] == ["ЦЕНА"]
    for s in ("RSI(14)", "MACD", "Signal", "Histogram", "MFI", "CMF", "OBV", "EMA20", "BB"):
        assert s in tf["text"], s
    assert tm["shown"] == "block" and [x.upper() for x in tm["on"]] == ["MACD(12,26,9)"] and "закрытая свеча" in tm["text"]
    assert f"{i['rsiClosed']:.1f}".replace(".", ",") in tm["text"]                   # то же значение, что в ряду ядра


def test_mobile_fixed_bar_values_block(live_ui):
    m = live_ui["mob"]
    assert m["barinfo"] == "block" and m["tip"] != "block" and "MACD" in m["text"] and m["sw"] <= m["cw"]
    assert live_ui["errs"] == []


@pytest.fixture(scope="module")
def struct_ui(tmp_path_factory):
    ne = _node_env()
    if ne is None:
        pytest.skip("Node.js + Playwright не установлены — UI-тест пропущен")
    import uvicorn
    from tradinglab.web.app import create_app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(tmp_path_factory.mktemp("st")), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        r = subprocess.run([ne[0], str(SCRIPT.parent / "structure_check.js"), f"http://127.0.0.1:{port}/"], env=ne[1], capture_output=True, text=True, timeout=180)
    finally:
        srv.should_exit = True
        th.join(10)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if "fatal" in out and "Executable doesn't exist" in out["fatal"]:
        pytest.skip("браузер Playwright не установлен")
    assert "fatal" not in out, out
    return out


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_structure_segments_go_from_broken_pivot_to_break_candle(struct_ui, tf):
    x = struct_ui["res"][tf]["off"]                       # по умолчанию: только внешняя (swing) структура
    want = [[[e["pts"], e["level"]], [e["ts"], e["level"]]] for e in x["ext"] if e["pts"] < e["ts"]]
    assert x["segs"] == want
    lab = lambda e: e["type"].replace("_UP", "↑").replace("_DOWN", "↓")   # noqa: E731
    assert all([e["ts"], lab(e)] in x["markers"] for e in x["ext"])
    assert not any(m[1].startswith("i") for m in x["markers"])
    assert bool(x["prot"]) == (x["protected"] is not None)


def test_internal_structure_toggle_and_tooltip(struct_ui):
    on, off = struct_ui["res"]["1h"]["on"], struct_ui["res"]["1h"]["off"]
    assert len(on["segs"]) == len(off["segs"]) + sum(1 for e in on["int"] if e["pts"] < e["ts"])
    assert any(m[1].startswith("iBOS") or m[1].startswith("iCHoCH") for m in on["markers"])
    t = struct_ui["tip"]
    assert t["shown"] == "block"
    for s in ("Пробой", "Сигнал известен", "Пробитый уровень", "Подтверждение", "Предыдущая структура"):
        assert s in t["text"], s
    assert struct_ui["ev"]["type"].split("_")[0].lstrip("i") in t["text"]
    assert ("внешняя swing-структура v2" in t["text"].lower()) or ("внутренняя структура v1" in t["text"].lower())
    assert any("внешняя swing v2" in x for x in struct_ui["toggles"]) and any("внутренняя v1" in x for x in struct_ui["toggles"])
    assert struct_ui["table"] and struct_ui["errs"] == []
