"""Constants and enums for the arbitrage system."""

from enum import IntEnum


class MexcSide(IntEnum):
    OPEN_LONG = 1
    CLOSE_SHORT = 2
    OPEN_SHORT = 3
    CLOSE_LONG = 4


class MexcOrderType(IntEnum):
    LIMIT = 1
    POST_ONLY = 2
    IMMEDIATE_OR_CANCEL = 3
    FILL_OR_KILL = 4
    MARKET = 5


class MexcOpenType(IntEnum):
    ISOLATED = 1
    CROSS = 2


class Direction(IntEnum):
    """
    Direction of the arbitrage pair.
    LONG  = MT5 BUY  + MEXC SHORT (MT5 cheaper than MEXC)
    SHORT = MT5 SELL + MEXC LONG  (MT5 richer than MEXC)
    """

    NONE = 0
    LONG = 1
    SHORT = 2


class Signal(IntEnum):
    NONE = 0
    ENTRY_LONG = 1
    ENTRY_SHORT = 2
    CLOSE = 3


class MT5Action(IntEnum):
    BUY = 0
    SELL = 1


MT5_MAGIC = 123456


def mt5_lot_to_exchange_volume(
    mt5_volume: float,
    *,
    mt5_units_per_lot: float,
    exchange_units_per_contract: float,
) -> int:
    """Convert MT5 lot size to exchange-native contracts using product metadata."""
    if mt5_units_per_lot <= 0 or exchange_units_per_contract <= 0:
        raise ValueError("Volume conversion factors must be > 0")
    exposure_units = float(mt5_volume) * float(mt5_units_per_lot)
    return int(exposure_units / float(exchange_units_per_contract))


def exchange_volume_to_mt5_lot(
    exchange_volume: int | float,
    *,
    mt5_units_per_lot: float,
    exchange_units_per_contract: float,
    lot_precision: int = 2,
) -> float:
    """Convert exchange-native contracts back into MT5 lot size."""
    if mt5_units_per_lot <= 0 or exchange_units_per_contract <= 0:
        raise ValueError("Volume conversion factors must be > 0")
    exposure_units = float(exchange_volume) * float(exchange_units_per_contract)
    lot = exposure_units / float(mt5_units_per_lot)
    return round(lot, max(int(lot_precision), 0))


# Legacy XAU helpers kept for compatibility with older scripts.
MT5_LOT_TO_OZ = 100
MEXC_CONTRACT_SIZE = 0.001


def mt5_lot_to_mexc_contracts(mt5_volume: float) -> int:
    """Legacy XAU shortcut. Prefer mt5_lot_to_exchange_volume() for new code."""
    return mt5_lot_to_exchange_volume(
        mt5_volume,
        mt5_units_per_lot=MT5_LOT_TO_OZ,
        exchange_units_per_contract=MEXC_CONTRACT_SIZE,
    )


def mexc_contracts_to_mt5_lot(contracts: int) -> float:
    """Legacy XAU shortcut. Prefer exchange_volume_to_mt5_lot() for new code."""
    return exchange_volume_to_mt5_lot(
        contracts,
        mt5_units_per_lot=MT5_LOT_TO_OZ,
        exchange_units_per_contract=MEXC_CONTRACT_SIZE,
    )


REDIS_TICK_MT5 = "TICK:MT5:{symbol}"
REDIS_TICK_MEXC = "TICK:MEXC:{symbol}"
REDIS_QUEUE_ACCOUNTANT = "QUEUE:ACCOUNTANT"
REDIS_QUEUE_TELEGRAM = "QUEUE:TELEGRAM"
REDIS_SIGNAL_SHUTDOWN = "SIGNAL:SHUTDOWN"
REDIS_STATE_CORE = "STATE:CORE"

# v2 real-time IPC keys
REDIS_MT5_TICK = "mt5:tick"
REDIS_EXCHANGE_TICK = "exchange:tick"
REDIS_MT5_TICK_CHANNEL = "tick:mt5"
REDIS_MT5_ORDER_QUEUE = "mt5:order:queue"
REDIS_MT5_ORDER_PROCESSING = "mt5:order:processing"
REDIS_MT5_ORDER_RESULT = "mt5:order:result"
REDIS_MT5_HEARTBEAT = "mt5:heartbeat"
REDIS_MT5_QUERY_POSITIONS = "mt5:cmd:query_positions"
REDIS_MT5_POSITIONS_RESPONSE = "mt5:positions:response"
REDIS_MT5_TRACKED_TICKETS = "mt5:tracked_tickets"
REDIS_MT5_POSITION_GONE = "mt5:position:gone"
REDIS_MT5_CMD_SHUTDOWN = "mt5:cmd:shutdown"
REDIS_CORE_CMD_SHUTDOWN = "core:cmd:shutdown"
REDIS_RECON_CMD_SHUTDOWN = "recon:cmd:shutdown"
REDIS_BRAIN_HEARTBEAT = "brain:heartbeat"
REDIS_RECON_HEARTBEAT = "recon:heartbeat"

MEXC_CONTRACT_URL = "https://contract.mexc.com"
MEXC_FUTURES_URL = "https://futures.mexc.com"
MEXC_WS_URL = "wss://contract.mexc.com/edge"
MEXC_WS_PING_INTERVAL = 15
