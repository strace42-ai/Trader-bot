# Этап E — автономный Forward Paper Trading на живых данных FABLES (отчёт, 2026-11-05)

Репозиторий `strace42-ai/Trader-bot`, ветка `claude/stage-e-forward-live` (от `claude/stage-d-web-terminal`, `cd34332`).
Defi-Alpha не использовался и не изменялся. Реальных транзакций, приватных ключей и отправки свопов нет: RPC-транспорт
разрешает только `eth_call`, `eth_getLogs`, `eth_blockNumber`, `eth_gasPrice`, `eth_chainId` (проверяется тестом).

## 1. Архитектура
```
bot (tradinglab.live.worker) ── единственный писатель торгового состояния
  Market Data Service (collector) → trading_lab.db (live_candles / forming / quotes / swaps / context)
  Forward Scheduler (forward.on_bars)   — решение только по закрытой свече в окне актуальности
  Risk Monitor (forward.on_quote)       — исполнение и стопы по проверенной котировке, независимо от ТФ
  синхронные оценки (forward.mark)      — все портфели и B&H по одной котировке раз в 5 мин
web (tradinglab.web.app)  ── читает БД, команды ставит в очередь `commands`
storage: data/trading_lab.db (SQLite WAL, схема v3, контролируемые миграции)
```
Режимы запуска: `serve` (бот + веб, `start.bat`), `bot`, `web`. Один бот на папку данных (блокировка файла).

## 2. Живые данные
| Источник | Что | Частота | Проверено 2026-11-05 17:11 UTC |
|---|---|---|---|
| GeckoTerminal | OHLCV 1H / 4H / 1D пула `0xcce8…869e` | после ожидаемого закрытия +180 с, повтор 120 с; обновление 10/15/30 мин | 380 / 95 / 16 закрытых свечей |
| DexScreener | цена, ликвидность, объём, buys/sells | 60 с | 0,0594 USDG, ликвидность $685K |
| Robinhood RPC | StateView `getSlot0` + `getLiquidity`, `eth_blockNumber` | 60 с | mid 0,059409, L 2,26·10¹⁹ |
| Robinhood RPC | `eth_getLogs` Swap **только этого пула** (комиссия hook) | 5 мин, ≤ 20 000 блоков за вызов | BUY 5000 ppm (n=12), SELL 10000 ppm (n=26) |
| Robinhood RPC | `eth_gasPrice` | 10 мин | ≈ $0,0126 за своп (250k gas) |
| CoinGecko | BTC, ETH, USDG; дневные ETH/BTC (закрытые дни) | 1 ч | ETH $2 255, BTC $76 134, USDG $1,00 |

Лимиты ниже документированных (GT 8/мин, DS 30/мин, RPC 60/мин, CoinGecko 4/мин), повторы с задержкой, Retry-After, кеш,
circuit breaker. Индексации сети и сбора посторонних токенов нет.

**Свечи.** Закрытая свеча (now ≥ ts + Δ + 180 с) сохраняется один раз и не меняется (триггер); ретро-правки источника → `candle_revisions`.
Формирующаяся — отдельно (`forming`), только для графика. Время закрытия (`close_ts`) и время получения системой (`available_at`)
хранятся раздельно; при старте/восстановлении загружается история (`backfill`), пропуски фиксируются (`candle_gap`) и не заполняются.

## 3. Трейдеры и Forward Scheduler
- Каждый трейдер обрабатывает каждую новую закрытую свечу своего ТФ (UTC): 1H — каждый час, 4H — 00/04/…/20, 1D — 00:00.
- Свеча, полученная позже окна актуальности (1H 30 мин, 4H 60 мин, 1D 3 ч), — `SKIPPED`: решения и сделок задним числом нет.
- Стратегии v1.0.0 и их параметры не менялись (решения backtest идентичны этапам C/D: 1H WARMUP 20 / WAIT 353, 4H 32 / 61, 1D 15).
- Для решения используются фактическая комиссия hook, ликвидность и газ; запасы на slippage — консервативные, как в backtest.
- S2 получает рыночный контекст ETH (EMA20 по закрытым дням, относительная сила 7 д); S3 — buys/sells h24 и изменение ликвидности за 7 д
  (до накопления 7 дней снимков — «не проверено»). 1D остаётся в `WARMUP` до 30 дневных свечей (≈ 19.11), синтетики нет.

## 4. Risk Monitor (между закрытиями свечей)
Каждые ~60 с, на каждой новой котировке:
- проверенная котировка = StateView (on-chain) при свежести ≤ 180 с; при наличии DexScreener расхождение ≤ 2%;
  скачок > 15% требует подтверждения следующей котировкой;
- стоп, trailing и цель проверяются для всех трейдеров независимо от ТФ; стоп исполняется по первой проверенной котировке
  ниже уровня с дополнительным проскальзыванием (не по цене стопа);
- недостоверная котировка: выход не имитируется, позиция помечается «без проверки», попытки продолжаются;
- новые входы блокируются: недостоверная котировка, ликвидность < 60% медианы 24 ч, USDG отклонился > 0,5%, комиссия hook > 3%;
  отложенный вход ждёт условий не дольше 15 мин / 45 мин / 3 ч (1H/4H/1D), затем отменяется.

## 5. Модель исполнения (оценка, не подтверждённый своп)
`tradinglab/live/clmm.py`: своп по формулам концентрированной ликвидности Uniswap v4 в активном диапазоне (√P и L из StateView),
комиссия — медиана последних событий Swap по направлению (не константа), газ — `eth_gasPrice` × 250k × ETH/USD,
задержка исполнения — +0,1%. Смещение > 20 тиков (возможен выход из диапазона) → берётся худшая из оценок CLMM и консервативной
x·y=k-модели с пометкой. Нет состояния пула → `conservative-fallback`. V4Quoter для этой сети в официальном конфиге не найден.
Пример (измеренный пул): $500 покупка — импакт $0,069 (0,014%); полный круг $500 → $491,40 (−1,72%).

В каждой операции хранятся: время сигнала (закрытие свечи), получения свечи, решения и исполнения; ожидаемая цена и mid исполнения;
размер; fee (и ppm, источник), impact, slippage, gas; модель; источник котировки; причина.

## 6. Управление
START, PAUSE, RESUME, STOP (**без автопродажи** — исправлена логика этапа D, где STOP закрывал позиции), CLOSE POSITION,
EMERGENCY EXIT (подтверждение, закрытие всех позиций, RUNNING → PAUSED). Команды проходят через очередь бота, журналируются в
`controls` вместе с состоянием портфелей, переживают перезапуск; устаревшие (> 60 с) не исполняются. Управляющие API — только
127.0.0.1, токен установки и проверка Origin.

## 7. Панель
Сохранён дизайн этапа D; FORWARD · LIVE — режим по умолчанию. Добавлено: живая цена и её свежесть, статус проверки котировки,
источники и heartbeat бота, капитал/PnL/B&H по синхронной оценке, формирующаяся свеча и линия текущей цены, отсчёт до закрытия,
CLOSE POSITION, полный журнал с временами и моделью, таблица `Условие | Значение | Требование | Результат`, статистика причин
отклонённых входов, FORWARD-график и метрики на странице Performance. Проверено в Chromium на реальных данных (2560×1440, 390×844),
ошибок консоли нет.

## 8. Контрольные проверки ТЗ → тесты
| # | Проверка | Тест |
|---|---|---|
| 1 | три стратегии получают реальные данные и независимо решают | `test_three_strategies_decide_independently_on_real_stream` + `python -m tradinglab check` на живых API |
| 2 | повторная обработка свечи не создаёт сделку | `test_same_candle_not_reprocessed_and_restart_restores`, `test_reprocessing_bar_is_noop` |
| 3 | сделка не раньше сигнала | `test_execution_after_signal_at_live_quote` |
| 4 | отключение API не создаёт свечей и исполнений | `test_source_outage_creates_no_candles_or_executions_and_late_bars_are_skipped`, `test_missing_candle_is_not_filled_and_blocks_entries`, `test_collector_never_stores_forming_candle_as_closed` |
| 5 | стоп независимо от ТФ | `test_daily_trader_stop_is_checked_between_daily_closes`, `test_single_tick_spike_requires_confirmation`, `test_unreliable_quote_does_not_fake_exit` |
| 6 | портфели не смешиваются | `test_costs_reduce_capital_and_portfolios_isolated`, `test_three_portfolios_isolated` |
| 7 | перезапуск не сбрасывает капитал и журнал | `test_same_candle_not_reprocessed_and_restart_restores`, `test_offline_period_is_not_traded_retroactively` |
| 8 | FORWARD ≠ BACKTEST | `test_forward_separate_from_backtest_and_marks_are_synchronous`, `test_backtest_and_forward_are_separated` |
| 9 | издержки уменьшают капитал | `test_costs_reduce_capital_and_portfolios_isolated`, `test_clmm_round_trip_loses_fees_and_uses_observed_fee` |
| 10 | панель показывает фактические данные | `test_live_overview_and_terminal_show_actual_bot_data`, `test_pages_and_backtest_still_match_core` |
| 11 | PAUSE / STOP / аварийный выход | `test_pause_blocks_entries_but_keeps_stop_protection`, `test_stop_does_not_sell_and_freezes_experiment`, `test_emergency_exit_closes_all_and_pauses`, `test_all_controls_through_api`, `test_commands_via_queue_expire_and_restart_restores_status` |
| 12 | работа без Claude и без браузера | `test_worker_runs_unattended_in_background_thread`, `test_web_only_with_separate_bot_process_uses_queue` |

Интеграционные тесты воспроизводят записанный поток реальных данных (`tests/replay.py`): реальные свечи с задержкой GeckoTerminal,
согласованные котировки пула и DexScreener, сценарии пропуска свечей, скачков, расхождения источников, аварий, офлайна и рестарта.

## 9. Ограничения
- Исполнение — оценочная модель (активный диапазон CLMM), не подтверждённый quote; пересечение тиков не моделируется точно.
- Котировки опрашиваются раз в ~60 с: внутриминутные проколы стопа не видны (как и реальному боту с таким опросом).
- Цены свечей — USD (GeckoTerminal), исполнение — USDG; при отклонении USDG > 0,5% входы блокируются.
- 1D в WARMUP до 30 дневных свечей; S3-фильтр ликвидности — после 7 дней снимков.
- Старый контроллер этапа D (STOP с автопродажей) заменён; `tests/test_forward.py` удалён — новая семантика в интеграционных тестах.
- Бот работает, пока запущен процесс на ПК; спящий режим/выключение = пауза в наблюдении (без сделок задним числом).
