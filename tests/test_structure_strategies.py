"""Стратегии на structure-v3 (исследование): без look-ahead, исполнение не раньше сигнала, издержки на плановый размер,
цель впереди цены, действующие S1/S2/S3 не тронуты."""
import pytest

from tradinglab.analysis import analyze
from tradinglab.candles import TF_SEC, Series
from tradinglab.execution import CONSERVATIVE_V1
from tradinglab.research import data, structure_strategies as SS
from tradinglab.research.strategy_audit import htf_index, run_engine
from tradinglab.strategies import STRATEGIES, Ctx


@pytest.mark.parametrize("name", list(SS.STRATS))
def test_no_look_ahead_decisions_equal_on_truncated_history(name):
    meta, fn = SS.STRATS[name]
    s, h = data.binance("SOLUSDT", "4h"), data.binance("SOLUSDT", "1d")
    s = Series("4h", s.candles[:900])
    full, hf = analyze(s), analyze(h)
    jj = htf_index(full, hf)
    cost = data.scaled_cost(CONSERVATIVE_V1, 0.25)
    for i in range(200, 900, 97):
        part = analyze(Series("4h", s.candles[:i + 1]))
        hp = analyze(Series("1d", [k for k in h.candles if k.ts + 86400 <= s.candles[i].ts + 14400]))
        d1 = fn(Ctx(full, i, hf, jj[i], cost, False))
        d2 = fn(Ctx(part, i, hp, len(hp.s) - 1 if len(hp.s) else None, cost, False))
        assert (d1["action"], [c["ok"] for c in d1["conditions"]]) == (d2["action"], [c["ok"] for c in d2["conditions"]]), (name, i)


def test_execution_never_before_signal_and_target_ahead():
    meta, fn = SS.STRATS["P2-reversal"]
    hits = 0
    for sym in ("ETHUSDT", "DOGEUSDT", "BTCUSDT"):
        s, h = data.binance(sym, "1h"), data.binance(sym, "4h")
        a, ha = analyze(s), analyze(h)
        t = run_engine(a, "1h", ha, fn, meta, cost=data.scaled_cost(CONSERVATIVE_V1, 0.25), risk_cfg=SS.risk_cfg("1h"))
        enters = [d for d in t.decisions if d["action_final"] == "ENTER"]
        for o in [o for o in t.ops if o["side"] == "BUY"]:
            sig = max(d["bar_ts"] for d in enters if d["bar_ts"] < o["ts"])
            assert o["ts"] == sig + TF_SEC["1h"]
            hits += 1
        for d in enters:
            assert d["plan"]["t1"] > d["plan"]["entry_ref"] > d["plan"]["stop"] and d["plan"]["rr_net"] >= SS.MIN_RR
    assert hits > 0


def test_net_rr_includes_fees_impact_slippage_and_gas():
    rr0, usd0, rt0 = SS.net_rr("1h", 1.0, 0.95, 1.10, data.scaled_cost(CONSERVATIVE_V1, 0.0))
    rr1, usd1, rt1 = SS.net_rr("1h", 1.0, 0.95, 1.10, CONSERVATIVE_V1)
    assert rr0 == pytest.approx(2.0, rel=0.01) and rt0 == pytest.approx(0.0, abs=0.01)
    assert rr1 < rr0 and rt1 > 2.0                      # круг издержек FABLES > 2% (комиссии 1,5% + проскальзывание + импакт)
    assert SS.net_rr("1h", 1.0, 0.95, 0.99, CONSERVATIVE_V1)[0] is None    # цель позади цены — не план


def test_live_strategies_untouched_and_research_not_registered():
    assert {tf: (s["id"], s["version"]) for tf, (s, _) in STRATEGIES.items()} == {
        "1h": ("S1-1H-sweep-reversal", "1.0.0"), "4h": ("S2-4H-structure-pullback", "1.0.0"), "1d": ("S3-1D-range-breakout", "1.0.0")}
    import pathlib
    root = pathlib.Path(SS.__file__).parents[1]
    for f in ("strategies.py", "engine.py", "risk.py", "forward.py", "backtest.py", "live/worker.py"):
        txt = (root / f).read_text()
        assert "structure_strategies" not in txt and "structure_v3" not in txt, f
