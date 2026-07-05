# -*- coding: utf-8 -*-
"""DurableOutbound -- Stage-3 Task 2: the Body-side durable outbound WAL.

Journals every bridgeable StreamEventBroker event (op_id matching
``op_prefix``, default ``trinity:``) into an fsync'd write-ahead log AT
PUBLISH TIME -- upstream of any connection state. A network partition or
a Body crash can therefore never lose a signal that ``publish()``
accepted: the WAL is the durable truth, the broker's bounded history
ring (512 default) is merely a hot cache.

Trim is ack-driven: Task 1 armed the ack lane (``BusBridgeClient``'s
``on_ack`` constructor kwarg); wiring that callback to
:meth:`DurableOutbound.on_ack` tombstones every journaled entry with
``lease_id <= acked_event_id`` (event ids are zero-padded 012x hex, so
plain string comparison is correct) and compacts the file every
``JARVIS_BODY_WAL_COMPACT_EVERY_N`` acks.

Durability heavy-lifting is the intake WAL, reused verbatim (operator
DRY mandate): flock+fsync per line, tombstone-applied crash recovery,
atomic tmp+fsync+replace compaction.

Capacity is DYNAMIC -- no hardcoded byte caps anywhere. The WAL file may
occupy at most ``JARVIS_BODY_WAL_DISK_FRACTION`` (default 0.05) of the
CURRENT free bytes on the WAL's filesystem, probed off-loop through the
cooperative_fs_io offload substrate and cached for
``JARVIS_BODY_WAL_PROBE_INTERVAL_S`` between appends. Over budget ->
compact first; still over -> ``degraded_capacity`` episode: drop the
OLDEST pending entry (dead_letter tombstone) per append with exactly ONE
``logger.warning`` per episode (mirroring the broker's ``stream_lag``
single-signal pattern). Never raises, never blocks the event loop.

Env knobs:
    JARVIS_BODY_WAL_PATH             (default <repo>/.jarvis/body_outbound_wal.jsonl)
    JARVIS_BODY_WAL_DISK_FRACTION    (default 0.05 -- the ONLY capacity knob)
    JARVIS_BODY_WAL_MAX_AGE_DAYS     (default 7, passed to WAL)
    JARVIS_BODY_WAL_COMPACT_EVERY_N  (default 256)
    JARVIS_BODY_WAL_PROBE_INTERVAL_S (default 5.0)
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.cooperative_fs_io import (
    is_offload_error,
    offload,
)
from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry

logger = logging.getLogger(__name__)

_HEX_DIGITS = frozenset("0123456789abcdef")


def _repo_root() -> Path:
    # .../backend/core/ouroboros/governance/transport/durable_outbound.py
    # parents: [0]=transport [1]=governance [2]=ouroboros [3]=core
    #          [4]=backend   [5]=<repo root>
    return Path(__file__).resolve().parents[5]


def _default_wal_path() -> Path:
    raw = os.environ.get("JARVIS_BODY_WAL_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / ".jarvis" / "body_outbound_wal.jsonl"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _probe_disk(wal_path_str: str) -> Tuple[Optional[int], int]:
    """Sync probe worker -- ALWAYS dispatched off-loop via ``offload``.

    Returns ``(free_bytes_or_None, wal_file_size)``. ``None`` free means
    the probe itself failed -- capacity enforcement then fails OPEN
    (journal everything) rather than dropping signals on a broken probe.
    """
    try:
        parent = os.path.dirname(wal_path_str) or "."
        free: Optional[int] = shutil.disk_usage(parent).free
    except OSError:
        free = None
    try:
        size = os.stat(wal_path_str).st_size
    except OSError:
        size = 0
    return free, size


class DurableOutbound:
    """Durable journal for outbound bridgeable events + ack-driven trim.

    Lifecycle: construct -> ``await start()`` (recovers pending set from
    disk, subscribes the broker, arms the journaling consumer) ->
    ``await stop()``. A NEVER-started instance still serves
    :meth:`pending` / :meth:`pending_count` from disk (crash recovery /
    replay callers).
    """

    def __init__(
        self,
        broker: Any,
        *,
        wal_path: Optional[str] = None,
        op_prefix: str = "trinity:",
    ) -> None:
        self._broker = broker
        self._op_prefix = op_prefix
        self._wal_path = Path(wal_path) if wal_path else _default_wal_path()
        self._wal = WAL(
            self._wal_path,
            max_age_days=_env_int("JARVIS_BODY_WAL_MAX_AGE_DAYS", 7),
        )
        self._disk_fraction = _env_float("JARVIS_BODY_WAL_DISK_FRACTION", 0.05)
        self._probe_interval_s = _env_float(
            "JARVIS_BODY_WAL_PROBE_INTERVAL_S", 5.0)
        self._compact_every_n = max(
            1, _env_int("JARVIS_BODY_WAL_COMPACT_EVERY_N", 256))

        self._pending: Dict[str, WALEntry] = {}
        self._ack_inflight: Set[str] = set()
        self._ack_cursor: Optional[str] = None
        self._acks_since_compact = 0
        self._degraded = False
        self._loaded = False
        self._probe_cache: Optional[Tuple[float, Optional[int], int]] = None

        self._sub: Any = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._bg_tasks: Set[asyncio.Task] = set()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Arm the durable subscriber. Idempotent."""
        if self._consumer_task is not None:
            return
        # Subscribe FIRST so events published during disk recovery queue up
        # (fresh event ids can never collide with recovered ones).
        self._sub = self._broker.subscribe()
        if self._sub is None:
            raise RuntimeError(
                "DurableOutbound: broker subscriber cap exceeded")
        if not self._loaded:
            entries = await offload(self._wal.pending_entries)
            if is_offload_error(entries):
                logger.debug(
                    "[DurableOutbound] WAL recovery read failed: %s", entries)
            else:
                for entry in entries:
                    self._pending.setdefault(entry.lease_id, entry)
            self._loaded = True
        self._consumer_task = asyncio.ensure_future(self._consume())

    async def stop(self) -> None:
        """Disarm the consumer and drain in-flight WAL work. Idempotent."""
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 -- fail-soft teardown
                logger.debug(
                    "[DurableOutbound] consumer teardown error", exc_info=True)
            self._consumer_task = None
        if self._sub is not None:
            try:
                self._broker.unsubscribe(self._sub)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DurableOutbound] broker unsubscribe failed",
                    exc_info=True)
            self._sub = None
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)

    # --- public read surface -------------------------------------------------

    def pending(self) -> List[Dict[str, Any]]:
        """Pending envelopes ordered by event_id (monotonic hex).

        Reflects acked-in-memory IMMEDIATELY: entries whose ack tombstone
        is still in flight to disk are already excluded.
        """
        self._ensure_loaded()
        live = [(lid, entry) for lid, entry in self._pending.items()
                if lid not in self._ack_inflight]
        live.sort(key=lambda pair: pair[0])
        return [dict(entry.envelope_dict) for _, entry in live]

    def pending_count(self) -> int:
        self._ensure_loaded()
        return sum(1 for lid in self._pending
                   if lid not in self._ack_inflight)

    @property
    def degraded_capacity(self) -> bool:
        return self._degraded

    # --- ack lane (Task-1 hook target) ---------------------------------------

    def on_ack(self, acked_event_id: str) -> None:
        """Cumulative-cursor trim. Cheap + non-blocking -- safe to call
        straight from BusBridgeClient's inbound loop.

        Marks every pending entry with ``lease_id <= acked_event_id``
        acked in memory immediately, then fires the disk tombstone work
        through the offload substrate (fail-soft, fire-and-forget).
        Never raises.
        """
        try:
            if not acked_event_id:
                return
            if self._ack_cursor is None or acked_event_id > self._ack_cursor:
                self._ack_cursor = acked_event_id
            ids = sorted(
                lid for lid in self._pending
                if lid <= acked_event_id and lid not in self._ack_inflight)
            if not ids:
                return
            self._ack_inflight.update(ids)
            self._fire_and_forget(self._flush_acks_sync, ids)
        except Exception:  # noqa: BLE001 -- must never take down the caller
            logger.debug("[DurableOutbound] on_ack failed", exc_info=True)

    # --- internal: journaling consumer ---------------------------------------

    async def _consume(self) -> None:
        while True:
            event = await self._sub.queue.get()
            try:
                await self._journal_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- never kill the journal loop
                logger.debug(
                    "[DurableOutbound] journal failed for event",
                    exc_info=True)

    async def _journal_event(self, event: Any) -> None:
        op_id = getattr(event, "op_id", "") or ""
        if not op_id.startswith(self._op_prefix):
            return
        event_id = getattr(event, "event_id", "") or ""
        # Control frames (heartbeat "hb:", "replay:", "<id>:lag") carry
        # non-hex ids -- only real 012x-hex publishes are journaled.
        if not event_id or not set(event_id) <= _HEX_DIGITS:
            return
        if event_id in self._pending or event_id in self._ack_inflight:
            return
        if self._ack_cursor is not None and event_id <= self._ack_cursor:
            return  # the far side already acked past this id
        await self._enforce_capacity()
        entry = WALEntry(
            lease_id=event_id,
            envelope_dict=event.to_dict(),
            status="pending",
            ts_monotonic=time.monotonic(),
            ts_utc=datetime.now(timezone.utc).isoformat(),
        )
        result = await offload(self._wal.append, entry)
        if is_offload_error(result):
            logger.debug(
                "[DurableOutbound] WAL append failed (kept in memory): %s",
                result)
        # In-memory view tracks the accepted publish regardless -- the WAL
        # append path itself never raises, so this is belt-and-braces.
        self._pending[event_id] = entry

    # --- internal: dynamic capacity ------------------------------------------

    async def _enforce_capacity(self) -> None:
        """Disk-fraction budget check, entirely off-loop, never raises."""
        try:
            free, size = await self._probe(force=False)
            if free is None:
                return  # probe broken -> fail OPEN, keep journaling
            if size <= self._disk_fraction * free:
                self._clear_degraded()
                return
            # Over budget: compact first (drops aged terminal entries).
            result = await offload(self._wal.compact)
            if is_offload_error(result):
                logger.debug(
                    "[DurableOutbound] capacity compact failed: %s", result)
            free, size = await self._probe(force=True)
            if free is None or size <= self._disk_fraction * free:
                self._clear_degraded()
                return
            # STILL over: degraded episode -- one warning, drop oldest.
            if not self._degraded:
                self._degraded = True
                logger.warning(
                    "[DurableOutbound] degraded_capacity: wal_size=%d over "
                    "budget=%d (fraction=%s of free=%d) -- dropping oldest "
                    "pending per append until capacity clears",
                    size, int(self._disk_fraction * free),
                    self._disk_fraction, free)
            self._drop_oldest_pending()
        except Exception:  # noqa: BLE001 -- capacity must never break journal
            logger.debug(
                "[DurableOutbound] capacity enforcement failed",
                exc_info=True)

    def _clear_degraded(self) -> None:
        if self._degraded:
            self._degraded = False
            logger.info(
                "[DurableOutbound] capacity recovered -- degraded episode "
                "cleared")

    def _drop_oldest_pending(self) -> None:
        live = sorted(lid for lid in self._pending
                      if lid not in self._ack_inflight)
        if not live:
            return
        oldest = live[0]
        self._pending.pop(oldest, None)
        self._fire_and_forget(self._wal.update_status, oldest, "dead_letter")

    async def _probe(
        self, force: bool,
    ) -> Tuple[Optional[int], int]:
        """Cached off-loop disk probe (free bytes + WAL file size)."""
        now = time.monotonic()
        if (not force and self._probe_cache is not None
                and (now - self._probe_cache[0]) < self._probe_interval_s):
            return self._probe_cache[1], self._probe_cache[2]
        result = await offload(_probe_disk, str(self._wal_path))
        if is_offload_error(result):
            logger.debug("[DurableOutbound] disk probe failed: %s", result)
            return None, 0
        free, size = result
        self._probe_cache = (time.monotonic(), free, size)
        return free, size

    # --- internal: recovery + offload plumbing --------------------------------

    def _ensure_loaded(self) -> None:
        """One-time lazy recovery read for never-started instances."""
        if self._loaded:
            return
        self._loaded = True
        try:
            for entry in self._wal.pending_entries():
                self._pending.setdefault(entry.lease_id, entry)
        except Exception:  # noqa: BLE001 -- fail-soft
            logger.debug(
                "[DurableOutbound] lazy WAL recovery failed", exc_info=True)

    def _flush_acks_sync(self, ids: List[str]) -> None:
        """Tombstone acked ids + cadence compaction. Runs OFF the event
        loop (offload thread) or inline in the no-loop fallback -- the
        WAL substrate is flock-safe either way."""
        for lease_id in ids:
            try:
                self._wal.update_status(lease_id, "acked")
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DurableOutbound] ack tombstone failed lease=%s",
                    lease_id, exc_info=True)
            self._pending.pop(lease_id, None)
            self._ack_inflight.discard(lease_id)
        self._acks_since_compact += len(ids)
        if self._acks_since_compact >= self._compact_every_n:
            self._acks_since_compact = 0
            try:
                self._wal.compact()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DurableOutbound] cadence compact failed", exc_info=True)

    def _fire_and_forget(self, fn: Callable[..., Any], *args: Any) -> None:
        """Schedule sync WAL work through the offload substrate as a
        tracked fire-and-forget task; inline fail-soft fallback when no
        loop is running (pure-sync callers/tests)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DurableOutbound] inline WAL op failed", exc_info=True)
            return

        async def _run() -> None:
            result = await offload(fn, *args)
            if is_offload_error(result):
                logger.debug(
                    "[DurableOutbound] offloaded WAL op failed: %s", result)

        task = loop.create_task(_run())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
