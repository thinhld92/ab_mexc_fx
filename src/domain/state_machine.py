"""Pair lifecycle transition rules."""

from __future__ import annotations

from .enums import PairStatus


TERMINAL_STATUSES = {
    PairStatus.CLOSED,
    PairStatus.FAILED,
    PairStatus.HEALED,
    PairStatus.FORCE_CLOSED,
}

NON_TERMINAL_STATUSES = {
    status for status in PairStatus if status not in TERMINAL_STATUSES
}

_TRANSITIONS: dict[PairStatus | None, set[PairStatus]] = {
    None: {PairStatus.PENDING},
    PairStatus.PENDING: {PairStatus.ENTRY_SENT, PairStatus.FAILED},
    PairStatus.ENTRY_SENT: {
        PairStatus.ENTRY_MT5_FILLED,
        PairStatus.ENTRY_EXCHANGE_FILLED,
        PairStatus.OPEN,
        PairStatus.FAILED,
    },
    PairStatus.ENTRY_MT5_FILLED: {PairStatus.OPEN, PairStatus.ORPHAN_MT5},
    PairStatus.ENTRY_EXCHANGE_FILLED: {PairStatus.OPEN, PairStatus.ORPHAN_EXCHANGE},
    PairStatus.OPEN: {
        PairStatus.CLOSE_SENT,
        PairStatus.ORPHAN_MT5,
        PairStatus.ORPHAN_EXCHANGE,
        PairStatus.FORCE_CLOSED,
    },
    PairStatus.CLOSE_SENT: {
        PairStatus.CLOSE_MT5_DONE,
        PairStatus.CLOSE_EXCHANGE_DONE,
        PairStatus.CLOSED,
        PairStatus.FORCE_CLOSED,
    },
    PairStatus.CLOSE_MT5_DONE: {PairStatus.CLOSED, PairStatus.ORPHAN_EXCHANGE},
    PairStatus.CLOSE_EXCHANGE_DONE: {PairStatus.CLOSED, PairStatus.ORPHAN_MT5},
    PairStatus.ORPHAN_MT5: {PairStatus.HEALING},
    PairStatus.ORPHAN_EXCHANGE: {PairStatus.HEALING},
    PairStatus.HEALING: {
        PairStatus.HEALED,
        PairStatus.ORPHAN_MT5,
        PairStatus.ORPHAN_EXCHANGE,
    },
}


def can_transition(
    current: PairStatus | str | None,
    target: PairStatus | str,
) -> bool:
    current_status = None if current is None else PairStatus(current)
    target_status = PairStatus(target)
    return target_status in _TRANSITIONS.get(current_status, set())


def ensure_transition(
    current: PairStatus | str | None,
    target: PairStatus | str,
) -> PairStatus:
    target_status = PairStatus(target)
    if not can_transition(current, target_status):
        raise ValueError(f"Invalid pair status transition: {current!r} -> {target_status.value}")
    return target_status


def next_statuses(current: PairStatus | str | None) -> set[PairStatus]:
    current_status = None if current is None else PairStatus(current)
    return set(_TRANSITIONS.get(current_status, set()))

