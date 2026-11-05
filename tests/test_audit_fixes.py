"""Регрессионные тесты по аудиту торгового ядра (4 дефекта)."""
from decimal import Decimal

import pytest

from tradinglab import analysis, backtest, candles, performance
from tradinglab.candles import Candle
from tradinglab.engine import Trader
from tradinglab.execution import CONSERVATIVE_V1 as M, liquidation_value
from tradinglab.store import Store

from test_engine import H, flat_series, real_series, stub


def rows(ks):
    return [[k.ts, k.o, k.h, k.l, k.c, k.v] for k in ks]


def trader_upto(s, fn, upto):
    a = analysis.analyze(s)
    tr = Trader("1h", "BACKTEST", "T", M)
    tr.fn = fn
    for i in range(upto):
        tr.process_bar(a, i)
    return tr, a


# ================================================================== 1. pending при PAUSE / данных / kill switch
def test_pending_entry_cancelled_after_pause():
    s = flat_series(60, extra=[(1.05, 1.06, 1.04, 1.05)] * 3)
    tr, a = trader_upto(s, stub(enter_at=59), 60)
    assert tr.pending["kind"] == "ENTER"
    tr.paused = True
    tr.process_bar(a, 60)
    assert not [o for o in tr.ops if o["side"] == "BUY"] and tr.pending is None
    ev = tr.events[-1]
    assert ev["type"] == "ENTRY_CANCELLED" and ev["policy"] == "pause"
    tr.process_bar(a, 61)                                       # на паузе новых входов нет
    assert tr.ops == [] and tr.state == "PAUSED"


def test_pending_entry_cancelled_on_kill_switch_and_global_stop():
    s = flat_series(60, extra=[(1.05, 1.06, 1.04, 1.05)] * 3)
    tr, a = trader_upto(s, stub(enter_at=59), 60)
    tr.risk.killed, tr.risk.kill_reason = True, "тест"
    tr.process_bar(a, 60)
    assert tr.ops == [] and tr.events[-1]["policy"] == "kill"
    tr2, a2 = trader_upto(s, stub(enter_at=59), 60)
    tr2.process_bar(a2, 60, global_kill=True)
    assert tr2.ops == [] and tr2.events[-1]["policy"] == "kill"


def test_pending_entry_cancelled_when_data_invalid_at_execution():
    ks = flat_series(60).candles + [Candle(62 * H, 1.05, 1.06, 1.04, 1.05, 1000.0), Candle(63 * H, 1.05, 1.06, 1.04, 1.05, 1000.0)]
    s = candles.normalize(rows(ks), "1h", now=10 ** 9)          # пропуск 60H, 61H сразу после бара решения
    tr, a = trader_upto(s, stub(enter_at=59), 60)
    assert tr.pending["kind"] == "ENTER"                         # на баре 59 пропуск ещё не известен
    tr.process_bar(a, 60)
    assert tr.ops == [] and tr.events[-1]["policy"] == "data_error" and tr.state == "DATA_ERROR"


def test_exit_still_executes_when_paused():
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0)] * 4)
    tr, a = trader_upto(s, stub(enter_at=59, exit_at=61), 62)
    assert tr.pending["kind"] == "EXIT"
    tr.paused = True
    tr.process_bar(a, 62)
    assert tr.position is None and [o for o in tr.ops if o["side"] == "SELL"]


def test_stop_protects_position_when_paused():
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0), (0.95, 0.96, 0.85, 0.9)])
    tr, a = trader_upto(s, stub(enter_at=59), 61)
    tr.paused = True
    tr.process_bar(a, 61)
    assert tr.position is None and "стоп" in tr.ops[-1]["reason"]


def test_exit_deferred_on_bad_price_then_executed():
    base = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0)] * 4).candles
    rs = rows(base) + [[62 * H, 9, 9, 9, 9, 9]]                 # конфликт значений на свече 62 → цена ненадёжна
    s = candles.normalize(rs, "1h", now=10 ** 9)
    tr, a = trader_upto(s, stub(enter_at=59, exit_at=61), 62)
    assert tr.pending["kind"] == "EXIT"
    tr.process_bar(a, 62)
    assert tr.position is not None and tr.pending["kind"] == "EXIT"
    assert tr.events[-1]["type"] in ("EXIT_DEFERRED", "STOP_CHECK_DEFERRED")
    assert any(e["type"] == "EXIT_DEFERRED" for e in tr.events)
    tr.process_bar(a, 63)
    assert tr.position is None and tr.ops[-1]["ts"] == 63 * H


def test_global_stop_closes_open_position_and_blocks_entries():
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0)] * 5)
    tr, a = trader_upto(s, stub(enter_at=59), 61)
    assert tr.position is not None
    tr.process_bar(a, 61, global_kill=True)
    assert tr.pending["kind"] == "EXIT" and "STOP" in tr.pending["reason"]
    tr.process_bar(a, 62, global_kill=True)
    assert tr.position is None and tr.state in ("RISK_OFF", "FLAT")
    tr.process_bar(a, 63, global_kill=True)
    assert len([o for o in tr.ops if o["side"] == "BUY"]) == 1


# ================================================================== 2. потеря событий при ошибке транзакции
def test_records_not_lost_when_commit_fails(tmp_path):
    st = Store(tmp_path / "lab.db")
    st.create_experiment("T", "BACKTEST", 0, {}, "h")
    s = flat_series(60, extra=[(1.05, 1.06, 1.04, 1.05)] * 2)
    a = analysis.analyze(s)
    tr = Trader("1h", "BACKTEST", "T", M)
    tr.fn = stub(enter_at=59)
    for i in range(60):
        tr.process_bar(a, i)
        st.commit_bar(tr)
    st.conn.execute("CREATE TRIGGER fail_eq BEFORE INSERT ON equity BEGIN SELECT RAISE(ABORT, 'disk error'); END")
    tr.process_bar(a, 60)                                         # бар с исполнением BUY
    with pytest.raises(Exception):
        st.commit_bar(tr)
    buf = tr.pending_records()
    assert len(buf["ops"]) == 1 and len(buf["decisions"]) == 1   # записи не потеряны
    assert st.fills("T", "1h") == []                              # и ничего не записано частично
    assert st.trader_state("T", "1h")["last_bar_ts"] == 59 * H
    st.conn.execute("DROP TRIGGER fail_eq")
    st.commit_bar(tr)                                             # повтор после восстановления
    assert [f["op_id"] for f in st.fills("T", "1h")] == [o["op_id"] for o in tr.ops]
    assert len(st.decisions("T", "1h", 10_000)) == len(tr.decisions)
    assert st.trader_state("T", "1h")["last_bar_ts"] == 60 * H
    assert tr.pending_records()["ops"] == []


# ================================================================== 3. look-ahead в проверке качества
def test_future_gap_does_not_block_past_decisions():
    ks = flat_series(60).candles + [Candle(62 * H, 1.0, 1.01, 0.99, 1.0, 1000.0)] + \
         [Candle((63 + j) * H, 1.0, 1.01, 0.99, 1.0, 1000.0) for j in range(3)]
    s = candles.normalize(rows(ks), "1h", now=10 ** 9)
    assert not s.quality()["ok"]                                   # для ряда целиком пропуск в окне последних 50
    assert s.quality_at(40)["ok"]                                  # но на баре 40 он ещё в будущем
    tr, a = trader_upto(s, stub(enter_at=40), 42)
    assert [o for o in tr.ops if o["side"] == "BUY"]               # раньше вход блокировался будущим пропуском
    assert a.snapshot(40)["quality"]["ok"] and not a.snapshot(len(s) - 1)["quality"]["ok"]


def test_quality_at_counts_only_past_conflicts():
    rs = rows(flat_series(60).candles) + [[55 * H, 2, 2, 2, 2, 2]]
    s = candles.normalize(rs, "1h", now=10 ** 9)
    assert s.quality_at(50)["conflicts_recent"] == 0 and s.quality_at(56)["conflicts_recent"] == 1


# ================================================================== 4. синхронная оценка по общей котировке
def test_all_portfolios_and_bh_valued_at_same_quote():
    S = real_series()
    r = backtest.run(S)
    mark = r["mark"]
    last1h = S["1h"].candles[-1]
    assert mark["ts"] == last1h.ts + H and mark["mid"] == last1h.c
    bh_vals = set()
    for tf, sm in r["summary"].items():
        t = r["traders"][tf]
        assert sm["mark"] == mark
        assert sm["equity"] == pytest.approx(float(t.portfolio.cash + liquidation_value(t.portfolio.qty, mark["mid"], M)))
        bh_vals.add(sm["bh_equity_same_time"])
    assert len(bh_vals) == 1                                       # 1D больше не сравнивается с B&H на другой момент
    v = r["valuation"]
    assert v["total"] == pytest.approx(sum(sm["equity"] for sm in r["summary"].values()))
    assert v["bh_total"] == pytest.approx(3 * bh_vals.pop())


def test_newer_external_quote_wins_older_ignored():
    S = real_series()
    last = S["1h"].candles[-1]
    newer = {"ts": last.ts + 2 * H, "mid": 0.07, "source": "snapshot"}
    older = {"ts": last.ts, "mid": 0.01, "source": "old"}
    assert backtest.run(S, quote=newer)["mark"]["source"] == "snapshot"
    assert backtest.run(S, quote=older)["mark"]["mid"] == last.c


def test_open_position_valued_at_common_quote_not_own_bar():
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0)] * 2)
    tr, a = trader_upto(s, stub(enter_at=59), 62)
    assert tr.position is not None
    mark = {"ts": 62 * H + H, "mid": 1.2, "source": "test"}
    sm = performance.summarize(tr, None, H, mark=mark)
    assert sm["equity"] == pytest.approx(float(tr.portfolio.cash + liquidation_value(tr.portfolio.qty, 1.2, M)))
    assert sm["unrealized_pnl"] > 0
    with pytest.raises(ValueError):
        performance.summarize(tr, None, H, mark={"ts": 10, "mid": 1.0, "source": "stale"})
