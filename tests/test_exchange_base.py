"""Unit tests for the exchange abstraction layer (Phase 0)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exchanges.base import (
    ErrorCategory,
    ExchangeOrderStatus,
    ExchangePosition,
    MarginMode,
    OrderResult,
    OrderSide,
    TickData,
)
from src.exchanges import create_exchange


def test_order_side_enum():
    assert list(OrderSide) == [1, 2, 3, 4]
    assert OrderSide.OPEN_LONG == 1
    assert OrderSide.CLOSE_LONG == 4
    print("[OK] OrderSide enum")


def test_exchange_order_status():
    assert ExchangeOrderStatus.FILLED.value == "FILLED"
    assert ExchangeOrderStatus.UNKNOWN.value == "UNKNOWN"
    assert ExchangeOrderStatus.PARTIALLY_FILLED.value == "PARTIAL"
    print("[OK] ExchangeOrderStatus enum")


def test_error_category_retryable():
    assert ErrorCategory.is_retryable(ErrorCategory.TIMEOUT) is True
    assert ErrorCategory.is_retryable(ErrorCategory.RATE_LIMIT) is True
    assert ErrorCategory.is_retryable(ErrorCategory.NETWORK) is True
    assert ErrorCategory.is_retryable(ErrorCategory.REQUOTE) is True
    assert ErrorCategory.is_retryable(ErrorCategory.BUSY) is True
    assert ErrorCategory.is_retryable(ErrorCategory.AUTH_EXPIRED) is False
    assert ErrorCategory.is_retryable(ErrorCategory.INSUFFICIENT_BALANCE) is False
    assert ErrorCategory.is_retryable(ErrorCategory.UNKNOWN) is False
    assert ErrorCategory.is_retryable(None) is False
    print("[OK] ErrorCategory.is_retryable")


def test_order_result_success():
    r = OrderResult(
        success=True,
        order_status=ExchangeOrderStatus.FILLED,
        order_id="ABC123",
        fill_price=3245.50,
        fill_volume=1000.0,
        latency_ms=42.5,
    )
    assert r.success is True
    assert r.order_id == "ABC123"
    assert r.fill_price == 3245.50
    assert r.latency_ms == 42.5

    # frozen
    try:
        r.success = False
        assert False, "Should be frozen"
    except AttributeError:
        pass

    print("[OK] OrderResult success (frozen)")


def test_order_result_dict_compat():
    r = OrderResult(
        success=True,
        order_status=ExchangeOrderStatus.FILLED,
        order_id="X",
        latency_ms=10.0,
    )
    assert r.get("success") is True
    assert r.get("order_id") == "X"
    assert r.get("nonexistent", "default") == "default"
    assert r["order_status"] == "FILLED"
    assert "success" in r.keys()
    assert len(list(r.items())) > 0
    print("[OK] OrderResult dict-compat (.get, [], .keys, .items)")


def test_order_result_failure():
    f = OrderResult(
        success=False,
        order_status=ExchangeOrderStatus.REJECTED,
        error="Insufficient balance",
        error_category=ErrorCategory.INSUFFICIENT_BALANCE,
        error_code="602",
    )
    assert f.success is False
    assert f.error_category == ErrorCategory.INSUFFICIENT_BALANCE
    assert f.get("error_category") == "BALANCE"
    assert f.get("error_code") == "602"
    print("[OK] OrderResult failure case")


def test_order_result_unknown():
    u = OrderResult(
        success=False,
        order_status=ExchangeOrderStatus.UNKNOWN,
        error="Request timeout",
        error_category=ErrorCategory.TIMEOUT,
    )
    assert u.order_status == ExchangeOrderStatus.UNKNOWN
    assert ErrorCategory.is_retryable(u.error_category) is True
    print("[OK] OrderResult UNKNOWN (retryable)")


def test_factory_callable():
    assert callable(create_exchange)
    print("[OK] create_exchange factory callable")


def test_is_terminal():
    assert ExchangeOrderStatus.is_terminal(ExchangeOrderStatus.FILLED) is True
    assert ExchangeOrderStatus.is_terminal(ExchangeOrderStatus.REJECTED) is True
    assert ExchangeOrderStatus.is_terminal(ExchangeOrderStatus.UNKNOWN) is True
    assert ExchangeOrderStatus.is_terminal(ExchangeOrderStatus.ACCEPTED) is False
    assert ExchangeOrderStatus.is_terminal(ExchangeOrderStatus.PARTIALLY_FILLED) is False
    assert ExchangeOrderStatus.is_terminal(ExchangeOrderStatus.CANCELLED) is False
    assert ExchangeOrderStatus.is_terminal(ExchangeOrderStatus.EXPIRED) is False
    print("[OK] ExchangeOrderStatus.is_terminal")


if __name__ == "__main__":
    test_order_side_enum()
    test_exchange_order_status()
    test_error_category_retryable()
    test_order_result_success()
    test_order_result_dict_compat()
    test_order_result_failure()
    test_order_result_unknown()
    test_factory_callable()
    test_is_terminal()
    print("\n=== test_exchange_base: ALL PASSED ===")
