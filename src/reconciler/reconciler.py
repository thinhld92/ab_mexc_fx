"""Phase 4 reconciler and crash-recovery loop."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import orjson

from ..domain import CloseReason, PairEventType, PairRecord, PairStatus, ReconStatus
from ..exchanges import create_exchange
from ..exchanges.base import ErrorCategory, ExchangeOrderStatus, OrderResult, OrderSide
from ..storage import Database, JournalRepository, PairRepository
from ..utils.config import Config
from ..utils.constants import (
    REDIS_BRAIN_HEARTBEAT,
    REDIS_MT5_HEARTBEAT,
    REDIS_MT5_ORDER_QUEUE,
    REDIS_MT5_ORDER_RESULT,
    REDIS_MT5_POSITIONS_RESPONSE,
    REDIS_MT5_QUERY_POSITIONS,
    REDIS_QUEUE_TELEGRAM,
    REDIS_RECON_CMD_SHUTDOWN,
    REDIS_RECON_HEARTBEAT,
)
from ..utils.logger import log
from ..utils.redis_client import get_redis


@dataclass(frozen=True)
class ExchangeMatch:
    kind: str
    index: int | None = None
    position: dict | None = None
    note: str | None = None


@dataclass(frozen=True)
class PairObservation:
    mt5_position: dict | None
    exchange_match: ExchangeMatch


class Reconciler:
    """Compare DB, MT5, and exchange state; recover stale or orphaned pairs."""

    HEARTBEAT_TTL_SEC = 60
    READY_TIMEOUT_SEC = 30
    MT5_QUERY_TIMEOUT_SEC = 10.0
    MT5_ORDER_TIMEOUT_SEC = 10.0

    def __init__(
        self,
        config_path: str | None = None,
        *,
        config: Config | None = None,
        redis_client=None,
        exchange=None,
        shutdown_event: asyncio.Event | None = None,
        db: Database | None = None,
        repo: PairRepository | None = None,
        journal: JournalRepository | None = None,
    ) -> None:
        self.config = config or Config(config_path)
        self.redis = redis_client or get_redis(self.config.redis)
        self.shutdown_event = shutdown_event or asyncio.Event()

        self.db = db or Database(self.config)
        self.repo = repo or PairRepository(self.db)
        self.journal = journal or JournalRepository(self.db)
        self.exchange = exchange or create_exchange(self.config, self.shutdown_event, self.redis)

        self._mt5_query_cache: dict[str, dict] = {}
        self._mt5_result_cache: dict[str, dict] = {}
        self._cycle_count = 0
        self._shutdown_started = False
        self._shutdown_monitor_task: asyncio.Task | None = None

        if self.config.max_orders > 1:
            log(
                "WARN",
                "RECON",
                "Phase 4 matching assumes max_orders=1; using single-pair heuristics",
                configured=self.config.max_orders,
            )

    async def start(self) -> None:
        log("INFO", "RECON", "Phase 4 reconciler starting")
        try:
            self.db.initialize()
            await self.exchange.connect()
            await self._wait_for_key(REDIS_MT5_HEARTBEAT, "MT5 heartbeat")
            await self._wait_for_key(REDIS_BRAIN_HEARTBEAT, "Brain heartbeat")
            self._shutdown_monitor_task = asyncio.create_task(
                self._shutdown_monitor_loop(),
                name="recon-shutdown-monitor",
            )

            while not self.shutdown_event.is_set():
                await self.reconcile_once()
                try:
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=max(self.config.reconciler_interval_sec, 1),
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.shutdown_event.set()
        if self._shutdown_monitor_task is not None and not self._shutdown_monitor_task.done():
            self._shutdown_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._shutdown_monitor_task
        with contextlib.suppress(Exception):
            await self.exchange.stop()
        log("INFO", "RECON", "Reconciler stopped")

    async def request_shutdown(self) -> None:
        self.shutdown_event.set()

    async def reconcile_once(self) -> dict:
        run_at = time.time()
        actions: list[dict] = []
        mismatches = 0
        mt5_positions: list[dict] = []
        exchange_positions: list[dict] = []
        pairs: list[PairRecord] = []

        try:
            auth_ok = await self.exchange.check_auth_alive()
            if not auth_ok:
                raise RuntimeError("Exchange auth check failed")

            mt5_positions = await self._query_mt5_positions()
            exchange_positions = self._filter_exchange_positions(await self.exchange.get_positions())
            pairs = self.repo.list_non_terminal()

            matched_mt5_tickets: set[int] = set()
            matched_exchange_indices: set[int] = set()

            for pair in sorted(pairs, key=lambda item: item.entry_time):
                pair_mt5, pair_exchange, pair_actions, pair_mismatches = await self._reconcile_pair(
                    pair,
                    mt5_positions,
                    exchange_positions,
                    run_at,
                )
                matched_mt5_tickets.update(pair_mt5)
                matched_exchange_indices.update(pair_exchange)
                actions.extend(pair_actions)
                mismatches += pair_mismatches

            ghost_actions, ghost_mismatches = await self._handle_ghosts(
                mt5_positions,
                exchange_positions,
                matched_mt5_tickets,
                matched_exchange_indices,
            )
            actions.extend(ghost_actions)
            mismatches += ghost_mismatches
        except Exception as exc:
            log("ERROR", "RECON", f"Reconcile cycle failed: {exc}")
            actions.append({"type": "cycle_error", "error": str(exc)})

        self.journal.add_recon_log(
            run_at=run_at,
            mt5_positions=len(mt5_positions),
            exchange_positions=len(exchange_positions),
            local_pairs=len(pairs),
            mismatches=mismatches,
            actions_taken=actions,
        )
        summary = {
            "ts": run_at,
            "cycle": self._cycle_count + 1,
            "mt5_positions": len(mt5_positions),
            "exchange_positions": len(exchange_positions),
            "local_pairs": len(pairs),
            "mismatches": mismatches,
            "actions": len(actions),
        }
        self._publish_heartbeat(summary)
        self._cycle_count += 1
        return summary

    async def _reconcile_pair(
        self,
        pair: PairRecord,
        mt5_positions: list[dict],
        exchange_positions: list[dict],
        run_at: float,
    ) -> tuple[set[int], set[int], list[dict], int]:
        observation = self._observe_pair(pair, mt5_positions, exchange_positions)
        matched_mt5_tickets: set[int] = set()
        matched_exchange_indices: set[int] = set()
        actions: list[dict] = []
        mismatches = 0

        if observation.mt5_position is not None:
            matched_mt5_tickets.add(int(observation.mt5_position.get("ticket", 0)))
        if observation.exchange_match.index is not None:
            matched_exchange_indices.add(observation.exchange_match.index)

        status = pair.status
        if status == PairStatus.OPEN:
            open_actions, open_mismatches = await self._handle_open_pair(pair, observation, run_at)
            actions.extend(open_actions)
            mismatches += open_mismatches
        elif status in {PairStatus.ORPHAN_MT5, PairStatus.ORPHAN_EXCHANGE}:
            orphan_actions, orphan_mismatches = await self._handle_orphan_pair(pair, observation, run_at)
            actions.extend(orphan_actions)
            mismatches += orphan_mismatches
        elif status in {
            PairStatus.PENDING,
            PairStatus.ENTRY_SENT,
            PairStatus.ENTRY_MT5_FILLED,
            PairStatus.ENTRY_EXCHANGE_FILLED,
        }:
            age = self._age_from_entry(pair)
            if age >= self.config.reconciler_stale_entry_sec:
                stale_actions, stale_mismatches = await self._recover_stale_entry(pair, observation, run_at, age)
                actions.extend(stale_actions)
                mismatches += stale_mismatches
        elif status in {PairStatus.CLOSE_SENT, PairStatus.CLOSE_MT5_DONE, PairStatus.CLOSE_EXCHANGE_DONE}:
            age = self._age_from_update(pair)
            if age >= self.config.reconciler_stale_close_sec:
                stale_actions, stale_mismatches = await self._recover_stale_close(pair, observation, run_at, age)
                actions.extend(stale_actions)
                mismatches += stale_mismatches
        elif status == PairStatus.HEALING:
            age = self._age_from_update(pair)
            if age >= self.config.reconciler_stale_healing_sec:
                healing_actions, healing_mismatches = await self._handle_stuck_healing(
                    pair,
                    observation,
                    run_at,
                    age,
                )
                actions.extend(healing_actions)
                mismatches += healing_mismatches

        return matched_mt5_tickets, matched_exchange_indices, actions, mismatches
    async def _handle_open_pair(
        self,
        pair: PairRecord,
        observation: PairObservation,
        run_at: float,
    ) -> tuple[list[dict], int]:
        actions: list[dict] = []
        exchange_kind = observation.exchange_match.kind
        has_mt5 = observation.mt5_position is not None
        has_exchange = exchange_kind == "exact"

        if has_mt5 and has_exchange:
            self._mark_recon_ok(pair, run_at, note="OPEN pair matches MT5 + exchange")
            return ([{"type": "ok", "pair_token": pair.pair_token}], 0)

        if observation.exchange_match.kind in {"direction_mismatch", "volume_mismatch", "mismatch"}:
            note = observation.exchange_match.note or "Exchange mismatch while pair is OPEN"
            self._mark_recon_mismatch(pair, run_at, note)
            return ([{"type": "mismatch", "pair_token": pair.pair_token, "note": note}], 1)

        if has_mt5 and not has_exchange:
            note = "Exchange position missing while MT5 still open"
            updated = self._transition_open_to_orphan(pair, PairStatus.ORPHAN_MT5, run_at, note)
            actions.append({"type": "orphan_mt5", "pair_token": pair.pair_token})
            if self.config.reconciler_auto_heal:
                heal_actions, heal_mismatches = await self._heal_orphan_mt5(updated, observation.mt5_position, run_at)
                actions.extend(heal_actions)
                return actions, 1 + heal_mismatches
            self._alert(f"ORPHAN_MT5 detected for pair {pair.pair_token}")
            return actions, 1

        if (not has_mt5) and has_exchange:
            note = "MT5 position missing while exchange still open"
            updated = self._transition_open_to_orphan(pair, PairStatus.ORPHAN_EXCHANGE, run_at, note)
            actions.append({"type": "orphan_exchange", "pair_token": pair.pair_token})
            if self.config.reconciler_auto_heal:
                heal_actions, heal_mismatches = await self._heal_orphan_exchange(
                    updated,
                    observation.exchange_match.position,
                    run_at,
                )
                actions.extend(heal_actions)
                return actions, 1 + heal_mismatches
            self._alert(f"ORPHAN_EXCHANGE detected for pair {pair.pair_token}")
            return actions, 1

        note = "OPEN pair has no matching MT5 or exchange position"
        self._mark_recon_mismatch(pair, run_at, note)
        self._alert(f"OPEN pair {pair.pair_token} has no live positions")
        return ([{"type": "mismatch", "pair_token": pair.pair_token, "note": note}], 1)

    async def _handle_orphan_pair(
        self,
        pair: PairRecord,
        observation: PairObservation,
        run_at: float,
    ) -> tuple[list[dict], int]:
        if pair.status == PairStatus.ORPHAN_MT5:
            if observation.mt5_position is None:
                self._mark_healed_without_order(pair, run_at, "ORPHAN_MT5 already resolved")
                return ([{"type": "healed", "pair_token": pair.pair_token, "source": "mt5_absent"}], 0)
            if not self.config.reconciler_auto_heal:
                self._mark_orphan_recon(pair, run_at, ReconStatus.ORPHAN_MT5, "Waiting for MT5 heal")
                self._alert(f"ORPHAN_MT5 persists for pair {pair.pair_token}")
                return ([{"type": "orphan_mt5", "pair_token": pair.pair_token}], 1)
            return await self._heal_orphan_mt5(pair, observation.mt5_position, run_at)

        if observation.exchange_match.kind == "exact":
            if not self.config.reconciler_auto_heal:
                self._mark_orphan_recon(pair, run_at, ReconStatus.ORPHAN_EXCHANGE, "Waiting for exchange heal")
                self._alert(f"ORPHAN_EXCHANGE persists for pair {pair.pair_token}")
                return ([{"type": "orphan_exchange", "pair_token": pair.pair_token}], 1)
            return await self._heal_orphan_exchange(pair, observation.exchange_match.position, run_at)

        if observation.exchange_match.kind == "missing":
            self._mark_healed_without_order(pair, run_at, "ORPHAN_EXCHANGE already resolved")
            return ([{"type": "healed", "pair_token": pair.pair_token, "source": "exchange_absent"}], 0)

        note = observation.exchange_match.note or "Unexpected exchange state while pair is ORPHAN_EXCHANGE"
        self._mark_recon_mismatch(pair, run_at, note)
        self._alert(f"ORPHAN_EXCHANGE mismatch for pair {pair.pair_token}: {note}")
        return ([{"type": "mismatch", "pair_token": pair.pair_token, "note": note}], 1)

    async def _recover_stale_entry(
        self,
        pair: PairRecord,
        observation: PairObservation,
        run_at: float,
        age: float,
    ) -> tuple[list[dict], int]:
        status = pair.status
        working_status = status

        if status == PairStatus.PENDING and (
            observation.mt5_position is not None or observation.exchange_match.kind == "exact"
        ):
            self._promote_pending_to_entry_sent(pair, run_at)
            working_status = PairStatus.ENTRY_SENT
            pair = self.repo.get_by_token(pair.pair_token) or pair

        if working_status == PairStatus.PENDING:
            self._finalize_stale_entry_failed(pair, run_at, f"stale_pending age={age:.1f}s")
            return ([{"type": "stale_entry_failed", "pair_token": pair.pair_token}], 1)

        if observation.mt5_position is not None and observation.exchange_match.kind == "exact":
            self._recover_entry_to_open(pair, observation, run_at, f"stale_entry recovered age={age:.1f}s")
            return ([{"type": "stale_entry_open", "pair_token": pair.pair_token}], 1)

        if observation.mt5_position is not None and observation.exchange_match.kind == "missing":
            if working_status in {PairStatus.ENTRY_SENT, PairStatus.ENTRY_MT5_FILLED}:
                self._recover_entry_to_orphan_mt5(pair, observation.mt5_position, run_at, age)
                return ([{"type": "stale_entry_orphan_mt5", "pair_token": pair.pair_token}], 1)

        if observation.mt5_position is None and observation.exchange_match.kind == "exact":
            if working_status in {PairStatus.ENTRY_SENT, PairStatus.ENTRY_EXCHANGE_FILLED}:
                self._recover_entry_to_orphan_exchange(
                    pair,
                    observation.exchange_match.position,
                    run_at,
                    age,
                )
                return ([{"type": "stale_entry_orphan_exchange", "pair_token": pair.pair_token}], 1)

        if observation.mt5_position is None and observation.exchange_match.kind == "missing":
            if working_status == PairStatus.ENTRY_SENT:
                self._finalize_stale_entry_failed(pair, run_at, f"stale_entry timeout age={age:.1f}s")
                return ([{"type": "stale_entry_failed", "pair_token": pair.pair_token}], 1)

        note = observation.exchange_match.note or f"Unsupported stale entry combination age={age:.1f}s"
        self._mark_recon_mismatch(pair, run_at, note)
        self._alert(f"STALE_ENTRY mismatch for pair {pair.pair_token}: {note}")
        return ([{"type": "mismatch", "pair_token": pair.pair_token, "note": note}], 1)

    async def _recover_stale_close(
        self,
        pair: PairRecord,
        observation: PairObservation,
        run_at: float,
        age: float,
    ) -> tuple[list[dict], int]:
        status = pair.status

        if status == PairStatus.CLOSE_SENT:
            has_mt5 = observation.mt5_position is not None
            has_exchange = observation.exchange_match.kind == "exact"
            if not has_mt5 and not has_exchange:
                self._complete_stale_close(pair, run_at, note=f"stale_close recovered age={age:.1f}s")
                return ([{"type": "stale_close_closed", "pair_token": pair.pair_token}], 1)
            if has_mt5 and (not has_exchange) and observation.exchange_match.kind == "missing":
                self._stale_close_to_orphan_mt5(pair, run_at, note=f"stale_close exchange already closed age={age:.1f}s")
                return ([{"type": "stale_close_orphan_mt5", "pair_token": pair.pair_token}], 1)
            if (not has_mt5) and has_exchange:
                self._stale_close_to_orphan_exchange(pair, run_at, note=f"stale_close mt5 already closed age={age:.1f}s")
                return ([{"type": "stale_close_orphan_exchange", "pair_token": pair.pair_token}], 1)
            if has_mt5 and has_exchange:
                return await self._retry_close(pair, run_at, close_mt5=True, close_exchange=True)

        if status == PairStatus.CLOSE_MT5_DONE:
            if observation.exchange_match.kind == "missing":
                self._complete_stale_close(pair, run_at, note=f"exchange already closed age={age:.1f}s")
                return ([{"type": "stale_close_closed", "pair_token": pair.pair_token}], 1)
            if observation.exchange_match.kind == "exact":
                return await self._retry_close(pair, run_at, close_mt5=False, close_exchange=True)

        if status == PairStatus.CLOSE_EXCHANGE_DONE:
            if observation.mt5_position is None:
                self._complete_stale_close(pair, run_at, note=f"mt5 already closed age={age:.1f}s")
                return ([{"type": "stale_close_closed", "pair_token": pair.pair_token}], 1)
            return await self._retry_close(pair, run_at, close_mt5=True, close_exchange=False)

        note = observation.exchange_match.note or f"Unsupported stale close combination age={age:.1f}s"
        self._mark_recon_mismatch(pair, run_at, note)
        self._alert(f"STALE_CLOSE mismatch for pair {pair.pair_token}: {note}")
        return ([{"type": "mismatch", "pair_token": pair.pair_token, "note": note}], 1)

    async def _handle_stuck_healing(
        self,
        pair: PairRecord,
        observation: PairObservation,
        run_at: float,
        age: float,
    ) -> tuple[list[dict], int]:
        if observation.mt5_position is None and observation.exchange_match.kind == "missing":
            self._mark_healed_without_order(pair, run_at, f"HEALING resolved while stuck age={age:.1f}s")
            return ([{"type": "healed", "pair_token": pair.pair_token, "source": "stuck_healing"}], 0)

        target = pair.recon_status
        if observation.mt5_position is not None and observation.exchange_match.kind == "missing":
            target = ReconStatus.ORPHAN_MT5
        elif observation.mt5_position is None and observation.exchange_match.kind == "exact":
            target = ReconStatus.ORPHAN_EXCHANGE

        if target == ReconStatus.ORPHAN_MT5:
            self._reset_healing(pair, PairStatus.ORPHAN_MT5, run_at, f"healing timeout age={age:.1f}s")
            return ([{"type": "reset_healing", "pair_token": pair.pair_token, "target": "ORPHAN_MT5"}], 1)
        if target == ReconStatus.ORPHAN_EXCHANGE:
            self._reset_healing(pair, PairStatus.ORPHAN_EXCHANGE, run_at, f"healing timeout age={age:.1f}s")
            return ([{"type": "reset_healing", "pair_token": pair.pair_token, "target": "ORPHAN_EXCHANGE"}], 1)

        note = observation.exchange_match.note or f"Unable to classify HEALING state age={age:.1f}s"
        self._mark_recon_mismatch(pair, run_at, note)
        return ([{"type": "mismatch", "pair_token": pair.pair_token, "note": note}], 1)

    async def _heal_orphan_mt5(
        self,
        pair: PairRecord,
        mt5_position: dict | None,
        run_at: float,
    ) -> tuple[list[dict], int]:
        if mt5_position is None:
            self._mark_healed_without_order(pair, run_at, "ORPHAN_MT5 already closed")
            return ([{"type": "healed", "pair_token": pair.pair_token}], 0)

        working_pair = self._start_heal(pair, run_at, orphan_status=ReconStatus.ORPHAN_MT5, action="close_mt5")
        result = await self._close_mt5_ticket(
            ticket=int(mt5_position.get("ticket", 0)),
            volume=float(mt5_position.get("volume") or working_pair.raw.get("mt5_volume") or self.config.mt5_volume),
            pair_token=working_pair.pair_token,
        )
        still_open = await self._mt5_ticket_exists(int(mt5_position.get("ticket", 0)), pair_token=working_pair.pair_token)
        if not still_open:
            self._finish_heal_mt5_success(working_pair, run_at, result)
            return ([{"type": "heal_success", "pair_token": pair.pair_token, "side": "MT5"}], 0)

        self._finish_heal_failure(working_pair, PairStatus.ORPHAN_MT5, run_at, result, "close_mt5")
        self._alert(f"Failed to auto-heal MT5 orphan for pair {pair.pair_token}")
        return ([{"type": "heal_failed", "pair_token": pair.pair_token, "side": "MT5"}], 1)
    async def _heal_orphan_exchange(
        self,
        pair: PairRecord,
        exchange_position: dict | None,
        run_at: float,
    ) -> tuple[list[dict], int]:
        if exchange_position is None:
            self._mark_healed_without_order(pair, run_at, "ORPHAN_EXCHANGE already closed")
            return ([{"type": "healed", "pair_token": pair.pair_token}], 0)

        working_pair = self._start_heal(pair, run_at, orphan_status=ReconStatus.ORPHAN_EXCHANGE, action="close_exchange")
        result = await self._close_exchange_position(
            position_side=str(exchange_position.get("side", "")).upper(),
            volume=int(exchange_position.get("volume_raw") or exchange_position.get("volume") or self.config.mexc_volume),
        )
        still_open = await self._exchange_position_exists(working_pair)
        if not still_open:
            self._finish_heal_exchange_success(working_pair, run_at, result)
            return ([{"type": "heal_success", "pair_token": pair.pair_token, "side": "EXCHANGE"}], 0)

        self._finish_heal_failure(working_pair, PairStatus.ORPHAN_EXCHANGE, run_at, result.to_dict(), "close_exchange")
        self._alert(f"Failed to auto-heal exchange orphan for pair {pair.pair_token}")
        return ([{"type": "heal_failed", "pair_token": pair.pair_token, "side": "EXCHANGE"}], 1)

    async def _retry_close(
        self,
        pair: PairRecord,
        run_at: float,
        *,
        close_mt5: bool,
        close_exchange: bool,
    ) -> tuple[list[dict], int]:
        mt5_result: dict | None = None
        exchange_result = self._unknown_order_result("close_not_requested")

        if close_mt5 and pair.mt5_ticket:
            mt5_result = await self._close_mt5_ticket(
                ticket=int(pair.mt5_ticket),
                volume=float(pair.raw.get("mt5_volume") or self.config.mt5_volume),
                pair_token=pair.pair_token,
            )
        if close_exchange:
            exchange_result = await self._close_exchange_position(
                position_side=self._expected_exchange_side(pair),
                volume=int(pair.raw.get("exchange_volume") or self.config.mexc_volume),
            )

        mt5_exists = False
        if pair.mt5_ticket:
            mt5_exists = await self._mt5_ticket_exists(int(pair.mt5_ticket), pair_token=pair.pair_token)
        exchange_exists = await self._exchange_position_exists(pair)

        if not mt5_exists and not exchange_exists:
            self._complete_stale_close(
                pair,
                run_at,
                note="stale close retry succeeded",
                mt5_result=mt5_result,
                exchange_result=exchange_result,
            )
            return ([{"type": "retry_close_success", "pair_token": pair.pair_token}], 1)

        if mt5_exists and not exchange_exists:
            self._stale_close_to_orphan_mt5(
                pair,
                run_at,
                note="stale close retry left MT5 open",
                exchange_result=exchange_result,
            )
            return ([{"type": "retry_close_orphan_mt5", "pair_token": pair.pair_token}], 1)

        if (not mt5_exists) and exchange_exists:
            self._stale_close_to_orphan_exchange(
                pair,
                run_at,
                note="stale close retry left exchange open",
                mt5_result=mt5_result,
            )
            return ([{"type": "retry_close_orphan_exchange", "pair_token": pair.pair_token}], 1)

        note = "stale close retry did not close either side"
        self._mark_recon_mismatch(pair, run_at, note)
        self._alert(f"Close retry still has both sides open for pair {pair.pair_token}")
        return ([{"type": "retry_close_pending", "pair_token": pair.pair_token}], 1)

    async def _handle_ghosts(
        self,
        mt5_positions: list[dict],
        exchange_positions: list[dict],
        matched_mt5_tickets: set[int],
        matched_exchange_indices: set[int],
    ) -> tuple[list[dict], int]:
        actions: list[dict] = []
        mismatches = 0

        for position in mt5_positions:
            ticket = int(position.get("ticket", 0))
            if ticket in matched_mt5_tickets:
                continue
            comment = str(position.get("comment", ""))
            if "arb" not in comment.lower():
                actions.append({"type": "skip_non_bot_mt5", "ticket": ticket})
                continue

            action = self.config.reconciler_ghost_action
            if action == "close":
                result = await self._close_mt5_ticket(
                    ticket=ticket,
                    volume=float(position.get("volume") or self.config.mt5_volume),
                    pair_token=None,
                )
                actions.append({"type": "ghost_mt5", "ticket": ticket, "action": "close", "result": result})
            else:
                actions.append({"type": "ghost_mt5", "ticket": ticket, "action": "alert"})
                self._alert(f"GHOST MT5 ticket={ticket} requires manual check")
            mismatches += 1

        for index, position in enumerate(exchange_positions):
            if index in matched_exchange_indices:
                continue
            side = str(position.get("side", "")).upper()
            volume = int(position.get("volume_raw") or position.get("volume") or 0)
            action = self.config.reconciler_ghost_action
            if action == "close":
                result = await self._close_exchange_position(side, volume)
                actions.append(
                    {
                        "type": "ghost_exchange",
                        "side": side,
                        "volume": volume,
                        "action": "close",
                        "result": result.to_dict(),
                    }
                )
            else:
                actions.append(
                    {
                        "type": "ghost_exchange",
                        "side": side,
                        "volume": volume,
                        "action": "alert",
                    }
                )
                self._alert(f"GHOST EXCHANGE {side} {volume} requires manual check")
            mismatches += 1

        return actions, mismatches
    def _observe_pair(
        self,
        pair: PairRecord,
        mt5_positions: list[dict],
        exchange_positions: list[dict],
    ) -> PairObservation:
        return PairObservation(
            mt5_position=self._match_mt5_position(pair, mt5_positions),
            exchange_match=self._match_exchange_position(pair, exchange_positions),
        )

    def _match_mt5_position(self, pair: PairRecord, mt5_positions: list[dict]) -> dict | None:
        if pair.mt5_ticket is not None:
            ticket = int(pair.mt5_ticket)
            for position in mt5_positions:
                if int(position.get("ticket", 0)) == ticket:
                    return position

        token_hint = pair.pair_token
        expected_type = self._expected_mt5_type(pair)
        expected_volume = float(pair.raw.get("mt5_volume") or self.config.mt5_volume)
        for position in mt5_positions:
            comment = str(position.get("comment", ""))
            if token_hint and token_hint not in comment:
                continue
            position_type = str(position.get("type", "")).upper()
            if expected_type and position_type != expected_type:
                continue
            volume = float(position.get("volume") or 0.0)
            if abs(volume - expected_volume) > 1e-9:
                continue
            return position
        return None

    def _match_exchange_position(self, pair: PairRecord, exchange_positions: list[dict]) -> ExchangeMatch:
        expected_side = self._expected_exchange_side(pair)
        expected_volume = int(pair.raw.get("exchange_volume") or self.config.mexc_volume)

        exact: tuple[int, dict] | None = None
        same_side: tuple[int, dict] | None = None
        same_volume: tuple[int, dict] | None = None
        fallback: tuple[int, dict] | None = None

        for index, position in enumerate(exchange_positions):
            side = str(position.get("side", "")).upper()
            volume = int(position.get("volume_raw") or position.get("volume") or 0)
            if fallback is None:
                fallback = (index, position)
            if side == expected_side and volume == expected_volume:
                exact = (index, position)
                break
            if side == expected_side and same_side is None:
                same_side = (index, position)
            if volume == expected_volume and same_volume is None:
                same_volume = (index, position)

        if exact is not None:
            return ExchangeMatch("exact", exact[0], exact[1])
        if same_side is not None:
            actual = int(same_side[1].get("volume_raw") or same_side[1].get("volume") or 0)
            return ExchangeMatch(
                "volume_mismatch",
                same_side[0],
                same_side[1],
                f"volume: expected {expected_volume}, actual {actual}",
            )
        if same_volume is not None:
            actual_side = str(same_volume[1].get("side", "")).upper()
            return ExchangeMatch(
                "direction_mismatch",
                same_volume[0],
                same_volume[1],
                f"direction mismatch: pair={pair.direction}, expected_exchange={expected_side}, actual_exchange={actual_side}",
            )
        if fallback is not None and len(exchange_positions) == 1:
            actual_side = str(fallback[1].get("side", "")).upper()
            actual_volume = int(fallback[1].get("volume_raw") or fallback[1].get("volume") or 0)
            return ExchangeMatch(
                "mismatch",
                fallback[0],
                fallback[1],
                f"unexpected exchange position: side={actual_side}, volume={actual_volume}",
            )
        return ExchangeMatch("missing")

    async def _query_mt5_positions(self) -> list[dict]:
        query_id = uuid.uuid4().hex[:8]
        payload = {"query_id": query_id, "ts": time.time()}
        self.redis.lpush(REDIS_MT5_QUERY_POSITIONS, orjson.dumps(payload).decode("utf-8"))

        cached = self._mt5_query_cache.pop(query_id, None)
        if cached is not None:
            return list(cached.get("positions") or [])

        deadline = time.monotonic() + self.MT5_QUERY_TIMEOUT_SEC
        while not self.shutdown_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for mt5:positions:response")

            raw = await asyncio.to_thread(
                self.redis.brpop,
                REDIS_MT5_POSITIONS_RESPONSE,
                max(int(remaining), 1),
            )
            if raw is None:
                continue
            if isinstance(raw, (list, tuple)):
                raw = raw[1]
            response = self._decode_payload(raw)
            response_id = str(response.get("query_id", ""))
            if response_id == query_id:
                return list(response.get("positions") or [])
            if response_id:
                self._mt5_query_cache[response_id] = response

        return []

    async def _close_mt5_ticket(
        self,
        *,
        ticket: int,
        volume: float,
        pair_token: str | None,
    ) -> dict | None:
        if ticket <= 0:
            return None
        job_id = uuid.uuid4().hex[:8]
        payload = {
            "action": "CLOSE",
            "ticket": ticket,
            "volume": volume,
            "job_id": job_id,
            "ts": time.time(),
        }
        if pair_token:
            payload["pair_token"] = pair_token
        self.redis.lpush(REDIS_MT5_ORDER_QUEUE, orjson.dumps(payload).decode("utf-8"))
        return await self._wait_mt5_result(job_id, self.MT5_ORDER_TIMEOUT_SEC)

    async def _wait_mt5_result(self, job_id: str, timeout: float) -> dict | None:
        cached = self._mt5_result_cache.pop(job_id, None)
        if cached is not None:
            return cached

        deadline = time.monotonic() + timeout
        while not self.shutdown_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            raw = await asyncio.to_thread(
                self.redis.brpop,
                REDIS_MT5_ORDER_RESULT,
                max(int(remaining), 1),
            )
            if raw is None:
                continue
            if isinstance(raw, (list, tuple)):
                raw = raw[1]
            payload = self._decode_payload(raw)
            result_job_id = str(payload.get("job_id", ""))
            if result_job_id == job_id:
                return payload
            if result_job_id:
                self._mt5_result_cache[result_job_id] = payload
        return None

    async def _close_exchange_position(self, position_side: str, volume: int) -> OrderResult:
        side = str(position_side).upper()
        if side == "LONG":
            close_side = OrderSide.CLOSE_LONG
        elif side == "SHORT":
            close_side = OrderSide.CLOSE_SHORT
        else:
            return self._unknown_order_result(f"Unknown exchange position side: {position_side}")

        try:
            return await self.exchange.place_order(close_side, volume)
        except Exception as exc:
            log("ERROR", "RECON", f"Exchange close crashed: {exc}")
            return OrderResult(
                success=False,
                order_status=ExchangeOrderStatus.UNKNOWN,
                error=str(exc),
                error_category=ErrorCategory.UNKNOWN,
            )

    async def _mt5_ticket_exists(self, ticket: int, *, pair_token: str | None) -> bool:
        positions = await self._query_mt5_positions()
        for position in positions:
            if int(position.get("ticket", 0)) == ticket:
                return True
            if pair_token and pair_token in str(position.get("comment", "")):
                return True
        return False

    async def _exchange_position_exists(self, pair: PairRecord) -> bool:
        positions = self._filter_exchange_positions(await self.exchange.get_positions())
        match = self._match_exchange_position(pair, positions)
        return match.kind == "exact"
    def _mark_recon_ok(self, pair: PairRecord, run_at: float, note: str) -> None:
        with self.db.transaction() as conn:
            self.repo.update_recon_status(
                pair.pair_token,
                ReconStatus.OK,
                recon_note=note,
                last_recon_at=run_at,
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.RECON_OK, {"note": note}, conn=conn)

    def _mark_orphan_recon(
        self,
        pair: PairRecord,
        run_at: float,
        recon_status: ReconStatus,
        note: str,
    ) -> None:
        with self.db.transaction() as conn:
            self.repo.update_recon_status(
                pair.pair_token,
                recon_status,
                recon_note=note,
                last_recon_at=run_at,
                conn=conn,
            )
            self.journal.add_event(
                pair.pair_token,
                PairEventType.RECON_MISMATCH,
                {"note": note, "recon_status": recon_status.value},
                conn=conn,
            )

    def _mark_recon_mismatch(self, pair: PairRecord, run_at: float, note: str) -> None:
        with self.db.transaction() as conn:
            self.repo.update_recon_status(
                pair.pair_token,
                ReconStatus.MISMATCH,
                recon_note=note,
                last_recon_at=run_at,
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.RECON_MISMATCH, {"note": note}, conn=conn)

    def _transition_open_to_orphan(
        self,
        pair: PairRecord,
        target_status: PairStatus,
        run_at: float,
        note: str,
    ) -> PairRecord:
        recon_status = ReconStatus(target_status.value)
        with self.db.transaction() as conn:
            updated = self.repo.transition(
                pair.pair_token,
                target_status,
                expected_current=PairStatus.OPEN,
                fields={
                    "recon_status": recon_status.value,
                    "recon_note": note,
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(
                pair.pair_token,
                PairEventType.RECON_MISMATCH,
                {"note": note, "target_status": target_status.value},
                conn=conn,
            )
            return updated

    def _promote_pending_to_entry_sent(self, pair: PairRecord, run_at: float) -> None:
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                PairStatus.ENTRY_SENT,
                expected_current=PairStatus.PENDING,
                fields={"last_recon_at": run_at, "recon_note": "Recovered pending pair before stale-entry resolution"},
                conn=conn,
            )
            self.journal.add_event(
                pair.pair_token,
                PairEventType.ENTRY_DISPATCHED,
                {"source": "reconciler_recovery"},
                conn=conn,
            )

    def _finalize_stale_entry_failed(self, pair: PairRecord, run_at: float, note: str) -> None:
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                PairStatus.FAILED,
                expected_current=pair.status,
                fields={
                    "recon_status": ReconStatus.MISMATCH.value,
                    "recon_note": note,
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.ENTRY_FAILED, {"reason": note}, conn=conn)

    def _recover_entry_to_open(
        self,
        pair: PairRecord,
        observation: PairObservation,
        run_at: float,
        note: str,
    ) -> None:
        mt5_position = observation.mt5_position or {}
        exchange_position = observation.exchange_match.position or {}
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                PairStatus.OPEN,
                expected_current=pair.status,
                fields={
                    "mt5_ticket": int(mt5_position.get("ticket", 0) or pair.raw.get("mt5_ticket") or 0),
                    "mt5_action": str(mt5_position.get("type", "") or pair.raw.get("mt5_action") or ""),
                    "mt5_volume": float(mt5_position.get("volume") or pair.raw.get("mt5_volume") or self.config.mt5_volume),
                    "mt5_entry_price": self._float_or_none(mt5_position.get("price") or pair.raw.get("mt5_entry_price")),
                    "exchange_order_id": str(exchange_position.get("position_id") or pair.raw.get("exchange_order_id") or ""),
                    "exchange_volume": int(exchange_position.get("volume_raw") or pair.raw.get("exchange_volume") or self.config.mexc_volume),
                    "exchange_side": int(self._exchange_open_side(pair)),
                    "recon_status": ReconStatus.OK.value,
                    "recon_note": note,
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.RECON_OK, {"note": note}, conn=conn)

    def _recover_entry_to_orphan_mt5(
        self,
        pair: PairRecord,
        mt5_position: dict,
        run_at: float,
        age: float,
    ) -> None:
        with self.db.transaction() as conn:
            current = pair.status
            if current == PairStatus.ENTRY_SENT:
                self.repo.mark_mt5_entry_filled(
                    pair.pair_token,
                    mt5_ticket=int(mt5_position.get("ticket", 0)),
                    mt5_action=str(mt5_position.get("type", "")),
                    mt5_volume=float(mt5_position.get("volume") or self.config.mt5_volume),
                    mt5_entry_price=self._float_or_none(mt5_position.get("price")),
                    expected_current=PairStatus.ENTRY_SENT,
                    conn=conn,
                )
                current = PairStatus.ENTRY_MT5_FILLED
            self.repo.transition(
                pair.pair_token,
                PairStatus.ORPHAN_MT5,
                expected_current=current,
                fields={
                    "recon_status": ReconStatus.ORPHAN_MT5.value,
                    "recon_note": f"stale_entry mt5_only age={age:.1f}s",
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(
                pair.pair_token,
                PairEventType.RECON_MISMATCH,
                {"note": f"stale_entry mt5_only age={age:.1f}s"},
                conn=conn,
            )

    def _recover_entry_to_orphan_exchange(
        self,
        pair: PairRecord,
        exchange_position: dict,
        run_at: float,
        age: float,
    ) -> None:
        with self.db.transaction() as conn:
            current = pair.status
            if current == PairStatus.ENTRY_SENT:
                self.repo.mark_exchange_entry_filled(
                    pair.pair_token,
                    exchange_order_id=str(exchange_position.get("position_id") or ""),
                    exchange_volume=int(exchange_position.get("volume_raw") or self.config.mexc_volume),
                    exchange_side=int(self._exchange_open_side(pair)),
                    expected_current=PairStatus.ENTRY_SENT,
                    conn=conn,
                )
                current = PairStatus.ENTRY_EXCHANGE_FILLED
            self.repo.transition(
                pair.pair_token,
                PairStatus.ORPHAN_EXCHANGE,
                expected_current=current,
                fields={
                    "recon_status": ReconStatus.ORPHAN_EXCHANGE.value,
                    "recon_note": f"stale_entry exchange_only age={age:.1f}s",
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(
                pair.pair_token,
                PairEventType.RECON_MISMATCH,
                {"note": f"stale_entry exchange_only age={age:.1f}s"},
                conn=conn,
            )

    def _complete_stale_close(
        self,
        pair: PairRecord,
        run_at: float,
        *,
        note: str,
        mt5_result: dict | None = None,
        exchange_result: OrderResult | None = None,
    ) -> None:
        fields = {
            "close_time": time.time(),
            "recon_status": ReconStatus.OK.value,
            "recon_note": note,
            "last_recon_at": run_at,
        }
        if mt5_result and mt5_result.get("success"):
            fields.update(
                {
                    "mt5_close_price": self._float_or_none(mt5_result.get("close_price")),
                    "mt5_profit": self._float_or_none(mt5_result.get("profit")),
                    "mt5_close_latency_ms": self._float_or_none(mt5_result.get("latency_ms")),
                }
            )
        if exchange_result and exchange_result.success:
            fields.update(
                {
                    "exchange_close_order_id": exchange_result.order_id,
                    "exchange_close_latency_ms": self._float_or_none(exchange_result.latency_ms),
                }
            )
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                PairStatus.CLOSED,
                expected_current=pair.status,
                fields=fields,
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.CLOSE_COMPLETE, {"note": note}, conn=conn)

    def _stale_close_to_orphan_mt5(
        self,
        pair: PairRecord,
        run_at: float,
        *,
        note: str,
        exchange_result: OrderResult | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            current = pair.status
            if current == PairStatus.CLOSE_SENT:
                self.repo.mark_exchange_close_done(
                    pair.pair_token,
                    exchange_close_order_id=(exchange_result.order_id if exchange_result else pair.raw.get("exchange_close_order_id") or ""),
                    exchange_close_latency_ms=self._float_or_none(
                        exchange_result.latency_ms if exchange_result else pair.raw.get("exchange_close_latency_ms")
                    ),
                    expected_current=PairStatus.CLOSE_SENT,
                    conn=conn,
                )
                current = PairStatus.CLOSE_EXCHANGE_DONE
            self.repo.transition(
                pair.pair_token,
                PairStatus.ORPHAN_MT5,
                expected_current=current,
                fields={
                    "recon_status": ReconStatus.ORPHAN_MT5.value,
                    "recon_note": note,
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.CLOSE_PARTIAL_FAIL, {"note": note}, conn=conn)

    def _stale_close_to_orphan_exchange(
        self,
        pair: PairRecord,
        run_at: float,
        *,
        note: str,
        mt5_result: dict | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            current = pair.status
            if current == PairStatus.CLOSE_SENT:
                self.repo.mark_mt5_close_done(
                    pair.pair_token,
                    mt5_close_price=self._float_or_none(
                        mt5_result.get("close_price") if mt5_result else pair.raw.get("mt5_close_price")
                    ),
                    mt5_profit=self._float_or_none(mt5_result.get("profit") if mt5_result else pair.raw.get("mt5_profit")),
                    mt5_close_latency_ms=self._float_or_none(
                        mt5_result.get("latency_ms") if mt5_result else pair.raw.get("mt5_close_latency_ms")
                    ),
                    expected_current=PairStatus.CLOSE_SENT,
                    conn=conn,
                )
                current = PairStatus.CLOSE_MT5_DONE
            self.repo.transition(
                pair.pair_token,
                PairStatus.ORPHAN_EXCHANGE,
                expected_current=current,
                fields={
                    "recon_status": ReconStatus.ORPHAN_EXCHANGE.value,
                    "recon_note": note,
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.CLOSE_PARTIAL_FAIL, {"note": note}, conn=conn)

    def _start_heal(
        self,
        pair: PairRecord,
        run_at: float,
        *,
        orphan_status: ReconStatus,
        action: str,
    ) -> PairRecord:
        with self.db.transaction() as conn:
            updated = self.repo.transition(
                pair.pair_token,
                PairStatus.HEALING,
                expected_current=pair.status,
                fields={
                    "recon_status": orphan_status.value,
                    "recon_note": f"HEALING via {action}",
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(
                pair.pair_token,
                PairEventType.HEAL_START,
                {"orphan_type": orphan_status.value, "action": action},
                conn=conn,
            )
            return updated

    def _finish_heal_mt5_success(self, pair: PairRecord, run_at: float, result: dict | None) -> None:
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                PairStatus.HEALED,
                expected_current=PairStatus.HEALING,
                fields={
                    "close_time": time.time(),
                    "close_reason": CloseReason.HEAL_ORPHAN_MT5.value,
                    "mt5_close_price": self._float_or_none((result or {}).get("close_price")),
                    "mt5_profit": self._float_or_none((result or {}).get("profit")),
                    "mt5_close_latency_ms": self._float_or_none((result or {}).get("latency_ms")),
                    "recon_status": ReconStatus.HEALED.value,
                    "recon_note": "Auto-healed by closing MT5 orphan",
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.HEAL_SUCCESS, {"side": "MT5"}, conn=conn)

    def _finish_heal_exchange_success(self, pair: PairRecord, run_at: float, result: OrderResult) -> None:
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                PairStatus.HEALED,
                expected_current=PairStatus.HEALING,
                fields={
                    "close_time": time.time(),
                    "close_reason": CloseReason.HEAL_ORPHAN_EXCHANGE.value,
                    "exchange_close_order_id": result.order_id,
                    "exchange_close_latency_ms": self._float_or_none(result.latency_ms),
                    "recon_status": ReconStatus.HEALED.value,
                    "recon_note": "Auto-healed by closing exchange orphan",
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.HEAL_SUCCESS, {"side": "EXCHANGE"}, conn=conn)

    def _finish_heal_failure(
        self,
        pair: PairRecord,
        target_status: PairStatus,
        run_at: float,
        result: dict | None,
        action: str,
    ) -> None:
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                target_status,
                expected_current=PairStatus.HEALING,
                fields={
                    "recon_status": ReconStatus(target_status.value).value,
                    "recon_note": f"Heal failed via {action}",
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(
                pair.pair_token,
                PairEventType.HEAL_FAILED,
                {"action": action, "result": result or {}},
                conn=conn,
            )

    def _mark_healed_without_order(self, pair: PairRecord, run_at: float, note: str) -> None:
        with self.db.transaction() as conn:
            current = pair.status
            if current in {PairStatus.ORPHAN_MT5, PairStatus.ORPHAN_EXCHANGE}:
                self.repo.transition(
                    pair.pair_token,
                    PairStatus.HEALING,
                    expected_current=current,
                    fields={
                        "recon_status": ReconStatus(current.value).value,
                        "recon_note": note,
                        "last_recon_at": run_at,
                    },
                    conn=conn,
                )
                current = PairStatus.HEALING
            self.repo.transition(
                pair.pair_token,
                PairStatus.HEALED,
                expected_current=current,
                fields={
                    "close_time": time.time(),
                    "recon_status": ReconStatus.HEALED.value,
                    "recon_note": note,
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.HEAL_SUCCESS, {"note": note}, conn=conn)

    def _reset_healing(self, pair: PairRecord, target_status: PairStatus, run_at: float, note: str) -> None:
        with self.db.transaction() as conn:
            self.repo.transition(
                pair.pair_token,
                target_status,
                expected_current=PairStatus.HEALING,
                fields={
                    "recon_status": ReconStatus(target_status.value).value,
                    "recon_note": note,
                    "last_recon_at": run_at,
                },
                conn=conn,
            )
            self.journal.add_event(pair.pair_token, PairEventType.HEAL_FAILED, {"note": note}, conn=conn)

    async def _wait_for_key(self, key: str, label: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.READY_TIMEOUT_SEC
        while asyncio.get_running_loop().time() < deadline and not self.shutdown_event.is_set():
            if self.redis.get(key):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"Timed out waiting for {label}")

    def _publish_heartbeat(self, summary: dict) -> None:
        self.redis.set(
            REDIS_RECON_HEARTBEAT,
            orjson.dumps({**summary, "pid": os.getpid()}).decode("utf-8"),
            ex=self.HEARTBEAT_TTL_SEC,
        )

    async def _shutdown_monitor_loop(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                signal = self.redis.get(REDIS_RECON_CMD_SHUTDOWN)
                if signal == "1":
                    log("WARN", "RECON", "Watchdog shutdown flag received")
                    self.shutdown_event.set()
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    def _filter_exchange_positions(self, positions: list[dict]) -> list[dict]:
        expected_symbol = str(self.config.mexc_symbol)
        filtered = []
        for position in positions or []:
            symbol = str(position.get("symbol", ""))
            if symbol and symbol != expected_symbol:
                continue
            filtered.append(position)
        return filtered

    def _expected_mt5_type(self, pair: PairRecord) -> str:
        return "BUY" if pair.direction == "LONG" else "SELL"

    def _expected_exchange_side(self, pair: PairRecord) -> str:
        return "SHORT" if pair.direction == "LONG" else "LONG"

    def _exchange_open_side(self, pair: PairRecord) -> OrderSide:
        return OrderSide.OPEN_SHORT if pair.direction == "LONG" else OrderSide.OPEN_LONG

    def _age_from_entry(self, pair: PairRecord) -> float:
        return max(0.0, time.time() - float(pair.entry_time))

    def _age_from_update(self, pair: PairRecord) -> float:
        raw = pair.raw.get("updated_at")
        if not raw:
            return self._age_from_entry(pair)
        return max(0.0, time.time() - self._parse_utc_text(str(raw)))

    def _alert(self, message: str) -> None:
        if not self.config.reconciler_alert_telegram:
            return
        payload = {"ts": time.time(), "source": "RECON", "message": message}
        try:
            self.redis.lpush(REDIS_QUEUE_TELEGRAM, orjson.dumps(payload).decode("utf-8"))
        except Exception as exc:
            log("ERROR", "RECON", f"Failed to enqueue telegram alert: {exc}")
        log("WARN", "RECON", message)

    @staticmethod
    def _parse_utc_text(value: str) -> float:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @staticmethod
    def _decode_payload(raw) -> dict:
        if raw is None:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = orjson.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object payload")
        return data

    @staticmethod
    def _float_or_none(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _unknown_order_result(error: str) -> OrderResult:
        return OrderResult(
            success=False,
            order_status=ExchangeOrderStatus.UNKNOWN,
            error=error,
            error_category=ErrorCategory.UNKNOWN,
        )


def main(config_path: str | None = None) -> None:
    reconciler = Reconciler(config_path)
    try:
        asyncio.run(reconciler.start())
    except KeyboardInterrupt:
        log("WARN", "RECON", "Ctrl+C received")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)



