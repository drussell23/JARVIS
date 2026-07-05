# -*- coding: utf-8 -*-
"""DurableOutbound -- Stage-3 Task 2: the Body-side durable outbound WAL.

Journals every bridgeable StreamEventBroker event (op_id matching
``op_prefix``, default ``trinity:``; optionally narrowed further by the
``journal_filter`` predicate -- the Body driver uses it to exclude
peer-republished events whose client-local ids the server never issued)
into a flock-journaled write-ahead log AT PUBLISH TIME -- upstream of
any connection state. A network
partition or a Body crash can therefore never lose a signal that
``publish()`` accepted: the WAL is the durable truth, the broker's
bounded history ring (512 default) is merely a hot cache.

Trim is ack-driven: Task 1 armed the ack lane (``BusBridgeClient``'s
``on_ack`` constructor kwarg); wiring that callback to
:meth:`DurableOutbound.on_ack` tombstones journaled entries and compacts
the file every ``JARVIS_BODY_WAL_COMPACT_EVERY_N`` acks.

Stage-4 Task 2 -- EXACT-SET trim + priority replay. Replay is now
PRIORITY-ordered (:meth:`pending_prioritized` /
``pending(order="priority")``, sorted by ``(urgency_rank, event_id)``
with ranks imported from the UrgencyRouter vocabulary -- never
re-declared here), which breaks the Stage-3 id-order assumption that
made a cumulative ``lease_id <= acked_event_id`` sweep correct: an ack
for a high IMMEDIATE id sent FIRST must not sweep numerically-lower ids
that were never sent. :meth:`on_ack` therefore trims EXACTLY the acked
id (idempotent against replays of the same id); cumulation moved to the
CLIENT, whose send-order-cumulative ``_apply_ack`` fires ``on_ack`` once
per confirmed id in send order. This also retires the Stage-3 strand
class where an ack racing an offloaded append could sweep unsent ids.

Durability heavy-lifting is the intake WAL, reused verbatim (operator
DRY mandate): flock-serialized append per line (fsync happens on
compaction and on the no-flock fallback path -- the hot path is
flock+flush), tombstone-applied crash recovery, atomic
tmp+fsync+replace compaction.

Durability honesty (review round): an append that FAILS is NEVER
reported as pending-durable. The event moves to an in-memory at-risk
set -- surfaced via :attr:`DurableOutbound.journal_failures` plus one
loud warning per episode -- and is re-attempted on every subsequent
journal cycle until it lands (a cumulative ack past it does NOT purge
it: the ack cannot prove the gap was received; server dedup makes the
resulting duplicate safe). Likewise
a failed ack tombstone keeps its lease parked for retry on later
flushes: redelivery after a crash is SAFE downstream (server dedup);
a silent live-acked/disk-pending split-brain is not.

Capacity is DYNAMIC -- no hardcoded byte caps anywhere. The WAL file may
occupy at most ``JARVIS_BODY_WAL_DISK_FRACTION`` (default 0.05) of the
CURRENT free bytes on the WAL's filesystem, probed off-loop through the
cooperative_fs_io offload substrate and cached for
``JARVIS_BODY_WAL_PROBE_INTERVAL_S`` between appends. Over budget ->
compact ONCE at episode onset; still over -> ``degraded_capacity``
episode: drop the OLDEST pending entry (dead_letter tombstone) per
append with exactly ONE ``logger.warning`` per episode (mirroring the
broker's ``stream_lag`` single-signal pattern). Never raises, never
blocks the event loop.

Env knobs:
    JARVIS_BODY_WAL_PATH             (default <repo>/.jarvis/body_outbound_wal.jsonl)
    JARVIS_BODY_WAL_DISK_FRACTION    (default 0.05 -- the ONLY capacity knob)
    JARVIS_BODY_WAL_MAX_AGE_DAYS     (default 7, passed to WAL)
    JARVIS_BODY_WAL_COMPACT_EVERY_N  (default 256)
    JARVIS_BODY_WAL_PROBE_INTERVAL_S (default 5.0)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.background_agent_pool import (
    _ROUTE_PRIORITY,
)
from backend.core.ouroboros.governance.cooperative_fs_io import (
    is_offload_error,
    offload,
)
from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry
from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    _BACKGROUND_SOURCES,
    _BACKGROUND_URGENCIES,
    _IMMEDIATE_SOURCES,
    _IMMEDIATE_URGENCIES,
    _SPECULATIVE_SOURCES,
)

logger = logging.getLogger(__name__)

_HEX_DIGITS = frozenset("0123456789abcdef")

# ---------------------------------------------------------------------------
# Stage-4 Task 2: urgency -> replay rank, derived ENTIRELY from the
# UrgencyRouter vocabulary (operator mandate: no re-declared rank table).
# The priority ints come from the pool's canonical _ROUTE_PRIORITY map
# (IMMEDIATE=1 ... SPECULATIVE=7, keyed by ProviderRoute values); the
# urgency/source affinity sets are the router's own frozensets.
# ---------------------------------------------------------------------------

_STANDARD_RANK = _ROUTE_PRIORITY[ProviderRoute.STANDARD.value]


def _route_rank(route: ProviderRoute) -> int:
    """Priority int for a route, from the imported canonical table.
    Routes absent from the table (INFORMATIONAL / WIRING_VALIDATION)
    rank STANDARD -- the default cascade."""
    return _ROUTE_PRIORITY.get(route.value, _STANDARD_RANK)


def urgency_rank(envelope_dict: Dict[str, Any]) -> int:
    """Replay rank for a journaled WAL entry's StreamEvent dict.

    Trinity-bridged intake signals carry the IntentEnvelope at
    ``envelope_dict["payload"]["data"]`` (RemoteIntakeSubmitter publishes
    ``envelope.to_dict()`` as the TrinityEvent payload;
    ``TrinityBusBridge._on_outbound`` wraps it as
    ``{"topic": ..., "data": <envelope dict>, "origin": ...}``). The
    envelope's ``urgency`` (critical/high/normal/low) plus ``source``
    map onto the UrgencyRouter's Priority 1-3 matrix (the deterministic
    subset a WAL entry can know -- complexity/file-count classification
    needs the live ROUTE phase and is unavailable here):

      * urgency in _IMMEDIATE_URGENCIES            -> IMMEDIATE
      * urgency == "high" + source immediate-class -> IMMEDIATE
      * source speculative-class + low/normal      -> SPECULATIVE
      * low urgency + source background-class      -> BACKGROUND
      * everything else / missing / garbage        -> STANDARD

    Tolerant by mandate: any shape violation ranks STANDARD. NEVER
    raises. Lower rank replays first.
    """
    try:
        payload = envelope_dict.get("payload") if isinstance(
            envelope_dict, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return _STANDARD_RANK
        urgency_raw = data.get("urgency")
        source_raw = data.get("source")
        urgency = urgency_raw.strip().lower() if isinstance(
            urgency_raw, str) else ""
        source = source_raw.strip().lower() if isinstance(
            source_raw, str) else ""
        if urgency in _IMMEDIATE_URGENCIES:
            return _route_rank(ProviderRoute.IMMEDIATE)
        if urgency == "high" and source in _IMMEDIATE_SOURCES:
            return _route_rank(ProviderRoute.IMMEDIATE)
        if source in _SPECULATIVE_SOURCES and urgency in ("low", "normal"):
            return _route_rank(ProviderRoute.SPECULATIVE)
        if urgency in _BACKGROUND_URGENCIES and source in _BACKGROUND_SOURCES:
            return _route_rank(ProviderRoute.BACKGROUND)
        return _STANDARD_RANK
    except Exception:  # noqa: BLE001 -- shape tolerance is the contract
        return _STANDARD_RANK


def _sorted_priority_envelopes(
    snapshot: List[Tuple[str, WALEntry]],
) -> List[Dict[str, Any]]:
    """Sync sort worker for :meth:`DurableOutbound.pending_prioritized`
    -- ALWAYS dispatched off-loop via ``offload`` (operator mandate: no
    blocking sort of a large backlog on the event loop)."""
    snapshot.sort(
        key=lambda pair: (urgency_rank(pair[1].envelope_dict), pair[0]))
    return [dict(entry.envelope_dict) for _, entry in snapshot]


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


def wal_high_water(wal_path: Any) -> int:
    """Highest event-id sequence EVER journaled into the WAL at ``wal_path``.

    Stage-3 Task 7 (cross-lifetime identity, live-fire finding A): the
    broker's event-id sequence is in-memory and restarts at 0 every
    process lifetime, while the WAL (and the far side's qualified-id
    dedup) remember ids across lifetimes. A restarted Body re-minted ids
    a previous lifetime had already journaled -- new publishes were
    skipped by ``_journal_event`` as already-pending (published but
    UNJOURNALED: silent loss during a partition). Seeding
    ``StreamEventBroker(initial_event_seq=wal_high_water(path))`` makes
    every new lifetime mint ids strictly above every id ever journaled.

    Scans ALL records regardless of status -- acked / dead_letter
    tombstones included: an id that was EVER minted must never be minted
    again, trimmed or not. Parses ``lease_id`` as base-16 (event ids are
    zero-padded 012x hex). Tolerant line-by-line parse mirroring
    ``WAL.pending_entries``: blank / corrupt / non-hex lines are skipped.
    Returns 0 for a missing or empty file. NEVER raises (fail-soft:
    an unreadable WAL degrades to the legacy 0 seed).
    """
    high = 0
    try:
        path = Path(wal_path)
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "[wal_high_water] corrupt entry at line %d, "
                        "skipping", line_no)
                    continue
                if not isinstance(record, dict):
                    continue
                lease_id = record.get("lease_id", "")
                if not isinstance(lease_id, str) or not lease_id:
                    continue
                if not set(lease_id) <= _HEX_DIGITS:
                    continue  # non-hex control ids never seed the broker
                try:
                    value = int(lease_id, 16)
                except ValueError:
                    continue
                if value > high:
                    high = value
    except Exception:  # noqa: BLE001 -- fail-soft: seed degrades to 0
        logger.debug("[wal_high_water] scan failed for %r", wal_path,
                     exc_info=True)
    return high


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
        journal_filter: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self._broker = broker
        self._op_prefix = op_prefix
        # Task-4 (Task-3 review carry-in): optional publish-side filter.
        # When provided, only events where journal_filter(event) is True
        # are journaled. The driver's live default excludes PEER-
        # republished events (payload origin != local source_id): those
        # carry client-local event ids the server has never seen --
        # replaying them at reconnect defeats the server's qualified-id
        # dedup and duplicates events. A filter that RAISES fails open
        # (journal anyway): durability bias over filtering precision.
        self._journal_filter = journal_filter
        # Review round: fail-open must not be SILENT -- one warning per
        # failure episode (the file's established warn-once pattern),
        # reset when the filter evaluates cleanly again.
        self._filter_fail_warned = False
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
        # Review CRITICAL: events whose WAL append FAILED -- accepted but
        # NOT durable. Never counted as pending; retried each journal
        # cycle until they land or the ack cursor passes them.
        self._at_risk: Dict[str, WALEntry] = {}
        self._journal_fail_warned = False
        # Review IMPORTANT-1: leases whose ack tombstone write FAILED --
        # kept in _ack_inflight (so pending() stays trimmed) and retried
        # on every later flush until the tombstone lands.
        self._ack_retry: Set[str] = set()
        self._ack_fail_warned = False

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

    def pending(self, order: str = "id") -> List[Dict[str, Any]]:
        """Pending envelopes, ordered by event_id (monotonic hex) by
        default. ``order="priority"`` (Stage-4 Task 2) sorts by
        ``(urgency_rank(entry), event_id)`` instead -- IMMEDIATE-class
        entries first, id-ordered within each class.

        Reflects acked-in-memory IMMEDIATELY: entries whose ack tombstone
        is still in flight to disk are already excluded.

        Iterates a SNAPSHOT of the pending dict (Task-3 carry-forward):
        the ack flush pops entries from an offload THREAD, and iterating
        the live dict here races that mutation (RuntimeError: dict
        changed size during iteration).

        SYNC api -- legacy callers unchanged. Async callers replaying a
        large backlog should use :meth:`pending_prioritized`, which
        computes the sort off-loop.
        """
        self._ensure_loaded()
        live = [(lid, entry) for lid, entry in list(self._pending.items())
                if lid not in self._ack_inflight]
        if order == "priority":
            return _sorted_priority_envelopes(live)
        live.sort(key=lambda pair: pair[0])
        return [dict(entry.envelope_dict) for _, entry in live]

    async def pending_prioritized(self) -> List[Dict[str, Any]]:
        """Priority-ordered pending snapshot with the sort computed
        OFF-LOOP (Stage-4 Task 2 operator mandate: ``_replay_durable``
        is async and must never block the event loop sorting a large
        backlog). The snapshot itself is a cheap on-loop list copy; the
        ``(urgency_rank, event_id)`` sort + envelope materialization run
        through the offload substrate. A broken offload fails soft to
        the legacy id-ordered snapshot (same cost class as the sync
        :meth:`pending` every legacy caller already pays)."""
        if not self._loaded:
            self._loaded = True
            entries = await offload(self._wal.pending_entries)
            if is_offload_error(entries):
                logger.debug(
                    "[DurableOutbound] WAL recovery read failed: %s", entries)
            else:
                for entry in entries:
                    self._pending.setdefault(entry.lease_id, entry)
        snapshot = [(lid, entry) for lid, entry in list(self._pending.items())
                    if lid not in self._ack_inflight]
        result = await offload(_sorted_priority_envelopes, snapshot)
        if is_offload_error(result):
            logger.debug(
                "[DurableOutbound] offloaded priority sort failed: %s -- "
                "falling back to id order", result)
            snapshot.sort(key=lambda pair: pair[0])
            return [dict(entry.envelope_dict) for _, entry in snapshot]
        return result

    def pending_count(self) -> int:
        self._ensure_loaded()
        return sum(1 for lid in list(self._pending)
                   if lid not in self._ack_inflight)

    @property
    def degraded_capacity(self) -> bool:
        return self._degraded

    @property
    def journal_failures(self) -> int:
        """Count of accepted events currently at risk (memory-only --
        their WAL append failed and has not yet been retried to
        success). Zero means every accepted publish is on disk."""
        return len(self._at_risk)

    # --- ack lane (Task-1 hook target) ---------------------------------------

    def on_ack(self, acked_event_id: str) -> None:
        """EXACT-SET trim (Stage-4 Task 2). Cheap + non-blocking -- safe
        to call straight from BusBridgeClient's inbound loop.

        Trims EXACTLY ``acked_event_id`` -- never a ``<=`` sweep. Replay
        is priority-ordered now, so an ack for a high IMMEDIATE id sent
        FIRST proves nothing about numerically-lower ids that may never
        have been sent (the Stage-3 over-trim strand class; it also
        covered an ack racing an offloaded append). Cumulation is the
        CLIENT's job: its send-order-cumulative ``_apply_ack`` calls
        this once per confirmed id, in send order.

        Idempotent against replays of the same id (already trimmed /
        already in-flight -> no-op, modulo draining any parked ack
        retries). Marks the entry acked in memory immediately, then
        fires the disk tombstone work through the offload substrate
        (fail-soft, fire-and-forget). Never raises.
        """
        try:
            if not acked_event_id:
                return
            # Observability cursor: highest id ever exact-acked. No longer
            # a cumulative sweep boundary -- see _journal_event for why the
            # <= duplicate-guard there remains sound (broker ids are
            # strictly monotonic within a lifetime).
            if self._ack_cursor is None or acked_event_id > self._ack_cursor:
                self._ack_cursor = acked_event_id
            # Task-3 carry-forward (re-review): an at-risk entry (failed
            # append, memory-only) is NOT purged by its ack -- the ack
            # proves the server INGESTED the live send, not that the entry
            # is durably journaled; purging it here loses it forever if
            # the process dies before the append lands. _retry_at_risk
            # keeps re-attempting; the eventual duplicate delivery is
            # absorbed by server-side qualified-id dedup.
            ids: List[str] = []
            if (acked_event_id in self._pending
                    and acked_event_id not in self._ack_inflight):
                ids.append(acked_event_id)
            if not ids and not self._ack_retry:
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
        if self._journal_filter is not None:
            try:
                keep = bool(self._journal_filter(event))
            except Exception as exc:  # noqa: BLE001 -- fail OPEN: durability bias
                keep = True
                if not self._filter_fail_warned:
                    self._filter_fail_warned = True
                    logger.warning(
                        "[DurableOutbound] journal_filter raising -- "
                        "peer-exclusion degraded to fail-open (journaling "
                        "everything; duplicates absorbed by server dedup): "
                        "%s", exc)
            else:
                if self._filter_fail_warned:
                    self._filter_fail_warned = False
                    logger.info(
                        "[DurableOutbound] journal_filter recovered -- "
                        "peer-exclusion back in force")
            if not keep:
                return
        if (event_id in self._pending or event_id in self._ack_inflight
                or event_id in self._at_risk):
            return
        if self._ack_cursor is not None and event_id <= self._ack_cursor:
            # Duplicate guard, still sound under exact-set acks (Stage-4
            # Task 2): broker ids are strictly monotonic within a
            # lifetime and _consume sees publishes in id order, so any
            # id at-or-below the highest EXACT-acked id can only be a
            # replay of a publish this journal already processed --
            # never a fresh event.
            return
        await self._enforce_capacity()
        # Bounded at-risk retry: each journal cycle re-attempts every
        # previously-failed append exactly once (review CRITICAL).
        await self._retry_at_risk()
        entry = WALEntry(
            lease_id=event_id,
            envelope_dict=event.to_dict(),
            status="pending",
            ts_monotonic=time.monotonic(),
            ts_utc=datetime.now(timezone.utc).isoformat(),
        )
        result = await offload(self._wal.append, entry)
        if is_offload_error(result):
            # Durability honesty (review CRITICAL): the append did NOT
            # land -- the event must NOT masquerade as pending-durable.
            # Park it at-risk (memory-only), surface loudly ONCE per
            # episode, retry on subsequent journal cycles.
            self._at_risk[event_id] = entry
            if not self._journal_fail_warned:
                self._journal_fail_warned = True
                logger.warning(
                    "[DurableOutbound] journal append FAILED -- %d event(s) "
                    "at risk (memory-only, NOT durable; will retry on "
                    "subsequent journal cycles): %s",
                    len(self._at_risk), result)
            return
        self._pending[event_id] = entry

    async def _retry_at_risk(self) -> None:
        """Re-attempt every at-risk append once. Clears the failure
        episode (and says so) when the at-risk set drains."""
        if not self._at_risk:
            if self._journal_fail_warned:
                self._journal_fail_warned = False
                logger.info(
                    "[DurableOutbound] journal append recovered -- no "
                    "events remain at risk")
            return
        for event_id in sorted(self._at_risk):
            entry = self._at_risk.get(event_id)
            if entry is None:
                continue
            # Task-3 carry-forward: no ack-cursor short-circuit here -- a
            # cumulative ack past this id does not prove the far side
            # received it (see on_ack). Retry until the append LANDS; the
            # journaled entry then rides pending()/WAL replay, and server
            # dedup makes any duplicate delivery safe.
            result = await offload(self._wal.append, entry)
            if is_offload_error(result):
                continue  # still failing -- next cycle retries again
            self._at_risk.pop(event_id, None)
            self._pending[event_id] = entry
        if not self._at_risk and self._journal_fail_warned:
            self._journal_fail_warned = False
            logger.info(
                "[DurableOutbound] journal append recovered -- all at-risk "
                "events landed durably")

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
            if not self._degraded:
                # Episode ONSET only (review IMPORTANT-2): compact once
                # (drops aged terminal entries), re-probe; only if STILL
                # over does the degraded episode begin. While degraded,
                # subsequent over-budget appends drop-oldest WITHOUT
                # re-running a full compaction each time.
                result = await offload(self._wal.compact)
                if is_offload_error(result):
                    logger.debug(
                        "[DurableOutbound] capacity compact failed: %s",
                        result)
                free, size = await self._probe(force=True)
                if free is None or size <= self._disk_fraction * free:
                    return  # compact bought back the budget -- no episode
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
        live = sorted(lid for lid in list(self._pending)
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
        """One-time lazy recovery read for never-started instances.

        GUARD (review Minor-b): this is a SYNCHRONOUS disk read. It is
        intended ONLY for never-started instances queried from sync
        contexts (crash-recovery / replay callers); started instances
        recover through the offloaded read in :meth:`start`. If a
        running event loop is detected we still proceed -- it is a
        one-time bounded read and pending()/pending_count() are sync
        APIs -- but we flag the architectural-purity violation loudly
        so the caller can switch to ``await start()`` first.
        """
        if self._loaded:
            return
        self._loaded = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no loop -- the intended sync-context caller
        else:
            logger.warning(
                "[DurableOutbound] _ensure_loaded performing a sync WAL "
                "read ON a running event loop -- call 'await start()' "
                "before pending()/pending_count() to keep the loop clean")
        try:
            for entry in self._wal.pending_entries():
                self._pending.setdefault(entry.lease_id, entry)
        except Exception:  # noqa: BLE001 -- fail-soft
            logger.debug(
                "[DurableOutbound] lazy WAL recovery failed", exc_info=True)

    def _flush_acks_sync(self, ids: List[str]) -> None:
        """Tombstone acked ids (plus any leases parked for retry) +
        cadence compaction. Runs OFF the event loop (offload thread) or
        inline in the no-loop fallback -- the WAL substrate is
        flock-safe either way.

        Review IMPORTANT-1: a tombstone write that fails must NOT be
        forgotten (that would leave live-says-acked / disk-says-pending
        split-brain silently). The lease stays in ``_ack_inflight`` (so
        pending() remains trimmed) and is parked in ``_ack_retry`` for
        the next flush; ONE distinguishing warning per episode.
        Redelivery after a crash is SAFE downstream (server dedup)."""
        to_flush = list(dict.fromkeys(list(ids) + sorted(self._ack_retry)))
        landed = 0
        failed = False
        for lease_id in to_flush:
            try:
                self._wal.update_status(lease_id, "acked")
            except Exception:  # noqa: BLE001 -- park for retry
                failed = True
                self._ack_retry.add(lease_id)
                logger.debug(
                    "[DurableOutbound] ack tombstone failed lease=%s "
                    "(parked for retry)", lease_id, exc_info=True)
                continue
            self._ack_retry.discard(lease_id)
            self._pending.pop(lease_id, None)
            self._ack_inflight.discard(lease_id)
            landed += 1
        if failed and not self._ack_fail_warned:
            self._ack_fail_warned = True
            logger.warning(
                "[DurableOutbound] ack tombstone write failing -- "
                "redelivery possible after crash (%d lease(s) parked "
                "for retry)", len(self._ack_retry))
        if not self._ack_retry and self._ack_fail_warned:
            self._ack_fail_warned = False
            logger.info(
                "[DurableOutbound] ack tombstone writes recovered -- "
                "retry park drained")
        self._acks_since_compact += landed
        if landed and self._acks_since_compact >= self._compact_every_n:
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
