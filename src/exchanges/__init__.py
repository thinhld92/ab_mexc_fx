"""Factory and shared exports for exchange handlers."""

from __future__ import annotations

import asyncio

from .base import (
    ErrorCategory,
    ExchangeHandler,
    ExchangeOrderStatus,
    ExchangePosition,
    MarginMode,
    OrderResult,
    OrderSide,
    TickData,
)


def create_exchange(
    config,
    shutdown_event: asyncio.Event,
    redis_client=None,
) -> ExchangeHandler:
    """Create the configured exchange handler."""

    exchange_cfg = getattr(config, "exchange", None) or {"name": "mexc"}
    name = exchange_cfg.get("name", "mexc").lower()

    if name == "mexc":
        from .mexc_handler import MEXCHandler

        return MEXCHandler(config, shutdown_event, redis_client)

    raise ValueError(f"Unknown exchange: '{name}'. Supported: mexc")


__all__ = [
    "ErrorCategory",
    "ExchangeHandler",
    "ExchangeOrderStatus",
    "ExchangePosition",
    "MarginMode",
    "OrderResult",
    "OrderSide",
    "TickData",
    "create_exchange",
]
