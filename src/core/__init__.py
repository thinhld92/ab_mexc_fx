"""Phase 3 core package exports."""

from __future__ import annotations

__all__ = ["CoreEngine", "PairManager", "TradingBrain"]


def __getattr__(name: str):
    if name == "CoreEngine":
        from .engine import CoreEngine

        return CoreEngine
    if name == "PairManager":
        from .pair_manager import PairManager

        return PairManager
    if name == "TradingBrain":
        from .trading_brain import TradingBrain

        return TradingBrain
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
