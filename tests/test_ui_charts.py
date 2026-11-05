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
