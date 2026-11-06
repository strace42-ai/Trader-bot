"""Исследование стратегий: воронка, разбор R:R, альтернативы без look-ahead, исполнение не раньше сигнала, S1/S2/S3 не тронуты."""
import datetime as dt

import pytest

from tradinglab.analysis import analyze
from tradinglab.candles import TF_SEC, Series
from tradinglab.execution import CONSERVATIVE_V1
from tradinglab.research import alt_strategies as AL
from tradinglab.research import data, strategy_audit as A
from tradinglab.research.structure_compare import synthetic
from tradinglab.strategies import ANALYSIS_PARAMS, STRATEGIES, Ctx

ts = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC).timestamp())  # noqa: E731


@pytest.fixture(scope="module")
def fab():
    f = data.fables()
    return {"s2": A.audit_s2(f["4h"]), "s1": A.audit_s1(f["1h"], f["4h"])}


def test_funnel_is_monotone_and_consistent(fab):
    for key in ("s1", "s2"):
        fun = fab[key]["funnel"]
        rem = [x["remain"] for x in fun]
        assert rem == sorted(rem, reverse=True) and rem[0] == len(fab[key]["rows"])
        for a, b in zip(fun, fun[1:]):
            assert b["entered"] == a["remain"]


def test_s2_fables_candidates_fail_on_old_high_target(fab):
    """10-28 00:00/16:00/20:00 UTC: все условия S2, кроме R:R, выполнены, но close уже выше T1 = старого максимума."""
    rows = {r["ts"]: r for r in fab["s2"]["rows"]}
    for t in ("2026-10-28T00:00", "2026-10-28T16:00", "2026-10-28T20:00"):
        r = rows[ts(t)]
        assert r["base"] and r["candle"] and r["structure"] and r["macd_up"] and r["adx"] and r["cmf"]
        assert r["rr_cat"] == "entry_ge_T1" and r["t1_atr"] < 0 and r["rr_ok"] is False
    assert fab["s2"]["enters"] == 0


def test_s1_fables_entries_blocked_by_risk_rr_to_nearest_target(fab):
    """S1 сама выдала ENTER 2026-11-01 07:00 и 08:00, но риск-слой требует R:R ≥ 2 до БЛИЖАЙШЕЙ цели (≈0,3)."""
    we = [r for r in fab["s1"]["rows"] if r["action"] == "ENTER"]
    assert [r["ts"] for r in we] == [ts("2026-11-01T07:00"), ts("2026-11-01T08:00")]
    assert all(r["rr_t1"] < 0.5 and r["t1_atr"] < r["stop_atr"] for r in we)
    assert fab["s1"]["enters"] == 0


@pytest.mark.parametrize("name", [n for n in AL.ALTS if n != "S1-diag"])
def test_alternatives_have_no_look_ahead(name):
    meta, fn = AL.ALTS[name]
    s = synthetic("regime", n=500, seed=3, tf="4h")
    full = analyze(s, ANALYSIS_PARAMS["4h"])
    for i in range(120, 500, 23):
        part = analyze(Series("4h", s.candles[:i + 1]), ANALYSIS_PARAMS["4h"])
        d1 = fn(Ctx(full, i, None, None, CONSERVATIVE_V1, False))
        d2 = fn(Ctx(part, i, None, None, CONSERVATIVE_V1, False))
        assert (d1["action"], [c["ok"] for c in d1["conditions"]]) == (d2["action"], [c["ok"] for c in d2["conditions"]]), (name, i)


@pytest.mark.parametrize("name", ["B-continuation", "A-reversal", "S2-diag"])
def test_execution_never_before_signal(name):
    meta, fn = AL.ALTS[name]
    s = synthetic("regime", n=900, seed=3, tf="4h") if name == "A-reversal" else synthetic("trend", n=900, seed=1, tf="4h")
    a = analyze(s, ANALYSIS_PARAMS["4h"])
    t = A.run_engine(a, "4h", None, fn, meta, cost=data.scaled_cost(CONSERVATIVE_V1, 0.2), risk_cfg=AL.risk_cfg("4h"))
    enters = {d["decision_id"]: d["bar_ts"] for d in t.decisions if d["action_final"] == "ENTER"}
    buys = [o for o in t.ops if o["side"] == "BUY"]
    assert buys
    for o in buys:
        sig = max(b for b in enters.values() if b < o["ts"])
        assert o["ts"] == sig + TF_SEC["4h"]                       # open следующей свечи после закрытия сигнального бара


def test_research_does_not_touch_live_strategies():
    ids = {tf: (st["id"], st["version"], dict(st["params"])) for tf, (st, _) in STRATEGIES.items()}
    assert ids["1h"][:2] == ("S1-1H-sweep-reversal", "1.0.0") and ids["4h"][:2] == ("S2-4H-structure-pullback", "1.0.0")
    assert all(m["id"] not in {v[0] for v in ids.values()} for m, _ in AL.ALTS.values())
    import pathlib
    root = pathlib.Path(A.__file__).parents[1]
    for f in ("strategies.py", "engine.py", "risk.py", "forward.py", "backtest.py", "live/worker.py"):
        assert "research" not in (root / f).read_text(), f


def test_cost_scaling():
    z = data.scaled_cost(CONSERVATIVE_V1, 0.0)
    assert z.buy_fee == z.sell_fee == z.slippage == 0
    d = data.scaled_cost(CONSERVATIVE_V1, 2.0)
    assert d.sell_fee == 2 * CONSERVATIVE_V1.sell_fee and d.depth_usd == CONSERVATIVE_V1.depth_usd / 2
