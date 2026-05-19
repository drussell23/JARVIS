"""SWE-Bench-Pro evaluator façade — Phase 2 Phase B.2.2
(PRD §40.7.9 / §40.7.10-b22).

Async façade composing the entire B.2 arc end-to-end for a single
problem evaluation:

    prepare_problem(problem)                          # B.1
        ↓ PreparedProblem
    build_evaluation_envelope(problem, prepared)      # B.2.1
        ↓ IntentEnvelope (causal_id = future op_id)
    broker.subscribe(op_id_filter=envelope.causal_id) # B.2.0.5 SSE
        ↓ subscriber
    intake_service.ingest_envelope(envelope)          # canonical intake
        ↓ orchestrator picks up async
    asyncio.wait_for(terminal_event, timeout_s)       # bounded primary path
        ↓ on timeout
    operation_ledger.get_latest_state(op_id)          # one-shot fallback
        ↓ terminal state resolved
    capture_produced_patch(prepared)                  # B.1 — patch diff
        ↓ on cleanup=True (default)
    cleanup_prepared(prepared)                        # B.1 — finally block

Composition discipline (mandate compliance)
-------------------------------------------

  * **Composes canonical surfaces only**:
      - ``swe_bench_pro_enabled`` / ``ProblemSpec`` (Phase A)
      - ``prepare_problem`` / ``PreparedProblem`` /
        ``capture_produced_patch`` / ``cleanup_prepared`` (B.1)
      - ``EVIDENCE_REPO_ROOT_KEY`` (B.2.0; via builder transitively)
      - ``EVENT_TYPE_OPERATION_TERMINAL`` /
        ``get_default_broker`` / ``StreamEventBroker.subscribe`` /
        ``StreamEventBroker.unsubscribe`` /
        ``StreamEventBroker.stream_iter`` (B.2.0.5)
      - ``build_evaluation_envelope`` / ``ENVELOPE_SOURCE`` (B.2.1)
      - ``IntakeLayerService.ingest_envelope`` (canonical intake)
      - ``OperationLedger.get_latest_state`` (canonical ledger)

  * **No parallel state**: NEVER constructs a
    ``Dict[op_id, asyncio.Event]`` or any other process-local
    op-tracking registry. The canonical broker IS the truth table
    for op-lifecycle terminals.

  * **Subscribe BEFORE ingest** (race-free primary path): the
    envelope is built first (allocating ``causal_id``). The broker
    subscription is registered with that filter BEFORE
    ``ingest_envelope`` is called, so even an instant terminal
    transition reaches the subscriber's queue. AST pin in the spine
    asserts this source-order invariant via ``ast.unparse``.

  * **Bounded primary wait** (operator binding "never unbounded wait"):
    the terminal-event subscription is awaited via
    ``asyncio.wait_for(..., timeout=...)`` with the env-overridable
    ``JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S`` (default 1800s).
    AST pin in the spine forbids any naked ``asyncio.wait()`` /
    ``await queue.get()`` without a wrapping timeout.

  * **One-shot ledger fallback** (operator binding "one-shot, never
    polling-loop"): on ``asyncio.TimeoutError``, the façade queries
    ``OperationLedger.get_latest_state(op_id)`` EXACTLY ONCE to
    disambiguate "still running" vs "we missed the terminal event"
    (network drop, broker backpressure, etc.). If the ledger shows
    a terminal state, that state wins — the ledger is authoritative
    over the SSE channel. AST pin in the spine forbids any
    ``while True`` polling loop in the façade body.

  * **Cooperative cancel** (asyncio.CancelledError propagates): the
    ``finally`` block at the end of ``evaluate_problem`` unsubscribes
    from the broker AND cleans up the worktree before re-raising.

  * **Master-flag gate is FIRST**: ``swe_bench_pro_enabled()`` is
    the first executable statement; when OFF, the façade returns
    ``EvaluationOutcome.MASTER_FLAG_OFF`` cleanly without any side
    effects (no prepare, no ingest, no broker subscription).

§7 fail-closed contract
-----------------------

Every code path in ``evaluate_problem`` produces an
``EvaluationResult`` rather than raising — except
``asyncio.CancelledError`` which propagates per the orchestrator
POSTMORTEM convention.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_OPERATION_TERMINAL,
    StreamEventBroker,
    get_default_broker,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
    swe_bench_pro_enabled,
)
from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
    build_evaluation_envelope,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    DiffCaptureOutcome,
    HarnessOutcome,
    capture_produced_patch,
    cleanup_prepared,
    prepare_problem,
)


logger = logging.getLogger("Ouroboros.SWEBenchPro.Evaluator")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


EVALUATION_RESULT_SCHEMA_VERSION: str = "swe_bench_pro_evaluation.v1"


EVAL_TIMEOUT_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S"


# Default terminal-wait ceiling: 30 minutes. Real SWE-Bench-Pro
# problems vary widely in runtime (5 min to 30+ min for hard cases);
# 1800s is conservative but never unbounded. Operators tuning for
# fast eval cycles can flip this via env.
_DEFAULT_TIMEOUT_S: float = 1800.0


# Task #21 — Dynamic Timeout Coherence. The harness publishes its
# absolute monotonic WallClockWatchdog deadline here at arm time.
# Reading it (NOT importing battle_test) lets the inner eval timeout
# structurally end + emit TERMINAL_TIMEOUT BEFORE the outer bounded-
# shutdown — so a verdict ALWAYS lands (A″ proved the 1800==1800
# inversion otherwise yields zero verdicts). Absent ⇒ no clamp
# (byte-identical legacy; non-battle-test callers unaffected).
WALL_DEADLINE_ENV_VAR: str = "OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC"
_AUTOSCORE_GRACE_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_AUTOSCORE_SHUTDOWN_GRACE_S"
)
_DRAIN_BUFFER_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_EVAL_DRAIN_BUFFER_S"
# Task #22 — drain must cover the REAL post-eval teardown chain, not
# a 2× heuristic. Env-string PARITY with
# ``shutdown_watchdog.default_deadline_s`` (deliberately NOT imported
# — preserves the "evaluator never imports battle_test" AST pin; the
# parity is documented + pinned by a regression test instead).
_SHUTDOWN_DEADLINE_ENV_VAR: str = "JARVIS_BATTLE_SHUTDOWN_DEADLINE_S"
_DRAIN_MARGIN_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_EVAL_DRAIN_MARGIN_S"
_DEFAULT_SHUTDOWN_DEADLINE_S: float = 30.0   # parity w/ watchdog default
_DEFAULT_AUTOSCORE_GRACE_S: float = 30.0
_DEFAULT_DRAIN_MARGIN_S: float = 15.0
# Never return <=0: a near-expired session still does the smallest
# bounded wait so wait_for raises TERMINAL_TIMEOUT (a verdict) fast,
# rather than a 0/negative timeout that would crash asyncio.wait_for.
_MIN_EVAL_FLOOR_S: float = 10.0


def _env_pos_float(name: str, default: float) -> float:
    """Read a positive float env; fall back to ``default`` on
    unset / invalid / non-positive. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


def _eval_drain_buffer_s() -> float:
    """Wall time the session needs AFTER the inner eval wait so the
    verdict ALWAYS flushes before the process exits.

    Task #22 root fix — composes the REAL post-eval teardown chain
    (deep-run bt-2026-05-19-011003 proved the prior 2×autoscore-grace
    =60s was undersized: bounded-shutdown arms its 30s deadline IN
    PARALLEL with the autoscore drain, so under a heavy 950k-node
    session ``os._exit(75)`` fired before ``harness_inject`` logged
    the verdict)::

        drain = shutdown_deadline + autoscore_grace + margin

    - ``shutdown_deadline`` = ``JARVIS_BATTLE_SHUTDOWN_DEADLINE_S``
      (env-string parity with ``shutdown_watchdog.default_deadline_s``
      — documented + test-pinned, NOT imported).
    - ``autoscore_grace`` = ``JARVIS_SWE_BENCH_PRO_AUTOSCORE_SHUTDOWN_
      GRACE_S`` (the existing drain knob — single source of truth).
    - ``margin`` = ``JARVIS_SWE_BENCH_PRO_EVAL_DRAIN_MARGIN_S`` (slack
      for GC / asyncio teardown / log flush).

    Explicit ``JARVIS_SWE_BENCH_PRO_EVAL_DRAIN_BUFFER_S`` still wins
    (operator override). No hardcoded magic — every term is an
    env-composed, individually-tunable structural component. NEVER
    raises.
    """
    explicit = os.environ.get(_DRAIN_BUFFER_ENV_VAR, "").strip()
    if explicit:
        try:
            v = float(explicit)
            if v > 0:
                return v
        except (ValueError, TypeError):
            pass
    shutdown_deadline = _env_pos_float(
        _SHUTDOWN_DEADLINE_ENV_VAR, _DEFAULT_SHUTDOWN_DEADLINE_S,
    )
    autoscore_grace = _env_pos_float(
        _AUTOSCORE_GRACE_ENV_VAR, _DEFAULT_AUTOSCORE_GRACE_S,
    )
    margin = _env_pos_float(
        _DRAIN_MARGIN_ENV_VAR, _DEFAULT_DRAIN_MARGIN_S,
    )
    return shutdown_deadline + autoscore_grace + margin


def _apply_wall_coherence(configured: float) -> float:
    """Clamp ``configured`` below the published session wall deadline.

    ``eval = min(configured, wall_remaining - drain_buffer)``. No
    deadline env ⇒ return ``configured`` unchanged (byte-identical
    legacy). Coherent budget <=0 ⇒ ``_MIN_EVAL_FLOOR_S`` (still a
    fast bounded wait → TERMINAL_TIMEOUT, never 0/negative). NEVER
    raises; NEVER imports battle_test (env-var seam only).
    """
    raw = os.environ.get(WALL_DEADLINE_ENV_VAR, "").strip()
    if not raw:
        return configured
    try:
        deadline = float(raw)
    except (ValueError, TypeError):
        return configured
    remaining = deadline - time.monotonic()
    drain = _eval_drain_buffer_s()
    coherent = remaining - drain
    if coherent <= 0:
        logger.info(
            "[SWEBenchPro] wall budget exhausted (remaining=%.1fs "
            "drain=%.1fs) — eval floored to %.1fs so wait_for still "
            "emits TERMINAL_TIMEOUT (Task #21)",
            remaining, drain, _MIN_EVAL_FLOOR_S,
        )
        return _MIN_EVAL_FLOOR_S
    clamped = min(configured, coherent)
    if clamped < configured:
        logger.info(
            "[SWEBenchPro] eval timeout clamped %.1fs -> %.1fs "
            "(wall_remaining=%.1fs drain_buffer=%.1fs) — Dynamic "
            "Timeout Coherence (Task #21)",
            configured, clamped, remaining, drain,
        )
    return clamped


# Terminal OperationState values that translate to "the model
# produced a working fix" (RESOLVED) vs "the model failed" (UNRESOLVED).
# Mirrors B.2.0.5's TERMINAL_OPERATION_STATES split. Closed taxonomy
# — drift detected by the B.2.3 spine.
_RESOLVED_STATES: frozenset = frozenset({"applied"})
_UNRESOLVED_STATES: frozenset = frozenset({
    "failed", "blocked", "rolled_back",
})


# ===========================================================================
# Closed taxonomy — EvaluationOutcome (7 values; AST-pinned)
# ===========================================================================


class EvaluationOutcome(str, enum.Enum):
    """Seven canonical outcomes for :func:`evaluate_problem`."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    PREPARE_FAILED = "prepare_failed"
    INGEST_FAILED = "ingest_failed"
    TERMINAL_TIMEOUT = "terminal_timeout"
    CANCELLED = "cancelled"
    MASTER_FLAG_OFF = "master_flag_off"


# ===========================================================================
# Frozen EvaluationResult dataclass (§33.5 symmetric to_dict/from_dict)
# ===========================================================================


@dataclass(frozen=True)
class EvaluationResult:
    """Result of a single :func:`evaluate_problem` call."""

    outcome: EvaluationOutcome
    problem_instance_id: str
    op_id: str = ""
    terminal_phase: str = ""
    terminal_state: str = ""
    terminal_reason_code: str = ""
    captured_patch: Optional[str] = None
    diff_outcome: Optional[str] = None
    elapsed_s: float = 0.0
    schema_version: str = EVALUATION_RESULT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "problem_instance_id": self.problem_instance_id,
            "op_id": self.op_id,
            "terminal_phase": self.terminal_phase,
            "terminal_state": self.terminal_state,
            "terminal_reason_code": self.terminal_reason_code,
            "captured_patch": self.captured_patch,
            "diff_outcome": self.diff_outcome,
            "elapsed_s": self.elapsed_s,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationResult":
        return cls(
            schema_version=str(payload.get(
                "schema_version", EVALUATION_RESULT_SCHEMA_VERSION,
            )),
            outcome=EvaluationOutcome(str(payload["outcome"])),
            problem_instance_id=str(payload["problem_instance_id"]),
            op_id=str(payload.get("op_id", "")),
            terminal_phase=str(payload.get("terminal_phase", "")),
            terminal_state=str(payload.get("terminal_state", "")),
            terminal_reason_code=str(payload.get("terminal_reason_code", "")),
            captured_patch=(
                payload.get("captured_patch")
                if payload.get("captured_patch") is None
                or isinstance(payload.get("captured_patch"), str)
                else None
            ),
            diff_outcome=(
                payload.get("diff_outcome")
                if payload.get("diff_outcome") is None
                or isinstance(payload.get("diff_outcome"), str)
                else None
            ),
            elapsed_s=float(payload.get("elapsed_s", 0.0)),
        )


# ===========================================================================
# Env loaders (NEVER raise)
# ===========================================================================


def _resolve_timeout_s(explicit: Optional[float]) -> float:
    """Resolve the bounded-wait timeout.

    Precedence: explicit argument > env var > default. Invalid
    env values fall back to the default with a WARN log. The
    resolved value is then passed through Task #21 Dynamic Timeout
    Coherence (:func:`_apply_wall_coherence`) so the inner wait
    ALWAYS ends + emits TERMINAL_TIMEOUT before the outer bounded-
    shutdown when a session wall deadline is published (no-op
    otherwise — byte-identical legacy). NEVER raises.
    """
    if explicit is not None and explicit > 0:
        configured = float(explicit)
    else:
        raw = os.environ.get(EVAL_TIMEOUT_ENV_VAR, "").strip()
        if not raw:
            configured = _DEFAULT_TIMEOUT_S
        else:
            try:
                value = float(raw)
                if value <= 0:
                    raise ValueError("must be > 0")
                configured = value
            except (ValueError, TypeError):
                logger.warning(
                    "[SWEBenchPro] invalid %s=%r — using default "
                    "%.1fs",
                    EVAL_TIMEOUT_ENV_VAR, raw, _DEFAULT_TIMEOUT_S,
                )
                configured = _DEFAULT_TIMEOUT_S
    return _apply_wall_coherence(configured)


# ===========================================================================
# Internal helpers
# ===========================================================================


def _classify_terminal_state(state_value: str) -> EvaluationOutcome:
    """Map a terminal OperationState value to an EvaluationOutcome.

    Pure function; deterministic; NEVER raises. Unknown values
    default to UNRESOLVED (conservative — we observed a terminal
    but can't classify it positively).
    """
    if state_value in _RESOLVED_STATES:
        return EvaluationOutcome.RESOLVED
    if state_value in _UNRESOLVED_STATES:
        return EvaluationOutcome.UNRESOLVED
    return EvaluationOutcome.UNRESOLVED


async def _await_broker_terminal_event(
    broker: StreamEventBroker,
    subscriber: Any,
    op_id: str,
    timeout_s: float,
) -> Optional[Dict[str, Any]]:
    """Await the next ``operation_terminal`` event for ``op_id`` on
    the subscriber's queue. Returns the event payload dict on
    success, ``None`` on timeout.

    Composes ``StreamEventBroker.stream_iter`` (canonical iteration
    API) with ``heartbeat_s=0`` so the wait_for ceiling is the only
    timeout that fires. ``asyncio.CancelledError`` propagates.

    The op_id filter on the subscriber already gates events to the
    target op, but we also filter by ``event_type`` here so non-
    terminal events for the same op (future op_started / phase
    transitions) don't satisfy the wait.
    """
    async def _drain_until_terminal() -> Optional[Dict[str, Any]]:
        async for event in broker.stream_iter(subscriber, heartbeat_s=0):
            if event.event_type != EVENT_TYPE_OPERATION_TERMINAL:
                continue
            if event.op_id != op_id:
                # Defensive: broker filter should have caught this.
                continue
            return dict(event.payload or {})
        return None

    try:
        return await asyncio.wait_for(
            _drain_until_terminal(), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return None


async def _ledger_fallback_classify(
    operation_ledger: Optional[Any],
    op_id: str,
) -> Optional[str]:
    """One-shot ledger query — NEVER a polling loop.

    Returns the latest OperationState value (string) for ``op_id``
    if the ledger reports one, ``None`` otherwise. NEVER raises.
    """
    if operation_ledger is None:
        return None
    try:
        state = await operation_ledger.get_latest_state(op_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fallback is best-effort
        logger.debug(
            "[SWEBenchPro] ledger.get_latest_state raised for op=%s",
            op_id, exc_info=True,
        )
        return None
    if state is None:
        return None
    return getattr(state, "value", None)


def _is_terminal_state(state_value: Optional[str]) -> bool:
    if not state_value:
        return False
    return state_value in _RESOLVED_STATES or state_value in _UNRESOLVED_STATES


# ===========================================================================
# Public API — evaluate_problem
# ===========================================================================


async def evaluate_problem(
    problem: ProblemSpec,
    *,
    intake_service: Any,
    operation_ledger: Optional[Any] = None,
    broker: Optional[StreamEventBroker] = None,
    timeout_s: Optional[float] = None,
    cleanup: bool = True,
) -> EvaluationResult:
    """Evaluate a single SWE-Bench-Pro problem end-to-end.

    Parameters
    ----------
    problem:
        Phase A ``ProblemSpec`` (loaded via
        :func:`backend.core.ouroboros.governance.swe_bench_pro.dataset_loader.load_problem`).
    intake_service:
        An ``IntakeLayerService`` (or anything exposing an
        ``async ingest_envelope(envelope) -> bool``). REQUIRED —
        no default. Tests inject a stub.
    operation_ledger:
        Optional ``OperationLedger`` for the one-shot timeout
        fallback. When ``None``, ``TERMINAL_TIMEOUT`` is the
        only timeout outcome. Tests inject a stub.
    broker:
        Optional ``StreamEventBroker``. Defaults to
        :func:`get_default_broker` (the process-global canonical
        instance). Tests inject a fresh broker via
        :func:`reset_default_broker` to isolate subscriber state.
    timeout_s:
        Bounded terminal-wait ceiling in seconds. Precedence:
        argument > ``JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S`` env >
        default 1800s. NEVER unbounded.
    cleanup:
        When ``True`` (default), the per-problem worktree + branch
        are removed after diff capture. Set ``False`` to preserve
        the worktree for forensic inspection.

    Returns
    -------
    EvaluationResult
        Always returns a populated result; the ``outcome`` field
        identifies which path the evaluation took. The function
        NEVER raises except ``asyncio.CancelledError`` (cooperative
        cancel per orchestrator convention; cleanup still runs in
        ``finally`` before the exception propagates).
    """
    started_at = time.monotonic()
    instance_id = getattr(problem, "instance_id", "") or ""

    # ---- Master-flag gate (FIRST executable statement) ----
    # AST-pinned by the B.2.3 spine. When OFF, no side effects
    # whatsoever — no prepare, no ingest, no broker subscription.
    if not swe_bench_pro_enabled():
        return EvaluationResult(
            outcome=EvaluationOutcome.MASTER_FLAG_OFF,
            problem_instance_id=instance_id,
            elapsed_s=time.monotonic() - started_at,
        )

    # ---- Phase B.1: prepare worktree + apply test_patch ----
    prepared, harness_outcome = await prepare_problem(problem)
    if prepared is None or harness_outcome != HarnessOutcome.READY:
        return EvaluationResult(
            outcome=EvaluationOutcome.PREPARE_FAILED,
            problem_instance_id=instance_id,
            terminal_reason_code=getattr(harness_outcome, "value", ""),
            elapsed_s=time.monotonic() - started_at,
        )

    # From here on, the `finally` block at the end guarantees
    # cleanup_prepared runs regardless of outcome.
    captured_patch: Optional[str] = None
    diff_outcome: Optional[str] = None
    subscriber: Any = None
    resolved_broker = broker if broker is not None else get_default_broker()
    op_id: str = ""
    outcome = EvaluationOutcome.TERMINAL_TIMEOUT
    terminal_state: str = ""
    terminal_phase: str = ""
    terminal_reason_code: str = ""

    try:
        # ---- Phase B.2.1: build envelope (allocates causal_id) ----
        envelope = build_evaluation_envelope(problem, prepared)
        op_id = envelope.causal_id

        # ---- Phase B.2.0.5: subscribe BEFORE ingest (race-free) ----
        # AST-pinned by the B.2.3 spine — source-order invariant.
        subscriber = resolved_broker.subscribe(op_id_filter=op_id)
        if subscriber is None:
            # Broker capacity exhausted. Don't ingest — there's no
            # observer to rendezvous with, and a polling-only fallback
            # is forbidden by operator binding.
            logger.warning(
                "[SWEBenchPro] broker.subscribe returned None for "
                "op=%s (subscriber cap exceeded) — aborting eval",
                op_id,
            )
            return EvaluationResult(
                outcome=EvaluationOutcome.INGEST_FAILED,
                problem_instance_id=instance_id,
                op_id=op_id,
                terminal_reason_code="broker_subscribe_capacity_exceeded",
                elapsed_s=time.monotonic() - started_at,
            )

        # ---- Canonical intake: ingest_envelope ----
        try:
            ingested = await intake_service.ingest_envelope(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            logger.warning(
                "[SWEBenchPro] intake_service.ingest_envelope raised "
                "for op=%s", op_id, exc_info=True,
            )
            ingested = False
        if not ingested:
            return EvaluationResult(
                outcome=EvaluationOutcome.INGEST_FAILED,
                problem_instance_id=instance_id,
                op_id=op_id,
                terminal_reason_code="ingest_returned_false",
                elapsed_s=time.monotonic() - started_at,
            )

        # ---- Phase B.2.0.5: bounded wait_for terminal event ----
        timeout = _resolve_timeout_s(timeout_s)
        terminal_event = await _await_broker_terminal_event(
            resolved_broker, subscriber, op_id, timeout,
        )

        if terminal_event is not None:
            # SSE primary-path success — broker delivered terminal.
            terminal_state = str(terminal_event.get("state") or "")
            terminal_phase = str(terminal_event.get("phase") or "")
            terminal_reason_code = str(
                terminal_event.get("terminal_reason_code") or ""
            )
            outcome = _classify_terminal_state(terminal_state)
        else:
            # SSE timeout. One-shot ledger fallback — NEVER a
            # polling loop. AST-pinned in the B.2.3 spine.
            ledger_state = await _ledger_fallback_classify(
                operation_ledger, op_id,
            )
            if _is_terminal_state(ledger_state):
                # Ledger is authoritative — promote to RESOLVED/
                # UNRESOLVED. SSE event likely dropped (broker
                # backpressure / disconnect / race).
                terminal_state = ledger_state or ""
                terminal_reason_code = "sse_timeout_ledger_fallback_terminal"
                outcome = _classify_terminal_state(terminal_state)
            else:
                # Operation genuinely still running OR ledger has no
                # record (also a degraded signal). Honor the timeout.
                terminal_reason_code = (
                    f"sse_timeout_after_{timeout:.0f}s"
                    if ledger_state is None
                    else f"sse_timeout_ledger_state={ledger_state}"
                )
                outcome = EvaluationOutcome.TERMINAL_TIMEOUT

        # ---- Phase B.1: capture produced patch ----
        try:
            patch, diff_oc = await capture_produced_patch(prepared)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — defensive
            patch = None
            diff_oc = DiffCaptureOutcome.CAPTURE_FAILED
            logger.debug(
                "[SWEBenchPro] capture_produced_patch raised for op=%s",
                op_id, exc_info=True,
            )
        captured_patch = patch
        diff_outcome = (
            diff_oc.value if isinstance(diff_oc, DiffCaptureOutcome)
            else None
        )

        return EvaluationResult(
            outcome=outcome,
            problem_instance_id=instance_id,
            op_id=op_id,
            terminal_phase=terminal_phase,
            terminal_state=terminal_state,
            terminal_reason_code=terminal_reason_code,
            captured_patch=captured_patch,
            diff_outcome=diff_outcome,
            elapsed_s=time.monotonic() - started_at,
        )

    except asyncio.CancelledError:
        # Cooperative cancel — record outcome BEFORE re-raising so
        # forensic state survives. We can't return from inside
        # except CancelledError + re-raise, so we log + re-raise.
        # The cleanup in `finally` runs unconditionally.
        logger.info(
            "[SWEBenchPro] evaluate_problem cancelled for op=%s "
            "instance=%s after %.1fs (cleanup will run)",
            op_id, instance_id, time.monotonic() - started_at,
        )
        raise
    finally:
        # Always unsubscribe from the broker (releases the
        # subscriber slot for future ops).
        if subscriber is not None:
            try:
                resolved_broker.unsubscribe(subscriber)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[SWEBenchPro] broker.unsubscribe raised for op=%s",
                    op_id, exc_info=True,
                )
        # Cleanup the worktree if requested. Best-effort — failures
        # here log at DEBUG and don't shadow the primary outcome.
        if cleanup:
            try:
                await cleanup_prepared(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[SWEBenchPro] cleanup_prepared raised for op=%s",
                    op_id, exc_info=True,
                )


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count
    successfully registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=EVAL_TIMEOUT_ENV_VAR,
            type=FlagType.INT,
            default=int(_DEFAULT_TIMEOUT_S),
            description=(
                "Bounded terminal-wait timeout (seconds) for the "
                "SWE-Bench-Pro Phase B.2.2 evaluator façade's "
                "asyncio.wait_for over the canonical operation_terminal "
                "SSE rendezvous. Default 1800s = 30 min, covers the "
                "long tail of hard problems. NEVER unbounded — on "
                "timeout the façade falls back to a one-shot "
                "OperationLedger.get_latest_state query and either "
                "promotes the outcome (if the ledger shows a terminal "
                "state — SSE event likely dropped) or returns "
                "TERMINAL_TIMEOUT (operation genuinely still running)."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "evaluator.py"
            ),
            example=str(int(_DEFAULT_TIMEOUT_S)),
            since="v3.7 Phase 2 Phase B.2.2 (2026-05-12)",
        ),
        FlagSpec(
            name=_DRAIN_BUFFER_ENV_VAR,
            type=FlagType.INT,
            default=0,
            description=(
                "Explicit override (seconds) for the post-eval drain "
                "buffer Task #21/#22 Dynamic Timeout Coherence "
                "subtracts from wall-remaining. 0/unset ⇒ COMPOSED = "
                "JARVIS_BATTLE_SHUTDOWN_DEADLINE_S + "
                "JARVIS_SWE_BENCH_PRO_AUTOSCORE_SHUTDOWN_GRACE_S + "
                "JARVIS_SWE_BENCH_PRO_EVAL_DRAIN_MARGIN_S. >0 wins "
                "verbatim. Seeded by Task #22 (was env-only since #21)."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "evaluator.py"
            ),
            example="0",
            since="v3.7 Task #21/#22 (2026-05-18)",
        ),
        FlagSpec(
            name=_DRAIN_MARGIN_ENV_VAR,
            type=FlagType.INT,
            default=int(_DEFAULT_DRAIN_MARGIN_S),
            description=(
                "Slack (seconds) added to the composed drain buffer "
                "for GC / asyncio teardown / log flush so the "
                "autoscore verdict ALWAYS flushes before os._exit. "
                "Task #22 root fix — deep-run bt-2026-05-19-011003 "
                "proved the prior 2×autoscore-grace heuristic "
                "undersized vs real bounded-shutdown latency."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "evaluator.py"
            ),
            example=str(int(_DEFAULT_DRAIN_MARGIN_S)),
            since="v3.7 Task #22 (2026-05-18)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro] evaluator flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "EVALUATION_RESULT_SCHEMA_VERSION",
    "EVAL_TIMEOUT_ENV_VAR",
    "EvaluationOutcome",
    "EvaluationResult",
    "evaluate_problem",
    "register_flags",
]
