"""Сетевой транспорт живого режима: только GET/eth_call/eth_getLogs (read-only).

- равномерный лимитер на источник (запросов в минуту), учёт Retry-After при 429;
- повторы с экспоненциальной задержкой при сетевых ошибках и 5xx (ограниченное число, чтобы не блокировать Risk Monitor);
- короткий кеш одинаковых запросов;
- circuit breaker: после 3 подряд неудач источник пропускается на растущий интервал (до 10 мин);
- здоровье источников (последний успех/ошибка, задержка) — для панели.
Транспорт подменяется в тестах (ReplayTransport) — логика сборщика одинакова.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from . import config as C

USER_AGENT = "FABLES-Trading-Lab/0.2 (local read-only paper-trading research)"


class SourceError(Exception):
    def __init__(self, source: str, kind: str, message: str, status: int | None = None):
        super().__init__(f"[{source}] {kind}: {message}")
        self.source, self.kind, self.message, self.status = source, kind, message, status


class Health:
    """Состояние источников (потокобезопасно)."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.lock = threading.Lock()
        self.s: dict[str, dict] = {}

    def ok(self, src: str, ms: float) -> None:
        with self.lock:
            h = self.s.setdefault(src, {"calls": 0, "errors": 0})
            h.update(last_ok=int(self.clock()), latency_ms=round(ms), consecutive_fail=0, calls=h["calls"] + 1, open_until=0)

    def fail(self, src: str, err: str) -> None:
        with self.lock:
            h = self.s.setdefault(src, {"calls": 0, "errors": 0})
            n = h.get("consecutive_fail", 0) + 1
            h.update(last_error=err[:300], last_error_at=int(self.clock()), consecutive_fail=n, calls=h["calls"] + 1,
                     errors=h["errors"] + 1)
            if n >= 3:
                h["open_until"] = self.clock() + min(600, 30 * 2 ** (n - 3))

    def available(self, src: str) -> bool:
        with self.lock:
            return self.clock() >= self.s.get(src, {}).get("open_until", 0)

    def snapshot(self) -> dict:
        with self.lock:
            now = self.clock()
            return {k: {**v, "open_for_sec": max(0, round(v.get("open_until", 0) - now))} for k, v in self.s.items()}


class HttpTransport:
    def __init__(self, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep, timeout: float = 15.0):
        import httpx
        self.clock, self.sleep = clock, sleep
        self.health = Health(clock)
        self.client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), headers={"User-Agent": USER_AGENT},
                                   follow_redirects=True)
        self._next: dict[str, float] = {}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._rpc_id = 0

    def _wait(self, src: str) -> None:
        gap = 60.0 / C.RATE_PER_MIN[src]
        with self._lock:
            now = time.monotonic()
            t = max(self._next.get(src, 0.0), now)
            self._next[src] = t + gap
        if t > now:
            self.sleep(t - now)

    def _penalize(self, src: str, sec: float) -> None:
        with self._lock:
            self._next[src] = max(self._next.get(src, 0.0), time.monotonic() + sec)

    def request(self, src: str, method: str, url: str, *, params: dict | None = None, body: Any = None, headers: dict | None = None,
                cache_ttl: float = 0, retries: int = 2) -> Any:
        key = f"{method} {url} {json.dumps(params, sort_keys=True)} {json.dumps(body, sort_keys=True)}"
        if cache_ttl:
            hit = self._cache.get(key)
            if hit and hit[0] > self.clock():
                return hit[1]
        if not self.health.available(src):
            raise SourceError(src, "circuit_open", "источник временно пропускается после серии ошибок")
        import httpx
        last = "unknown"
        for attempt in range(retries + 1):
            self._wait(src)
            t0 = time.monotonic()
            try:
                r = self.client.request(method, url, params=params, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"сеть: {type(e).__name__}"
                self.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 429:
                ra = r.headers.get("retry-after", "")
                wait = float(ra) if ra.replace(".", "").isdigit() else 20.0 * (attempt + 1)
                self._penalize(src, min(wait, 90))
                last = f"HTTP 429, пауза {min(wait, 90):.0f} с"
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                self.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code >= 400:
                self.health.fail(src, f"HTTP {r.status_code}: {r.text[:120]}")
                raise SourceError(src, "http_error", r.text[:200], r.status_code)
            try:
                data = r.json()
            except ValueError:
                last = "ответ не JSON"
                continue
            self.health.ok(src, (time.monotonic() - t0) * 1000)
            if cache_ttl:
                self._cache[key] = (self.clock() + cache_ttl, data)
            return data
        self.health.fail(src, last)
        raise SourceError(src, "unavailable", last)

    def get(self, src: str, url: str, params: dict | None = None, headers: dict | None = None, cache_ttl: float = 0) -> Any:
        return self.request(src, "GET", url, params=params, headers=headers, cache_ttl=cache_ttl)

    def rpc(self, method: str, params: list) -> Any:
        if method not in ("eth_call", "eth_getLogs", "eth_blockNumber", "eth_gasPrice", "eth_chainId"):
            raise ValueError(f"RPC {method} не разрешён: только read-only методы")
        self._rpc_id += 1
        r = self.request("rpc", "POST", C.RPC_URL, body={"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params})
        if "error" in r:
            self.health.fail("rpc", str(r["error"])[:200])
            raise SourceError("rpc", "rpc_error", str(r["error"])[:200])
        return r.get("result")

    def close(self) -> None:
        self.client.close()


class ReplayTransport:
    """Транспорт для тестов: ответы задаёт функция handler(src, url_or_method, params, now) → данные | исключение."""

    def __init__(self, handler: Callable, clock: Callable[[], float]):
        self.handler, self.clock = handler, clock
        self.health = Health(clock)
        self.calls: list[tuple] = []

    def _call(self, src: str, what: str, params) -> Any:
        self.calls.append((int(self.clock()), src, what))
        if not self.health.available(src):
            raise SourceError(src, "circuit_open", "источник временно пропускается")
        try:
            out = self.handler(src, what, params, self.clock())
        except SourceError as e:
            self.health.fail(src, e.message)
            raise
        self.health.ok(src, 1.0)
        return out

    def get(self, src: str, url: str, params: dict | None = None, headers: dict | None = None, cache_ttl: float = 0) -> Any:
        return self._call(src, url, params)

    def rpc(self, method: str, params: list) -> Any:
        if method not in ("eth_call", "eth_getLogs", "eth_blockNumber", "eth_gasPrice", "eth_chainId"):
            raise ValueError(f"RPC {method} не разрешён")
        return self._call("rpc", method, params)

    def close(self) -> None:
        pass
