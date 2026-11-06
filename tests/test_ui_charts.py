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
    assert "Live" not in i["priceLines"]
    # статус формирующейся свечи — только в подсказке: ни надписи FORMING, ни серого квадратного маркера на графике
    assert not any("FORMING" in m or "ждёт GT" in m for m in i["markers"]) and "square" not in i["markerShapes"]


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
def test_v3_layers_match_api_and_legacy_is_off(struct_ui, tf):
    x = struct_ui["res"][tf]
    tog = dict(x["toggles"])
    assert tog["mswing"] and tog["mevents"] and tog["prot"] and tog["phase"]
    assert not tog["legacy"] and not tog["ievents"] and not tog["iswing"]          # читаемость по умолчанию
    want = [[[e["src"], e["level"]], [e["ts"], e["level"]]] for e in x["events"] if e["level"] is not None and e["src"] is not None and e["src"] < e["ts"]]
    assert all(seg in x["segs"] for seg in want)                                   # отрезок от источника уровня до свечи события
    assert not any(m[1].startswith("v1 ") for m in x["markers"])                   # legacy v1/v2 не смешаны с v3
    if x["state"] and x["state"]["ph"]:
        assert any(t.startswith("Protected H") and abs(pr - x["state"]["ph"]) < 1e-12 for t, pr in x["plines"])


def test_v3_layer_toggles_tooltip_and_five_tables(struct_ui):
    off, on = struct_ui["res"]["off"], struct_ui["res"]["allon"]
    assert off["segs"] == [] and not any(t.startswith("Protected") for t, _ in off["plines"])
    assert on["lines"]["ievents"] > 0 and on["lines"]["iswing"] == 1 and len(on["segs"]) > len(struct_ui["res"]["1h"]["segs"])
    t = struct_ui["tip"]
    assert t["shown"] == "block"
    for s in ("MAJOR: BOS", "Уровень", "Триггер", "известно", "Причина"):
        assert s in t["text"], s
    tab = struct_ui["tab"]
    assert all(tab["tables"]) and "Major Trend" in tab["panel"] and "Protected High" in tab["panel"] and "Старший ТФ" in tab["panel"]
    assert struct_ui["errs"] == []


@pytest.fixture(scope="module")
def major_ui(tmp_path_factory):
    ne = _node_env()
    if ne is None:
        pytest.skip("Node.js + Playwright не установлены — UI-тест пропущен")
    import uvicorn
    from tradinglab.web.app import create_app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(tmp_path_factory.mktemp("mj")), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        r = subprocess.run([ne[0], str(SCRIPT.parent / "major_layers_check.js"), f"http://127.0.0.1:{port}/"], env=ne[1], capture_output=True, text=True, timeout=180)
    finally:
        srv.should_exit = True
        th.join(10)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if "fatal" in out and "Executable doesn't exist" in out["fatal"]:
        pytest.skip("браузер Playwright не установлен")
    assert "fatal" not in out, out
    return out


def test_major_on_intermediate_off_shows_only_core_major_points(major_ui):
    d = major_ui["def"]
    assert d["toggles"]["mswing"] and not d["toggles"]["iswing_mid"] and not d["toggles"]["iswing"] and not d["toggles"]["ievents"]
    maj = [w for w in d["api"] if w["tier"] == "major"]
    inter = [w for w in d["api"] if w["tier"] == "intermediate"]
    assert len(maj) == 4 and len(inter) > 10
    assert d["major"] == [[w["ts"], w["price"]] for w in maj]                    # линия: major → major, без промежуточных
    inter_ts = {w["ts"] for w in inter}
    assert not inter_ts & {p[0] for p in d["major"]}
    mk = [m for m in d["markers"] if m[1].endswith("· major")]
    assert sorted(m[0] for m in mk) == sorted(w["ts"] for w in maj)
    assert not any(m[1].startswith("int·") or m[0] in inter_ts and "major" in m[1] for m in d["markers"])
    assert d["inter"] is None and d["internal"] is None
    # FABLES 1H: исходная структура по ролям ядра — LL 21.10 (0.05126) → LH 0.07975 → LL 0.04024
    seq = [(w["label"], round(w["price"], 5)) for w in maj]
    assert seq[1:] == [("", 0.05126), ("LH", 0.07975), ("LL", 0.04024)]
    for w in maj:                                                               # не раньше подтверждения и назначения роли
        assert w["avail"] >= w["conf"] and w["avail"] >= min(r["assigned_ts"] for r in w["roles"])


def test_intermediate_and_internal_layers_are_separate_and_independent(major_ui):
    m, a, io, back = major_ui["mid"], major_ui["allint"], major_ui["intOnly"], major_ui["back"]
    inter = [w for w in m["api"] if w["tier"] == "intermediate"]
    assert m["inter"] == [[w["ts"], w["price"]] for w in m["api"]]             # пунктир по полному major-ZigZag, отдельной серией
    assert sorted(x[0] for x in m["markers"] if x[1].startswith("int·")) == sorted(w["ts"] for w in inter)
    assert m["major"] == major_ui["def"]["major"]                              # главная линия не изменилась
    assert a["internal"] == [list(x) for x in a["apiInt"]] and a["major"] == major_ui["def"]["major"]
    assert io["inter"] is None and io["internal"] is not None and io["major"] == major_ui["def"]["major"]
    assert back["major"] == major_ui["def"]["major"] and back["inter"] is None and back["internal"] is None
    assert sorted(back["markers"]) == sorted(major_ui["def"]["markers"])


def test_zoom_keeps_markup_and_tooltip_shows_availability(major_ui):
    z = major_ui["zoom"]
    assert z["major"] == major_ui["def"]["major"] and sorted(z["markers"]) == sorted(major_ui["def"]["markers"])
    t = major_ui["tip"].lower()
    for s in ("swing major", "подтверждён zigzag", "доступен как major", "protected high", "обоснование", "ретроспективно"):
        assert s in t, s
    assert major_ui["errs"] == []



def test_4h_own_major_and_optional_1h_comparison_layer(major_ui):
    off, on = major_ui["h4off"], major_ui["h4on"]
    assert off["tog"] and not off["on"] and not off["has1hToggle"] and "1H Major (сравнение)" in off["label"]
    assert off["ltf"] is None and not any("(сравн.)" in m for m in off["markers"])
    assert [round(p[1], 5) for p in off["api4"]] == [0.14502, 0.05126, 0.07975, 0.04024]     # собственная 4H-модель
    assert off["major"] == off["api4"]
    assert on["ltf"] == on["api"] and on["major"] == off["major"]                             # отдельная серия, 4H major не меняется
    assert sum(1 for m in on["markers"] if "(сравн.)" in m) == len(on["api"])


# ====================================================================== тест 9: формирующаяся свеча без маркера, но обновляется
@pytest.fixture(scope="module")
def forming_ui(tmp_path_factory):
    ne = _node_env()
    if ne is None:
        pytest.skip("Node.js + Playwright не установлены — UI-тест пропущен")
    import uvicorn
    from replay import Clock, Market, run, ts
    from tradinglab.live.worker import Worker
    from tradinglab.store import Store
    from tradinglab.web.app import create_app
    d = tmp_path_factory.mktemp("forming")
    clock = Clock(ts("2026-11-04T20:02:00Z"))
    w = Worker(Store(d / "trading_lab.db", durable=False), Market(clock).transport(), clock)
    run(w, clock, ts("2026-11-04T20:30:00Z"))
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
    stop = threading.Event()

    def ticker():                       # бот продолжает работать: +60 с рыночного времени каждые 0,5 с (в пределах того же часа)
        while not stop.is_set() and clock.t < ts("2026-11-04T20:55:00Z"):
            clock.t += 60
            w.tick(clock.t)
            stop.wait(0.5)
    tk = threading.Thread(target=ticker, daemon=True)
    try:
        tk.start()
        r = subprocess.run([ne[0], str(SCRIPT.parent / "forming_check.js"), f"http://127.0.0.1:{port}/"], env=ne[1], capture_output=True, text=True, timeout=180)
    finally:
        stop.set()
        tk.join(10)
        srv.should_exit = True
        th.join(10)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if "fatal" in out and "Executable doesn't exist" in out["fatal"]:
        pytest.skip("браузер Playwright не установлен")
    assert "fatal" not in out, out
    return out


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_no_forming_label_or_grey_marker_but_live_candle_updates(forming_ui, tf):
    a, b = forming_ui["first"][tf], forming_ui["second"][tf]
    for x in (a, b):
        assert x["time"] == x["fbTime"] and x["status"] == "FORMING"            # последняя свеча графика — формирующаяся
        assert x["markers"] == [] and x["allSquares"] == 0                     # ни надписи, ни серого квадрата
    assert a["time"] == b["time"]                                              # та же свеча…
    assert (a["c"], a["h"], a["l"]) != (b["c"], b["h"], b["l"])                # …но OHLC обновились по новым котировкам
    assert b["v"] is not None


def test_forming_status_kept_in_tooltip(forming_ui):
    assert "FORMING" in forming_ui["tipText"] and forming_ui["errs"] == []


# ====================================================================== тест 8: SHADOW LAB в Chromium
@pytest.fixture(scope="module")
def shadow_ui(tmp_path_factory):
    import os
    ne = _node_env()
    if ne is None:
        pytest.skip("Node.js + Playwright не установлены — UI-тест пропущен")
    import uvicorn
    from replay import Clock, Market, run, ts
    from test_shadow import enter_once
    from tradinglab.live.worker import Worker
    from tradinglab.shadow.process import ShadowProcess
    from tradinglab.store import Store
    from tradinglab.web.app import create_app
    d = tmp_path_factory.mktemp("shadow")
    clock = Clock(ts("2026-11-04T23:40:00Z"))
    w = Worker(Store(d / "trading_lab.db", durable=False), Market(clock).transport(), clock)
    run(w, clock, ts("2026-11-04T23:45:00Z"))
    w.runner.start()
    sp = ShadowProcess(d, clock, durable=False)
    sp.tick(clock.t)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(d, worker=w, clock=clock), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    stop = threading.Event()

    def ticker():
        """Основной бот и процесс SHADOW работают; после START (через страницу) время идёт к закрытию 4H-свечи 00:00."""
        while not stop.is_set():
            r = sp.runner
            if r is not None and r.status == "RUNNING" and "4h" in r.traders:
                t = r.traders["4h"]
                if not getattr(t, "_stub", False):
                    t.fn, t._stub = enter_once(), True             # заглушка входа: проверка интерфейса исполнения
                if clock.t < ts("2026-11-05T00:20:00Z"):
                    clock.t += 30
            w.tick(clock.t)
            sp.tick(clock.t)
            stop.wait(0.15)
    tk = threading.Thread(target=ticker, daemon=True)
    shots = os.environ.get("SHADOW_SHOTS", "")
    try:
        tk.start()
        r = subprocess.run([ne[0], str(SCRIPT.parent / "shadow_check.js"), f"http://127.0.0.1:{port}/", shots], env=ne[1],
                           capture_output=True, text=True, timeout=300)
    finally:
        stop.set()
        tk.join(10)
        srv.should_exit = True
        th.join(10)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if "fatal" in out and "Executable doesn't exist" in out["fatal"]:
        pytest.skip("браузер Playwright не установлен")
    assert "fatal" not in out, (out, r.stderr[-2000:])
    out["main_exp"] = w.runner.experiment_id
    out["main_fills"] = {tf: len(w.store.fills(w.runner.experiment_id, tf)) for tf in ("1h", "4h", "1d")}
    return out


def test_shadow_lab_before_activation_shows_preview_and_no_trades(shadow_ui):
    x = shadow_ui["pre"]
    assert x["status"] == "NOT_STARTED" and not x["trader"]
    assert x["chartBars"] >= x["candles"] > 30 and x["majorSwings"] >= 2
    assert any(m.endswith("· major") for m in x["markers"]) and not any("FORMING" in m for m in x["markers"])
    assert "ПРЕДПРОСМОТР" in x["decision"] and "Major TRANSITION" in x["decision"] and "R:R" in x["decision"]
    assert "не входит" in x["banner"] and "только на чтение" in x["banner"]
    assert "$1 000" in x["kpis"].replace("\xa0", " ") and "CASH" in x["kpis"].upper() and "EQUITY" in x["kpis"].upper()
    ctl = dict(shadow_ui["pre"]["ctl"])
    assert ctl["START"] is False and ctl["STOP"] is True and ctl["EMERGENCY EXIT"] is True
    assert "Основной бот работает" in x["ready"] and "Тики CLMM" in x["ready"]


def test_shadow_lab_start_position_close_journal(shadow_ui):
    r, c = shadow_ui["run"], shadow_ui["closed"]
    assert r["status"] == "RUNNING" and "Вход" in r["position"] and {"вход P2", "стоп P2", "T1 P2"} <= set(r["priceLines"])
    assert any(m.startswith("BUY") for m in r["markers"]) and any(m == "P2 ENTER" for m in r["markers"])
    assert "Комиссия hook" in r["costs"] and "Газ" in r["costs"]
    assert any(m.startswith("SELL") for m in c["markers"]) and "Позиции нет" in c["position"]
    for s in ("ENTER", "BUY", "SELL", "PAPER", "CLOSE POSITION"):
        assert s in c["journal"], s
    assert "SHADOW LAB" in shadow_ui["nav"]
    assert shadow_ui["main_fills"] == {"1h": 0, "4h": 0, "1d": 0}         # основной FORWARD не получил ни одной операции


def test_shadow_lab_mobile_and_no_console_errors(shadow_ui):
    assert shadow_ui["mob"]["sw"] <= shadow_ui["mob"]["cw"]
    assert shadow_ui["errs"] == []


def test_performance_compares_shadow_s2_bh_cash(shadow_ui):
    t = shadow_ui["perf"]["text"].upper()
    for x in ("SHADOW P2 4H", "S2 (4H)", "BUY & HOLD", "КЕШ", "НЕ ВХОДИТ", "ДОХОДНОСТЬ С АКТИВАЦИИ SHADOW"):
        assert x in t, x
    assert "SHADOW P2 4H" in shadow_ui["perf"]["legend"] and "S2" in shadow_ui["perf"]["legend"]
