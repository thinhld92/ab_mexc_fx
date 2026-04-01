"""MT5 process package for the v2 architecture."""

from __future__ import annotations

__all__ = ["MT5Process", "main"]


def __getattr__(name: str):
    if name in {"MT5Process", "main"}:
        from .main import MT5Process, main

        exports = {"MT5Process": MT5Process, "main": main}
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
