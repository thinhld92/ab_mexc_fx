"""Small typed models for pair creation and pair snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import PairStatus, ReconStatus


@dataclass(frozen=True)
class PairCreate:
    pair_token: str
    direction: str
    entry_time: float
    entry_spread: float | None = None
    conf_dev_entry: float | None = None
    conf_dev_close: float | None = None
    status: PairStatus = PairStatus.PENDING


@dataclass(frozen=True)
class PairRecord:
    pair_token: str
    direction: str
    status: PairStatus
    entry_time: float
    entry_spread: float | None = None
    close_time: float | None = None
    close_spread: float | None = None
    mt5_ticket: int | None = None
    exchange_order_id: str | None = None
    recon_status: ReconStatus = ReconStatus.PENDING
    recon_note: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PairRecord":
        return cls(
            pair_token=row["pair_token"],
            direction=row["direction"],
            status=PairStatus(row["status"]),
            entry_time=row["entry_time"],
            entry_spread=row.get("entry_spread"),
            close_time=row.get("close_time"),
            close_spread=row.get("close_spread"),
            mt5_ticket=row.get("mt5_ticket"),
            exchange_order_id=row.get("exchange_order_id"),
            recon_status=ReconStatus(row.get("recon_status", ReconStatus.PENDING.value)),
            recon_note=row.get("recon_note"),
            raw=dict(row),
        )
