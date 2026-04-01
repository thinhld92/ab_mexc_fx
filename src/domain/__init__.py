"""Domain enums, models, and state transition helpers for v2."""

from .enums import CloseReason, PairEventType, PairStatus, ReconStatus
from .models import PairCreate, PairRecord
from .state_machine import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    can_transition,
    ensure_transition,
    next_statuses,
)

__all__ = [
    "CloseReason",
    "PairCreate",
    "PairEventType",
    "PairRecord",
    "PairStatus",
    "ReconStatus",
    "NON_TERMINAL_STATUSES",
    "TERMINAL_STATUSES",
    "can_transition",
    "ensure_transition",
    "next_statuses",
]
