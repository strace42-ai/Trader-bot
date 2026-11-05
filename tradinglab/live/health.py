"""Проверки готовности и целостности: preflight перед START, сверка состояния с журналом, самопроверка рестарта.

Preflight (обязателен для START через панель/очередь): свежие данные GeckoTerminal/DexScreener/RPC, проверенная ончейн-котировка,
свежие тики пула, загружены закрытые свечи всех трёх ТФ до ожидаемой, целостность базы. CoinGecko — предупреждение (контекст).
Integrity: для каждого трейдера текущего FORWARD-эксперимента кеш и количество FABLES пересчитываются из журнала операций
и сравниваются с сохранённым состоянием; проверяется, что решения не новее last_bar_ts и операции не раньше начала эксперимента.
"""
from __future__ import annotations

from decimal import Decimal

from . import config as C
from .marketdb import MarketDB
from ..candles import GRACE_SEC, TF_SEC

TFS = ("1h", "4h", "1d")


def _t(ts) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if ts else "—"


def _chk(name: str, ok: bool, detail: str, level: str = "required") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "level": level}


def preflight(store, now: int) -> dict:
    db = MarketDB(store)
    checks = []
    health, _ = db.kv("health", {})
    health = health or {}
    for src, name, req in (("geckoterminal", "GeckoTerminal OHLCV", "required"), ("dexscreener", "DexScreener", "required"),
                           ("rpc", "Robinhood RPC (StateView)", "required"), ("coingecko", "CoinGecko BTC/ETH/USDG", "warning")):
        h = health.get(src) or {}
        last = h.get("last_ok")
        limit = 3600 * 2 if src == "coingecko" else 15 * 60
        ok = bool(last) and now - last <= limit and h.get("consecutive_fail", 0) == 0
        checks.append(_chk(name, ok, f"последний успех {now - last} с назад, ошибок подряд {h.get('consecutive_fail', 0)}" if last
                           else "успешных запросов ещё не было", req))
    risk, _ = db.kv("risk", {})
    pool = db.last_quote("pool")
    ds = db.last_quote("ds")
    pool_ok = pool is not None and now - pool["ts"] <= C.POOL_MAX_AGE
    checks.append(_chk("Ончейн-котировка StateView", pool_ok, f"{pool['mid']:.6g} USDG, {now - pool['ts']} с назад" if pool else "нет"))
    if pool and ds and now - ds["ts"] <= C.QUOTE_MAX_AGE:
        div = abs(ds["mid"] / pool["mid"] - 1)
        checks.append(_chk("Согласие StateView и DexScreener", div <= C.SOURCE_MAX_DIVERGENCE, f"расхождение {div * 100:.2f}%"))
    pt, _ = db.kv("pool_ticks")
    tick_ok = bool(pt) and pool is not None and now - pt.get("fetched_at", 0) <= C.TICKS_MAX_AGE and pt["known"][0] < pool["tick"] < pt["known"][1]
    checks.append(_chk("Данные тиков пула", tick_ok, f"известная область {pt['known']}, {now - pt.get('fetched_at', 0)} с назад" if pt else "нет"))
    for tf in TFS:
        d = TF_SEC[tf]
        exp = (now - GRACE_SEC) // d * d - d
        last = db.last_closed_ts(tf)
        late = now - (exp + d) > 1800                               # GeckoTerminal может отдать закрытую свечу с задержкой
        ok = last is not None and (last >= exp or not late)
        checks.append(_chk(f"Закрытые свечи {tf.upper()}", ok, f"{db.count(tf)} свечей, последняя открыта {_t(last)}" + ("" if last is None or last >= exp
                           else f", ожидается {_t(exp)}") if last else "свечей нет"))
    integ = integrity(store)
    checks.append(_chk("Целостность состояния", integ["ok"], "; ".join(integ["issues"]) or "состояние совпадает с журналом"))
    failed = [c for c in checks if not c["ok"] and c["level"] == "required"]
    return {"ok": not failed, "ts": now, "checks": checks, "failed": [c["name"] for c in failed],
            "warnings": [c["name"] for c in checks if not c["ok"] and c["level"] == "warning"]}


def integrity(store, experiment_id: str | None = None) -> dict:
    """Сверка сохранённого состояния трейдеров с журналом операций (append-only)."""
    issues, checked = [], []
    last = store.last_control()
    exp_id = experiment_id or (last["experiment_id"] if last else None)
    try:
        r = store._one("PRAGMA quick_check")
        if r and r[0] != "ok":
            issues.append(f"SQLite quick_check: {r[0]}")
    except Exception as e:  # noqa: BLE001
        issues.append(f"SQLite недоступна: {e}")
    if exp_id:
        exp = store.experiment(exp_id)
        for tf in TFS:
            st = store.trader_state(exp_id, tf)
            if not st:
                continue
            fills = store.fills(exp_id, tf)
            cash = Decimal(st["portfolio"]["initial"])
            qty = Decimal(0)
            for f in fills:
                if f["side"] == "BUY":
                    cash -= Decimal(f["usd"]) + Decimal(f["gas_usd"])
                    qty += Decimal(f["qty"])
                else:
                    cash += Decimal(f["usd"]) - Decimal(f["gas_usd"])
                    qty -= Decimal(f["qty"])
                if exp and f["ts"] < exp["started_at"]:
                    issues.append(f"{tf}: операция {f['op_id']} раньше начала эксперимента")
            s_cash = Decimal(st["portfolio"]["cash"])
            s_qty = sum((Decimal(l["qty"]) for l in st["portfolio"]["lots"]), Decimal(0))
            if s_cash != cash:
                issues.append(f"{tf}: кеш в состоянии {s_cash} ≠ журналу {cash}")
            if s_qty != qty:
                issues.append(f"{tf}: FABLES в состоянии {s_qty} ≠ журналу {qty}")
            if len({f["op_id"] for f in fills}) != len(fills):
                issues.append(f"{tf}: повторяющиеся операции")
            decs = store.decisions(exp_id, tf, 1)
            if decs and decs[0]["bar_ts"] > st["last_bar_ts"]:
                issues.append(f"{tf}: решение новее сохранённого last_bar_ts")
            checked.append({"tf": tf, "fills": len(fills), "cash": str(s_cash), "qty": str(s_qty)})
    return {"ok": not issues, "experiment": exp_id, "issues": issues, "checked": checked}


def restart_selftest(tmp_dir) -> dict:
    """Самопроверка во временной папке: START → PAUSE → «перезапуск» → состояние и журнал восстановлены, журналы неизменяемы."""
    import sqlite3
    from pathlib import Path

    from ..forward import ForwardRunner
    from ..store import Store
    p = Path(tmp_dir) / "selftest.db"
    st = Store(p)
    clk = lambda: 1_800_000_000  # noqa: E731
    r = ForwardRunner(st, clock=clk)
    r.start()
    r.pause()
    exp = r.experiment_id
    st.close()
    st2 = Store(p)
    r2 = ForwardRunner(st2, clock=clk)
    ok = r2.status == "PAUSED" and r2.experiment_id == exp and all(t.paused for t in r2.traders.values()) and len(r2.traders) == 3
    ok = ok and all(str(t.portfolio.cash) == "1000" for t in r2.traders.values())
    ro = False
    try:
        st2.conn.execute("DELETE FROM controls")
    except sqlite3.IntegrityError:
        ro = True
    integ = integrity(st2, exp)
    st2.close()
    return {"ok": ok and ro and integ["ok"], "status_restored": r2.status, "journal_append_only": ro, "integrity": integ["ok"]}
