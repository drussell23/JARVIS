"""dw_batch_ledger.py — durable ownership of every DoubleWord batch we create.

Slice 18. The law this module exists to enforce:

    **Silence is not consent.**

    A ``200 OK`` at ``POST /batches`` is not a grant to serve. On a transport
    that *cannot refuse*, absence of an error is not evidence of health.

Root cause (Seb @ Doubleword, 2026-07-14, "Cancelled batches"): two batches —
``3d302917`` and ``ac978f6d``, both ``mistralai/Devstral-2-123B-Instruct-2512``
— were accepted by DW, flipped to ``in_progress`` one second later, and then
did *nothing at all*: ``request_counts={total:1, completed:0, failed:0}``, an
empty output file, an empty error file, for the full 1h completion window. That
same model answers ``403`` on the REAL-TIME endpoint in 0.68s. The batch API
does not enforce entitlement at submit time, so a denial that is loud and
instant on one plane becomes a silent one-hour black hole on the other.

Our temporal breaker (``SovereignBatchTimeoutError``, 300s) *did* walk away from
the wedged batch. It walked away **without cancelling it**. So the batch outlived
the process, squatted on DW's queue, and 5 hours later a human at DoubleWord had
to cancel it by hand and email us. That email is our missing cleanup path,
outsourced to their support team.

This ledger is the missing half of ``_create_batch``: the moment we ask DW to do
work on our behalf we incur an *obligation* — to collect the result or to release
the claim. Nothing else in the codebase knew a batch existed once the owning
coroutine died, because ``BatchFutureRegistry`` is in-memory (two plain dicts of
``asyncio.Future``) and every ``batch_id`` evaporated on process exit.

Three capabilities, in ascending order of authority:

1. **Ownership** (``record_open`` / ``settle``): a durable, atomically
   written record of every batch we hold a claim on. Pure observation.
2. **Release** (``open_claims`` → the provider's ``_cancel_batch``): every
   walk-away path can now settle its claim instead of leaking it.
3. **Reconciliation** (the provider's ``reconcile_orphan_batches``, reading
   ``open_claims(foreign_only=True)``): on boot, batches a dead session still
   owned are either accounted as already-settled by DW or *released*
   (cancelled). This is the same §2 Progressive Awakening pattern as
   ``WorktreeManager.reap_orphans()``: recover from SIGKILL / OOM /
   power-loss leftovers rather than pretending they cannot happen.

Authority-free by construction: this module records and reports. It never
selects a model, never routes, never cancels anything itself — it hands the
open-claim set to the provider, which owns the wire. Fail-soft everywhere: a
ledger fault must never perturb dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

# Reuse the proven atomic writer — no duplication (same discipline as
# dw_transport_profile, which imports it from the same place).
from backend.core.ouroboros.governance.dw_ttft_observer import _atomic_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "dw_batch_ledger.1"

_ENV_MASTER = "JARVIS_DW_BATCH_LEDGER_ENABLED"
_ENV_STATE_PATH = "JARVIS_DW_BATCH_LEDGER_STATE_PATH"
_ENV_RETENTION_S = "JARVIS_DW_BATCH_LEDGER_RETENTION_S"
_ENV_COMPACT_INTERVAL_S = "JARVIS_DW_BATCH_LEDGER_COMPACT_INTERVAL_S"

# Compaction (dropping settled records past retention) is an O(N) scan; running
# it on EVERY save makes each single-claim mutation pay for the whole history.
# Time-gate it instead — correctness does not depend on when it runs.
_DEFAULT_COMPACT_INTERVAL_S = 3600.0

# How long a SETTLED record is kept for forensics before compaction drops it.
# Open claims are NEVER pruned by age — an unsettled obligation does not expire
# just because we stopped looking at it. That is the whole bug this module
# exists to kill.
_DEFAULT_RETENTION_S = 7 * 24 * 3600.0  # 7 days of settled history

# ── Claim states ──────────────────────────────────────────────────────
# OPEN is the only state that carries an obligation. Everything else is settled.
STATE_OPEN = "open"              # we hold a claim; DW may still be working
STATE_COMPLETED = "completed"    # result collected
STATE_TERMINAL = "terminal"      # DW settled it (failed/expired/cancelled)
STATE_CANCELLED = "cancelled"    # we released the claim on the wire
STATE_ABANDONED = "abandoned"    # we walked away and could NOT cancel (leak!)

_SETTLED_STATES = (
    STATE_COMPLETED, STATE_TERMINAL, STATE_CANCELLED, STATE_ABANDONED,
)


def batch_ledger_enabled() -> bool:
    """Master gate. Default TRUE — this is pure observation plus a durable
    file write, and the legacy behavior it replaces is *leaking the claim*.
    Reverting to OFF restores the leak, so OFF is never the safer default.
    NEVER raises."""
    return (os.environ.get(_ENV_MASTER, "true") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _state_path() -> Path:
    raw = (os.environ.get(
        _ENV_STATE_PATH, ".jarvis/dw_batch_ledger.json",
    ) or "").strip()
    return Path(raw or ".jarvis/dw_batch_ledger.json")


def _retention_s() -> float:
    """Settled-record retention. Clamped to (0, 90d]; garbage → default.
    NEVER raises."""
    raw = (os.environ.get(_ENV_RETENTION_S, "") or "").strip()
    try:
        v = float(raw) if raw else _DEFAULT_RETENTION_S
    except (TypeError, ValueError):
        v = _DEFAULT_RETENTION_S
    if v <= 0.0:
        v = _DEFAULT_RETENTION_S
    return min(v, 90 * 24 * 3600.0)


def _compact_interval_s() -> float:
    """Minimum seconds between compaction scans. Garbage → default.
    NEVER raises."""
    raw = (os.environ.get(_ENV_COMPACT_INTERVAL_S, "") or "").strip()
    try:
        v = float(raw) if raw else _DEFAULT_COMPACT_INTERVAL_S
    except (TypeError, ValueError):
        v = _DEFAULT_COMPACT_INTERVAL_S
    if v <= 0.0:
        v = _DEFAULT_COMPACT_INTERVAL_S
    return v


@dataclass
class BatchClaim:
    """One obligation: a batch DW is holding on our behalf.

    ``expires_at`` is **DW's own number** (echoed back from the batch object),
    not one we invent. When we need to reason about how long a batch may
    legitimately take, we use the window the vendor told us about rather than
    a constant of our own — the vendor is the authority on its own queue.
    """

    batch_id: str
    model: str = ""
    op_id: str = ""
    route: str = ""
    # Wall-clock (time.time) — must survive a process boundary, so monotonic
    # is useless here.
    created_at: float = field(default_factory=time.time)
    # DW's declared completion deadline (unix seconds), 0 if unknown.
    expires_at: float = 0.0
    state: str = STATE_OPEN
    # The reasoning_effort we actually SENT for this batch. Carried here rather
    # than on the in-memory PendingBatch because the terminal handler runs deep
    # inside the poll loop, which only holds a batch_id — and because a batch
    # that outlives the process must still be diagnosable on the next boot.
    # Feeds dw_reasoning_profile.maybe_learn_from_error, which needs to know what
    # we sent in order to learn what the model refuses.
    reasoning_effort: str = ""
    # Why the claim was settled the way it was — free-text forensics.
    reason: str = ""
    settled_at: float = 0.0
    # The pid that opened the claim. A claim whose pid is not us is, by
    # definition, an orphan from a dead session.
    pid: int = field(default_factory=os.getpid)

    @property
    def is_open(self) -> bool:
        return self.state == STATE_OPEN

    def to_obj(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any]) -> Optional["BatchClaim"]:
        """Tolerant rehydration — an unparseable record is dropped, never
        raised. A corrupt ledger must degrade to a smaller ledger, never to a
        crashed boot."""
        try:
            bid = str(obj.get("batch_id") or "").strip()
            if not bid:
                return None
            return cls(
                batch_id=bid,
                model=str(obj.get("model") or ""),
                op_id=str(obj.get("op_id") or ""),
                route=str(obj.get("route") or ""),
                created_at=float(obj.get("created_at") or 0.0),
                expires_at=float(obj.get("expires_at") or 0.0),
                reasoning_effort=str(obj.get("reasoning_effort") or ""),
                state=str(obj.get("state") or STATE_OPEN),
                reason=str(obj.get("reason") or ""),
                settled_at=float(obj.get("settled_at") or 0.0),
                pid=int(obj.get("pid") or 0),
            )
        except Exception:  # noqa: BLE001 — a bad row is a dropped row
            return None


class BatchLedger:
    """Durable record of every DW batch claim. Thread-safe, fail-soft.

    Not an async class on purpose: every method here is a fast in-memory
    mutation plus an atomic file write, and it is called from inside the
    provider's hot paths (including ``finally`` blocks and shutdown, where an
    ``await`` may not be safe). The one operation that *does* touch the wire —
    cancellation — lives in the provider, which owns the session.
    """

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._path = state_path
        self._claims: Dict[str, BatchClaim] = {}
        self._lock = threading.RLock()
        self._loaded = False
        # Async-first discipline (CLAUDE.md: no blocking calls on the event
        # loop). When save() is reached from a coroutine, the serialized
        # payload is handed to a coalescing off-loop flusher instead of
        # blocking the loop on an fsync-rename. ``_pending_payload`` always
        # holds the LATEST snapshot; one in-flight flush task drains it in a
        # loop, so bursts of settles collapse into few writes and out-of-order
        # completion (stale-data-wins) is structurally impossible.
        self._pending_payload: Optional[str] = None
        self._flush_task: Optional[Any] = None
        self._last_compact_at: float = 0.0

    # ── Persistence ───────────────────────────────────────────────────

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _state_path()

    def load(self) -> None:
        """Rehydrate from disk. A missing/corrupt file yields an empty ledger.
        NEVER raises."""
        with self._lock:
            self._loaded = True
            try:
                path = self._resolved_path()
                if not path.exists():
                    return
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return
                if str(raw.get("schema_version") or "") != SCHEMA_VERSION:
                    logger.info(
                        "[BatchLedger] schema mismatch (found=%s want=%s) — "
                        "starting clean; open claims from the old schema are "
                        "not recoverable",
                        raw.get("schema_version"), SCHEMA_VERSION,
                    )
                    return
                for obj in (raw.get("claims") or []):
                    claim = BatchClaim.from_obj(obj)
                    if claim is not None:
                        self._claims[claim.batch_id] = claim
                logger.debug(
                    "[BatchLedger] loaded %d claim(s), %d open",
                    len(self._claims),
                    sum(1 for c in self._claims.values() if c.is_open),
                )
            except Exception:  # noqa: BLE001
                logger.debug("[BatchLedger] load swallowed", exc_info=True)

    def save(self) -> None:
        """Persist atomically — off the event loop when one is running.

        Serialization happens inline under the lock (cheap, and it pins the
        exact snapshot this save represents). The blocking disk write is then:

          * **coalesced off-loop** when called from a coroutine context —
            handed to the shared ``cooperative_fs_io`` pool (the codebase's
            established sync-FS-on-loop remedy) via a single drainer task that
            always writes the LATEST pending snapshot; or
          * **inline** when no loop is running (boot, shutdown tails, tests) —
            durability beats latency where there is no loop to starve.

        NEVER raises."""
        try:
            with self._lock:
                self._maybe_compact_locked()
                text = json.dumps({
                    "schema_version": SCHEMA_VERSION,
                    "saved_at": time.time(),
                    "claims": [c.to_obj() for c in self._claims.values()],
                }, indent=2)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None:
                self._write_now(text)
                return
            with self._lock:
                self._pending_payload = text
                if self._flush_task is not None and not self._flush_task.done():
                    return  # the in-flight drainer will pick this snapshot up
                self._flush_task = loop.create_task(
                    self._drain_pending(), name="dw-batch-ledger-flush",
                )
        except Exception:  # noqa: BLE001
            logger.debug("[BatchLedger] save swallowed", exc_info=True)

    async def _drain_pending(self) -> None:
        """Write pending snapshots until none remain. One instance in flight.
        NEVER raises."""
        try:
            from backend.core.ouroboros.governance.cooperative_fs_io import (
                offload,
            )
            while True:
                with self._lock:
                    text = self._pending_payload
                    self._pending_payload = None
                if text is None:
                    return
                await offload(self._write_now, text)
        except Exception:  # noqa: BLE001
            logger.debug("[BatchLedger] flush drain swallowed", exc_info=True)

    def _write_now(self, text: str) -> None:
        """The one blocking write. NEVER raises."""
        try:
            path = self._resolved_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, text)
        except Exception:  # noqa: BLE001
            logger.debug("[BatchLedger] write swallowed", exc_info=True)

    def flush_sync(self) -> None:
        """Force any pending snapshot to disk inline. For shutdown tails where
        the loop is about to die and the drainer may never run. NEVER raises."""
        try:
            with self._lock:
                text = self._pending_payload
                self._pending_payload = None
            if text is not None:
                self._write_now(text)
        except Exception:  # noqa: BLE001
            logger.debug("[BatchLedger] flush_sync swallowed", exc_info=True)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _maybe_compact_locked(self) -> None:
        """Drop SETTLED records past retention — at most once per compact
        interval, because the scan is O(N) and a per-save run would make every
        single-claim mutation pay for the whole history. Open claims are never
        dropped: an unsettled obligation does not expire because we stopped
        looking."""
        now = time.time()
        if now - self._last_compact_at < _compact_interval_s():
            return
        self._last_compact_at = now
        cutoff = now - _retention_s()
        stale = [
            bid for bid, c in self._claims.items()
            if c.state in _SETTLED_STATES and c.settled_at and c.settled_at < cutoff
        ]
        for bid in stale:
            self._claims.pop(bid, None)

    # ── Write side ────────────────────────────────────────────────────

    def record_open(
        self,
        batch_id: str,
        *,
        model: str = "",
        op_id: str = "",
        route: str = "",
        expires_at: float = 0.0,
        reasoning_effort: str = "",
    ) -> None:
        """Open a claim. Called immediately after ``POST /batches`` returns an
        id — i.e. at the exact instant the obligation is incurred, before any
        poller exists. That ordering is load-bearing: the pre-Slice-18 orphan
        generator (``candidate_generator.py:4111``) submitted the batch and
        *then* discovered the background-poll cap was full, leaving a batch on
        DW's queue that no code in the process even knew about.
        NEVER raises."""
        try:
            if not batch_id or not batch_ledger_enabled():
                return
            self._ensure_loaded()
            with self._lock:
                self._claims[batch_id] = BatchClaim(
                    batch_id=batch_id, model=model, op_id=op_id, route=route,
                    expires_at=float(expires_at or 0.0),
                    reasoning_effort=reasoning_effort,
                )
            self.save()
            logger.debug(
                "[BatchLedger] claim OPEN batch=%s model=%s op=%s",
                batch_id, model or "?", op_id or "?",
            )
        except Exception:  # noqa: BLE001 — never perturb dispatch
            logger.debug("[BatchLedger] record_open swallowed", exc_info=True)

    def settle(self, batch_id: str, state: str, *, reason: str = "") -> None:
        """Close a claim in *state*. Idempotent; unknown ids are ignored.
        NEVER raises."""
        try:
            if not batch_id or not batch_ledger_enabled():
                return
            self._ensure_loaded()
            with self._lock:
                claim = self._claims.get(batch_id)
                if claim is None:
                    return
                if claim.state in _SETTLED_STATES:
                    return
                claim.state = state
                claim.reason = reason
                claim.settled_at = time.time()
            self.save()
            if state == STATE_ABANDONED:
                # The one outcome that is never OK. Say so loudly: an abandoned
                # claim is a live batch on DW's queue with nobody coming back
                # for it — the exact condition that produced the support email.
                logger.warning(
                    "[BatchLedger] claim ABANDONED batch=%s reason=%s — the "
                    "batch is STILL LIVE on DW and we failed to cancel it; it "
                    "will be reconciled on next boot",
                    batch_id, reason or "?",
                )
            else:
                logger.debug(
                    "[BatchLedger] claim %s batch=%s reason=%s",
                    state.upper(), batch_id, reason or "-",
                )
        except Exception:  # noqa: BLE001
            logger.debug("[BatchLedger] settle swallowed", exc_info=True)

    # ── Read side ─────────────────────────────────────────────────────

    def open_claims(self, *, foreign_only: bool = False) -> Tuple[BatchClaim, ...]:
        """Every claim still carrying an obligation.

        ``foreign_only`` restricts to claims opened by a *different* pid — i.e.
        leftovers from a dead session, which are unambiguously orphans. Claims
        from the current pid may still have a live poller attached, so boot
        reconciliation uses ``foreign_only=True`` and shutdown uses the full
        set. NEVER raises."""
        try:
            if not batch_ledger_enabled():
                return ()
            self._ensure_loaded()
            me = os.getpid()
            with self._lock:
                return tuple(
                    c for c in self._claims.values()
                    if c.is_open and (not foreign_only or c.pid != me)
                )
        except Exception:  # noqa: BLE001
            logger.debug("[BatchLedger] open_claims swallowed", exc_info=True)
            return ()

    def get(self, batch_id: str) -> Optional[BatchClaim]:
        """The claim for *batch_id*, or None. NEVER raises."""
        try:
            self._ensure_loaded()
            with self._lock:
                return self._claims.get(batch_id)
        except Exception:  # noqa: BLE001
            return None

    def snapshot(self) -> Dict[str, Any]:
        """Observability projection (Pillar 7). NEVER raises."""
        try:
            self._ensure_loaded()
            with self._lock:
                claims = list(self._claims.values())
            by_state: Dict[str, int] = {}
            for c in claims:
                by_state[c.state] = by_state.get(c.state, 0) + 1
            return {
                "schema_version": SCHEMA_VERSION,
                "enabled": batch_ledger_enabled(),
                "total": len(claims),
                "open": sum(1 for c in claims if c.is_open),
                "by_state": by_state,
                "state_path": str(self._resolved_path()),
            }
        except Exception:  # noqa: BLE001
            return {"schema_version": SCHEMA_VERSION, "error": "snapshot_failed"}


# ── Module singleton ──────────────────────────────────────────────────

_ledger: Optional[BatchLedger] = None
_ledger_lock = threading.Lock()


def get_batch_ledger() -> BatchLedger:
    """Process-wide ledger singleton. NEVER raises."""
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = BatchLedger()
        return _ledger


def reset_batch_ledger() -> None:
    """Test seam — drop the singleton so the next get() rebuilds from env."""
    global _ledger
    with _ledger_lock:
        _ledger = None


__all__ = [
    "SCHEMA_VERSION",
    "STATE_OPEN",
    "STATE_COMPLETED",
    "STATE_TERMINAL",
    "STATE_CANCELLED",
    "STATE_ABANDONED",
    "BatchClaim",
    "BatchLedger",
    "batch_ledger_enabled",
    "get_batch_ledger",
    "reset_batch_ledger",
]
