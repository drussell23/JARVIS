"""W3(7) Slice 1 — CancelToken primitive + Class D (REPL operator) cancel record.

Slice 1 scope per `project_wave3_item7_mid_op_cancel_scope.md` §9:
    "CancelToken primitive + Class D wiring (JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE)."

This module ships the observability layer: the primitive, the record schema,
the `[CancelOrigin]` log emission, and the optional `cancel_records.jsonl`
durable artifact. Propagation through PhaseDispatcher / ToolLoop (i.e., the
mechanism that *actually* cancels mid-phase) is Slice 2 and is NOT included
here; in Slice 1 the operator-facing surface is structurally complete but
the cancel still settles at the next phase boundary (existing behavior).

Master flag invariant: when ``JARVIS_MID_OP_CANCEL_ENABLED`` is false
(default), nothing in this module is invoked from production code paths.
The byte-for-byte pre-W3(7) contract holds.

Cancel classes (full taxonomy per scope doc §3):
    A — per-call timeout (TimeoutError, own deadline)        [pre-existing]
    B — outer wait_for / ToolLoop round budget               [pre-existing]
    C — sibling-task cancel inside asyncio.gather            [pre-existing]
    D — operator REPL /cancel <op-id> --immediate            [SLICE 1]
    E — watchdog (cost / wall / productivity / idle)         [Slice 3]
    F — system signal (SIGTERM / SIGINT / SIGHUP)            [Slice 4]
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Optional, Set


logger = logging.getLogger("Ouroboros.CancelToken")


# ---------------------------------------------------------------------------
# Flags (defaults align with scope doc §8 — all default OFF when master is off)
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    """Standard JARVIS env-bool parse — true/1/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def mid_op_cancel_enabled() -> bool:
    """Master flag — `JARVIS_MID_OP_CANCEL_ENABLED` (default false).

    When false: no `[CancelOrigin]` emissions, no record artifacts, no
    behavior change vs pre-W3(7). Existing REPL /cancel keeps phase-
    boundary semantics (see GovernedLoopService.request_cancel).
    """
    return _env_bool("JARVIS_MID_OP_CANCEL_ENABLED", False)


def repl_immediate_enabled() -> bool:
    """REPL `/cancel ... --immediate` sub-flag (default true when master on).

    Always returns False when master is off, regardless of this sub-flag.
    """
    if not mid_op_cancel_enabled():
        return False
    return _env_bool("JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE", True)


def record_persist_enabled() -> bool:
    """`cancel_records.jsonl` write sub-flag (default true when master on).

    When false: log-only mode. When true: durable artifact under
    `<session_dir>/cancel_records.jsonl`.
    """
    if not mid_op_cancel_enabled():
        return False
    return _env_bool("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", True)


def bounded_deadline_s() -> float:
    """`JARVIS_CANCEL_BOUNDED_DEADLINE_S` — settle budget (default 30s).

    Slice 1 records this on the CancelRecord for downstream slices to
    consult; the deadline itself is enforced in Slice 2 (propagation).
    """
    raw = os.environ.get("JARVIS_CANCEL_BOUNDED_DEADLINE_S", "30.0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 30.0


# ---------------------------------------------------------------------------
# CancelRecord — schema cancel.1 per scope doc §6.2
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CancelRecord:
    """Immutable record of one cancel decision (schema `cancel.1`).

    Frozen because once committed the record is the system of record for
    attribution. Mutating it would break the deterministic-origin contract.

    Slice 1 fields cover the trigger side. Slice 2 will populate
    ``tasks_cancelled``, ``settle_monotonic``, and ``settle_within_deadline``
    once propagation lands.
    """

    schema_version: str
    cancel_id: str
    op_id: str
    origin: str
    phase_at_trigger: str
    trigger_monotonic: float
    trigger_wall_iso: str
    bounded_deadline_s: float
    reason: str
    tasks_cancelled: list = field(default_factory=list)
    settle_monotonic: Optional[float] = None
    settle_within_deadline: Optional[bool] = None

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":")) + "\n"


def _new_cancel_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CancelToken — async primitive for awaitable cancel + race
# ---------------------------------------------------------------------------


class CancelToken:
    """Per-op cancel signal with idempotent set + awaitable wait.

    Contract (scope doc §4 deterministic guarantees):
        * ``set(record)`` is idempotent — only the first call commits a
          record; subsequent calls return False without overwriting.
        * ``is_cancelled`` is a sync boolean readable any time.
        * ``wait()`` blocks until set; returns the committed CancelRecord.
        * ``race(coro)`` runs ``coro`` concurrent with ``wait()``; whichever
          finishes first wins. The loser is cancelled cooperatively.
        * ``get_record()`` returns the committed CancelRecord or None.

    The token is lazily-bound to an event loop on first ``wait()`` /
    ``race()`` call. Safe to construct in sync code (e.g., test fixtures)
    and consume from async.
    """

    def __init__(self, op_id: str) -> None:
        self._op_id = op_id
        self._record: Optional[CancelRecord] = None
        self._event: Optional[asyncio.Event] = None
        # Track which event loop owns the lazy ``_event`` so we can detect
        # cross-loop misuse early instead of getting silent no-fire bugs.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def is_cancelled(self) -> bool:
        return self._record is not None

    def get_record(self) -> Optional[CancelRecord]:
        return self._record

    def set(self, record: CancelRecord) -> bool:
        """Commit a CancelRecord. Idempotent — only first call wins.

        Returns True on first commit, False if already cancelled.
        Mismatched op_id raises ValueError (defensive — token is per-op).
        """
        if record.op_id != self._op_id:
            raise ValueError(
                f"CancelRecord op_id={record.op_id!r} does not match "
                f"token op_id={self._op_id!r}"
            )
        if self._record is not None:
            # Idempotent — already cancelled. Caller can inspect
            # `get_record()` to see who won the race.
            return False
        self._record = record
        # Wake any waiters lazily; if no event was created, nobody is
        # waiting and we don't need one.
        if self._event is not None:
            self._event.set()
        return True

    def _ensure_event(self) -> asyncio.Event:
        """Lazy event allocation; pinned to the current running loop."""
        loop = asyncio.get_event_loop()
        if self._event is None:
            self._event = asyncio.Event()
            self._loop = loop
            if self._record is not None:
                # Already-cancelled tokens fire immediately.
                self._event.set()
        return self._event

    async def wait(self) -> CancelRecord:
        """Block until set; return the CancelRecord."""
        evt = self._ensure_event()
        await evt.wait()
        # Invariant: when event fires, _record must be populated (set()
        # writes _record before signalling).
        assert self._record is not None
        return self._record

    async def race(self, coro: Awaitable[Any]) -> Any:
        """Race ``coro`` against cancellation.

        If ``coro`` finishes first → returns its result; cancel-wait task
        is cancelled. If cancellation fires first → returns the
        CancelRecord; ``coro`` is cancelled cooperatively.

        Caller is responsible for handling the CancelRecord vs result-of-coro
        disambiguation (e.g. via isinstance check).
        """
        evt = self._ensure_event()
        coro_task = asyncio.ensure_future(coro)
        wait_task = asyncio.ensure_future(evt.wait())
        try:
            done, pending = await asyncio.wait(
                {coro_task, wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            # Drain pending so we don't leave warning-spewing tasks
            for p in pending:
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass
            if coro_task in done:
                return coro_task.result()
            # cancel-wait fired first — return the record
            return self._record
        finally:
            # Final defensive guard against stray pending tasks.
            for t in (coro_task, wait_task):
                if not t.done():
                    t.cancel()


# ---------------------------------------------------------------------------
# Class D — REPL operator immediate cancel trigger
# ---------------------------------------------------------------------------


class CancelOriginEmitter:
    """Trigger-side helper that creates a CancelRecord and emits telemetry.

    Slice 1 covers Class D (REPL operator). Slices 3/4 add E/F via the
    same emit path with different ``origin`` strings.

    The emitter is intentionally NOT a singleton — callers (REPL handler,
    watchdogs) construct their own and pass in the session_dir for the
    durable artifact. This keeps the dependency graph simple and makes
    multi-session test setups trivial.
    """

    def __init__(
        self,
        session_dir: Optional[Path] = None,
    ) -> None:
        self._session_dir = session_dir

    def emit_class_d(
        self,
        *,
        op_id: str,
        token: CancelToken,
        phase_at_trigger: str,
        reason: str = "operator-initiated immediate cancel",
        initiator_task: str = "repl_operator",
    ) -> Optional[CancelRecord]:
        """Class D — REPL operator `/cancel <op-id> --immediate`.

        Returns the committed CancelRecord, or None if:
        * master flag is off (no-op, byte-for-byte pre-W3(7) behavior),
        * REPL-immediate sub-flag is off,
        * the token was already cancelled by another origin (idempotent loss).

        Side effects:
        * ``[CancelOrigin] op=... origin=D:repl_operator ...`` INFO log.
        * ``cancel_records.jsonl`` append (when persist sub-flag is on AND
          session_dir is set).

        No PhaseDispatcher / ToolLoop propagation in Slice 1 — that's
        Slice 2. The op continues until the existing phase-boundary
        cancellation check fires (current behavior).
        """
        if not mid_op_cancel_enabled():
            return None
        if not repl_immediate_enabled():
            return None

        record = CancelRecord(
            schema_version="cancel.1",
            cancel_id=_new_cancel_id(),
            op_id=op_id,
            origin="D:repl_operator",
            phase_at_trigger=phase_at_trigger,
            trigger_monotonic=time.monotonic(),
            trigger_wall_iso=_now_iso(),
            bounded_deadline_s=bounded_deadline_s(),
            reason=reason,
        )

        committed = token.set(record)
        if not committed:
            # Race with another origin (in Slice 1 this is rare — REPL is
            # the only trigger path. In later slices Class E/F can race.)
            existing = token.get_record()
            logger.info(
                "[CancelOrigin] superseded — op=%s requested_origin=D:repl_operator "
                "winner_origin=%s winner_cancel_id=%s",
                op_id[:16],
                existing.origin if existing else "unknown",
                existing.cancel_id if existing else "unknown",
            )
            return None

        logger.info(
            "[CancelOrigin] op=%s origin=%s phase=%s cancel_id=%s "
            "at_monotonic=%.3f reason=%r initiator_task=%s "
            "bounded_deadline_s=%.1f",
            op_id[:16],
            record.origin,
            phase_at_trigger,
            record.cancel_id,
            record.trigger_monotonic,
            reason,
            initiator_task,
            record.bounded_deadline_s,
        )

        if record_persist_enabled() and self._session_dir is not None:
            self._persist(record)

        return record

    def _persist(self, record: CancelRecord) -> None:
        """Append the record to ``cancel_records.jsonl``. Best-effort."""
        try:
            artifact = self._session_dir / "cancel_records.jsonl"  # type: ignore[union-attr]
            artifact.parent.mkdir(parents=True, exist_ok=True)
            with artifact.open("a", encoding="utf-8") as f:
                f.write(record.to_jsonl())
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning(
                "[CancelOrigin] persist failed op=%s cancel_id=%s err=%s",
                record.op_id[:16],
                record.cancel_id,
                f"{type(exc).__name__}: {exc}",
            )


# ---------------------------------------------------------------------------
# Per-session token registry — opt-in, used by Slice 2 for token lookup
# ---------------------------------------------------------------------------


class CancelTokenRegistry:
    """Per-session registry mapping op_id → CancelToken.

    Slice 1 ships this as an empty primitive consumed only by the test
    suite + the REPL trigger. Slice 2 will wire it into PhaseDispatcher /
    ToolLoop so runner code can look up the token for the in-flight op.

    The registry is intentionally NOT a singleton; ownership lives with
    GovernedLoopService (Slice 2 will attach one). Tests construct their
    own per-test instance.
    """

    def __init__(self) -> None:
        self._tokens: dict = {}
        # Track op_ids ever registered (incl. settled) so attribution
        # logs can resolve historical lookups during postmortem.
        self._known_ops: Set[str] = set()

    def get_or_create(self, op_id: str) -> CancelToken:
        if op_id not in self._tokens:
            self._tokens[op_id] = CancelToken(op_id)
            self._known_ops.add(op_id)
        return self._tokens[op_id]

    def get(self, op_id: str) -> Optional[CancelToken]:
        return self._tokens.get(op_id)

    def find_by_prefix(self, prefix: str) -> Optional[CancelToken]:
        """Match by op-id prefix (REPL UX — operators type abbreviations).

        Returns the unique match, or None if 0 or >1 active tokens match.
        Ambiguous matches are a no-op (caller should report to operator).
        """
        matches = [
            tok for op_id, tok in self._tokens.items()
            if op_id.startswith(prefix)
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def discard(self, op_id: str) -> None:
        """Remove a token from the active set (post-terminal cleanup).

        op_id remains in ``_known_ops`` for postmortem attribution.
        """
        self._tokens.pop(op_id, None)

    def active_op_ids(self) -> Set[str]:
        return set(self._tokens.keys())


__all__ = [
    "CancelRecord",
    "CancelToken",
    "CancelTokenRegistry",
    "CancelOriginEmitter",
    "mid_op_cancel_enabled",
    "repl_immediate_enabled",
    "record_persist_enabled",
    "bounded_deadline_s",
]
