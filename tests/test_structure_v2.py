"""Структурный анализ v2 (исследование): каузальность, правила внешней структуры, совместимость с v1, стратегии не затронуты."""
import pytest

from tradinglab import structure_v2 as SV2
from tradinglab.analysis import analyze
from tradinglab.candles import Candle, Series
from tradinglab.research import structure_compare as R
from tradinglab.strategies import ANALYSIS_PARAMS


def series_from_path(path, tf="4h", wick=0.002):
    """Свечи по заданной траектории close (open = предыдущий close)."""
    out, prev = [], path[0]
    for i, c in enumerate(path):
        out.append(Candle(i * 14400, prev, max(prev, c) * (1 + wick), min(prev, c) * (1 - wick), c, 1000.0))
        prev = c
    return Series(tf, out)


def legs(points, n=8):
    path = []
    for a, b in zip(points, points[1:]):
        path += [a + (b - a) * k / n for k in range(1, n + 1)]
    return [points[0]] + path


def run(s, k=SV2.K_ATR):
    return SV2.analyze_structure(analyze(s, ANALYSIS_PARAMS.get(s.tf, {})), k)


@pytest.mark.parametrize("name", ["synth range #1", "synth regime #2", "FABLES 1h", "ETH 4h"])
def test_no_look_ahead_truncated_history_gives_same_labels(name):
    s = R.load_sets()[name] if not name.startswith("synth") else R.synthetic(name.split()[1], n=400, seed=int(name[-1]))
    full = run(s)
    for cut in range(60, len(s), 37):
        part = run(Series(s.tf, s.candles[:cut]))
        assert part["external"] == [e for e in full["external"] if e["idx"] < cut]
        assert part["state"] == full["state"][:cut]
        assert part["internal"] == [e for e in full["internal"] if e["idx"] < cut]
        assert part["swings"] == [w for w in full["swings"] if w["confirmed_idx"] < cut]


def test_events_use_only_confirmed_swings_and_break_by_close():
    r = run(R.synthetic("regime", seed=1))
    assert r["external"]
    for e in r["external"]:
        assert e["pivot"]["confirmed_idx"] <= e["idx"] and e["pivot"]["idx"] < e["idx"]
        assert (e["close"] > e["level"]) if e["type"].endswith("UP") else (e["close"] < e["level"])
    for w in r["swings"]:
        assert w["idx"] < w["confirmed_idx"]


UP_THEN_DOWN = [1.0, 0.7, 1.0, 0.8, 1.3, 1.05, 1.2, 1.1, 1.4, 1.15, 1.5, 1.25, 1.35, 1.2, 1.32, 1.0, 0.9]
LH_INSIDE = [1.0, 0.7, 1.0, 0.8, 1.3, 1.05, 1.2, 1.1, 1.25, 0.95, 0.75]


def test_uptrend_bos_then_choch_only_below_protected_low():
    r = run(series_from_path(legs(UP_THEN_DOWN, 10), wick=0.001), k=2.0)
    ext = r["external"]
    assert [e["type"] for e in ext] == ["BOS_UP", "BOS_UP", "BOS_UP", "CHoCH_DOWN"]
    assert [round(e["level"], 2) for e in ext[:3]] == [1.0, 1.3, 1.4]                 # HH-уровни импульса
    ch, before = ext[-1], ext[-2]
    assert ch["level"] == before["protected_after"] == pytest.approx(1.15, abs=0.002)   # HL отката перед последним BOS
    assert ch["pivot"]["label"] == "protected" and ch["prev_trend"] == "up"
    assert all(r["state"][i]["trend"] == "up" for i in range(before["idx"], ch["idx"]))
    # внутренняя структура сломалась раньше (iCHoCH_DOWN на LL 1.2 < 1.25), внешняя — только ниже protected low
    ich = [e for e in r["internal"] if e["type"] == "iCHoCH_DOWN"]
    assert ich and before["idx"] < ich[0]["idx"] < ch["idx"]


def test_break_of_lower_high_inside_pullback_is_internal_only():
    r = run(series_from_path(legs(LH_INSIDE, 10), wick=0.001), k=2.0)
    assert [e["type"] for e in r["external"]] == ["BOS_UP", "CHoCH_DOWN"]
    assert r["external"][1]["level"] == pytest.approx(0.8, abs=0.002)                  # protected low = HL 0.8
    lh_break = [e for e in r["internal"] if e["type"] == "iBOS_UP" and e["level"] == pytest.approx(1.2, abs=0.002)]
    assert lh_break and not any(e["idx"] == lh_break[0]["idx"] for e in r["external"])


def test_protected_is_pullback_extreme_between_broken_swing_and_break():
    for kind in ("trend", "regime"):
        s = R.synthetic(kind, seed=1)
        r = run(s)
        lo, hi = s.col("l"), s.col("h")
        for e in r["external"]:
            j, start = e["protected_src"]["idx"], (e["pivot"]["idx"] if e["type"].startswith("BOS") and not e["initial"] else None)
            assert j <= e["idx"]
            if e["type"].startswith("BOS"):
                start = e["pivot"]["idx"]
                seg = range(start, e["idx"] + 1)
                assert e["protected_after"] == (min(lo[x] for x in seg) if e["type"].endswith("UP") else max(hi[x] for x in seg))


def test_range_produces_far_fewer_false_flips_than_v1():
    for seed in (1, 2, 3):
        c = R.compare_events(R.synthetic("range", seed=seed))
        assert c["v2_choch"] * 3 <= c["v1_choch"]


def test_internal_events_are_exactly_v1_events():
    from tradinglab import structure
    s = R.load_sets()["FABLES 1h"]
    a = analyze(s, ANALYSIS_PARAMS["1h"])
    v1 = structure.structure_events(a.s, a.pivots)
    inn = SV2.analyze_structure(a)["internal"]
    assert [(e["idx"], e["type"], e["level"]) for e in v1] == [(e["idx"], e["v1_type"], e["level"]) for e in inn]
    assert all(e["type"] == "i" + e["v1_type"] for e in inn)


def test_s2_research_run_does_not_touch_registered_strategies():
    from tradinglab.strategies import STRATEGIES
    ids = {tf: st["id"] for tf, (st, _) in STRATEGIES.items()}
    r = R.s2_research(R.load_sets()["FABLES 4h"])
    assert {tf: st["id"] for tf, (st, _) in STRATEGIES.items()} == ids
    assert all("RESEARCH" not in i for i in ids.values())
    assert r["v1"]["decisions"] == r["v2"]["decisions"] == 97


def test_terminal_payload_contains_v2_block(tmp_path):
    from fastapi.testclient import TestClient
    from tradinglab.web.app import create_app
    d = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8770").get("/api/terminal/1h?mode=BACKTEST").json()
    st = d["trader"]["analysis"]["structure"]
    assert "events" in st and st["v2"]["version"] == SV2.VERSION
    for e in st["v2"]["external"] + st["v2"]["internal"]:
        assert {"type", "ts", "level", "close", "prev_trend", "pivot"} <= set(e) and "confirmed_ts" in e["pivot"]


def test_every_event_carries_actual_confirmation_time_without_retro_signals():
    from tradinglab.candles import TF_SEC
    for name, s in list(R.load_sets().items()) + [("regime", R.synthetic("regime", seed=2))]:
        r = run(s)
        dt = TF_SEC[s.tf]
        for e in r["external"] + r["internal"]:
            assert e["known_ts"] == e["ts"] + dt                       # известно на закрытии свечи пробоя
            assert e["pivot"]["known_ts"] == e["pivot"]["confirmed_ts"] + dt <= e["known_ts"]
            assert e["pivot"]["ts"] <= e["pivot"]["confirmed_ts"]
        prev = None
        for e in r["external"]:                                        # protected-уровень CHoCH установлен прошлым событием
            if e["type"].startswith("CHoCH"):
                assert prev is not None and e["pivot"]["confirmed_ts"] == prev["ts"] and e["level"] == prev["protected_after"]
            prev = e


def test_visualisation_does_not_change_trading_decisions_or_backtest(monkeypatch):
    """structure_v2 нигде в торговом контуре: если сломать модуль, решения и результаты backtest те же."""
    import pathlib
    from tradinglab import backtest, dataset
    root = pathlib.Path(SV2.__file__).parent
    for f in ("strategies.py", "engine.py", "risk.py", "forward.py", "backtest.py", "analysis.py", "structure.py"):
        assert "structure_v2" not in (root / f).read_text() and "research" not in (root / f).read_text(), f
    ds = dataset.load()
    ref = backtest.run(ds["series"], quote=ds["quote"])

    def boom(*a, **k):
        raise AssertionError("structure_v2 вызван из торгового контура")
    monkeypatch.setattr(SV2, "analyze_structure", boom)
    monkeypatch.setattr(SV2, "external_structure", boom)
    monkeypatch.setattr(SV2, "atr_swings", boom)
    got = backtest.run(ds["series"], quote=ds["quote"])
    for tf in ("1h", "4h", "1d"):
        a, b = ref["traders"][tf], got["traders"][tf]
        assert [(d["bar_ts"], d["action_final"], d["explanation"]) for d in a.decisions] == \
               [(d["bar_ts"], d["action_final"], d["explanation"]) for d in b.decisions]
        assert a.trades == b.trades == []
    assert ref["valuation"]["total"] == got["valuation"]["total"]
    acts = {tf: [d["action_final"] for d in ref["traders"][tf].decisions] for tf in ("1h", "4h", "1d")}
    assert (acts["1h"].count("WAIT"), acts["1h"].count("WARMUP")) == (353, 20)        # зафиксированные результаты v1.0.0
    assert (acts["4h"].count("WAIT"), acts["4h"].count("WARMUP")) == (61, 32)
    assert acts["1d"].count("WARMUP") == 15 and len(acts["1d"]) == 15
