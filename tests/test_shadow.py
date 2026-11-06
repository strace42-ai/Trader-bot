"""SHADOW LAB (SHADOW-P2-4H 1.0.0): изоляция от основного бота, восстановление, идемпотентность, издержки, защита.

Сценарии — на воспроизведении записанного потока реальных данных FABLES (tests/replay.py): основной Worker пишет
trading_lab.db, процесс SHADOW читает её только на чтение и ведёт свою shadow_lab.db. Для проверок исполнения стратегия
трейдера заменяется заглушкой с одним входом (как в test_live_integration) — правила P2 при этом не меняются.
"""
import hashlib
import sqlite3
from decimal import Decimal

import pytest

from replay import H, Clock, Market, ts
from tradinglab import strategies as S
from tradinglab.live import config as C
from tradinglab.live.worker import Worker
from tradinglab.shadow import strategy as P2
from tradinglab.shadow.process import InstanceLock, ShadowProcess
from tradinglab.shadow.readonly import ReadOnlyError, ReadOnlyStore
from tradinglab.store import Store

START = "2026-11-04T20:05:00Z"


def main_bot(d, start=START):
    clock = Clock(ts(start))
    m = Market(clock)
    st = Store(d / "trading_lab.db", durable=False)
    w = Worker(st, m.transport(), clock)
    w.tick(clock.t)
    w.runner.start()
    return clock, m, st, w


def both(d, start=START):
    clock, m, st, w = main_bot(d, start)
    sp = ShadowProcess(d, clock, durable=False)
    sp.own.enqueue(clock.t, "START")
    sp.tick(clock.t)
    assert sp.runner.status == "RUNNING", sp.own.command(1)
    return clock, m, st, w, sp


def step(clock, w, sp, until, dt=60):
    while clock.t < until:
        clock.t += dt
        w.tick(clock.t)
        if sp is not None:
            sp.tick(clock.t)


def enter_once(stop_frac=0.9, t1_frac=1.5):
    done = {"v": False}

    def fn(ctx):
        if ctx.in_position:
            return S.decision(P2.STRATEGY, ctx, "HOLD", [S.C("stub", True, "")], None, {}, "")
        if not done["v"]:
            done["v"] = True
            c = ctx.a.s.candles[ctx.i].c
            plan = {"entry_ref": c, "stop": c * stop_frac, "t1": c * t1_frac, "t1_frac": 0.5, "trail_atr": 2.5, "trail_ref": "close",
                    "time_stop": 10_000, "rr_net": S.rr_net(c, c * stop_frac, c * t1_frac, ctx.cost)}
            return S.decision(P2.STRATEGY, ctx, "ENTER", [S.C("stub", True, "тест", "1", "1")], plan, {}, "")
        return S.decision(P2.STRATEGY, ctx, "WAIT", [S.C("stub", False, "", "0", "1")], None, {}, "")
    return fn


def with_position(d, **kw):
    clock, m, st, w, sp = both(d)
    sp.runner.traders["4h"].fn = enter_once(**kw)
    step(clock, w, sp, ts("2026-11-05T00:10:00Z"))            # первая закрытая 4H-свеча после START — 00:00
    t = sp.runner.traders["4h"]
    assert t.position is not None, "позиция заглушки должна открыться"
    return clock, m, st, w, sp, t


def fills(sp):
    return sp.store.fills(sp.runner.experiment_id, "4h")


def main_snapshot(st) -> dict:
    """Содержимое всех таблиц основной БД, кроме данных, которые основной бот сам пишет по часам (они сравниваются отдельно)."""
    out = {}
    for (name,) in st._all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        rows = st._all(f"SELECT * FROM {name}")
        out[name] = hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()
    return out


# ====================================================================== правила заморожены
def test_frozen_rules_match_research_p2_and_hash_is_recorded(tmp_path):
    from tradinglab.research import structure_strategies as SS
    assert P2.STRATEGY["id"] == "SHADOW-P2-4H" and P2.VERSION == "1.0.0"
    assert (P2.MIN_RR, P2.STOP_BUF, P2.STOP_MIN, P2.STOP_MAX, P2.T1_FRAC, P2.TRAIL_ATR, P2.TIME_STOP) == (1.5, 0.25, 0.5, 3.0, 0.5, 2.5, 40)
    assert P2.risk_cfg() == SS.risk_cfg("4h") and P2.risk_cfg().risk_per_trade == Decimal("0.010")
    clock, m, st, w, sp = both(tmp_path)
    fixed = __import__("json").loads(sp.store.experiment(sp.runner.experiment_id)["params_json"])["shadow"]
    assert fixed["rules_hash"] == P2.rules_hash() and fixed["version"] == "1.0.0" and fixed["initial"] == "1000"
    assert sp.runner.traders["4h"].strategy["id"] == "SHADOW-P2-4H"


def test_changed_rules_block_execution_until_new_experiment(tmp_path, monkeypatch):
    clock, m, st, w, sp = both(tmp_path)
    monkeypatch.setattr(P2, "rules_hash", lambda: "0" * 64)
    sp2 = ShadowProcess(tmp_path, clock, durable=False)
    assert not sp2.runner.version_ok and "не совпадает" in sp2.runner.version_issue
    assert sp2.runner.on_bars(clock.t + 10 * H) == 0
    sp2.own.enqueue(clock.t, "RESUME")
    assert sp2.process_commands(clock.t)[0]["status"] == "ERROR"


# ====================================================================== 1. восстановление после рестарта
def test_state_survives_restart(tmp_path):
    clock, m, st, w, sp, t = with_position(tmp_path)
    before = {"exp": sp.runner.experiment_id, "cash": t.portfolio.cash, "qty": t.portfolio.qty, "stop": t.position.stop,
              "last": t.last_bar_ts, "fills": len(fills(sp)), "dec": len(sp.store.decisions(sp.runner.experiment_id, "4h", 1000))}
    sp.close()
    sp2 = ShadowProcess(tmp_path, clock, durable=False)          # «перезапуск» процесса SHADOW
    t2 = sp2.runner.traders["4h"]
    assert sp2.runner.status == "RUNNING" and sp2.runner.experiment_id == before["exp"]
    assert (t2.portfolio.cash, t2.portfolio.qty, t2.position.stop, t2.last_bar_ts) == (before["cash"], before["qty"], before["stop"], before["last"])
    assert sp2.runner.integrity()["ok"]
    t2.fn = lambda ctx: pytest.fail("уже обработанная свеча не должна решаться повторно")
    assert sp2.runner.on_bars(clock.t) == 0
    step(clock, w, sp2, clock.t + 600)
    assert len(fills(sp2)) == before["fills"] and len(sp2.store.decisions(before["exp"], "4h", 1000)) == before["dec"]


def test_offline_shadow_does_not_trade_retroactively(tmp_path):
    clock, m, st, w, sp = both(tmp_path)
    sp.close()
    step(clock, w, None, ts("2026-11-05T04:30:00Z"))              # SHADOW выключен, основной бот работает и получает свечи вовремя
    back = clock.t
    sp2 = ShadowProcess(tmp_path, clock, durable=False)
    sp2.runner.traders["4h"].fn = enter_once()
    step(clock, w, sp2, ts("2026-11-05T04:40:00Z"))
    decs = {d["bar_ts"]: d for d in sp2.store.decisions(sp2.runner.experiment_id, "4h", 100)}
    late = decs[ts("2026-11-04T20:00:00Z")]                       # закрылась 00:00, SHADOW вернулся в 04:31 → только SKIPPED
    assert late["action_final"] == "SKIPPED" and "процесс SHADOW не работал" in late["explanation"]
    fresh = decs[ts("2026-11-05T00:00:00Z")]                      # закрылась 04:00 — в пределах окна, решение законное
    assert fresh["action_final"] == "ENTER" and fresh["decided_ts"] >= back
    assert all(f["ts"] > fresh["decided_ts"] for f in fills(sp2))  # исполнение — только после решения, по живой котировке


# ====================================================================== 2. без дублей
def test_no_duplicate_trades_or_commands(tmp_path):
    clock, m, st, w, sp, t = with_position(tmp_path)
    n = len(fills(sp))
    for _ in range(3):
        sp.tick(clock.t)                                           # тот же момент — повторная обработка
        assert sp.runner.on_bars(clock.t) == 0
    assert len(fills(sp)) == n == 1
    ids = [f["op_id"] for f in fills(sp)]
    assert len(set(ids)) == len(ids)
    with pytest.raises(sqlite3.IntegrityError):                    # повторная запись той же свечи отклоняется базой
        sp.store.conn.execute("INSERT INTO decisions SELECT * FROM decisions LIMIT 1")
    cid = sp.own.enqueue(clock.t, "PAUSE")
    sp.tick(clock.t)
    sp.tick(clock.t)
    assert sp.own.command(cid)["status"] == "DONE" and [c["command"] for c in sp.store.controls(5)].count("PAUSE") == 1
    old = sp.own.enqueue(clock.t - C.COMMAND_MAX_AGE - 1, "RESUME")
    sp.tick(clock.t)
    assert sp.own.command(old)["status"] == "EXPIRED" and sp.runner.status == "PAUSED"


def test_second_shadow_process_on_same_db_is_refused(tmp_path):
    a, b = InstanceLock(tmp_path / "shadow.lock"), InstanceLock(tmp_path / "shadow.lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


# ====================================================================== 3. издержки
def test_fees_impact_slippage_gas_are_charged_and_paper_labelled(tmp_path):
    clock, m, st, w, sp, t = with_position(tmp_path)
    sp.own.enqueue(clock.t, "CLOSE_POSITION")
    step(clock, w, sp, clock.t + 180)
    b, s_ = fills(sp)
    for f in (b, s_):
        assert Decimal(f["fee_usd"]) > 0 and Decimal(f["gas_usd"]) > 0 and Decimal(f["impact_usd"]) >= 0 and Decimal(f["slippage_usd"]) > 0
        assert "PAPER" in f["exec_note"] and "не ончейн-своп" in f["exec_note"]
    assert b["fee_ppm"] == 5000 and s_["fee_ppm"] == 10000            # фактическая комиссия hook из свопов пула
    tp = sp.runner.traders["4h"].portfolio
    assert tp.cash == Decimal("1000") - Decimal(b["usd"]) - Decimal(b["gas_usd"]) + Decimal(s_["usd"]) - Decimal(s_["gas_usd"])
    trade = sp.store.trades(sp.runner.experiment_id, "4h")[0]
    assert Decimal(trade["net_pnl"]) == tp.cash - Decimal("1000") and Decimal(trade["net_pnl"]) < 0
    assert sp.runner.integrity()["ok"]


# ====================================================================== 4. вход только по проверенной котировке
def test_entry_forbidden_on_invalid_quote(tmp_path):
    clock, m, st, w, sp = both(tmp_path)
    sp.runner.traders["4h"].fn = enter_once()
    m.rpc_fail += [(C.SEL_GET_SLOT0, ts("2026-11-04T23:30:00Z"), 10 ** 12), (C.SEL_GET_LIQUIDITY, ts("2026-11-04T23:30:00Z"), 10 ** 12)]
    step(clock, w, sp, ts("2026-11-05T01:00:00Z"))
    assert fills(sp) == []
    r = sp.runner.risk
    assert r["quote_level"] == "OFFCHAIN_ONLY" and not r["quote_valid"]
    assert any("нет подтверждённой ончейн-котировки" in b for b in r["entry_blockers"])
    ev = [e for e in sp.store.events(sp.runner.experiment_id, "4h") if e["type"] == "ENTRY_CANCELLED"]
    assert ev and ev[-1]["policy"] == "entry_ttl"                    # вход отменён по TTL 45 мин, а не исполнен


def test_entry_forbidden_on_stale_quote_and_missing_ticks(tmp_path):
    clock, m, st, w, sp = both(tmp_path)
    sp.runner.traders["4h"].fn = enter_once()
    step(clock, w, sp, ts("2026-11-04T23:58:00Z"))
    w.collector.db.kv_set("pool_ticks", {"fetched_at": 0, "known": [0, 1], "ticks": {}}, clock.t)
    m.rpc_fail.append((C.SEL_GET_TICK_BITMAP, 0, 10 ** 12))
    step(clock, w, sp, ts("2026-11-05T00:20:00Z"))
    assert fills(sp) == [] and any("тиков пула" in b for b in sp.runner.risk["entry_blockers"])


# ====================================================================== 5. скачки рынка
def test_stop_executes_at_gap_price_after_confirmed_jump(tmp_path):
    clock, m, st, w, sp, t = with_position(tmp_path)
    stop = t.position.stop
    x = (clock.t // 60 + 2) * 60
    m.price_override = [(x, x + 60, stop * 0.5)]                     # одиночный выброс −50% — не исполняет стоп
    step(clock, w, sp, x + 600)
    assert t.position is not None
    m.price_override = [(clock.t, clock.t + 10 * H, stop * 0.7)]     # реальный гэп под стоп: подтверждён следующей котировкой
    step(clock, w, sp, clock.t + 300)
    assert t.position is None
    s_ = fills(sp)[-1]
    assert s_["side"] == "SELL" and "стоп" in s_["reason"]
    assert float(s_["price"]) < stop * 0.72                          # исполнено по рынку после гэпа, а не по уровню стопа


def test_stop_is_checked_between_4h_closes_on_each_quote(tmp_path):
    clock, m, st, w, sp, t = with_position(tmp_path)
    stop = t.position.stop
    x = (clock.t // 60 + 2) * 60
    m.price_override = [(x, x + 10 * H, stop * 0.97)]
    step(clock, w, sp, x + 300)
    assert t.position is None and (clock.t - x) < 4 * H                # не ждали закрытия 4H-свечи


# ====================================================================== 6. независимость основного FORWARD
def test_main_forward_unchanged_by_shadow(tmp_path):
    a, b = tmp_path / "with", tmp_path / "without"
    a.mkdir()
    b.mkdir()
    ca, ma, sa, wa, spa = both(a)
    spa.runner.traders["4h"].fn = enter_once()
    cb, mb, sb, wb = main_bot(b)
    step(ca, wa, spa, ts("2026-11-05T04:59:00Z"))
    step(cb, wb, None, ts("2026-11-05T04:59:00Z"))
    assert fills(spa)                                                # SHADOW торговал…
    ea, eb = wa.runner.experiment_id, wb.runner.experiment_id
    assert ea == eb                                                  # …а основной эксперимент — тот же самый
    for tf in ("1h", "4h", "1d"):
        assert [d["action_final"] for d in sa.decisions(ea, tf, 1000)] == [d["action_final"] for d in sb.decisions(eb, tf, 1000)]
        assert sa.fills(ea, tf) == sb.fills(eb, tf) and sa.trades(ea, tf) == sb.trades(eb, tf)
        assert sa.trader_state(ea, tf) == sb.trader_state(eb, tf)
    for tbl in ("decisions", "fills", "trades", "equity", "events", "controls", "experiments", "live_candles", "commands"):
        assert sa._one(f"SELECT COUNT(*) FROM {tbl}")[0] == sb._one(f"SELECT COUNT(*) FROM {tbl}")[0], tbl
    keys = lambda s: sorted(k for (k,) in s._all("SELECT key FROM kv"))  # noqa: E731
    assert keys(sa) == keys(sb)                                      # SHADOW не создал ни одного ключа в основной БД
    assert wa.db.marks(ea) == wb.db.marks(eb)                         # оценки основного FORWARD идентичны


def test_shadow_cannot_write_main_db(tmp_path):
    clock, m, st, w, sp = both(tmp_path)
    before = main_snapshot(st)
    ro = ReadOnlyStore(tmp_path / "trading_lab.db")
    with pytest.raises(ReadOnlyError):
        with ro.tx():
            pass
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("INSERT INTO kv VALUES('x','1',1)")
    with pytest.raises(ReadOnlyError):
        sp.runner.db.kv_set("gas", {}, clock.t)
    for _ in range(5):
        sp.tick(clock.t)                                             # SHADOW без хода времени основного бота
    assert main_snapshot(st) == before


def test_shadow_errors_do_not_stop_main_or_shadow_loop(tmp_path, monkeypatch):
    clock, m, st, w, sp = both(tmp_path)

    def boom(now=None):
        raise RuntimeError("сбой SHADOW")
    monkeypatch.setattr(sp.runner, "on_bars", boom)
    step(clock, w, sp, ts("2026-11-05T00:30:00Z"))
    assert any(e["where"] == "bars" for e in sp.errors)
    hb, _ = sp.own.kv("shadow_worker")
    assert hb["status"] == "running" and hb["ts"] == clock.t           # цикл SHADOW жив: котировки, оценки, heartbeat
    assert sp.own.marks(sp.runner.experiment_id)
    assert len(st.decisions(w.runner.experiment_id, "4h", 100)) == 1   # основной FORWARD принял решение по 00:00


def test_missing_main_db_waits_without_creating_it(tmp_path):
    clock = Clock(ts(START))
    sp = ShadowProcess(tmp_path, clock, durable=False)
    sp.tick(clock.t)
    assert sp.runner is None and not (tmp_path / "trading_lab.db").exists()
    assert sp.own.kv("shadow_worker")[0]["status"] == "waiting_main_db"


# ====================================================================== 7. $3 000 и история S1/S2/S3 не меняются
def test_main_capital_and_strategies_untouched(tmp_path):
    clock, m, st, w, sp, t = with_position(tmp_path)
    exp = st.experiment(w.runner.experiment_id)
    params = __import__("json").loads(exp["params_json"])
    assert params["initial_per_trader"] == "1000" and set(params["strategies"]) == {"1h", "4h", "1d"}
    assert [params["strategies"][tf]["id"] for tf in ("1h", "4h", "1d")] == ["S1-1H-sweep-reversal", "S2-4H-structure-pullback", "S3-1D-range-breakout"]
    assert all(params["strategies"][tf]["version"] == "1.0.0" for tf in ("1h", "4h", "1d"))
    assert sum(float(x.portfolio.initial) for x in w.runner.traders.values()) == 3000.0
    assert not any("SH-" in (e["id"] or "") for e in st.experiments())
    assert w.runner.traders["4h"].strategy["id"] == "S2-4H-structure-pullback"
    assert all(x.strategy["id"] != "SHADOW-P2-4H" for x in w.runner.traders.values())


def test_comparison_shadow_vs_s2_bh_cash_since_activation(tmp_path):
    from tradinglab.shadow.runner import compare
    clock, m, st, w, sp = both(tmp_path)
    step(clock, w, sp, ts("2026-11-05T02:00:00Z"))
    sm, mm = sp.own.marks(sp.runner.experiment_id), w.db.marks(w.runner.experiment_id)
    c = compare(sm, mm, sp.runner.started_at())
    assert c["rows"] and c["since"] >= sp.runner.started_at()
    r0 = c["rows"][0]
    assert r0["shadow"] == pytest.approx(1000, abs=0.01) and r0["cash"] == 1000
    s2 = [r for r in c["rows"] if "s2" in r]
    assert s2 and s2[0]["s2"] == pytest.approx(1000) and s2[0]["ts"] - r0["ts"] <= 300   # S2 нормирован к $1 000 на старте SHADOW
    assert all(r["bh"] is not None for r in c["rows"])
    assert "не входит" in c["note"]


# ====================================================================== 10. backtest не изменился
# эталон посчитан на базовой ветке claude/p2-shadow-prep (до SHADOW): решения, операции и equity S1/S2/S3 на наборе по умолчанию
BACKTEST_FINGERPRINT = "d3cf8440e4661d69492c6d61322837f14898302b804d08497b56cc92595fb742"


def backtest_fingerprint() -> str:
    from tradinglab import backtest, dataset
    ds = dataset.load(dataset.DEFAULT_DATASET)
    r = backtest.run(ds["series"], quote=ds["quote"])
    h = hashlib.sha256()
    for tf, t in sorted(r["traders"].items()):
        h.update(repr([(o["op_id"], o["side"], o["qty"], o["price"], o["usd"]) for o in t.ops]).encode())
        h.update(repr([(d["bar_ts"], d["action_final"]) for d in t.decisions]).encode())
        h.update(repr([(e["ts"], e["equity"]) for e in t.equity_curve]).encode())
    return h.hexdigest()


def test_backtest_results_unchanged():
    assert backtest_fingerprint() == BACKTEST_FINGERPRINT
