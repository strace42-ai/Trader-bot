"""FORWARD: START / PAUSE / RESUME / STOP, сохранение и восстановление, отсутствие ретро-сделок."""
import pytest

from tradinglab.forward import ControlError, ForwardController
from tradinglab.store import Store

from test_engine import H, flat_series, stub


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, t0=30 * H):
    st = Store(tmp_path / "lab.db")
    clk = Clock(t0)
    return st, clk, ForwardController(st, clock=clk)


def test_initial_not_started_and_invalid_commands(tmp_path):
    st, clk, fc = make(tmp_path)
    assert fc.status == "NOT_STARTED" and fc.allowed() == ["START"] and fc.traders == {}
    for cmd in (fc.pause, fc.resume, fc.stop):
        with pytest.raises(ControlError):
            cmd()
    assert st.controls() == [] and st.experiments() == []


def test_start_creates_immutable_forward_experiment(tmp_path):
    st, clk, fc = make(tmp_path)
    info = fc.start()
    assert info["status"] == "RUNNING" and info["started_at"] == 30 * H
    exp = st.experiment(fc.experiment_id)
    assert exp["mode"] == "FORWARD" and exp["started_at"] == 30 * H
    for tf in ("1h", "4h", "1d"):
        s = st.trader_state(fc.experiment_id, tf)
        assert s["mode"] == "FORWARD" and s["portfolio"]["cash"] == "1000"
    with pytest.raises(ControlError):
        fc.start()


def test_pause_resume_persist_and_restart(tmp_path):
    st, clk, fc = make(tmp_path)
    fc.start()
    fc.pause()
    assert all(t.paused for t in fc.traders.values())
    st2 = Store(tmp_path / "lab.db")                      # «перезапуск сервера»
    fc2 = ForwardController(st2, clock=clk)
    assert fc2.status == "PAUSED" and fc2.experiment_id == fc.experiment_id
    assert all(t.paused for t in fc2.traders.values())
    fc2.resume()
    assert fc2.status == "RUNNING" and not any(t.paused for t in fc2.traders.values())
    assert [c["command"] for c in st2.controls()][::-1] == ["START", "PAUSE", "RESUME"]


def test_step_ignores_bars_before_start_and_is_idempotent(tmp_path):
    st, clk, fc = make(tmp_path)
    fc.start()
    s = flat_series(60)
    fc.traders["1h"].fn = stub(enter_at=10)               # вход до started_at невозможен: бар 10 раньше старта
    r = fc.step({"1h": s})
    t = fc.traders["1h"]
    assert t.ops == []
    decs = st.decisions(fc.experiment_id, "1h", 1000)
    assert decs and min(d["decided_at"] for d in decs) >= 30 * H
    assert r["processed"] == len(decs)
    assert fc.step({"1h": s})["processed"] == 0           # повтор — ничего нового


def test_paused_forward_takes_no_entries(tmp_path):
    st, clk, fc = make(tmp_path)
    fc.start()
    fc.pause()
    fc.traders["1h"].fn = stub(enter_at=40)
    fc.step({"1h": flat_series(60)})
    assert fc.traders["1h"].ops == [] and fc.traders["1h"].state == "PAUSED"


def test_stop_closes_open_position_then_stopped_and_new_start(tmp_path):
    st, clk, fc = make(tmp_path)
    fc.start()
    s = flat_series(60, extra=[(1.0, 1.01, 0.99, 1.0)] * 6)
    fc.traders["1h"].fn = stub(enter_at=40)
    fc.step({"1h": flat_series(45)})                      # вход на открытии бара 41
    assert fc.traders["1h"].position is not None
    assert fc.stop()["status"] == "STOPPING"
    fc.step({"1h": s})
    t = fc.traders["1h"]
    assert t.position is None and fc.status == "STOPPED"
    assert len([o for o in t.ops if o["side"] == "BUY"]) == 1
    assert "STOP" in [o for o in t.ops if o["side"] == "SELL"][0]["reason"]
    old = fc.experiment_id
    clk.t = 100 * H
    fc.start()
    assert fc.experiment_id != old and st.experiment(old)["mode"] == "FORWARD"
    assert len(st.fills(old, "1h")) == 2 and st.fills(fc.experiment_id, "1h") == []


def test_stop_without_positions_is_immediate(tmp_path):
    st, clk, fc = make(tmp_path)
    fc.start()
    assert fc.stop()["status"] == "STOPPED"
    assert fc.allowed() == ["START"]
