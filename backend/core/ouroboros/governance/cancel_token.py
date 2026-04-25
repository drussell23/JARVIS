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
import contextvars
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
# ContextVar for ambient token propagation (W3(7) Slice 2)
# ---------------------------------------------------------------------------
#
# Slice 2 needs the per-op token reachable from the candidate_generator and
# tool_executor without threading a parameter through ~9 PhaseRunners,
# 6+ candidate_generator entry points, and 4 ToolExecutor methods. A
# ContextVar carries the token *through* asyncio's task copying machinery:
# `asyncio.create_task(coro)` copies the parent's contextvars to the child,
# so the token survives nested gather() / wait_for() boundaries naturally.
#
# Master-flag-off contract: when JARVIS_MID_OP_CANCEL_ENABLED=false, the
# token (if any) is never `set()` from a Class D/E/F trigger, so the
# helper functions below short-circuit on `is_cancelled is False` and
# fall through to plain `asyncio.wait_for(...)`. Byte-for-byte pre-W3(7).
#
# The Var is sentinel-aware: `Sentinel.UNSET` (the default) means "no
# token in scope" — different from `None` which a caller could legitimately
# set (defensive — if a caller wants to clear the token mid-operation,
# `cancel_token_var.set(None)` works and helpers see no-token).
cancel_token_var: contextvars.ContextVar[Optional["CancelToken"]] = (
    contextvars.ContextVar("ouroboros.cancel_token", default=None)
)


def current_cancel_token() -> Optional["CancelToken"]:
    """Read the ambient :class:`CancelToken` for this asyncio task chain.

    Returns ``None`` when no token has been bound (default — Slice 1
    callers, unit tests, pre-W3(7) call paths).
    """
    return cancel_token_var.get()


# ---------------------------------------------------------------------------
# Flags (defaults align with scope doc §8 + Slice 7 graduation policy line):
#   - Master `JARVIS_MID_OP_CANCEL_ENABLED` default `True` post-Slice-7
#     (was `False` during Slices 1–6 build).
#   - All actuating sub-flags (WATCHDOG / SIGNAL / SSE) stay default `False` —
#     operator opt-in required for Class E / F / SSE.
#   - REPL_IMMEDIATE / RECORD_PERSIST default `True` when master is on
#     (Slice 1 design — Class D is the operator-cancel surface, no
#     auto-cancellation, only fires on explicit `cancel <op-id> --immediate`).
#   - Hot-revert: `JARVIS_MID_OP_CANCEL_ENABLED=false` force-disables every
#     sub-flag → byte-for-byte pre-W3(7).
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    """Standard JARVIS env-bool parse — true/1/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def mid_op_cancel_enabled() -> bool:
    """Master flag — `JARVIS_MID_OP_CANCEL_ENABLED` (default **true** post-Slice-7).

    Pre-graduation (Slices 1–6) default was ``False``. Slice 7 flipped the
    default to ``True`` after the full propagation surface (D/E/F + SSE +
    IDE GET) landed and was unit-test green. The flip is safe because the
    *actuating* sub-flags stay default off:

    * ``JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED`` — Class E (cost / wall /
      productivity / idle) — default ``False``.
    * ``JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED`` — Class F signals — default ``False``.
    * ``JARVIS_CANCEL_SSE_ENABLED`` — additive SSE event — default ``False``.

    Class D REPL ``cancel <op-id> --immediate`` defaults active when master
    is on (sub-flag default ``True``) but only fires on explicit operator
    action — no auto-cancellation surface.

    The ContextVar token plumbing runs on every dispatched op once master
    is on, but is observably-no-op when no Class D/E/F trigger ever calls
    ``token.set(...)`` — ``race_or_wait_for`` falls through to plain
    ``asyncio.wait_for``. This is the standard graduation pattern from
    Wave 1 / Wave 2 / W3(6) Slice 5b.

    Hot-revert: ``JARVIS_MID_OP_CANCEL_ENABLED=false`` restores byte-for-byte
    pre-W3(7) behavior. No code revert needed. Pinned by graduation tests.
    """
    return _env_bool("JARVIS_MID_OP_CANCEL_ENABLED", True)


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


def watchdog_enabled() -> bool:
    """Class E watchdog sub-flag — `JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED`.

    Default false (even when master is on) per operator resolution-2 — the
    first rollout is operator-only (Class D); watchdog-initiated cancels
    are a separate trust step. Always returns False when master is off.

    Slice 3 (W3(7)) gates Class E (cost / wall / productivity / idle
    watchdog cancels) on this flag.
    """
    if not mid_op_cancel_enabled():
        return False
    return _env_bool("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", False)


def sse_enabled() -> bool:
    """SSE event sub-flag — `JARVIS_CANCEL_SSE_ENABLED` (default false).

    Slice 6 (W3(7)) — gates the additive `cancel_origin_emitted` SSE
    event publish to `IDEStreamRouter`. Independent of master flag —
    operators may want SSE observability without enabling the underlying
    cancel mechanism (or vice versa). Returns False whenever the parent
    IDE-stream master flag is off, regardless of this sub-flag.

    The cancel-records.jsonl artifact (Slice 1) and `[CancelOrigin]`
    log lines are NOT gated by this flag — those land regardless of
    the SSE consumer being enabled.
    """
    return _env_bool("JARVIS_CANCEL_SSE_ENABLED", False)


def signal_enabled() -> bool:
    """Class F signal sub-flag — `JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED`.

    Default false (even when master is on) per operator resolution-2 — Class F
    (system signal cancels: SIGTERM / SIGINT / SIGHUP) is a separate trust
    step beyond Class D operator and Class E watchdog. Always returns False
    when master is off.

    Slice 4 (W3(7)) gates the signal-handler-emitted Class F records on this
    flag. The existing harness signal-handler partial-summary write path is
    unchanged regardless of this flag — Class F is *additive* observability
    on top of the existing handler (operator resolution-4: no harness
    dependency for correctness).
    """
    if not mid_op_cancel_enabled():
        return False
    return _env_bool("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", False)


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

        # W3(7) Slice 6 — best-effort SSE publish (gated; never raises).
        bridge_cancel_origin_to_sse(record)

        return record

    # Class E watchdogs (W3(7) Slice 3) — cost / wall / productivity / idle.
    # Each watchdog identifies itself via the ``watchdog`` keyword; the
    # origin string follows the canonical ``E:<watchdog>`` shape.
    _ALLOWED_WATCHDOGS: frozenset = frozenset({
        "cost",          # cost_governor cap exceeded
        "wall",          # WallClockWatchdog wall-clock cap
        "productivity",  # productivity / forward-progress trip
        "idle",          # idle-timeout (lowest precedence per operator resolution-3)
    })

    def emit_class_e(
        self,
        *,
        watchdog: str,
        op_id: str,
        token: CancelToken,
        phase_at_trigger: str,
        reason: str = "",
        initiator_task: str = "",
    ) -> Optional[CancelRecord]:
        """Class E — watchdog-initiated immediate cancel.

        Slice 3 (W3(7)). Same mechanics as :meth:`emit_class_d` but:

        * Gated by :func:`watchdog_enabled` (sub-flag default false even
          when master is on — operator resolution-2).
        * ``watchdog`` MUST be in :attr:`_ALLOWED_WATCHDOGS` — surfaces
          typos loudly rather than silently emitting an unparseable origin.
        * Origin string is ``E:<watchdog>`` for machine-grep stability.

        Precedence (operator resolution-3) — operator/safety > idle/productivity:
        the actual *trigger* logic in each watchdog module owns the timing
        decision; if multiple watchdogs fire near-simultaneously, asyncio
        scheduling determines which wins ``token.set()``. The supersede
        log includes both origins so postmortem can audit. Watchdog
        modules SHOULD self-throttle (e.g., idle-timeout backs off when a
        Class D / safety-class E is in flight) per the resolution.

        Returns the committed CancelRecord, or None if:
        * master flag is off (no-op),
        * watchdog sub-flag is off (no-op),
        * the watchdog name is unknown (raises ValueError — typo guard),
        * the token was already cancelled by another origin (idempotent loss).
        """
        if watchdog not in self._ALLOWED_WATCHDOGS:
            raise ValueError(
                f"unknown watchdog={watchdog!r}; "
                f"allowed={sorted(self._ALLOWED_WATCHDOGS)}"
            )
        if not mid_op_cancel_enabled():
            return None
        if not watchdog_enabled():
            return None

        origin = f"E:{watchdog}"
        record = CancelRecord(
            schema_version="cancel.1",
            cancel_id=_new_cancel_id(),
            op_id=op_id,
            origin=origin,
            phase_at_trigger=phase_at_trigger,
            trigger_monotonic=time.monotonic(),
            trigger_wall_iso=_now_iso(),
            bounded_deadline_s=bounded_deadline_s(),
            reason=reason or f"watchdog-initiated cancel ({watchdog})",
        )

        committed = token.set(record)
        if not committed:
            existing = token.get_record()
            logger.info(
                "[CancelOrigin] superseded — op=%s requested_origin=%s "
                "winner_origin=%s winner_cancel_id=%s",
                op_id[:16],
                origin,
                existing.origin if existing else "unknown",
                existing.cancel_id if existing else "unknown",
            )
            return None

        logger.info(
            "[CancelOrigin] op=%s origin=%s phase=%s cancel_id=%s "
            "at_monotonic=%.3f reason=%r initiator_task=%s "
            "bounded_deadline_s=%.1f",
            op_id[:16],
            origin,
            phase_at_trigger,
            record.cancel_id,
            record.trigger_monotonic,
            record.reason,
            initiator_task or watchdog,
            record.bounded_deadline_s,
        )

        if record_persist_enabled() and self._session_dir is not None:
            self._persist(record)

        # W3(7) Slice 6 — best-effort SSE publish (gated; never raises).
        bridge_cancel_origin_to_sse(record)

        return record

    # Class F signals (W3(7) Slice 4) — SIGTERM / SIGINT / SIGHUP.
    # Names mirror the lowercase form already used by harness ticket B
    # (`signal_name` arg of `_handle_shutdown_signal`).
    _ALLOWED_SIGNALS: frozenset = frozenset({
        "sigterm",  # container kill / external orchestrator stop
        "sigint",   # operator Ctrl-C / interactive interrupt
        "sighup",   # parent-process death (per S5/S6 incidents — terminal pipeline)
    })

    def emit_class_f(
        self,
        *,
        signal_name: str,
        op_id: str,
        token: CancelToken,
        phase_at_trigger: str,
        reason: str = "",
        initiator_task: str = "",
    ) -> Optional[CancelRecord]:
        """Class F — system-signal-initiated immediate cancel.

        Slice 4 (W3(7)). Same mechanics as :meth:`emit_class_d` /
        :meth:`emit_class_e` but:

        * Gated by :func:`signal_enabled` (sub-flag default false even
          when master is on — operator resolution-2).
        * ``signal_name`` MUST be in :attr:`_ALLOWED_SIGNALS` — surfaces
          typos loudly. Use the same lowercase form as harness ticket B
          (``"sigterm"``, ``"sigint"``, ``"sighup"``).
        * Origin string is ``F:<signal_name>`` for machine-grep stability.

        Coordination with harness ticket B (operator resolution-4 — Class F
        works correctly even if harness epic items still have bugs):
        the existing harness ``_handle_shutdown_signal`` path that writes
        the partial ``summary.json`` is unchanged. Class F is *additive*
        observability on top — emitted ONCE per in-flight op when the
        signal handler chooses to. The `cancel_records.jsonl` artifact
        and `[CancelOrigin]` log line live alongside the existing
        partial-summary write; they do not replace or block it.

        Returns the committed CancelRecord, or None if:
        * master flag is off (no-op),
        * signal sub-flag is off (no-op),
        * the signal name is unknown (raises ValueError — typo guard),
        * the token was already cancelled by another origin (idempotent loss).
        """
        if signal_name not in self._ALLOWED_SIGNALS:
            raise ValueError(
                f"unknown signal_name={signal_name!r}; "
                f"allowed={sorted(self._ALLOWED_SIGNALS)}"
            )
        if not mid_op_cancel_enabled():
            return None
        if not signal_enabled():
            return None

        origin = f"F:{signal_name}"
        record = CancelRecord(
            schema_version="cancel.1",
            cancel_id=_new_cancel_id(),
            op_id=op_id,
            origin=origin,
            phase_at_trigger=phase_at_trigger,
            trigger_monotonic=time.monotonic(),
            trigger_wall_iso=_now_iso(),
            bounded_deadline_s=bounded_deadline_s(),
            reason=reason or f"system signal received ({signal_name})",
        )

        committed = token.set(record)
        if not committed:
            existing = token.get_record()
            logger.info(
                "[CancelOrigin] superseded — op=%s requested_origin=%s "
                "winner_origin=%s winner_cancel_id=%s",
                op_id[:16],
                origin,
                existing.origin if existing else "unknown",
                existing.cancel_id if existing else "unknown",
            )
            return None

        logger.info(
            "[CancelOrigin] op=%s origin=%s phase=%s cancel_id=%s "
            "at_monotonic=%.3f reason=%r initiator_task=%s "
            "bounded_deadline_s=%.1f",
            op_id[:16],
            origin,
            phase_at_trigger,
            record.cancel_id,
            record.trigger_monotonic,
            record.reason,
            initiator_task or signal_name,
            record.bounded_deadline_s,
        )

        if record_persist_enabled() and self._session_dir is not None:
            self._persist(record)

        # W3(7) Slice 6 — best-effort SSE publish (gated; never raises).
        bridge_cancel_origin_to_sse(record)

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


class OperationCancelledError(Exception):
    """Raised by Slice 2 helpers when the in-scope CancelToken fires.

    Carries the :class:`CancelRecord` so callers can route to POSTMORTEM
    with full attribution. NOT an :class:`asyncio.CancelledError` — keeping
    these distinct avoids tangling with asyncio's own cancellation
    machinery (which has subtle Python-version semantics; see PEP 479
    and Python 3.11's CancelledError-is-BaseException promotion).
    """

    def __init__(self, record: "CancelRecord") -> None:
        self.record = record
        super().__init__(
            f"op cancelled: op={record.op_id[:16]} origin={record.origin} "
            f"cancel_id={record.cancel_id}"
        )


async def race_or_wait_for(
    coro: Awaitable[Any],
    *,
    timeout: float,
    cancel_token: Optional["CancelToken"] = None,
) -> Any:
    """Race ``coro`` against (a) its timeout and (b) the cancel token.

    Drop-in replacement for ``asyncio.wait_for(coro, timeout=N)`` that
    additionally surfaces a Class D/E/F cancel as
    :class:`OperationCancelledError`.

    Resolution order:
        * If ``cancel_token`` is None or already cancelled: re-raise
          accordingly (already-cancelled → ``OperationCancelledError``
          immediately; None → behaves identically to ``asyncio.wait_for``).
        * If ``coro`` finishes first: return its result.
        * If ``timeout`` elapses first: raise ``asyncio.TimeoutError``.
        * If the cancel fires first: raise ``OperationCancelledError``.
    """
    if cancel_token is None:
        return await asyncio.wait_for(coro, timeout=timeout)

    if cancel_token.is_cancelled:
        # Pre-cancelled: don't even start the coro. Caller's `coro` was
        # already constructed and would otherwise leak as an unawaited
        # warning, so we close it explicitly.
        if hasattr(coro, "close"):
            try:
                coro.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        record = cancel_token.get_record()
        assert record is not None  # is_cancelled invariant
        raise OperationCancelledError(record)

    coro_task = asyncio.ensure_future(coro)
    cancel_task = asyncio.ensure_future(cancel_token.wait())
    try:
        done, pending = await asyncio.wait(
            {coro_task, cancel_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            # Timeout fired
            for p in pending:
                p.cancel()
            for p in pending:
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass
            raise asyncio.TimeoutError()

        if cancel_task in done and coro_task not in done:
            # Cancel won
            for p in pending:
                p.cancel()
            for p in pending:
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass
            record = cancel_task.result()
            raise OperationCancelledError(record)

        # Coro won (possibly with cancel arriving simultaneously — coro
        # result wins per the operator's "first-emitted wins" semantics).
        for p in pending:
            p.cancel()
        for p in pending:
            try:
                await p
            except (asyncio.CancelledError, Exception):
                pass
        return coro_task.result()
    finally:
        for t in (coro_task, cancel_task):
            if not t.done():
                t.cancel()


def emit_watchdog_cancel(
    *,
    watchdog: str,
    op_id: str,
    registry: "CancelTokenRegistry",
    session_dir: Optional[Path] = None,
    phase_at_trigger: str = "unknown",
    reason: str = "",
    initiator_task: str = "",
) -> Optional[CancelRecord]:
    """Convenience helper for watchdog modules — look up token + emit Class E.

    Slice 3 (W3(7)). Watchdogs (cost_governor, WallClockWatchdog,
    productivity-trip detector, idle-timeout) call this from their
    termination path instead of importing/instantiating
    :class:`CancelOriginEmitter` themselves.

    Returns None when:
    * master flag off (no-op, byte-for-byte pre-W3(7)),
    * Class E sub-flag off (no-op — operator hasn't enabled watchdog cancels),
    * the token was already cancelled by another origin (race lost,
      logged as supersede).

    The watchdog module's existing termination logic (raising
    OpCostCapExceeded, writing stop_reason=wall_clock_cap, etc.) is NOT
    replaced by this hook — it's *added alongside* so the cancel record
    + dispatcher's POSTMORTEM routing engages cleanly.
    """
    if not watchdog_enabled():
        return None
    token = registry.get_or_create(op_id)
    emitter = CancelOriginEmitter(session_dir=session_dir)
    return emitter.emit_class_e(
        watchdog=watchdog,
        op_id=op_id,
        token=token,
        phase_at_trigger=phase_at_trigger,
        reason=reason,
        initiator_task=initiator_task,
    )


def bridge_cancel_origin_to_sse(record: "CancelRecord") -> None:
    """Publish a ``cancel_origin_emitted`` SSE event for ``record``.

    Slice 6 (W3(7)). Best-effort, never raises. Gated by
    :func:`sse_enabled` AND the IDE stream's own master flag (looked up
    via :func:`ide_observability_stream.stream_enabled`). The SSE payload
    is the summary form per scope doc §6.3 — full record lives at the
    `/observability/cancels/<cancel_id>` GET endpoint.

    Called from :class:`CancelOriginEmitter` after a successful commit,
    or directly from external code that wants to surface a cancel record
    on SSE without going through an emitter (e.g. test harnesses).
    """
    if not sse_enabled():
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_CANCEL_ORIGIN_EMITTED as _EV_TYPE,
            get_default_broker as _get_default_broker,
            stream_enabled as _stream_enabled,
        )
        if not _stream_enabled():
            return
        _broker = _get_default_broker()
        if _broker is None:
            return
        _broker.publish(
            event_type=_EV_TYPE,
            op_id=record.op_id,
            payload={
                "cancel_id": record.cancel_id,
                "origin": record.origin,
                "phase": record.phase_at_trigger,
            },
        )
    except Exception:  # noqa: BLE001 — SSE publish is best-effort
        pass


def emit_signal_cancel(
    *,
    signal_name: str,
    registry: "CancelTokenRegistry",
    session_dir: Optional[Path] = None,
    phase_at_trigger: str = "unknown",
    reason: str = "",
) -> int:
    """Convenience — emit Class F:<signal> for **every active op** in the registry.

    Slice 4 (W3(7)). The harness signal handler calls this from within
    ``_handle_shutdown_signal`` so that one signal arrival fans out into
    one cancel record per in-flight op (typical case: 1-3 active ops at
    signal time).

    Returns the number of records emitted (0 when master/sub-flag off, or
    when no active ops exist). Never raises — the harness signal handler
    must remain interrupt-safe.

    The existing harness partial-summary write path is NOT touched by this
    helper (per operator resolution-4 — Class F is additive observability,
    not a replacement). Callers invoke both: the partial-summary write
    AND ``emit_signal_cancel`` from the same handler.
    """
    if not signal_enabled():
        return 0
    try:
        active = registry.active_op_ids()
    except Exception:  # noqa: BLE001 — registry must not crash signal handler
        return 0

    emitter = CancelOriginEmitter(session_dir=session_dir)
    emitted = 0
    for op_id in list(active):
        try:
            token = registry.get(op_id)
            if token is None:
                continue
            rec = emitter.emit_class_f(
                signal_name=signal_name,
                op_id=op_id,
                token=token,
                phase_at_trigger=phase_at_trigger,
                reason=reason,
                initiator_task=f"harness:{signal_name}",
            )
            if rec is not None:
                emitted += 1
        except Exception:  # noqa: BLE001 — per-op emit must not block others
            continue
    return emitted


def subprocess_grace_s() -> float:
    """`JARVIS_CANCEL_SUBPROCESS_GRACE_S` — terminate→kill grace (default 5s).

    Used by Slice 2 ToolExecutor subprocess wrappers between
    ``proc.terminate()`` and ``proc.kill()`` when a cancel fires mid-call.
    """
    raw = os.environ.get("JARVIS_CANCEL_SUBPROCESS_GRACE_S", "5.0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 5.0


__all__ = [
    "CancelRecord",
    "CancelToken",
    "CancelTokenRegistry",
    "CancelOriginEmitter",
    "OperationCancelledError",
    "cancel_token_var",
    "current_cancel_token",
    "race_or_wait_for",
    "subprocess_grace_s",
    "mid_op_cancel_enabled",
    "repl_immediate_enabled",
    "record_persist_enabled",
    "bounded_deadline_s",
    "watchdog_enabled",
    "emit_watchdog_cancel",
    "signal_enabled",
    "emit_signal_cancel",
    "sse_enabled",
    "bridge_cancel_origin_to_sse",
]
