"""Константы и настройки живого режима. Все запросы — read-only; только FABLES/USDG и рыночный контекст BTC/ETH/USDG."""
from __future__ import annotations

CHAIN = "robinhood"
CHAIN_ID = 4663
TOKEN = "0xb63d07cef74be747231d601bb2faa6b252c68106"      # FABLES, 18 знаков (currency1)
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"       # USDG, 6 знаков (currency0)
POOL_ID = "0xcce8dcf7c4dcfee6b6588db724240d9d6a22ee456f9387bb831c8fcea970869e"   # launchPoolId официального конфига
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
HOOK = "0x08e52564bad99e05a694b4809f397edca417a080"
TICK_SPACING = 200
DEC0, DEC1 = 6, 18

GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_BASE = "https://api.dexscreener.com"
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
CG_BASE = "https://api.coingecko.com/api/v3"

# keccak256 сигнатур (проверены: tests/test_live_units.py)
SEL_GET_SLOT0 = "c815641c"        # getSlot0(bytes32)
SEL_GET_LIQUIDITY = "fa6793d5"    # getLiquidity(bytes32)
TOPIC_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"  # Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)

# лимиты запросов в минуту (ниже документированных/измеренных)
RATE_PER_MIN = {"geckoterminal": 8, "dexscreener": 30, "rpc": 60, "coingecko": 4}

# расписание (секунды)
SCHEDULE = {
    "dexscreener": 60,
    "pool_state": 60,        # StateView: getSlot0 + getLiquidity
    "swaps": 300,            # события Swap только этого пула (комиссия hook, поток сделок)
    "gas": 600,
    "context": 3600,         # CoinGecko: BTC/ETH/USDG
    "gt_refresh": {"1h": 600, "4h": 900, "1d": 1800},   # обновление формирующейся свечи и страховочная проверка
    "gt_after_close": 180,   # первая проверка закрытой свечи через N с после закрытия
    "gt_retry": 120,         # повтор, если закрытая свеча ещё не появилась
    "mark": 300,             # синхронная оценка портфелей и B&H
}
SWAP_MAX_RANGE = 20_000      # блоков за один eth_getLogs (~35 мин при ~0,1 с/блок)
SWAP_INITIAL_LOOKBACK = 36_000

# окно актуальности закрытой свечи: если свеча получена позже close + N, решение по ней не принимается (бот не работал)
LATE_LIMIT = {"1h": 30 * 60, "4h": 60 * 60, "1d": 3 * 3600}
# сколько ждать проверенную котировку для отложенного входа, после — вход отменяется
ENTRY_TTL = {"1h": 15 * 60, "4h": 45 * 60, "1d": 3 * 3600}

# контроль котировки (Risk Monitor)
QUOTE_MAX_AGE = 180          # котировка старше — недостоверна
SOURCE_MAX_DIVERGENCE = 0.02  # пул (StateView) vs DexScreener
JUMP_CONFIRM = 0.15          # скачок > 15% к предыдущей проверенной котировке требует подтверждения вторым тиком
LIQ_DROP_RISK_OFF = 0.40     # ликвидность ниже 60% медианы 24 ч → новые входы запрещены
USDG_DEPEG = 0.005
FEE_MAX_PPM = 30_000         # комиссия hook > 3% → новые входы запрещены
WORKER_STALE_SEC = 30        # heartbeat старше — движок считается остановленным
GAS_UNITS = 250_000          # консервативно для свопа v4 через роутер
EXEC_SLIPPAGE = "0.001"      # задержка между котировкой и включением транзакции (оценка)
COMMAND_MAX_AGE = 60         # команда старше — не исполняется (EXPIRED)
