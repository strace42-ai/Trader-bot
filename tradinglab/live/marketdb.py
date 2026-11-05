"""Доступ к живым рыночным данным в trading_lab.db (таблицы схемы v3)."""
from __future__ import annotations

import json

from ..candles import TF_SEC, Series, normalize


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


class MarketDB:
    def __init__(self, store):
        self.st = store

    # ------------------------------------------------------------------ свечи
    def store_closed(self, tf: str, rows: list[list], now: int, backfill: bool, source: str = "geckoterminal") -> dict:
        """Сохранить ЗАКРЫТЫЕ свечи. Существующая свеча не перезаписывается: отличие → candle_revisions.
        available_at — момент, когда система впервые получила закрытую свечу (не время закрытия)."""
        d = TF_SEC[tf]
        new, revised = [], 0
        with self.st.tx() as c:
            for r in rows:
                ts = int(r[0])
                o, h, l, cl, v = (float(x or 0) for x in r[1:6])
                old = c.execute("SELECT o,h,l,c,v FROM live_candles WHERE tf=? AND ts=?", (tf, ts)).fetchone()
                if old is None:
                    c.execute("INSERT INTO live_candles VALUES(?,?,?,?,?,?,?,?,?,?,?)", (tf, ts, o, h, l, cl, v, source, ts + d, now, int(backfill)))
                    new.append(ts)
                elif any(abs(a - b) > 1e-12 * max(1.0, abs(a)) for a, b in zip(old, (o, h, l, cl, v))):
                    last = c.execute("SELECT o,h,l,c,v FROM candle_revisions WHERE tf=? AND ts=? ORDER BY id DESC LIMIT 1", (tf, ts)).fetchone()
                    if last is None or tuple(last) != (o, h, l, cl, v):
                        c.execute("INSERT INTO candle_revisions(tf, ts, seen_at, o, h, l, c, v) VALUES(?,?,?,?,?,?,?,?)", (tf, ts, now, o, h, l, cl, v))
                        revised += 1
        return {"new": new, "revised": revised}

    def set_forming(self, tf: str, row: list | None, now: int) -> None:
        with self.st.tx() as c:
            if row is None:
                c.execute("DELETE FROM forming WHERE tf=?", (tf,))
            else:
                c.execute("INSERT INTO forming VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tf) DO UPDATE SET ts=excluded.ts, o=excluded.o, h=excluded.h, "
                          "l=excluded.l, c=excluded.c, v=excluded.v, fetched_at=excluded.fetched_at", (tf, int(row[0]), *[float(x or 0) for x in row[1:6]], now))

    def forming(self, tf: str) -> dict | None:
        r = self.st._one("SELECT * FROM forming WHERE tf=?", (tf,))
        return dict(r) if r else None

    def candles(self, tf: str, now: int) -> tuple[Series, dict]:
        """Ряд закрытых свечей + {ts: available_at}."""
        rows = self.st._all("SELECT ts,o,h,l,c,v,available_at FROM live_candles WHERE tf=? ORDER BY ts", (tf,))
        s = normalize([list(r)[:6] for r in rows], tf, now)
        return s, {r[0]: r[6] for r in rows}

    def last_closed_ts(self, tf: str) -> int | None:
        r = self.st._one("SELECT MAX(ts) FROM live_candles WHERE tf=?", (tf,))
        return r[0] if r and r[0] is not None else None

    def count(self, tf: str) -> int:
        return self.st._one("SELECT COUNT(*) FROM live_candles WHERE tf=?", (tf,))[0]

    # ------------------------------------------------------------------ котировки, свопы, контекст
    def add_quote(self, ts: int, kind: str, mid: float | None, payload: dict) -> None:
        with self.st.tx() as c:
            c.execute("INSERT INTO quotes(ts, kind, mid, json) VALUES(?,?,?,?)", (ts, kind, mid, _j(payload)))

    def last_quote(self, kind: str) -> dict | None:
        r = self.st._one("SELECT ts, mid, json FROM quotes WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
        return {"ts": r[0], "mid": r[1], **json.loads(r[2])} if r else None

    def quotes_since(self, kind: str, since: int) -> list[dict]:
        return [{"ts": r[0], "mid": r[1], **json.loads(r[2])} for r in self.st._all("SELECT ts, mid, json FROM quotes WHERE kind=? AND ts>=? ORDER BY ts", (kind, since))]

    def add_swaps(self, rows: list[dict], now: int) -> int:
        n = 0
        with self.st.tx() as c:
            for s in rows:
                cur = c.execute("INSERT OR IGNORE INTO swaps VALUES(?,?,?,?,?,?,?,?)",
                                (s["block"], s["log_index"], now, s["side"], s["fee_ppm"], s["usdg"], s["fables"], s["tx"]))
                n += cur.rowcount
        return n

    def recent_fees(self, n: int = 30) -> dict:
        """Медиана fee_ppm последних n свопов по направлению (комиссия hook может меняться)."""
        out = {}
        for side in ("BUY", "SELL"):
            rows = [r[0] for r in self.st._all("SELECT fee_ppm FROM swaps WHERE side=? ORDER BY block DESC, log_index DESC LIMIT ?", (side, n))]
            if rows:
                rows.sort()
                out[side] = rows[len(rows) // 2]
                out[f"{side}_n"] = len(rows)
                out[f"{side}_distinct"] = sorted(set(rows))
        last = self.st._one("SELECT MAX(observed_at), MAX(block) FROM swaps")
        out["observed_at"], out["block"] = (last[0], last[1]) if last else (None, None)
        out["source"] = f"медиана последних {n} событий Swap пула"
        return out

    def swap_stats(self, since_block: int) -> dict:
        r = self.st._all("SELECT side, COUNT(*), SUM(usdg) FROM swaps WHERE block>=? GROUP BY side", (since_block,))
        return {s: {"n": n, "usdg": u} for s, n, u in r}

    def add_context(self, ts: int, kind: str, payload) -> None:
        with self.st.tx() as c:
            c.execute("INSERT INTO context(ts, kind, json) VALUES(?,?,?)", (ts, kind, _j(payload)))

    def last_context(self, kind: str) -> dict | None:
        r = self.st._one("SELECT ts, json FROM context WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
        return {"ts": r[0], "data": json.loads(r[1])} if r else None

    # ------------------------------------------------------------------ kv, события данных
    def kv_set(self, key: str, value, now: int) -> None:
        with self.st.tx() as c:
            c.execute("INSERT INTO kv VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (key, _j(value), now))

    def kv(self, key: str, default=None):
        r = self.st._one("SELECT value, updated_at FROM kv WHERE key=?", (key,))
        return (json.loads(r[0]), r[1]) if r else (default, None)

    def event(self, ts: int, kind: str, payload: dict) -> None:
        with self.st.tx() as c:
            c.execute("INSERT INTO data_events(ts, kind, json) VALUES(?,?,?)", (ts, kind, _j(payload)))

    def events(self, limit: int = 100) -> list[dict]:
        return [{"ts": r[0], "kind": r[1], **json.loads(r[2])} for r in self.st._all("SELECT ts, kind, json FROM data_events ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ команды
    def enqueue(self, ts: int, command: str, args: dict | None = None) -> int:
        with self.st.tx() as c:
            cur = c.execute("INSERT INTO commands(ts, command, args) VALUES(?,?,?)", (ts, command, _j(args or {})))
            return cur.lastrowid

    def pending_commands(self) -> list[dict]:
        return [dict(r) for r in self.st._all("SELECT * FROM commands WHERE status='PENDING' ORDER BY id")]

    def finish_command(self, cid: int, status: str, result, now: int) -> None:
        with self.st.tx() as c:
            c.execute("UPDATE commands SET status=?, result=?, done_at=? WHERE id=? AND status='PENDING'", (status, _j(result), now, cid))

    def command(self, cid: int) -> dict | None:
        r = self.st._one("SELECT * FROM commands WHERE id=?", (cid,))
        return dict(r) if r else None

    # ------------------------------------------------------------------ синхронные оценки FORWARD
    def add_mark(self, exp: str, ts: int, payload: dict) -> bool:
        with self.st.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO fw_marks VALUES(?,?,?)", (exp, ts, _j(payload)))
            return cur.rowcount == 1

    def marks(self, exp: str) -> list[dict]:
        return [{"ts": r[0], **json.loads(r[1])} for r in self.st._all("SELECT ts, json FROM fw_marks WHERE experiment_id=? ORDER BY ts", (exp,))]
