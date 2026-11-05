# FABLES Trading Lab

Исследовательская система **виртуальной (PAPER) спотовой торговли** FABLES/USDG (Uniswap v4, Robinhood Chain, chain id 4663).
Реальных транзакций нет: в коде нет приватных ключей, подписи и отправки транзакций.

Три независимых портфеля по $1 000 с разными стратегиями:

| Трейдер | Стратегия | Идея |
|---|---|---|
| 1H | `S1-1H-sweep-reversal` | отказ от пробоя минимума (liquidity sweep) с подтверждением денежным потоком |
| 4H | `S2-4H-structure-pullback` | смена структуры (CHoCH/BOS) и вход на откате в log-Fib зону |
| 1D | `S3-1D-range-breakout` | пробой диапазона накопления с объёмом и фундаментальными фильтрами |

Цель — проверить, есть ли устойчивое положительное ожидание после всех издержек и можно ли обойти Buy & Hold.
Гарантий прибыли нет; результаты на короткой истории FABLES помечаются `NOT VALIDATED`.

## Ключевые факты рынка (измерено 2026-11-05)
- Пул `0xcce8dcf7…a970869e` = `launchPoolId` официального конфига fables.fi; PoolKey сверен с poolId.
- Комиссия hook асимметрична: **покупка 0,5%, продажа 1,0%** (88 свопов, `evidence/fables_swaps/`).
- История: 1H — 376 закрытых свечей, 4H — 94, 1D — 16 (дневная стратегия в `WARMUP` до 30 свечей).

## Структура
```
tradinglab/
  candles.py      закрытые свечи, дедупликация, пропуски (без выдуманных свечей)
  indicators.py   EMA/SMA, RSI, MACD, ATR, ADX/DMI, Bollinger, MFI, CMF, OBV, отн. объём
  structure.py    pivots, BOS/CHoCH, sweeps, EQH/EQL, FVG, S/R, log-Fibonacci
  divergence.py   подтверждённые regular/hidden дивергенции
  analysis.py     снимок на баре i, INSUFFICIENT_HISTORY, режим рынка
  strategies.py   три стратегии v1.0.0 с условиями ✓/✗ и объяснением
  execution.py    модель издержек (fee, импакт, slippage, газ), точность 18/6 знаков
  portfolio.py    Decimal, FIFO, инварианты учёта
  risk.py         risk-v1: лимиты, cooldown, kill switch, размер позиции
  engine.py       автомат состояний трейдера
  store.py        SQLite, append-only журналы, BACKTEST/FORWARD раздельно
  backtest.py     backtest без look-ahead + Buy & Hold с того же момента
  performance.py  метрики
docs/             архитектура, спецификация стратегий, отчёты этапов
tests/            тесты + записанные реальные свечи (tests/fixtures/trading_lab)
```

## Запуск
```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m tradinglab backtest --fixtures tests/fixtures/trading_lab --now 2026-11-05T13:27:00Z
```
Backtest работает офлайн на записанных данных. Forward paper trading (живые данные, фоновый процесс) ещё не реализован
и будет включаться только с отдельного разрешения.

## Состояние
Этапы A–C готовы (аудит и архитектура, аналитическое ядро, paper-движок). Далее: этап D — панель, этап E — forward-режим.
Подробно: `docs/STAGE_A_ARCHITECTURE.md`, `docs/STAGE_BC_REPORT.md`.
