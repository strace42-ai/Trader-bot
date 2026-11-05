"""Загрузка набора записанных рыночных данных (datasets/<id>/manifest.json). Без сети."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import candles

DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "datasets" / "fables_2026-11-05"


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path: Path | str = DEFAULT_DATASET) -> dict:
    d = Path(path)
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    now = _ts(man["recorded_at"])
    series = {tf: candles.from_gt_payload(json.loads((d / fn).read_text(encoding="utf-8")), tf, now)
              for tf, fn in man["candles"].items()}
    snap = None
    if man.get("snapshot") and (d / man["snapshot"]).exists():
        raw = json.loads((d / man["snapshot"]).read_text(encoding="utf-8"))
        p = (raw.get("pairs") or [{}])[0]
        tx = p.get("txns") or {}
        snap = {"source": man.get("snapshot_source", "DexScreener"), "ts": now,
                "price_usd": _f(p.get("priceUsd")), "price_usdg": _f(p.get("priceNative")),
                "liquidity_usd": _f((p.get("liquidity") or {}).get("usd")),
                "liquidity_base": _f((p.get("liquidity") or {}).get("base")),
                "liquidity_quote": _f((p.get("liquidity") or {}).get("quote")),
                "fdv": _f(p.get("fdv")), "market_cap": _f(p.get("marketCap")),
                "volume": {k: _f(v) for k, v in (p.get("volume") or {}).items()},
                "price_change": {k: _f(v) for k, v in (p.get("priceChange") or {}).items()},
                "txns": tx, "pair": p.get("pairAddress"), "dex": p.get("dexId"), "labels": p.get("labels")}
    quote = {"ts": now, "mid": snap["price_usdg"], "source": "DexScreener snapshot (priceNative, USDG)"} if snap and snap["price_usdg"] else None
    return {"id": man["id"], "path": str(d), "manifest": man, "recorded_at": now, "series": series, "snapshot": snap, "quote": quote}
