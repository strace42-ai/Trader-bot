"""SHADOW LAB в веб-панели: страница /shadow и API /api/shadow*.

Веб-процесс (start.bat) SHADOW не исполняет: читает shadow_lab.db через read-only соединение и ставит команды в очередь
shadow_lab.db, которую исполняет отдельный процесс shadow.bat. Любая ошибка SHADOW-части возвращается как ошибка её
собственного API и не затрагивает основной FORWARD (другие маршруты, другая база, другой процесс).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import HTTPException

from ..candles import TF_SEC
from ..live import config as LC
from ..live.marketdb import MarketDB
from ..store import Store
from ..strategies import Ctx
from ..web import views
from . import strategy as P2
from .process import DB_NAME, readiness
from .readonly import ReadOnlyStore
from .runner import HTF, INITIAL, TF, ShadowRunner, compare

log = logging.getLogger("tradinglab.shadow.web")
COMMANDS = {"start": "START", "pause": "PAUSE", "resume": "RESUME", "stop": "STOP", "close": "CLOSE_POSITION", "emergency": "EMERGENCY_EXIT"}
MARKER_ACTIONS = ("ENTER", "EXIT", "SKIPPED")


class ShadowLab:
    def __init__(self, lab):
        self.lab = lab
        self.path = Path(lab.data_dir) / DB_NAME
        self._ro: ReadOnlyStore | None = None

    # ------------------------------------------------------------------ доступ
    def store(self):
        """shadow_lab.db только на чтение; если SHADOW ещё ни разу не запускался — пустое хранилище в памяти (файл не создаётся)."""
        if self.path.exists():
            if self._ro is None:
                self._ro = ReadOnlyStore(self.path)
            return self._ro
        return Store(":memory:", durable=False)

    def runner(self) -> ShadowRunner:
        r = ShadowRunner(self.store(), self.lab.db, clock=self.lab.clock)
        ex = r.executor()
        for t in r.traders.values():
            t.hydrate(r.store)
            t.ex = ex
        return r

    def worker(self, r: ShadowRunner) -> dict:
        w, _ = r.db.kv("shadow_worker")
        now = self.lab.now()
        alive = bool(w) and w.get("status") == "running" and now - w["ts"] <= LC.WORKER_STALE_SEC
        return {"alive": alive, "age": now - w["ts"] if w else None, "installed": self.path.exists(), **(w or {})}

    # ------------------------------------------------------------------ данные страницы
    def payload(self) -> dict:
        now = self.lab.now()
        r = self.runner()
        info = r.info()
        a = self.lab.live_analysis(TF)
        h = self.lab.live_analysis(HTF)
        lt = self.lab.live_analysis("1h")
        exp = r.store.experiment(r.experiment_id) if r.experiment_id else None
        out = {
            "now": now, "status": r.status, "experiment": r.experiment_id, "started_at": r.started_at(),
            "allowed": info["allowed"], "version_ok": r.version_ok, "version_issue": r.version_issue,
            "strategy": {**P2.STRATEGY, "rules_hash": P2.rules_hash()},
            "fixed": json.loads(exp["params_json"]).get("shadow") if exp else None, "params_hash": exp["params_hash"] if exp else None,
            "asset": {"pair": "FABLES/USDG", "chain": "Robinhood Chain", "kind": "спот, только long", "initial": float(INITIAL)},
            "worker": self.worker(r), "controls": info["controls"], "risk": r.risk or None,
            "readiness": readiness(r, self.lab.db, now) if r.status in ("NOT_STARTED", "STOPPED") else r.db.kv("readiness")[0],
            "integrity": r.integrity(), "events": r.db.events(50),
            "quote": self.lab.db.kv("last_valid_quote")[0],
            "paper": "Все операции SHADOW — моделируемое paper-исполнение по данным пула основного бота, не подтверждённые ончейн-свопы.",
        }
        # график 4H: закрытые свечи (как у стратегии) + формирующаяся (только отображение), структура v3
        if a is not None:
            out["chart"] = views.candles_payload(a)
            out["live"] = self.lab.live_block(TF, a)
            out["structure_v3"] = views.structure_v3_payload(a.s, h.s if h is not None else None, lt.s if lt is not None else None)
        t = r.traders.get(TF)
        lv = out["quote"]
        mark = {"ts": lv["ts"], "mid": lv["mid"], "source": lv["source"]} if lv and t is not None and (not t.equity_curve or lv["ts"] >= t.equity_curve[-1]["ts"]) else None
        if t is not None and a is not None:
            tp = views.trader_payload(t, a, mark, None, {"experiment": r.experiment_id, "dataset": "LIVE (данные основного бота, только чтение)"})
            tp.pop("analysis", None)
            bh = r.bh()
            if bh and mark:
                from ..execution import dec
                b = float(t.ex.liquidation(dec(bh["qty"]), mark["mid"]) + dec(bh["cash"]))
                tp["summary"]["bh_equity_same_time"], tp["summary"]["vs_bh_usd"] = b, tp["summary"]["equity"] - b
            tp["rejections"] = views.rejection_stats(t.decisions)
            tp["costs"] = cost_breakdown(t.ops)
            tp["journal"] = journal(t)
            tp["decisions_full"] = [d for d in t.decisions[-60:]][::-1]
            out["trader"] = tp
            marks = r.db.marks(r.experiment_id)
            eq = [m["traders"][TF]["equity"] for m in marks]
            from ..performance import max_drawdown
            tp["summary"]["max_drawdown_marks_pct"] = max_drawdown(eq) if eq else 0.0
            out["markers"] = markers(t)
        else:
            out["trader"] = None
            out["portfolio_idle"] = {"initial": float(INITIAL), "cash": float(INITIAL), "fables_qty": 0.0, "equity": float(INITIAL)}
        out["decision"] = (out["trader"] or {}).get("decision") or None
        out["preview"] = self.preview(r, a, h) if a is not None and r.status not in ("RUNNING", "PAUSED") else None
        return out

    def preview(self, r: ShadowRunner, a, h) -> dict | None:
        """Оценка правил P2 на последней закрытой 4H-свече ДО активации: только просмотр, не решение эксперимента."""
        import bisect
        if not len(a.s):
            return None
        i = len(a.s) - 1
        close = a.s.candles[i].ts + TF_SEC[TF]
        hclose = [k.ts + TF_SEC[HTF] for k in h.s.candles] if h is not None else []
        j = bisect.bisect_right(hclose, close) - 1 if hclose else -1
        try:
            d = P2.decide(Ctx(a, i, h, j if j >= 0 else None, r.decision_cost(), False))
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        d["preview"] = True
        d["note"] = "ПРЕДПРОСМОТР: правила P2 на последней закрытой 4H-свече; эксперимент не запущен — это не решение и не сделка"
        return d

    def performance(self) -> dict:
        r = self.runner()
        if not r.experiment_id:
            return {"status": r.status, "experiment": None, "rows": [], "note": "SHADOW не активирован: сравнение начнётся с момента START"}
        marks = r.db.marks(r.experiment_id)
        last = self.lab.store.last_control()
        fw_exp = last["experiment_id"] if last else None
        main_marks = self.lab.db.marks(fw_exp) if fw_exp else []
        cmp = compare(marks, main_marks, r.started_at())
        perf = views.forward_performance(r, marks)
        return {"status": r.status, "experiment": r.experiment_id, "started_at": r.started_at(), "fw_experiment": fw_exp,
                "strategy": f"{P2.STRATEGY['id']} {P2.VERSION}", **cmp, "table": perf["table"].get(TF), "bh_summary": perf["bh_summary"],
                "marks": len(marks)}

    # ------------------------------------------------------------------ команды
    def command(self, command: str) -> dict:
        r = self.runner()
        w = self.worker(r)
        if not w["alive"]:
            raise HTTPException(503, {"error": "процесс SHADOW не запущен (shadow.bat) — команда не поставлена в очередь"})
        now = self.lab.now()
        st = Store(self.path)                 # запись только в очередь команд shadow_lab.db
        try:
            db = MarketDB(st)
            cid = db.enqueue(now, command, {"tf": TF} if command == "CLOSE_POSITION" else {})
            res = None
            for _ in range(100):
                c = db.command(cid)
                if c["status"] != "PENDING":
                    res = {"id": cid, "status": c["status"], **(json.loads(c["result"]) if c["result"] else {})}
                    break
                time.sleep(0.1)
        finally:
            st.close()
        if res is None:
            raise HTTPException(504, {"error": "процесс SHADOW не ответил за 10 с; команда истечёт и не будет исполнена"})
        if res["status"] != "DONE":
            raise HTTPException(409, {"error": res.get("error") or res["status"]})
        return self.runner().info()


def cost_breakdown(ops: list[dict]) -> dict:
    out = {k: 0.0 for k in ("fee_usd", "impact_usd", "slippage_usd", "gas_usd")}
    for o in ops:
        for k in out:
            out[k] += float(o.get(k) or 0)
    out["total_usd"] = sum(out.values())
    out["operations"] = len(ops)
    return out


def journal(t) -> list[dict]:
    """Единый журнал: все решения (в т.ч. WAIT/SKIPPED с причиной), операции и события (отмены, отказы исполнения)."""
    rows = []
    for d in t.decisions:
        fails = [c["name"] for c in d.get("conditions") or [] if c.get("ok") is False]
        rows.append({"ts": d.get("decided_ts") or d["decided_at"], "kind": "DECISION", "action": d.get("action_final") or d["action"],
                     "bar_ts": d["bar_ts"], "text": d.get("explanation") or "", "blockers": fails[:3],
                     "rr_net": (d.get("plan") or {}).get("rr_net")})
    for o in t.ops:
        rows.append({"ts": o["ts"], "kind": "FILL", "action": o["side"], "text": o["reason"], "price": o["price"], "usd": o["usd"],
                     "costs": {k: o.get(k) for k in ("fee_usd", "impact_usd", "slippage_usd", "gas_usd")}, "note": o.get("exec_note")})
    for e in t.events:
        rows.append({"ts": e["ts"], "kind": "EVENT", "action": e.get("type"), "text": e.get("why") or e.get("reason") or "", "policy": e.get("policy")})
    rows.sort(key=lambda x: (x["ts"], {"DECISION": 0, "EVENT": 1, "FILL": 2}[x["kind"]]), reverse=True)
    return rows[:400]


def markers(t) -> list[dict]:
    out = []
    for d in t.decisions:
        act = d.get("action_final") or d["action"]
        if act in MARKER_ACTIONS:
            out.append({"time": d["bar_ts"], "kind": act, "text": {"ENTER": "P2 ENTER", "EXIT": "P2 EXIT", "SKIPPED": "SKIP"}[act]})
    for o in t.ops:
        out.append({"time": o["ts"] - o["ts"] % TF_SEC[TF], "kind": o["side"], "text": f"{o['side']} {float(o['price']):.5g}", "price": float(o["price"])})
    return out


def attach(app, lab, page) -> None:
    sl = ShadowLab(lab)
    app.state.shadow = sl

    def guarded(fn):
        try:
            return fn()
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — ошибка SHADOW не должна влиять на основной FORWARD
            log.exception("shadow api failed")
            raise HTTPException(500, {"error": f"SHADOW: {type(e).__name__}: {e}"})

    @app.get("/shadow")
    def shadow_page():
        return page("shadow.html")

    @app.get("/api/shadow")
    def shadow_api():
        return guarded(sl.payload)

    @app.get("/api/shadow/performance")
    def shadow_perf():
        return guarded(sl.performance)

    @app.get("/api/shadow/readiness")
    def shadow_ready():
        return guarded(lambda: readiness(sl.runner(), lab.db, lab.now()))

    @app.post("/api/shadow/{cmd}")
    def shadow_cmd(cmd: str):
        command = COMMANDS.get(cmd)
        if command is None:
            raise HTTPException(404, "команда: start | pause | resume | stop | close | emergency")
        return guarded(lambda: sl.command(command))
