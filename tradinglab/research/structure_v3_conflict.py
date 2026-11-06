"""Исследование: обработка конфликта масштабов (вторая форма подмены масштаба). Альтернативы сравниваются,
ни одна не меняет сам автомат major (он остаётся по правилам S1/S2); результат — диагностическая пометка.

  none    — без пометки;
  chain   — конфликт, если в текущем тренде есть более ранний protected-уровень дальше нарушенного (цепочка protected);
  senior  — конфликт, если автомат того же типа на старшем масштабе (k_senior·ATR) держит противоположное направление
            с непробитым protected (реализовано в structure_v3.Engine как SCALE_CONFLICT);
  htf     — то же по старшему таймфрейму (4H для 1H); при UNDEFINED/TRANSITION на HTF — «нет данных», не подтверждение.
"""
from __future__ import annotations

import bisect

from .. import structure_v3 as S
from ..candles import TF_SEC, Series


def choch_flags(s: Series, htf: Series | None = None) -> list[dict]:
    e = S.Engine(s.tf).run(s)
    m = e.major
    ev = m.events
    he = S.Engine(htf.tf).run(htf) if htf is not None else None
    hclose = [t + TF_SEC[htf.tf] for t in he.ts] if he else []
    out = []
    for k, x in enumerate(ev):
        if not (x["type"].startswith("CHoCH") and not x.get("restore")):
            continue
        up = x["type"] == "CHoCH_UP"
        # начало текущего тренда: последнее начальное/подтверждающее событие или восстановление до этого CHoCH
        start = max((y["idx"] for y in ev[:k] if y.get("initial") or y.get("confirmation") or y.get("restore")), default=0)
        side = "H" if up else "L"
        chain = [p for p in m.prot_hist if p["side"] == side and p["status"] == "PROTECTED" and start <= p["set_idx"] < x["idx"]
                 and (p["price"] > x["level"] if up else p["price"] < x["level"])]
        sen = e.senior.states[x["idx"]]
        sen_dir = sen["trend"] if sen["trend"] in ("UP", "DOWN") else None
        hdir = None
        if he:
            j = bisect.bisect_right(hclose, e.ts[x["idx"]] + e.dt) - 1
            if j >= 0:
                hs = he.major.states[j]
                hdir = hs["trend"] if hs["trend"] in ("UP", "DOWN") else "НЕТ ДАННЫХ"
        want = "UP" if up else "DOWN"
        out.append({"idx": x["idx"], "type": x["type"], "level": x["level"],
                    "chain": bool(chain), "senior": sen_dir is not None and sen_dir != want,
                    "htf": hdir not in (None, "НЕТ ДАННЫХ") and hdir != want, "htf_state": hdir})
    return out
