"""Хранилище Trading Lab: отдельная SQLite-база (по умолчанию data/trading_lab.db).

- Журналы (decisions, fills, trades, events) — append-only: UPDATE/DELETE запрещены триггерами.
- Эксперимент: mode (BACKTEST | FORWARD), started_at, параметры и их хеш неизменяемы (триггер).
- Обработка одной свечи одним трейдером записывается одной транзакцией вместе с состоянием портфеля:
  либо всё, либо ничего. Повтор той же свечи нарушает UNIQUE → откат (защита от двойного исполнения).
- FORWARD: запись решения/операции с временем раньше started_at запрещена (никаких задним числом добавленных сделок).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3
# Контролируемые миграции: версия → операторы. Применяются по порядку в одной транзакции, версия записывается в meta.
MIGRATIONS = {
    2: [  # журнал команд управления FORWARD (START/PAUSE/RESUME/STOP), только добавление
        """CREATE TABLE IF NOT EXISTS controls (
             id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT, ts INTEGER NOT NULL, command TEXT NOT NULL,
             from_status TEXT, to_status TEXT NOT NULL, note TEXT)""",
        "CREATE TRIGGER IF NOT EXISTS ro_controls_update BEFORE UPDATE ON controls BEGIN SELECT RAISE(ABORT, 'controls: журнал только для добавления'); END",
        "CREATE TRIGGER IF NOT EXISTS ro_controls_delete BEFORE DELETE ON controls BEGIN SELECT RAISE(ABORT, 'controls: журнал только для добавления'); END",
    ],
    3: [  # живые рыночные данные FORWARD (этап E)
        """CREATE TABLE IF NOT EXISTS live_candles (tf TEXT NOT NULL, ts INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,
             source TEXT NOT NULL, close_ts INTEGER NOT NULL, available_at INTEGER NOT NULL, backfill INTEGER NOT NULL DEFAULT 0,
             PRIMARY KEY(tf, ts))""",
        "CREATE TRIGGER IF NOT EXISTS ro_live_candles_update BEFORE UPDATE ON live_candles BEGIN SELECT RAISE(ABORT, 'live_candles: закрытая свеча неизменна (правки — в candle_revisions)'); END",
        "CREATE TRIGGER IF NOT EXISTS ro_live_candles_delete BEFORE DELETE ON live_candles BEGIN SELECT RAISE(ABORT, 'live_candles: удаление запрещено'); END",
        """CREATE TABLE IF NOT EXISTS candle_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, tf TEXT, ts INTEGER, seen_at INTEGER,
             o REAL, h REAL, l REAL, c REAL, v REAL)""",
        """CREATE TABLE IF NOT EXISTS forming (tf TEXT PRIMARY KEY, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, fetched_at INTEGER)""",
        """CREATE TABLE IF NOT EXISTS quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL,
             mid REAL, json TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_quotes ON quotes(kind, ts)",
        """CREATE TABLE IF NOT EXISTS swaps (block INTEGER NOT NULL, log_index INTEGER NOT NULL, observed_at INTEGER NOT NULL,
             side TEXT, fee_ppm INTEGER, usdg REAL, fables REAL, tx TEXT, PRIMARY KEY(block, log_index))""",
        """CREATE TABLE IF NOT EXISTS context (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL, json TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_context ON context(kind, ts)",
        """CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)""",
        """CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, command TEXT NOT NULL,
             args TEXT, status TEXT NOT NULL DEFAULT 'PENDING', result TEXT, done_at INTEGER)""",
        """CREATE TABLE IF NOT EXISTS fw_marks (experiment_id TEXT NOT NULL, ts INTEGER NOT NULL, json TEXT NOT NULL,
             PRIMARY KEY(experiment_id, ts))""",
        "CREATE TRIGGER IF NOT EXISTS ro_fw_marks_update BEFORE UPDATE ON fw_marks BEGIN SELECT RAISE(ABORT, 'fw_marks: только добавление'); END",
        """CREATE TABLE IF NOT EXISTS data_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL, json TEXT NOT NULL)""",
    ],
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('BACKTEST','FORWARD')), started_at INTEGER NOT NULL,
  created_at TEXT NOT NULL, params_json TEXT NOT NULL, params_hash TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS trader_state (
  experiment_id TEXT NOT NULL, trader TEXT NOT NULL, state_json TEXT NOT NULL, last_bar_ts INTEGER NOT NULL, updated_at TEXT,
  PRIMARY KEY(experiment_id, trader));
CREATE TABLE IF NOT EXISTS decisions (
  experiment_id TEXT NOT NULL, trader TEXT NOT NULL, bar_ts INTEGER NOT NULL, decided_at INTEGER NOT NULL,
  action TEXT, action_final TEXT, state TEXT, strategy_version TEXT, json TEXT NOT NULL,
  UNIQUE(experiment_id, trader, bar_ts));
CREATE TABLE IF NOT EXISTS fills (
  op_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, trader TEXT NOT NULL, ts INTEGER NOT NULL, side TEXT NOT NULL,
  position_id TEXT, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, trader TEXT NOT NULL, exit_ts INTEGER NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity (
  experiment_id TEXT NOT NULL, trader TEXT NOT NULL, ts INTEGER NOT NULL, json TEXT NOT NULL, PRIMARY KEY(experiment_id, trader, ts));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT, trader TEXT, ts INTEGER, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fills_exp ON fills(experiment_id, trader, ts);
CREATE INDEX IF NOT EXISTS ix_dec_exp ON decisions(experiment_id, trader, bar_ts);
"""
APPEND_ONLY = ("decisions", "fills", "trades", "events")


def _triggers() -> str:
    out = []
    for t in APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            out.append(f"CREATE TRIGGER IF NOT EXISTS ro_{t}_{op.lower()} BEFORE {op} ON {t} "
                       f"BEGIN SELECT RAISE(ABORT, '{t}: журнал только для добавления'); END;")
    out.append("CREATE TRIGGER IF NOT EXISTS ro_exp_update BEFORE UPDATE OF mode, started_at, params_json, params_hash ON experiments "
               "BEGIN SELECT RAISE(ABORT, 'experiments: параметры эксперимента неизменяемы'); END;")
    return "\n".join(out)


class StoreError(Exception):
    pass


class Store:
    def __init__(self, path: Path | str, durable: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # durable=False — только для тестов-воспроизведений (быстро, без fsync каждой транзакции)
        self.conn.execute("PRAGMA synchronous=FULL" if durable else "PRAGMA synchronous=OFF")
        # одно соединение на процесс; FastAPI вызывает обработчики из пула потоков → ВСЕ обращения к соединению
        # (чтение и запись) сериализуются этой блокировкой (RLock: чтение внутри транзакции того же потока допустимо)
        self.lock = threading.RLock()
        with self.tx() as c:
            for stmt in (SCHEMA + _triggers()).split(";\n"):
                if stmt.strip():
                    c.execute(stmt)
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta VALUES('schema_version', '1')")
                ver = 1
            else:
                ver = int(row[0])
            if ver > SCHEMA_VERSION:
                raise StoreError(f"база новее кода: схема {ver} > {SCHEMA_VERSION}")
            for v in range(ver + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS[v]:
                    c.execute(stmt)
                c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(v),))
            self.schema_version = SCHEMA_VERSION

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _all(self, sql: str, args=()) -> list:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------------ эксперименты
    def create_experiment(self, exp_id: str, mode: str, started_at: int, params: dict, params_hash: str, note: str = "") -> None:
        from .util import now_iso
        with self.tx() as c:
            if c.execute("SELECT 1 FROM experiments WHERE id=?", (exp_id,)).fetchone():
                raise StoreError(f"эксперимент {exp_id} уже существует")
            c.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?)",
                      (exp_id, mode, int(started_at), now_iso(), json.dumps(params, sort_keys=True, default=str), params_hash, note))

    def experiment(self, exp_id: str) -> dict | None:
        r = self._one("SELECT * FROM experiments WHERE id=?", (exp_id,))
        return dict(r) if r else None

    def experiments(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT id, mode, started_at, created_at, params_hash, note FROM experiments ORDER BY created_at")]

    # ------------------------------------------------------------------ запись обработанной свечи
    def commit_bar(self, trader) -> None:
        exp = self.experiment(trader.experiment)
        if exp is None:
            raise StoreError("эксперимент не создан")
        if exp["mode"] != trader.mode:
            raise StoreError(f"режим трейдера {trader.mode} ≠ режиму эксперимента {exp['mode']}")
        new = trader.pending_records()  # буфер очищается только после успешного COMMIT
        if exp["mode"] == "FORWARD":
            for d in new["decisions"]:
                if d["decided_at"] < exp["started_at"]:
                    raise StoreError("FORWARD: решение раньше начала наблюдения запрещено")
            for o in new["ops"]:
                if o["ts"] < exp["started_at"]:
                    raise StoreError("FORWARD: операция раньше начала наблюдения запрещена")
        eid, tf = trader.experiment, trader.tf
        dump = lambda x: json.dumps(x, ensure_ascii=False, default=str)  # noqa: E731
        with self.tx() as c:
            for d in new["decisions"]:
                c.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)",
                          (eid, tf, d["bar_ts"], d["decided_at"], d["action"], d.get("action_final"), d.get("state"), d["version"], dump(d)))
            for o in new["ops"]:
                c.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", (o["op_id"], eid, tf, o["ts"], o["side"], o["position_id"], dump(o)))
            for t in new["trades"]:
                c.execute("INSERT INTO trades VALUES(?,?,?,?,?)", (t["trade_id"], eid, tf, t["exit_ts"], dump(t)))
            for e in new["equity"]:
                c.execute("INSERT INTO equity VALUES(?,?,?,?)", (eid, tf, e["ts"], dump(e)))
            for e in new["events"]:
                c.execute("INSERT INTO events(experiment_id, trader, ts, json) VALUES(?,?,?,?)", (eid, tf, e["ts"], dump(e)))
            from .util import now_iso
            st = trader.to_state()
            c.execute("INSERT INTO trader_state VALUES(?,?,?,?,?) ON CONFLICT(experiment_id, trader) DO UPDATE SET "
                      "state_json=excluded.state_json, last_bar_ts=excluded.last_bar_ts, updated_at=excluded.updated_at",
                      (eid, tf, dump(st), st["last_bar_ts"], now_iso()))
        trader.ack()  # только после COMMIT: при ROLLBACK записи остаются в буфере и уйдут при следующей попытке

    def save_state(self, trader) -> None:
        """Сохранить состояние трейдера без новых журнальных записей (например, после PAUSE)."""
        from .util import now_iso
        st = trader.to_state()
        with self.tx() as c:
            c.execute("INSERT INTO trader_state VALUES(?,?,?,?,?) ON CONFLICT(experiment_id, trader) DO UPDATE SET "
                      "state_json=excluded.state_json, last_bar_ts=excluded.last_bar_ts, updated_at=excluded.updated_at",
                      (trader.experiment, trader.tf, json.dumps(st, ensure_ascii=False, default=str), st["last_bar_ts"], now_iso()))

    def log_control(self, experiment_id: str | None, ts: int, command: str, from_status: str | None, to_status: str,
                    note: str = "", traders: list | None = None) -> None:
        """Команда управления и (опционально) состояния трейдеров — одной транзакцией."""
        from .util import now_iso
        with self.tx() as c:
            c.execute("INSERT INTO controls(experiment_id, ts, command, from_status, to_status, note) VALUES(?,?,?,?,?,?)",
                      (experiment_id, int(ts), command, from_status, to_status, note))
            for t in traders or []:
                st = t.to_state()
                c.execute("INSERT INTO trader_state VALUES(?,?,?,?,?) ON CONFLICT(experiment_id, trader) DO UPDATE SET "
                          "state_json=excluded.state_json, last_bar_ts=excluded.last_bar_ts, updated_at=excluded.updated_at",
                          (t.experiment, t.tf, json.dumps(st, ensure_ascii=False, default=str), st["last_bar_ts"], now_iso()))

    def controls(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM controls ORDER BY id DESC LIMIT ?", (limit,))]

    def last_control(self) -> dict | None:
        r = self._one("SELECT * FROM controls ORDER BY id DESC LIMIT 1")
        return dict(r) if r else None

    # ------------------------------------------------------------------ чтение
    def trader_state(self, exp_id: str, tf: str) -> dict | None:
        r = self._one("SELECT state_json FROM trader_state WHERE experiment_id=? AND trader=?", (exp_id, tf))
        return json.loads(r[0]) if r else None

    def _rows(self, sql: str, args) -> list[dict]:
        return [json.loads(r[0]) for r in self._all(sql, args)]

    def fills(self, exp_id: str, tf: str) -> list[dict]:
        return self._rows("SELECT json FROM fills WHERE experiment_id=? AND trader=? ORDER BY ts, op_id", (exp_id, tf))

    def trades(self, exp_id: str, tf: str) -> list[dict]:
        return self._rows("SELECT json FROM trades WHERE experiment_id=? AND trader=? ORDER BY exit_ts", (exp_id, tf))

    def equity(self, exp_id: str, tf: str) -> list[dict]:
        return self._rows("SELECT json FROM equity WHERE experiment_id=? AND trader=? ORDER BY ts", (exp_id, tf))

    def decisions(self, exp_id: str, tf: str, limit: int = 200) -> list[dict]:
        return self._rows("SELECT json FROM decisions WHERE experiment_id=? AND trader=? ORDER BY bar_ts DESC LIMIT ?", (exp_id, tf, limit))

    def events(self, exp_id: str, tf: str) -> list[dict]:
        return self._rows("SELECT json FROM events WHERE experiment_id=? AND trader=? ORDER BY id", (exp_id, tf))
