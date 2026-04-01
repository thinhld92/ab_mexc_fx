"""Phase 4 reconciler package."""

from __future__ import annotations

__all__ = ["Reconciler", "main"]


def __getattr__(name: str):
    if name in {"Reconciler", "main"}:
        from .reconciler import Reconciler, main

        exports = {"Reconciler": Reconciler, "main": main}
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
