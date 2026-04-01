"""Watchdog package exports."""

from __future__ import annotations

__all__ = ["Watchdog", "main"]


def __getattr__(name: str):
    if name in {"Watchdog", "main"}:
        from .watchdog import Watchdog, main

        exports = {"Watchdog": Watchdog, "main": main}
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
