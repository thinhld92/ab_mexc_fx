"""Terminal spread monitor for latest MT5 and exchange ticks."""

from __future__ import annotations

import sys
import time

import orjson

from ..utils.config import Config
from ..utils.constants import REDIS_EXCHANGE_TICK, REDIS_MT5_TICK
from ..utils.logger import log
from ..utils.redis_client import get_redis


def main(config_path: str | None = None) -> int:
    config = Config(config_path)
    redis_client = get_redis(config.redis)

    log("INFO", "MON", "Spread monitor started", interval="5s")
    try:
        while True:
            mt5_tick = _load_tick(redis_client.get(REDIS_MT5_TICK))
            exchange_tick = _load_tick(redis_client.get(REDIS_EXCHANGE_TICK))
            if mt5_tick is None or exchange_tick is None:
                log("WARN", "MON", "Waiting for mt5:tick and exchange:tick")
                time.sleep(5)
                continue

            spread = _calculate_spread(mt5_tick, exchange_tick)
            signal = _detect_raw_signal(spread, config.dev_entry, config.dev_close)
            log(
                "INFO",
                "MON",
                "Spread snapshot",
                mt5=f"{_mid(mt5_tick):.4f}",
                exchange=f"{_mid(exchange_tick):.4f}",
                spread=f"{spread:+.4f}",
                signal=signal,
            )
            time.sleep(5)
    except KeyboardInterrupt:
        log("WARN", "MON", "Spread monitor stopped")
        return 0


def _load_tick(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload = orjson.loads(raw)
    if not isinstance(payload, dict):
        return None
    return payload


def _mid(tick: dict) -> float:
    return (float(tick["bid"]) + float(tick["ask"])) / 2


def _calculate_spread(mt5_tick: dict, exchange_tick: dict) -> float:
    return _mid(mt5_tick) - _mid(exchange_tick)


def _detect_raw_signal(spread: float, dev_entry: float, dev_close: float) -> str:
    if spread > dev_entry:
        return "ENTRY_SHORT"
    if spread < -dev_entry:
        return "ENTRY_LONG"
    if abs(spread) < dev_close:
        return "CLOSE_ZONE"
    return "NONE"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
