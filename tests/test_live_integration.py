"""Интеграционные тесты FORWARD на воспроизведении записанного потока реальных данных (tests/replay.py).

Покрывают контрольные проверки ТЗ этапа E: решения трёх стратегий на реальных данных, отсутствие повторной обработки,
исполнение не раньше сигнала, аварии источников без ложных свечей и сделок, стоп между закрытиями свечей, изоляция
портфелей, рестарт, разделение FORWARD/BACKTEST, издержки, PAUSE/STOP/EMERGENCY/CLOSE, автономная работа.
"""
import threading
from decimal import Decimal

import pytest

from replay import H, Clock, Market, run, ts
from tradinglab import strategies as S
from tradinglab.forward import ControlError
from tradinglab.live import config as C
from tradinglab.live.worker import Worker
from tradinglab.store import Store


def setup(tmp_path, start="2026-11-04T20:05:00Z"):
    clock = Clock(ts(start))
    m = Market(clock)
    st = Store(tmp_path / "lab.db", durable=False)
    w = Worker(st, m.transport(), clock)
    w.tick(clock.t)                       # первый цикл: загрузка истории (backfill), котировки
    return clock, m, st, w


def started(tmp_path, start="2026-11-04T20:05:00Z"):
    clock, m, st, w = setup(tmp_path, start)
    w.runner.start()
    clock.t += 60
    return clock, m, st, w


def enter_once(stop_frac=0.9, t1_frac=1.5):
    """Стратегия-заглушка для сценариев исполнения: один вход на первой живой свече."""
    done = {"v": False}

    def fn(ctx):
        if ctx.in_position:
            return S.decision(S.S1, ctx, "HOLD", [S.C("stub", True, "")], None, {}, "")
        if not done["v"]:
            done["v"] = True
            c = ctx.a.s.candles[ctx.i].c
            plan = {"entry_ref": c, "stop": c * stop_frac, "t1": c * t1_frac, "t1_frac": 0.5, "trail_atr": 2.0, "trail_ref": "close",
                    "time_stop": 10_000, "rr_net": S.rr_net(c, c * stop_frac, c * t1_frac, ctx.cost)}
            return S.decision(S.S1, ctx, "ENTER", [S.C("stub", True, "тест", "1", "1")], plan, {}, "")
        return S.decision(S.S1, ctx, "WAIT", [S.C("stub", False, "", "0", "1")], None, {}, "")
    return fn


def with_position(tmp_path, tf="1h", start="2026-11-04T20:05:00Z", until="2026-11-04T21:10:00Z", **kw):
    clock, m, st, w = started(tmp_path, start)
    w.runner.traders[tf].fn = enter_once(**kw)
    run(w, clock, ts(until))
    t = w.runner.traders[tf]
    assert t.position is not None, "позиция заглушки должна открыться"
    return clock, m, st, w, t


def fills(st, w, tf):
    return st.fills(w.runner.experiment_id, tf)


# ====================================================================== 1. реальные данные, три независимых трейдера
def test_three_strategies_decide_independently_on_real_stream(tmp_path):
    clock, m, st, w = started(tmp_path)
    run(w, clock, ts("2026-11-05T04:59:00Z"))
    exp = w.runner.experiment_id
    dec = {tf: st.decisions(exp, tf, 1000) for tf in ("1h", "4h", "1d")}
    assert len(dec["1h"]) == 8                          # 21:00…04:00 — каждый закрытый час
    assert len(dec["4h"]) == 2                          # закрытия 00:00 и 04:00
    assert len(dec["1d"]) == 1 and dec["1d"][0]["action_final"] == "WARMUP"   # 16 дневных свечей < 30: честный WARMUP
    for tf, ds in dec.items():
        for d in ds:
            assert d["strategy"] == S.STRATEGIES[tf][0]["id"] and d["mode"] == "FORWARD"
            assert d["decided_at"] <= d["received_ts"] <= d["decided_ts"]       # решение — после закрытия и получения свечи
            assert d["received_ts"] - d["decided_at"] <= C.LATE_LIMIT[tf]
            for c in d["conditions"]:
                assert c["result"] in ("PASS", "FAIL", "N/A") and "value" in c and "req" in c
    # решения сделаны по реальным записанным свечам
    s, _ = w.collector.db.candles("1h", clock.t)
    rec = {r[0]: r for r in m.rows["1h"]}
    assert all(abs(k.c - rec[k.ts][4]) < 1e-15 for k in s.candles)
    assert len({id(t.portfolio) for t in w.runner.traders.values()}) == 3


# ====================================================================== 2. повторная обработка и рестарт
def test_same_candle_not_reprocessed_and_restart_restores(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    exp = w.runner.experiment_id
    n_dec, n_fill = len(st.decisions(exp, "1h", 1000)), len(fills(st, w, "1h"))
    cash = t.portfolio.cash
    w.tick(clock.t)
    w.tick(clock.t)                                     # тот же момент дважды
    w2 = Worker(st, m.transport(), clock)               # «перезапуск процесса»
    t2 = w2.runner.traders["1h"]
    assert w2.runner.status == "RUNNING" and t2.portfolio.cash == cash and t2.position is not None
    w2.tick(clock.t)
    assert len(st.decisions(exp, "1h", 1000)) == n_dec and len(fills(st, w2, "1h")) == n_fill
    run(w2, clock, clock.t + 2 * H)
    ids = [f["op_id"] for f in fills(st, w2, "1h")]
    assert len(ids) == len(set(ids))
    assert len(st.decisions(exp, "1h", 1000)) == n_dec + 2


# ====================================================================== 3. исполнение не раньше сигнала, по живой котировке
def test_execution_after_signal_at_live_quote(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    buy = fills(st, w, "1h")[0]
    assert buy["side"] == "BUY"
    assert buy["signal_ts"] <= buy["received_ts"] <= buy["decided_ts"] < buy["exec_ts"]
    assert abs(float(buy["mid"]) - m.price(buy["exec_ts"])) < 1e-9          # цена пула в момент исполнения, не close свечи
    assert buy["expected_price"] != float(buy["mid"])
    assert buy["exec_model"] == "v4-clmm-active-range-estimate" and buy["fee_ppm"] == 5000
    assert "StateView" in buy["quote_source"]


# ====================================================================== 4. аварии источников: нет ложных свечей и сделок
def test_source_outage_creates_no_candles_or_executions_and_late_bars_are_skipped(tmp_path):
    clock, m, st, w = started(tmp_path, "2026-11-04T20:05:00Z")
    w.runner.traders["1h"].fn = enter_once()
    a, b = ts("2026-11-04T20:10:00Z"), ts("2026-11-05T00:10:00Z")
    m.down = [(src, a, b) for src in ("geckoterminal", "dexscreener", "rpc", "coingecko")]
    before = w.collector.db.count("1h")
    run(w, clock, b)
    assert w.collector.db.count("1h") == before                 # за время аварии ни одной свечи не появилось
    assert fills(st, w, "1h") == []                             # и ни одного исполнения
    assert w.runner.quote["valid"] is False
    run(w, clock, ts("2026-11-05T01:10:00Z"))
    exp = w.runner.experiment_id
    decs = {d["bar_ts"]: d for d in st.decisions(exp, "1h", 1000)}
    assert decs[ts("2026-11-04T21:00:00Z")]["action_final"] == "SKIPPED"      # получена через ~3 ч после закрытия
    assert decs[ts("2026-11-05T00:00:00Z")]["action_final"] != "SKIPPED"      # закрылась после восстановления — обычное решение
    rec = {r[0]: r for r in m.rows["1h"]}
    s, _ = w.collector.db.candles("1h", clock.t)
    assert all(k.ts in rec for k in s.candles)                  # ничего не придумано
    for f in fills(st, w, "1h"):
        assert f["exec_ts"] >= b                                # нет исполнения задним числом
    kinds = {e["kind"] for e in w.collector.db.events(500)}
    assert "source_error" in kinds


def test_missing_candle_is_not_filled_and_blocks_entries(tmp_path):
    clock, m, st, w = started(tmp_path)
    gap = ts("2026-11-04T21:00:00Z")
    m.missing.add(("1h", gap))
    run(w, clock, ts("2026-11-04T22:10:00Z"))                   # пропуск становится известен с приходом свечи 22:00
    w.runner.traders["1h"].fn = enter_once()                    # сигнал входа — уже после пропуска
    run(w, clock, ts("2026-11-05T01:10:00Z"))
    s, _ = w.collector.db.candles("1h", clock.t)
    assert gap not in s.ts and any(g["after_ts"] == gap - H for g in s.gaps)
    assert fills(st, w, "1h") == []                             # окно с пропуском → DATA_ERROR, входа нет
    last = st.decisions(w.runner.experiment_id, "1h", 1)[0]
    assert last["action_final"] == "NO_TRADE" and w.runner.traders["1h"].state == "DATA_ERROR"
    assert any(e["kind"] == "candle_gap" for e in w.collector.db.events(100))


# ====================================================================== 5. стоп между закрытиями свечей (1D)
def test_daily_trader_stop_is_checked_between_daily_closes(tmp_path):
    clock, m, st, w, t = with_position(tmp_path, "1d", start="2026-11-04T23:55:00Z", until="2026-11-05T00:30:00Z")
    stop = t.position.stop
    crash = ts("2026-11-05T06:00:00Z")
    m.price_override = [(crash, crash + 10 * H, stop * 0.95)]
    run(w, clock, crash + 5 * 60)
    assert t.position is None                                   # не ждал следующего дневного закрытия (00:00)
    sell = [f for f in fills(st, w, "1d") if f["side"] == "SELL"][0]
    assert crash <= sell["exec_ts"] <= crash + 180              # первая проверенная котировка после падения (с подтверждением скачка)
    assert Decimal(sell["price"]) < Decimal(str(stop))          # исполнено хуже уровня стопа, с проскальзыванием
    assert "стоп" in sell["reason"]


def test_unreliable_quote_does_not_fake_exit(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    stop = t.position.stop
    x = clock.t + 120
    m.price_override = [(x, x + 10 * H, stop * 0.97)]
    m.ds_mult = [(x - 120, x + 600, 1.06)]                      # DexScreener расходится с пулом на 6%
    run(w, clock, x + 540)
    assert t.position is not None and fills(st, w, "1h")[-1]["side"] == "BUY"   # выход не «имитирован»
    assert "1h" in w.runner.risk.get("unprotected", []) and w.runner.risk["entry_blockers"]
    run(w, clock, x + 900)                                      # источники снова согласованы
    assert t.position is None and "стоп" in fills(st, w, "1h")[-1]["reason"]


def test_single_tick_spike_requires_confirmation(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    stop = t.position.stop
    x = (clock.t // 60 + 2) * 60
    m.price_override = [(x, x + 60, stop * 0.5)]                # один тик −50%, затем цена вернулась
    run(w, clock, x + 600)
    assert t.position is not None                               # одиночный выброс не исполнил стоп
    m.price_override = [(clock.t, clock.t + 10 * H, stop * 0.6)]
    run(w, clock, clock.t + 300)
    assert t.position is None                                   # подтверждённое падение — стоп исполнен


# ====================================================================== 6. офлайн: не догонять сделками
def test_offline_period_is_not_traded_retroactively(tmp_path):
    clock, m, st, w = started(tmp_path)
    w.runner.traders["1h"].fn = enter_once()
    run(w, clock, ts("2026-11-04T20:30:00Z"))
    off_from = clock.t
    clock.t = ts("2026-11-05T03:40:00Z")                        # компьютер спал 7 часов
    w2 = Worker(st, m.transport(), clock)
    w2.runner.traders["1h"].fn = enter_once()
    run(w2, clock, ts("2026-11-05T04:30:00Z"))
    exp = w2.runner.experiment_id
    decs = st.decisions(exp, "1h", 1000)
    skipped = [d for d in decs if d["action_final"] == "SKIPPED"]
    assert len(skipped) >= 5                                    # 21:00…02:00 получены поздно → без решений
    for f in fills(st, w2, "1h"):
        assert f["exec_ts"] > clock.t - 3600 and f["exec_ts"] > off_from + 6 * H


# ====================================================================== 7. издержки и изоляция
def test_costs_reduce_capital_and_portfolios_isolated(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    w.runner.close_position("1h")
    run(w, clock, clock.t + 180)
    t = w.runner.traders["1h"]
    assert t.position is None and len(fills(st, w, "1h")) == 2
    b, s_ = fills(st, w, "1h")
    costs = sum(Decimal(f[k]) for f in (b, s_) for k in ("fee_usd", "impact_usd", "slippage_usd", "gas_usd"))
    assert costs > 0 and s_["fee_ppm"] == 10000 and b["fee_ppm"] == 5000
    trade = st.trades(w.runner.experiment_id, "1h")[0]
    assert Decimal(trade["net_pnl"]) == t.portfolio.cash - Decimal("1000")
    for tf in ("4h", "1d"):
        assert w.runner.traders[tf].portfolio.cash == Decimal("1000") and fills(st, w, tf) == []


# ====================================================================== 8. PAUSE / STOP / EMERGENCY / CLOSE
def test_pause_blocks_entries_but_keeps_stop_protection(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    w.runner.traders["4h"].fn = enter_once()
    w.runner.pause()
    run(w, clock, ts("2026-11-05T00:10:00Z"))
    assert fills(st, w, "4h") == []                             # 4H-сигнал на закрытии 00:00 при паузе — без входа
    stop = t.position.stop
    m.price_override = [(clock.t, clock.t + 10 * H, stop * 0.9)]
    run(w, clock, clock.t + 300)
    assert t.position is None and "стоп" in fills(st, w, "1h")[-1]["reason"]   # защита на паузе работает


def test_stop_does_not_sell_and_freezes_experiment(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    qty = t.portfolio.qty
    info = w.runner.stop()
    assert info["status"] == "STOPPED" and t.position is not None and t.portfolio.qty == qty
    stop = t.position.stop
    m.price_override = [(clock.t, clock.t + 10 * H, stop * 0.8)]
    run(w, clock, clock.t + 2 * H)
    assert t.position is not None and fills(st, w, "1h")[-1]["side"] == "BUY"   # STOP не продаёт, стопы не исполняются
    exp = w.runner.experiment_id
    n = len(st.decisions(exp, "1h", 1000))
    run(w, clock, clock.t + 2 * H)
    assert len(st.decisions(exp, "1h", 1000)) == n              # новых решений нет
    assert "CLOSE_POSITION" in w.runner.allowed() and "START" in w.runner.allowed()
    w.runner.close_position("1h")                               # ручная продажа после STOP
    run(w, clock, clock.t + 180)
    assert t.position is None and "CLOSE POSITION" in fills(st, w, "1h")[-1]["reason"]
    notes = [c["note"] for c in st.controls()]
    assert any("автоматической продажи нет" in x for x in notes)


def test_emergency_exit_closes_all_and_pauses(tmp_path):
    clock, m, st, w, t = with_position(tmp_path)
    with pytest.raises(ControlError):
        w.runner.close_position("4h")                           # у 4H позиции нет
    info = w.runner.emergency_exit()
    assert info["status"] == "PAUSED"
    run(w, clock, clock.t + 180)
    assert all(tr.position is None for tr in w.runner.traders.values())
    assert "EMERGENCY EXIT" in fills(st, w, "1h")[-1]["reason"]
    cmds = [c["command"] for c in st.controls()][::-1]
    assert cmds == ["START", "EMERGENCY_EXIT"]


def test_commands_via_queue_expire_and_restart_restores_status(tmp_path):
    clock, m, st, w = setup(tmp_path)
    old = w.db.enqueue(clock.t - 120, "START")                  # устаревшая команда не исполняется
    w.tick(clock.t)
    assert w.db.command(old)["status"] == "EXPIRED" and w.runner.status == "NOT_STARTED"
    cid = w.db.enqueue(clock.t, "START")
    w.tick(clock.t)
    assert w.db.command(cid)["status"] == "DONE" and w.runner.status == "RUNNING"
    bad = w.db.enqueue(clock.t, "RESUME")
    w.tick(clock.t)
    assert w.db.command(bad)["status"] == "ERROR"
    w.db.enqueue(clock.t, "PAUSE")
    w.tick(clock.t)
    w2 = Worker(st, m.transport(), clock)
    assert w2.runner.status == "PAUSED" and all(t.paused for t in w2.runner.traders.values())


# ====================================================================== 9. FORWARD ≠ BACKTEST, синхронная оценка
def test_forward_separate_from_backtest_and_marks_are_synchronous(tmp_path):
    from tradinglab import backtest, dataset
    clock, m, st, w = started(tmp_path)
    run(w, clock, clock.t + 3 * H)
    exp = st.experiment(w.runner.experiment_id)
    assert exp["mode"] == "FORWARD" and exp["started_at"] == ts("2026-11-04T20:05:00Z")
    ds = dataset.load()
    bt = backtest.run(ds["series"], store=st)
    assert st.experiment(bt["experiment"])["mode"] == "BACKTEST"
    assert all(d["mode"] == "FORWARD" for tf in ("1h", "4h", "1d") for d in st.decisions(w.runner.experiment_id, tf, 1000))
    marks = w.db.marks(w.runner.experiment_id)
    assert len(marks) >= 30
    for mk in marks:                                            # общий момент и одна котировка для всех портфелей и B&H
        assert set(mk["traders"]) == {"1h", "4h", "1d"} and "bh" in mk
        assert abs(mk["total"] - sum(v["equity"] for v in mk["traders"].values())) < 1e-6


# ====================================================================== 10. автономная работа без интерфейса
def test_worker_runs_unattended_in_background_thread(tmp_path):
    clock, m, st, w = setup(tmp_path)
    stop = threading.Event()
    th = threading.Thread(target=w.run_forever, args=(stop, 0.01), daemon=True)
    th.start()
    for _ in range(50):
        clock.t += 60
        stop.wait(0.01)
    stop.set()
    th.join(5)
    hb, _ = w.db.kv("worker")
    assert not th.is_alive() and hb["status"] == "stopped" and hb["ticks"] > 5
