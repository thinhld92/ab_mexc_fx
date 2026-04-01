"""Structured logger for the arbitrage system."""

import sys
from datetime import datetime


# ANSI color codes
_COLORS = {
    "TICK":    "\033[90m",    # Gray
    "INFO":    "\033[97m",    # White
    "SIGNAL":  "\033[96m",    # Cyan
    "ORDER":   "\033[93m",    # Yellow
    "SUCCESS": "\033[92m",    # Green
    "ERROR":   "\033[91m",    # Red
    "WARN":    "\033[33m",    # Orange
    "RESET":   "\033[0m",
}


def log(level: str, component: str, message: str, **kwargs):
    """
    Structured log output.
    
    Usage:
        log("INFO", "BRAIN", "Signal detected", spread=0.15, direction="LONG")
        log("ORDER", "MEXC", "Open Long", volume=1000, price=3050.5)
        log("ERROR", "MT5", "Connection lost", retry_in=5)
    """
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    color = _COLORS.get(level.upper(), _COLORS["INFO"])
    reset = _COLORS["RESET"]

    # Format kwargs as key=value
    extra = ""
    if kwargs:
        parts = [f"{k}={v}" for k, v in kwargs.items()]
        extra = " | " + " ".join(parts)

    line = f"{color}[{now}] [{level:>7s}] [{component:>5s}] {message}{extra}{reset}"
    print(line, flush=True)


def log_tick(component: str, bid: float, ask: float):
    """Log tick data (gray, minimal)."""
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    color = _COLORS["TICK"]
    reset = _COLORS["RESET"]
    mid = (bid + ask) / 2
    spread = ask - bid
    print(f"{color}[{now}] [   TICK] [{component:>5s}] bid={bid:.2f} ask={ask:.2f} mid={mid:.2f} sp={spread:.2f}{reset}", flush=True)
