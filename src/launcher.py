"""Launcher entry point for the Phase 5 watchdog runtime."""

from __future__ import annotations

import os
import sys

from src.utils.config import Config
from src.utils.logger import log
from src.watchdog import Watchdog


def main(config_path: str | None = None) -> int:
    project_root = os.path.dirname(os.path.abspath(__file__))
    resolved_config = config_path or os.path.join(os.path.dirname(project_root), "config.json")

    if not os.path.exists(resolved_config):
        print(f"Config not found: {resolved_config}")
        print("Copy config.example.json -> config.json and fill in your settings.")
        return 1

    config = Config(resolved_config)
    log("INFO", "MAIN", "=" * 50)
    log("INFO", "MAIN", "  ARBITRAGE SYSTEM LAUNCHER")
    log("INFO", "MAIN", f"  VPS: {config.vps_name}")
    log("INFO", "MAIN", f"  MT5: {config.mt5_symbol}")
    log("INFO", "MAIN", f"  EXCHANGE: {config.exchange_name}:{config.mexc_symbol}")
    log("INFO", "MAIN", "=" * 50)

    watchdog = Watchdog(resolved_config)
    return watchdog.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
