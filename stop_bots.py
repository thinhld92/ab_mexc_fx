"""Ask Watchdog to begin phased shutdown via Redis."""

import redis
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.utils.config import Config
from src.utils.constants import REDIS_SIGNAL_SHUTDOWN


def main():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    config = Config(config_path)

    r = redis.Redis(
        host=config.redis.get("host", "localhost"),
        port=config.redis.get("port", 6379),
        db=config.redis.get("db", 0),
        decode_responses=True,
    )

    print("Sending shutdown signal to Watchdog...")
    r.set(REDIS_SIGNAL_SHUTDOWN, "1", ex=60)  # TTL = 60s
    print("Shutdown signal sent (TTL=60s)")
    print("   Watchdog will stop Core -> Reconciler -> MT5 gracefully.")


if __name__ == "__main__":
    main()
