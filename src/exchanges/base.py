"""Exchange abstraction layer shared by all exchange handlers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, TypedDict

import orjson


class OrderSide(IntEnum):
    """Standardized order side for all exchanges."""

    OPEN_LONG = 1
    CLOSE_SHORT = 2
    OPEN_SHORT = 3
    CLOSE_LONG = 4


class ExchangeOrderStatus(str, Enum):
    """Standardized order status mapped from exchange-native values.

    Terminal statuses (returned by place_order to Brain):
        FILLED, REJECTED, UNKNOWN

    Internal statuses (used within handlers, never returned to Brain):
        ACCEPTED, PARTIALLY_FILLED, CANCELLED, EXPIRED

    Handlers must resolve non-terminal statuses internally (poll/retry)
    before returning an OrderResult.
    """

    # ─── Terminal (returned by place_order) ───
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    # ─── Internal (handler-only, never in OrderResult) ───
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    @classmethod
    def is_terminal(cls, status: ExchangeOrderStatus) -> bool:
        return status in _TERMINAL_ORDER_STATUSES


_TERMINAL_ORDER_STATUSES = {
    ExchangeOrderStatus.FILLED,
    ExchangeOrderStatus.REJECTED,
    ExchangeOrderStatus.UNKNOWN,
}


class ErrorCategory(str, Enum):
    """Standardized error categories used by the core and reconciler."""

    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK = "NETWORK"
    REQUOTE = "REQUOTE"
    BUSY = "BUSY"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    INSUFFICIENT_BALANCE = "BALANCE"
    INVALID_VOLUME = "INVALID_VOL"
    SYMBOL_NOT_FOUND = "NO_SYMBOL"
    POSITION_NOT_FOUND = "NO_POS"
    MARKET_CLOSED = "MKT_CLOSED"
    LIQUIDATION = "LIQUIDATION"
    ACCOUNT_FROZEN = "FROZEN"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def is_retryable(cls, category: ErrorCategory | None) -> bool:
        return category in {
            cls.TIMEOUT,
            cls.RATE_LIMIT,
            cls.NETWORK,
            cls.REQUOTE,
            cls.BUSY,
        }


class MarginMode(IntEnum):
    ISOLATED = 1
    CROSS = 2


class TickData(TypedDict):
    symbol: str
    bid: float
    ask: float
    last: float
    ts: int
    local_ts: float


class ExchangePosition(TypedDict):
    position_id: str
    symbol: str
    side: str
    volume: float
    volume_raw: int | float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    margin_mode: str
    updated_at: float


@dataclass(frozen=True)
class OrderResult:
    """
    Standardized result from place_order().

    The current v1 core still treats exchange results like dicts, so this
    dataclass exposes a small dict-like compatibility surface via `.get()`.
    """

    success: bool
    order_status: ExchangeOrderStatus = ExchangeOrderStatus.UNKNOWN
    order_id: str | None = None
    fill_price: float | None = None
    fill_volume: float | None = None
    fill_volume_raw: int | float | None = None
    error: str | None = None
    error_category: ErrorCategory | None = None
    error_code: str | None = None
    latency_ms: float = 0.0
    raw_response: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "order_status": self.order_status.value,
            "order_id": self.order_id,
            "fill_price": self.fill_price,
            "fill_volume": self.fill_volume,
            "fill_volume_raw": self.fill_volume_raw,
            "error": self.error,
            "error_category": self.error_category.value if self.error_category else None,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
            "raw_response": dict(self.raw_response or {}),
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def items(self):
        return self.to_dict().items()

    def keys(self):
        return self.to_dict().keys()


class ExchangeHandler(ABC):
    """Abstract interface for all exchange handlers."""

    def __init__(self, config, shutdown_event: asyncio.Event, redis_client=None):
        self._config = config
        self._shutdown_event = shutdown_event
        self._redis = redis_client
        self._tick_callback = None

    def set_tick_callback(self, callback) -> None:
        self._tick_callback = callback

    def _publish_tick(self, tick: TickData) -> None:
        if self._redis:
            self._redis.set("exchange:tick", orjson.dumps(tick))
        if self._tick_callback:
            self._tick_callback(tick)

    async def start(self) -> None:
        """Backward-compatible alias for the legacy engine."""
        await self.connect()

    async def check_token_alive(self) -> bool:
        """Backward-compatible alias for the legacy engine."""
        return await self.check_auth_alive()

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def run_ws(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    def get_latest_tick(self) -> TickData | None:
        ...

    @abstractmethod
    async def get_positions(self) -> list[ExchangePosition]:
        ...

    @abstractmethod
    async def check_auth_alive(self) -> bool:
        ...

    @abstractmethod
    async def place_order(self, side: OrderSide, volume: int | float) -> OrderResult:
        """Place a market order and return a TERMINAL result.

        Contract:
          - MUST block until the order reaches a terminal state.
          - order_status MUST be one of: FILLED, REJECTED, UNKNOWN.
          - Handler is responsible for resolving transient states
            (ACCEPTED, PARTIAL, etc.) internally via polling/retry.
          - If the handler cannot determine the final state within a
            reasonable timeout, return UNKNOWN so Reconciler can check.
        """
        ...

    @abstractmethod
    def validate_volume(self, volume: int | float) -> bool:
        ...

    @abstractmethod
    def get_volume_info(self) -> dict:
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @property
    @abstractmethod
    def exchange_name(self) -> str:
        ...
