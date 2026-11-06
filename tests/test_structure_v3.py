"""Структура v3: причинность (пошагово = пакетно = префикс), перезапуск, старт с разных точек, аномалии, сценарии,
FABLES (major DOWN, protected high 0.07975), SCALE_CONFLICT, отсутствие влияния на торговый контур."""
import datetime as dt
import json
import math
import random
from pathlib import Path

import pytest

from tradinglab import structure_v3 as S
from tradinglab.candles import Candle, Series, normalize
from tradinglab.research import structure_v3_eval as E

ROOT = Path(__file__).resolve().parents[1]
ts = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC).timestamp())  # noqa: E731


@pytest.fixture(scope="module")
def fab():
    d = json.loads((ROOT / "datasets/structure_v3_2026-11-06/fables_gt_full.json").read_text())
    return normalize(d["rows"]["1h"], "1h", d["fetched_at"])


def path(points, n=10, wick=0.002, extra=None, tf="1h"):
    out, prev, xs = [], points[0], [points[0]]
    for a, b in zip(points, points[1:]):
        xs += [a + (b - a) * k / n for k in range(1, n + 1)]
    for i, c in enumerate(xs):
        h, l = max(prev, c) * (1 + wick), min(prev, c) * (1 - wick)
        if extra and i in extra:
            h, l, c = extra[i](h, l, c)
        out.append(Candle(i * 3600, prev, h, l, c, 1000.0))
        prev = c
    return Series(tf, out)


def major(s, **kw):
    return S.Engine(s.tf, **kw).run(s).result()["major"]


def sig(e: S.Engine):
    r = e.result()
    return (json.dumps(r["major"]["events"], sort_keys=True), json.dumps(r["major"]["protected_history"], sort_keys=True),
            json.dumps(r["internal"]["events"], sort_keys=True), json.dumps(r["major"]["swings"], sort_keys=True),
            json.dumps(e.states(), sort_keys=True), json.dumps(r["scale_conflicts"], sort_keys=True))


# ------------------------------------------------------------------ причинность и воспроизводимость
def test_bar_by_bar_equals_batch_and_every_prefix(fab):
    full = S.Engine("1h").run(fab)
    inc = S.Engine("1h")
    for n, k in enumerate(fab.candles, 1):
        inc.step(k)
        if n in (20, 70, 130, 200, 260, 320, len(fab)):
            pre = S.Engine("1h").run(Series("1h", fab.candles[:n]))
            assert sig(inc) == sig(pre), n                           # пошагово == пакетно на префиксе
            assert inc.states() == full.states()[:n]                 # префикс полного прогона: прошлое не меняется
            fe = [e for e in full.major.events if e["idx"] < n]
            assert inc.major.events == fe


def test_no_lookahead_on_synthetic_with_random_cuts():
    s, _ = E.nested(3)
    full = S.Engine("1h").run(s)
    for n in random.Random(7).sample(range(30, len(s)), 12):
        e = S.Engine("1h").run(Series("1h", s.candles[:n]))
        assert e.states() == full.states()[:n]
        assert [x for x in full.major.events if x["idx"] < n] == e.major.events
        assert [x for x in full.major.prot_hist if x["set_idx"] < n] == e.major.prot_hist
        for ev in e.major.events + e.internal.events:                 # событие известно только на закрытии своей свечи
            assert ev["known_ts"] == ev["ts"] + 3600 and ev["idx"] < n


def test_restart_from_saved_state_is_identical(fab):
    full = S.Engine("1h").run(fab)
    for cut in (15, 90, 200, 333):
        a = S.Engine("1h").run(Series("1h", fab.candles[:cut]))
        st = json.loads(json.dumps(a.to_state()))                  # как при записи на диск
        b = S.Engine.from_state(st)
        for k in fab.candles[cut:]:
            b.step(k)
        assert sig(b) == sig(full), cut


def test_start_point_dependence_on_fables_is_documented(fab):
    """Старт, включающий первый минимум после запуска (0.05126, 10-21 00:00), даёт ту же major-структуру, что и полная
    история. Старт ПОСЛЕ него лишает модель этой границы диапазона: тогда major-диапазоном становится пара 0.05398/0.0644
    и модель приходит к UP — известное ограничение (результат зависит от доступной истории), а не look-ahead."""
    ref = major(fab)
    key = lambda m: [(e["ts"], e["type"]) for e in m["events"] if e["type"].startswith(("BOS", "CHoCH"))]  # noqa: E731
    for off in (0, 1, 2, 3):
        m = major(Series("1h", fab.candles[off:]))
        assert key(m) == key(ref) and m["state"]["ph"] == ref["state"]["ph"], off
    late = major(Series("1h", fab.candles[24:]))
    assert late["state"]["trend"] != ref["state"]["trend"]          # зафиксировано: см. docs/STRUCTURE_V3_REPORT.md


def test_initial_anomalous_candles_and_outliers():
    s = path([2.0, 1.5, 1.75, 1.3, 1.5, 1.1, 1.25, 0.95], extra={1: (lambda h, l, c: (h * 3, l, c))})   # выброс-шпиль
    bad = Candle(s.candles[5].ts + 1, 1.0, 0.5, 2.0, 1.0, 1.0)                                          # h < l
    flat = [Candle(s.candles[-1].ts + 3600 * (q + 1), s.candles[-1].c, s.candles[-1].c, s.candles[-1].c, s.candles[-1].c, 0.0) for q in range(5)]
    e = S.Engine("1h")
    for k in s.candles[:6] + [bad] + s.candles[6:] + flat:
        e.step(k)
    r = e.result()
    assert len(r["rejected"]) == 1 and r["major"]["state"]["trend"] == "DOWN"
    ph = [p for p in r["major"]["protected_history"] if p["side"] == "H"]
    assert ph and all(p["price"] < 3 for p in ph[1:])              # шпиль запуска — граница начального диапазона, не protected после BOS
    e2 = S.Engine("1h")
    for k in s.candles[:3]:
        e2.step(k)
    assert e2.result()["insufficient_history"] and e2.result()["major"]["events"] == []


# ------------------------------------------------------------------ сценарии
def test_major_down_lh_ll_moves_protected_only_on_bos():
    m = major(path([2.0, 1.5, 1.75, 1.3, 1.5, 1.1, 1.25, 0.95]))
    assert [e["type"] for e in m["events"] if e["type"].startswith(("BOS", "CHoCH"))] == ["BOS_DOWN"] * 3
    hs = [p["price"] for p in m["protected_history"] if p["side"] == "H" and p["status"] == "PROTECTED"]
    assert hs == sorted(hs, reverse=True) and m["state"]["trend"] == "DOWN"


def test_recovery_inside_down_is_internal():
    r = S.analyze(path([2.0, 1.5, 1.75, 1.3, 1.5, 1.1, 1.25, 1.15, 1.35, 1.28, 1.45, 1.4]))
    assert r["major"]["state"]["trend"] == "DOWN" and r["internal"]["state"] == "UP"
    assert not any(e["type"].startswith("CHoCH") for e in r["major"]["events"])
    assert any(e["type"] == "iCHoCH_UP" for e in r["internal"]["events"])


def test_real_reversal_violation_and_confirmation_are_separate_events():
    m = major(path([2.0, 1.5, 1.75, 1.3, 1.5, 1.1, 1.25, 1.15, 1.6, 1.4, 1.8, 1.6, 2.0]))
    t = [e["type"] for e in m["events"] if e["type"].startswith(("BOS", "CHoCH"))]
    i = t.index("CHoCH_UP")
    assert t[i + 1] == "BOS_UP" and m["events"][[e["type"] for e in m["events"]].index("CHoCH_UP")]["prev"] == "DOWN"
    conf = next(e for e in m["events"] if e["type"] == "BOS_UP")
    assert conf["confirmation"] and m["state"]["trend"] == "UP"


def test_wick_puncture_of_protected_with_return():
    s = path([2.0, 1.5, 1.75, 1.3, 1.5, 1.1, 1.45, 1.0], extra={61: (lambda h, l, c: (1.52, l, c))})
    m = major(s)
    ch = next(e for e in m["events"] if e["type"] == "CHoCH_UP")
    assert ch["sweep"] and ch["violation"] and ch["trigger"] == "high" and ch["trigger_price"] > ch["level"] > ch["close"]
    after = [e for e in m["events"] if e["idx"] > ch["idx"]]
    assert after and after[0]["type"] == "CHoCH_DOWN" and after[0]["restore"]      # новый LL → возврат к DOWN
    assert m["state"]["trend"] == "DOWN" and not any(e["type"] == "BOS_UP" for e in m["events"])


def test_consecutive_bos_without_pullback_and_protected_history():
    m = major(path([1.0, 1.5, 1.3, 1.8, 1.6, 2.6]))
    bos = [e for e in m["events"] if e["type"] == "BOS_UP"]
    assert len(bos) == 2 and bos[1]["idx"] - bos[0]["idx"] > 5                    # нет BOS на каждой свече импульса
    pls = [p for p in m["protected_history"] if p["side"] == "L"]
    assert [p["price"] for p in pls] == sorted(p["price"] for p in pls) and all(p["reason"] for p in pls)


def test_ambiguous_candle_does_not_infer_direction():
    s = path([1.0, 1.5, 1.3, 1.8, 1.6, 2.2, 1.9, 2.0], extra={70: (lambda h, l, c: (2.5, 1.4, 1.95))})
    m = major(s)
    amb = next(e for e in m["events"] if e["type"] == "AMBIGUOUS")
    assert set(amb["levels"]) >= {"protected_low", "structural_high"}
    assert not any(e["type"] in ("BOS_UP", "CHoCH_DOWN") and e["idx"] == amb["idx"] for e in m["events"])
    st = S.Engine("1h").run(Series("1h", s.candles[:71])).major.states[-1]
    assert st["trend"] == "TRANSITION" and st["bias"] == ""


def test_sweep_of_one_level_recorded_once():
    s = path([2.0, 1.5, 1.75, 1.3, 1.7, 1.12, 1.29, 1.15, 1.295, 1.14, 1.2, 0.9], n=10)
    m = major(s)
    keys = [(e["type"], round(e["level"], 6)) for e in m["events"] if e["type"].startswith("SWEEP")]
    assert len(keys) == len(set(keys))


def test_gaps_are_flagged_not_filled():
    g = path([2.0, 1.5, 1.75, 1.3, 1.5, 1.1, 1.25, 0.95])
    ks = g.candles[:30] + [Candle(k.ts + 7200, k.o, k.h, k.l, k.c, k.v) for k in g.candles[30:]]
    r = S.analyze(Series("1h", ks))
    assert r["gaps"] == [{"idx": 30, "ts": ks[30].ts}]
    assert all(e["gap"] == (e["idx"] == 30) for e in r["major"]["events"])


def test_nested_waves_initial_protected_and_scale_conflict():
    ok_init, flagged, true_flagged = 0, 0, 0
    for seed in range(1, 11):
        s, mk = E.nested(seed)
        e = S.Engine("1h").run(s)
        ph0 = next(p for p in e.major.prot_hist if p["side"] == "H")
        ok_init += abs(ph0["price"] / mk["H1"] - 1) < 0.03
        flagged += sum(1 for x in e.major.events if x["type"] == "CHoCH_UP" and x.get("scale_conflict"))
    for seed in range(1, 11):
        s, mk = E.nested(seed, reversal=True)
        e = S.Engine("1h").run(s)
        h = s.col("h")
        cross = next(i for i in range(mk["rev_bar"], len(h)) if h[i] > mk["tops"][-1])
        true_flagged += sum(1 for x in e.major.events if x["type"] == "CHoCH_UP" and x["idx"] >= cross and x.get("scale_conflict"))
    assert ok_init == 10                    # S1: начальный protected = первичный LH, а не внутренний
    assert flagged >= 3                     # локальные CHoCH против старшего масштаба помечены
    assert true_flagged == 0                # настоящий разворот (проходит старшую вершину) не помечен


# ------------------------------------------------------------------ FABLES
def test_fables_major_down_with_protected_high_07975(fab):
    e = S.Engine("1h").run(fab)
    r = e.result()
    m = r["major"]
    assert m["state"]["trend"] == "DOWN" and m["state"]["ph"] == pytest.approx(0.0797450739, rel=1e-6)
    bos = [x for x in m["events"] if x["type"].startswith(("BOS", "CHoCH"))]
    assert [(x["type"], x["ts"]) for x in bos] == [("BOS_DOWN", ts("2026-10-24T00:00"))]
    assert bos[0]["level"] == pytest.approx(0.051258, rel=1e-4) and bos[0]["initial"]
    p = m["protected_history"][0]
    assert p["src_ts"] == ts("2026-10-21T23:00") and p["set_ts"] == ts("2026-10-24T01:00")
    tiers = {round(w["price"], 5): w["tier"] for w in m["swings"]}
    assert tiers[0.0644] == "intermediate" and tiers[0.05398] == "intermediate" and tiers[0.07975] == "major"
    assert max(fab.col("h")[30:]) < 0.0797450739                              # восстановление до 0.06905 ниже protected
    assert any("0.0797451" in c for c in r["conditions"])
    assert r["scale_conflict_now"] is None and r["senior"]["state"]["trend"] == "DOWN"


def test_internal_structure_is_separate_from_major(fab):
    r = S.analyze(fab)
    assert r["internal"]["events"] and all(e["scope"] == "internal" for e in r["internal"]["events"])
    assert any(e["type"] == "iCHoCH_UP" for e in r["internal"]["events"]) and r["major"]["state"]["trend"] == "DOWN"


# ------------------------------------------------------------------ интеграция
def test_terminal_payload_and_htf_undefined_gives_no_confirmation(tmp_path):
    from fastapi.testclient import TestClient
    from tradinglab.web.app import create_app
    c = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8770")
    d = c.get("/api/terminal/1h?mode=BACKTEST").json()["structure_v3"]
    assert d["version"] == S.VERSION and d["major"]["state"]["trend"] == "DOWN"
    assert d["htf"]["tf"] == "4h" and not d["htf"]["conflict"] and "не определён" in d["htf"]["text"]
    for k in ("timeline", "range", "scale_conflicts", "conditions", "senior", "internal"):
        assert k in d


def test_trading_loop_does_not_use_structure_v3():
    root = ROOT / "tradinglab"
    for f in ("strategies.py", "engine.py", "risk.py", "forward.py", "backtest.py", "analysis.py", "live/worker.py"):
        assert "structure_v3" not in (root / f).read_text(), f
