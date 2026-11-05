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

## Установка и запуск на Windows
1. Установите **Python 3.11+** с <https://www.python.org/downloads/> (при установке отметьте «Add python.exe to PATH»).
2. Скачайте репозиторий: кнопка **Code → Download ZIP** на GitHub и распаковка, либо `git clone https://github.com/strace42-ai/Trader-bot`.
3. Дважды щёлкните **`start.bat`**. При первом запуске создаётся окружение `.venv` и ставятся зависимости (нужен интернет только для pip).
4. Панель откроется в браузере: **<http://127.0.0.1:8770/>** (страница сравнения — <http://127.0.0.1:8770/performance>).
5. Остановка: закройте окно консоли или нажмите `Ctrl+C`. Состояние хранится в `data\trading_lab.db`.

Параметры: `start.bat --port 8780` (другой порт), `start.bat --no-browser`. Linux/macOS: `./start.sh`.
Сервер слушает только `127.0.0.1`, не делает сетевых запросов к рынку и не запускает фоновых процессов; работает, пока открыто окно.

## Панель
- **Верхняя панель**: цена FABLES/USD и FABLES/USDG, ликвидность, стартовый капитал $3 000, стоимость портфелей, Net PnL,
  Buy & Hold и сравнение с ним (все портфели и B&H оцениваются по одной общей котировке), статусы источников и движка,
  кнопки START / PAUSE / RESUME / STOP (только для FORWARD paper trading).
- **Режимы**: `BACKTEST` — результат офлайн-бэктеста на записанных свечах (`NOT VALIDATED`); `FORWARD` — отдельный эксперимент,
  до запуска показывает `NOT STARTED`.
- **Три терминала 1H / 4H / 1D**: финансовые метрики; свечной график (TradingView Lightweight Charts 5.2.1) с EMA, Bollinger,
  объёмом, уровнями S/R, log-Fibonacci, BOS/CHoCH, дивергенциями и маркерами сделок (подсказка с параметрами операции);
  панели MACD / MFI / CMF / OBV; позиция, журнал операций с фильтрами, завершённые сделки; вкладки анализа
  «Обзор / Индикаторы / Структура / Ончейн / Решения / Диагностика» — всё строится из решений и расчётов ядра.
- **Performance**: equity трёх трейдеров, Buy & Hold и кеша на одном графике + таблица метрик; FORWARD — отдельно.

### FORWARD: что делают кнопки
| Команда | Действие |
|---|---|
| START | новый FORWARD-эксперимент: неизменяемый момент начала, три портфеля по $1 000 |
| PAUSE | новые входы запрещены, отложенные входы отменяются; стопы и выходы защищают позиции |
| RESUME | снятие паузы |
| STOP | новые входы запрещены; открытые позиции закрываются по первой доступной цене; эксперимент завершается |

Каждая команда записывается в журнал `controls` вместе с состоянием портфелей одной транзакцией и переживает перезапуск.
**Источник живых данных пока не подключён** (нужно отдельное разрешение на сетевой опрос и фоновый процесс), поэтому после START
новых свечей нет и сделок не будет — панель показывает это явно, а не имитирует работу.

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
  performance.py  метрики и синхронная оценка
  forward.py      START/PAUSE/RESUME/STOP и обработка новых свечей FORWARD
  dataset.py      загрузка записанного набора данных
  web/            сервер FastAPI и фронтенд (vanilla JS, без сборки)
datasets/         записанные реальные данные (свечи GeckoTerminal, снимок DexScreener)
docs/             архитектура, спецификация стратегий, отчёты этапов
tests/            тесты
```

## Разработка и тесты
```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m tradinglab backtest --fixtures datasets/fables_2026-11-05 --now 2026-11-05T13:27:00Z
```
(на Windows: `.venv\Scripts\python.exe` вместо `.venv/bin/python`)

## Состояние
Этапы A–D готовы (архитектура, аналитическое ядро, paper-движок, веб-терминал). Далее: этап E — подключение живых данных
для FORWARD (только после разрешения). Подробно: `docs/STAGE_A_ARCHITECTURE.md`, `docs/STAGE_BC_REPORT.md`, `docs/STAGE_D_REPORT.md`.

Сторонний код: TradingView Lightweight Charts™ 5.2.1 (Apache-2.0, © TradingView, Inc.), `tradinglab/web/static/vendor/`.
