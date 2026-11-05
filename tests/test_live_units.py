"""Модульные тесты живого режима: keccak/селекторы, разбор реальных событий Swap, модель CLMM, хранилище свечей."""
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from tradinglab.execution import CONSERVATIVE_V1
from tradinglab.live import config as C, sources as S
from tradinglab.live.clmm import LiveExecutor
from tradinglab.live.keccak import keccak256, selector, topic
from tradinglab.live.marketdb import MarketDB
from tradinglab.live.transport import ReplayTransport, SourceError
from tradinglab.store import Store

EVID = Path(__file__).resolve().parent.parent / "evidence" / "fables_swaps" / "swap_fee_sample.json"


def test_keccak_vectors_and_constants():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert selector("transfer(address,uint256)") == "a9059cbb"
    assert selector("getSlot0(bytes32)") == C.SEL_GET_SLOT0 and selector("getLiquidity(bytes32)") == C.SEL_GET_LIQUIDITY
    assert selector("getTickBitmap(bytes32,int16)") == C.SEL_GET_TICK_BITMAP
    assert selector("getTickLiquidity(bytes32,int24)") == C.SEL_GET_TICK_LIQUIDITY
    assert topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)") == C.TOPIC_SWAP


def test_parse_recorded_real_swap_logs():
    """Реальные логи 2026-11-05: 22 покупки по 5000 ppm и 66 продаж по 10000 ppm."""
    ev = json.loads(EVID.read_text())
    assert ev["logs"][0]["topics"][0] == C.TOPIC_SWAP
    tr = ReplayTransport(lambda src, what, p, now: ev["logs"], lambda: 0)
    rows = S.swaps(tr, ev["from_block"], ev["to_block"])
    assert sum(1 for r in rows if r["side"] == "BUY" and r["fee_ppm"] == 5000) == 22
    assert sum(1 for r in rows if r["side"] == "SELL" and r["fee_ppm"] == 10000) == 66


def test_mid_from_measured_slot0():
    assert S.mid_from_sqrt(320811093993539540833496928083732450) == pytest.approx(0.06099, rel=1e-4)


def test_rpc_is_read_only():
    tr = ReplayTransport(lambda *a: None, lambda: 0)
    for m in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign"):
        with pytest.raises(ValueError):
            tr.rpc(m, [])


SP = 320811093993539540833496928083732450      # измеренный slot0 2026-11-05
L0 = 14479089880190338852
TICK = 304295


def pool(L=L0, ticks=None, known=(TICK - 5000, TICK + 5000), sp=SP, tick=TICK):
    return {"sqrt_price_x96": str(sp), "tick": tick, "liquidity": str(L), "ticks": {str(k): str(v) for k, v in (ticks or {}).items()},
            "known": list(known) if known else None}


def exe(fees=None, **kw):
    return LiveExecutor(CONSERVATIVE_V1, pool(**kw), fees or {"BUY": 5000, "SELL": 10000, "source": "t"}, Decimal("0.01"))


MID = float(Decimal(10) ** 12 / (Decimal(SP) / Decimal(2) ** 96) ** 2)


def test_single_range_matches_virtual_reserves():
    f = exe().buy("500", MID)
    sp = Decimal(SP) / Decimal(2) ** 96
    x, y = Decimal(L0) / sp / Decimal(10) ** 6, Decimal(L0) * sp / Decimal(10) ** 18
    net = Decimal("500") * Decimal("0.995")
    cp = y * net / (x + net)
    assert abs((f.qty / Decimal("0.999")) - cp) / cp < Decimal("1e-9")
    assert f.model == "v4-local-quote-estimate" and "state-exact" in f.note and "не V4Quoter" in f.note


def test_crossing_a_tick_closer_than_old_20_tick_assumption():
    """Инициализированный тик в 3 тиках выше цены; за ним ликвидность падает в 4 раза. Продажа FABLES его пересекает."""
    from tradinglab.live.clmm import simulate, sqrt_at_tick
    up = TICK + 3
    net = -(L0 * 3 // 4)
    sp = Decimal(SP) / Decimal(2) ** 96
    qty = Decimal(40_000)                                       # ≈ $2 400 — больше, чем помещается до тика
    y_in = qty * Decimal("0.99") * Decimal(10) ** 18
    r = simulate(sp, Decimal(L0), TICK, {up: net}, (TICK - 5000, TICK + 5000), y_in, False)
    # ручной расчёт: до тика с L0, затем с L0 + net
    spt = sqrt_at_tick(up)
    y1 = Decimal(L0) * (spt - sp)
    x1 = Decimal(L0) * (1 / sp - 1 / spt)
    L1 = Decimal(L0 + net)
    sp2 = spt + (y_in - y1) / L1
    x2 = L1 * (1 / spt - 1 / sp2)
    assert r["ok"] and r["crossed"] == [up] and abs(r["out"] - (x1 + x2)) / (x1 + x2) < Decimal("1e-20")
    naive = Decimal(L0) * (1 / sp - 1 / (sp + y_in / Decimal(L0)))   # старая модель: L постоянна
    assert naive > r["out"] * Decimal("1.001")                   # старая модель завышала выручку
    f = exe(ticks={up: net}).sell(qty, MID)
    assert "пересечено тиков: 1" in f.note and f.model == "v4-local-quote-estimate"


def test_beyond_known_ticks_is_range_unknown_and_worst():
    f = exe(known=(TICK - 10, TICK + 10)).sell(Decimal(3_000_000), MID)
    assert f.model == "range-unknown→worst" and "exec_confidence=range-unknown" in f.note
    g = exe(known=None).buy("50000", MID)                         # без данных тиков точна только сделка внутри текущего тика
    assert g.model == "range-unknown→worst"
    small = exe(known=None).buy("1", MID)
    assert small.model == "v4-local-quote-estimate"


def test_round_trip_loses_fees_and_uses_observed_fee():
    ex = exe(fees={"BUY": 7000, "SELL": 12000, "source": "t"})
    b = ex.buy("500", MID)
    s = ex.sell(b.qty, MID)
    assert b.fee_ppm == 7000 and s.fee_ppm == 12000
    loss = Decimal("500") - (s.usd - s.gas_usd - b.gas_usd)
    assert Decimal("0.019") * 500 < loss < Decimal("0.023") * 500   # 0,7% + 1,2% + 2×0,1% задержки исполнения + импакт


def test_model_reproduces_recorded_real_swaps():
    """Проверка модели на 74 реальных свопах пула: состояние после свопа k → выход свопа k+1 (без изменения L между ними)."""
    from tradinglab.live.clmm import simulate
    ev = json.loads(EVID.read_text())
    w = lambda d, i: int(d[2 + 64 * i:2 + 64 * (i + 1)], 16)  # noqa: E731
    sg = lambda v: v - (1 << 256) if v >> 255 else v           # noqa: E731
    rows = [dict(a0=sg(w(l["data"], 0)), a1=sg(w(l["data"], 1)), sp=w(l["data"], 2), L=w(l["data"], 3), tick=sg(w(l["data"], 4)),
                 fee=w(l["data"], 5)) for l in ev["logs"]]
    errs = []
    for p, c in zip(rows, rows[1:]):
        if p["L"] != c["L"]:
            continue
        buy = c["a0"] < 0
        amt = Decimal(-c["a0"] if buy else -c["a1"]) * (1 - Decimal(c["fee"]) / Decimal(10 ** 6))
        r = simulate(Decimal(p["sp"]) / Decimal(2) ** 96, Decimal(c["L"]), p["tick"], {}, (p["tick"] - 1000, p["tick"] + 1000), amt, buy)
        act = Decimal(c["a1"] if buy else c["a0"])
        errs.append(abs(r["out"] - act) / act)
    errs.sort()
    assert len(errs) >= 70 and errs[len(errs) // 2] < Decimal("1e-7") and errs[-1] < Decimal("1e-4")


def test_executor_without_pool_state_is_labelled_fallback():
    f = LiveExecutor(CONSERVATIVE_V1, None, {}, None).buy("100", 0.06)
    assert f.model == "conservative-fallback" and f.fee_ppm == 5000 and "начальная выборка" in f.fee_source
    assert "не V4Quoter" in f.note


def test_closed_candles_immutable_revisions_recorded(tmp_path):
    st = Store(tmp_path / "lab.db", durable=False)
    db = MarketDB(st)
    r = db.store_closed("1h", [[0, 1, 2, 0.5, 1.5, 10]], now=4000, backfill=False)
    assert r["new"] == [0]
    r = db.store_closed("1h", [[0, 1, 2, 0.5, 1.7, 12]], now=5000, backfill=False)   # источник «переписал» свечу
    assert r["revised"] == 1
    s, avail = db.candles("1h", 10 ** 9)
    assert s.candles[0].c == 1.5 and avail[0] == 4000       # решение остаётся на первой версии
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("UPDATE live_candles SET c=9")
    with pytest.raises(sqlite3.IntegrityError):
        st.conn.execute("DELETE FROM live_candles")


def test_collector_never_stores_forming_candle_as_closed(tmp_path):
    from tradinglab.live.collector import Collector
    now = 10 * 3600 + 100                                       # 10:01:40 — свеча 10:00 ещё формируется
    rows = [[h * 3600, 1, 1.1, 0.9, 1, 5] for h in range(11)]

    def handler(src, what, params, t):
        if src == "geckoterminal":
            return {"data": {"attributes": {"ohlcv_list": rows}}, "meta": {"base": {"address": C.TOKEN}}}
        raise SourceError(src, "unavailable", "нет")
    st = Store(tmp_path / "lab.db", durable=False)
    col = Collector(st, ReplayTransport(handler, lambda: now), lambda: now)
    col.tick(now)
    db = MarketDB(st)
    assert db.last_closed_ts("1h") == 8 * 3600                 # 09:00 закрыта, но ещё в окне GRACE (180 с) → не берётся
    assert db.forming("1h")["ts"] == 10 * 3600
    assert any(e["kind"] == "source_error" for e in db.events())


def test_transport_circuit_breaker():
    calls = []

    def handler(src, what, params, t):
        calls.append(1)
        raise SourceError(src, "unavailable", "down")
    tr = ReplayTransport(handler, lambda: 0)
    for _ in range(5):
        with pytest.raises(SourceError):
            tr.get("dexscreener", "x")
    assert len(calls) == 3                                     # после 3 ошибок источник пропускается
    assert tr.health.snapshot()["dexscreener"]["open_for_sec"] > 0
