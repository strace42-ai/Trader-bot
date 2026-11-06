# SHADOW P2-4H — реализация (ветка `claude/shadow-p2-impl`)

Статус: **разработано, интегрировано и протестировано. Эксперимент НЕ запущен.** Процесс `shadow.bat` нигде не запускался
постоянно; START SHADOW даётся только вручную, на странице SHADOW LAB. PR не создавался и не объединялся.

## 1. Что сделано

| Требование | Реализация |
|---|---|
| FABLES/USDG, Robinhood Chain, только виртуальный спот, 4H | один трейдер `4h`, только long, исполнение paper |
| «SHADOW-P2-4H 1.0.0», $1 000, риск 1% | `tradinglab/shadow/strategy.py` — замороженная копия правил P2 research-0.1; `risk-v1 4H` (1%/сделку, экспозиция 0,8, лимит недели 4%, Max DD 18%) с `min_rr 1,5` |
| Правила зафиксированы до первого запуска | SHA-256 файла правил и модуля структуры v3 (`rules_hash`) и версия пишутся в неизменяемые параметры эксперимента при START (защищено триггером БД). Если код изменится, исполнение блокируется (`VERSION_MISMATCH`), START/RESUME запрещены. Новые правила — это новая версия и новый эксперимент |
| Своя база `data/shadow_lab.db` | эксперимент `SH-P2-4H-<ts>`, решения, операции, сделки, состояние, команды, оценки, B&H, события — всё в shadow_lab.db |
| Общие рыночные данные, без второго опроса API | SHADOW в сеть не ходит. Он читает `trading_lab.db` через `file:…?mode=ro` и `PRAGMA query_only` |
| Сигналы только по закрытым 4H | решения принимаются по `live_candles`, формирующаяся свеча не используется |
| Сопровождение на каждой новой котировке | Risk Monitor вызывается при каждой новой котировке пула или DexScreener (и не реже раза в минуту) |
| Защита от устаревших котировок, ошибок источников, пропусков, двойного исполнения | ShadowRunner наследует ForwardRunner: те же уровни ONCHAIN / OFFCHAIN_ONLY / NONE, подтверждение скачков, блокировки входа, ENTRY_TTL, DEGRADED-стоп по двум котировкам DexScreener, UNIQUE журналов |
| Нет сделок задним числом | решение пропускается (`SKIPPED`), если от закрытия свечи до момента решения **SHADOW** прошло больше 60 мин. Это учитывает и случай, когда основной бот получил свечу вовремя, а SHADOW был выключен (дефект найден тестом и исправлен) |
| Пометка paper | `exec_note` каждой операции: «PAPER: моделируемое исполнение SHADOW (не ончейн-своп)» |
| Отдельный процесс `shadow.bat` | `python -m tradinglab shadow`. `start.bat` и три трейдера не изменены |
| Восстановление, идемпотентность, сверка, атомарность | состояние и журнал пишутся одной транзакцией (`commit_bar`). При запуске и каждые 10 мин кеш и FABLES сверяются с журналом операций |
| Защита от двух процессов | блокировка ОС `data/shadow.lock`; второй процесс не стартует |
| Своё управление | очередь команд в shadow_lab.db: START / PAUSE / RESUME / STOP / CLOSE POSITION / EMERGENCY EXIT |
| Логи и диагностика | `data/logs/shadow.log`; heartbeat, ошибки и события в shadow_lab.db; вкладка «Готовность и диагностика» |
| Ошибки SHADOW не останавливают FORWARD | отдельный процесс и отдельная база. Каждая линия цикла (команды, свечи, котировки, оценки, сверка) в своём try. API SHADOW изолировано в вебе |

### Проверка данных в trading_lab.db

Всё нужное уже собирает основной бот, подставлять ничего не потребовалось:
- **цена:** `quotes(pool)` — StateView, `quotes(ds)` — DexScreener;
- **свежесть и качество:** возраст котировок, расхождение источников, `kv health`/`worker`;
- **тики CLMM:** `kv pool_ticks`;
- **комиссия hook:** таблица `swaps`, медиана последних 30 событий;
- **газ:** `kv gas`;
- **свечи:** `live_candles` с `available_at`.

Если основной бот не работает, данные устаревают. Тогда SHADOW блокирует входы, а позиция помечается как незащищённая: ничего не исполняется по старым ценам.

## 2. Архитектура

```
start.bat ─► основной процесс (без изменений): Worker → trading_lab.db (единственный писатель рыночных данных и FORWARD)
                                 └─ веб-панель :8770 ─ /shadow, /api/shadow* (читает shadow_lab.db только на чтение,
                                                       ставит команды в очередь shadow_lab.db)
shadow.bat ─► процесс SHADOW: ShadowProcess (цикл 1 с, блокировка data/shadow.lock, лог data/logs/shadow.log)
                 ├─ ReadOnlyStore(trading_lab.db, mode=ro) ─► MarketDB: свечи, котировки, тики, комиссии, газ
                 └─ Store(shadow_lab.db) ◄─ ShadowRunner(ForwardRunner) + SplitDB
```

`SplitDB` — это интерфейс MarketDB для ForwardRunner:
- рыночные чтения идут в основную базу (только чтение);
- `last_valid_quote`, `risk`, `bh:*`, оценки, события и команды идут в shadow_lab.db.

Запись в основную базу невозможна на трёх уровнях:
1. `SplitDB` отказывает в записи рыночных ключей;
2. `ReadOnlyStore.tx()` бросает исключение;
3. сама SQLite отвечает «attempt to write a readonly database».

Благодаря наследованию модель исполнения (v4-clmm), политика защиты, B&H и синхронные оценки у SHADOW — **те же, что у основного бота**, без копирования кода.

Цикл SHADOW:
1. heartbeat;
2. команды (старше 60 с — EXPIRED);
3. закрытые 4H (раз в 10 с, идемпотентно по `last_bar_ts`);
4. котировка (каждая новая или раз в минуту);
5. синхронная оценка (как у FORWARD);
6. сверка (раз в 10 мин).

START проходит проверку готовности:
- heartbeat основного бота;
- свежий StateView и согласие с DexScreener;
- свежие тики;
- комиссия hook;
- газ;
- закрытые 4H и 1D;
- 4H-история с запуска пула;
- совпадение хэша правил;
- целостность.

Сравнение на Performance (`/api/shadow/performance`): SHADOW, S2 (Trader 4H основного FORWARD, только чтение, нормирован к $1 000 на момент первой оценки SHADOW), B&H SHADOW (куплен по первой проверенной котировке после START SHADOW) и кеш $1 000. Все кривые берутся на общих отметках с момента активации. **SHADOW в $3 000 не входит.**

## 3. Изменённые и новые файлы

Новые:
- `tradinglab/shadow/__init__.py`;
- `strategy.py` — замороженные правила и хэш;
- `readonly.py` — `ReadOnlyStore`, `SplitDB`;
- `runner.py` — `ShadowRunner`, сравнение;
- `process.py` — процесс, блокировка, готовность, CLI;
- `web.py` — страница и API;
- `shadow.bat`;
- `tradinglab/web/static/shadow.html`, `shadow.js`, `shadow.css`;
- `tests/test_shadow.py`, `tests/ui/shadow_check.js`, `tests/ui/forming_check.js`;
- `docs/SHADOW_P2_IMPLEMENTATION.md`, `docs/shadow_screens/*.png`.

Изменённые:

| Файл | Изменение |
|---|---|
| `tradinglab/__main__.py` | подкоманда `shadow [--check]` (+7 строк) |
| `tradinglab/web/app.py` | подключение маршрутов SHADOW в try (+6 строк) |
| `tradinglab/web/static/common.js` | ссылка «SHADOW LAB» в навигации |
| `tradinglab/web/static/performance.html`, `perf.js` | секция сравнения SHADOW / S2 / B&H / кеш |
| `tradinglab/web/static/app.js` | **исправление UI:** убран маркер FORMING / «закрыта · ждёт GT» над последней свечой |
| `tests/test_ui_charts.py`, `tests/ui/live_check.js` | проверка отсутствия маркера; тесты 8 и 9 |

**Не изменены** (проверено `git diff`):
- `strategies.py`, `engine.py`, `forward.py`, `store.py`, `risk.py`, `execution.py`, `backtest.py`;
- весь `tradinglab/live/`, весь `tradinglab/research/`;
- `start.bat`.

## 4. Исправление UI: FORMING

Маркер-квадрат с надписью FORMING / «закрыта · ждёт GT» больше не рисуется на 1H, 4H и 1D.

Формирующаяся свеча по-прежнему:
- обновляется каждые 5 с: OHLC и объём;
- отображается полупрозрачной.

Её статус (FORMING / закрыта — ждёт GT / закрытая) показывается только в подсказке. Логика данных не менялась: стратегии по-прежнему используют только закрытые свечи.

## 5. Тесты

Полный набор: **218 passed** (на базовой ветке было 192, добавлено 26), Chromium через Playwright.

| № | Требование | Тесты |
|---|---|---|
| 1 | Состояние переживает рестарт | `test_state_survives_restart`, `test_offline_shadow_does_not_trade_retroactively` |
| 2 | Нет дублей | `test_no_duplicate_trades_or_commands`, `test_second_shadow_process_on_same_db_is_refused` |
| 3 | Комиссии, импакт, газ | `test_fees_impact_slippage_gas_are_charged_and_paper_labelled`: комиссия hook 0,5% / 1% из свопов, cash = журнал |
| 4 | Вход запрещён при невалидной котировке | `test_entry_forbidden_on_invalid_quote` (только DexScreener → отмена по TTL), `test_entry_forbidden_on_stale_quote_and_missing_ticks` |
| 5 | Стопы и скачки рынка | `test_stop_executes_at_gap_price_after_confirmed_jump` (одиночный выброс −50% не исполняет стоп; гэп исполняется по рыночной цене ниже стопа), `test_stop_is_checked_between_4h_closes_on_each_quote` |
| 6 | Основной FORWARD независим | `test_main_forward_unchanged_by_shadow` (решения, операции, состояние, оценки и ключи основной БД идентичны прогону без SHADOW), `test_shadow_cannot_write_main_db`, `test_shadow_errors_do_not_stop_main_or_shadow_loop`, `test_missing_main_db_waits_without_creating_it` |
| 7 | $3 000 и история S1/S2/S3 не меняются | `test_main_capital_and_strategies_untouched`, `test_comparison_shadow_vs_s2_bh_cash_since_activation` |
| 8 | Панель в Chromium | `test_shadow_lab_*`, `test_performance_compares_shadow_s2_bh_cash` |
| 9 | Нет FORMING и серого маркера, живая свеча обновляется | `test_no_forming_label_or_grey_marker_but_live_candle_updates[1h/4h/1d]`, `test_forming_status_kept_in_tooltip`, `test_forming_candle_replaces_live_line` |
| 10 | Backtest не изменился | `test_backtest_results_unchanged` — отпечаток решений, операций и equity S1/S2/S3 совпадает с эталоном, посчитанным на `claude/p2-shadow-prep` |

Дополнительно:
- `test_frozen_rules_match_research_p2_and_hash_is_recorded`;
- `test_changed_rules_block_execution_until_new_experiment`;
- эквивалентность замороженной копии и research P2 — 0 расхождений по всем барам FABLES 4H, DOGE 4H и BTC 4H.

Тесты исполнения (позиция, стоп, издержки) используют заглушку с одним входом вместо P2: на записанном отрезке P2 честно отвечает WAIT. Сами правила P2 в тестах не меняются.

## 6. Скриншоты (`docs/shadow_screens/`)

- `shadow_lab_preview.png` — до активации, реальные правила P2 на записанных данных. **WAIT** с причинами: Major 4H DOWN (protected high 0.07975), нет триггера, нет HL, стоп 10,2 ATR. Видны Major-свинги, protected/structural и события v3.
- `shadow_lab_position.png` — **тестовый сценарий с заглушкой входа** (не сигнал P2): позиция, линии вход / стоп / T1, издержки, защита FULL.
- `shadow_lab_journal.png` — журнал: ENTER → BUY → SELL (CLOSE POSITION), разбивка издержек, пометка PAPER, команды.
- `performance_shadow.png` — сравнение SHADOW / S2 / B&H / кеш с момента активации.

## 7. Обновление на Windows (сохраняя FW-1793900885)

Основной эксперимент хранится в `data\trading_lab.db`; обновление кода его не трогает.

1. Не останавливайте основной бот без необходимости. Если код обновляется через git — закройте окно `start.bat` (Ctrl+C): состояние сохранено, при запуске восстановится.
2. Резервная копия: скопируйте папку `data` целиком (например, в `data_backup_YYYYMMDD`).
3. Получите ветку:
   ```bash
   git fetch origin claude/shadow-p2-impl
   ```
   ```bash
   git checkout claude/shadow-p2-impl
   ```
   Не используйте `git reset --hard` и `git clean`.
4. Запустите `start.bat`. Проверка: на «Терминале» FORWARD — RUNNING, эксперимент **FW-1793900885**, капитал $3 000; в навигации появилась вкладка «SHADOW LAB».
5. Проверка готовности SHADOW без запуска цикла:
   ```bash
   shadow.bat --check
   ```
   Команда ничего не торгует. Она создаёт пустой `data\shadow_lab.db`, если его ещё нет.
6. **Только когда решите активировать:** откройте второе окно `shadow.bat`, затем на странице SHADOW LAB нажмите START. Без START процесс SHADOW только ждёт: эксперимента нет, сделок нет.
7. Откат: закройте `shadow.bat` и переключитесь на прежнюю ветку. `data\shadow_lab.db` можно оставить или удалить — `trading_lab.db` не затрагивается.

## 8. Проверка готовности перед активацией (чек-лист)

1. `start.bat` работает, FORWARD FW-1793900885 RUNNING, чипы источников зелёные.
2. `shadow.bat --check` → все пункты OK:
   - heartbeat основного бота;
   - StateView;
   - согласие с DexScreener;
   - тики CLMM;
   - комиссия hook;
   - газ;
   - 4H/1D;
   - 4H-история с запуска;
   - хэш правил `aa899ab973e8…`;
   - целостность.
3. Запущен `shadow.bat`; на SHADOW LAB чип «SHADOW-процесс: РАБОТАЕТ», вкладка «Готовность» зелёная.
4. START → подтверждение. После START проверьте:
   - баннер с номером `SH-P2-4H-…`;
   - в «Правила и версия» зафиксированный хэш совпадает с хэшем кода;
   - B&H куплен по первой проверенной котировке;
   - на Performance появилась секция SHADOW.
5. Основной FORWARD после START SHADOW: те же значения, сделки S1/S2/S3 не изменились.

Оценка результатов:
- контрольные точки — 30, 60 и 120 дней;
- 20 сделок — минимум для **предварительного** анализа, не доказательство;
- параметры под текущее поведение FABLES не подстраиваются.

## 9. Ограничения и замечания

- SHADOW зависит от работающего основного бота. Без него входы блокируются, а позиция становится незащищённой (NONE), пока данные не вернутся.
- Если основной бот остановлен, read-only открытие базы создаёт служебные файлы SQLite `-wal/-shm`. Содержимое базы не меняется (проверено тестом).
- Структура v3 работает только по закрытым свечам (L2): нарушение protected внутри свечи P2 не видит до закрытия 4H. Стоп при этом проверяется на каждой котировке.
- HOLDOUT уже использовался при выборе P2 (см. P2_SHADOW_READINESS.md), поэтому SHADOW — первый по-настоящему вневыборочный тест.
