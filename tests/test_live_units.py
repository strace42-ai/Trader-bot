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


def exe(L=14479089880190338852, fees=None):
    return LiveExecutor(CONSERVATIVE_V1, {"liquidity": str(L)}, fees or {"BUY": 5000, "SELL": 10000, "source": "t"}, Decimal("0.01"))


def test_clmm_small_trade_matches_constant_product_with_virtual_reserves():
    mid = 0.0609902496428114
    f = exe().buy("500", mid)
    # виртуальные резервы активного диапазона: x = L/√P (USDG), y = L·√P (FABLES)
    sp = (Decimal(10) ** 12 / Decimal(mid)).sqrt()
    x, y = Decimal(14479089880190338852) / sp / Decimal(10) ** 6, Decimal(14479089880190338852) * sp / Decimal(10) ** 18
    net = Decimal("500") * Decimal("0.995")
    cp = y * net / (x + net)
    assert abs((f.qty / Decimal("0.999")) - cp) / cp < Decimal("1e-6")
    assert f.fee_usd == Decimal("2.5") and f.model == "v4-clmm-active-range-estimate" and f.impact_usd > 0


def test_clmm_round_trip_loses_fees_and_uses_observed_fee():
    ex = exe(fees={"BUY": 7000, "SELL": 12000, "source": "t"})
    b = ex.buy("500", 0.06)
    s = ex.sell(b.qty, 0.06)
    assert b.fee_ppm == 7000 and s.fee_ppm == 12000
    loss = Decimal("500") - (s.usd - s.gas_usd - b.gas_usd)
    assert Decimal("0.019") * 500 < loss < Decimal("0.023") * 500     # 0,7% + 1,2% + 2×0,1% задержки исполнения + импакт


def test_clmm_large_trade_falls_back_to_worse_estimate():
    for L in (10 ** 17, 10 ** 19):                              # мелкий и крупный диапазон: всегда худшая оценка + пометка
        ex = exe(L=L)
        f = ex.buy("50000", 0.06)
        cons = __import__("tradinglab.execution", fromlist=["quote_buy"]).quote_buy("50000", 0.06, CONSERVATIVE_V1.__class__(
            **{**CONSERVATIVE_V1.__dict__, "slippage": Decimal("0.001")}))
        assert f.model in ("clmm→conservative", "v4-clmm-range-risk") and "выход из диапазона" in f.note
        assert f.qty <= cons.qty


def test_executor_without_pool_state_is_labelled_fallback():
    f = LiveExecutor(CONSERVATIVE_V1, None, {}, None).buy("100", 0.06)
    assert f.model == "conservative-fallback" and f.fee_ppm == 5000 and "начальная выборка" in f.fee_source


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
