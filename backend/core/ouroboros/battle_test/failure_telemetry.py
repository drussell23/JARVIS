"""failure_telemetry -- autonomous failure telemetry capture (Task 5, Isomorphic Local Sandbox).

Produces a bounded, fail-soft artifact directory composing EXISTING infrastructure:

  * FSM phase       -- ``op_ctx.phase.name``  (``OperationContext.phase``)
  * Causal chain    -- ``CommMessage.causal_parent_seq`` from the first
                       ``LogTransport`` found in ``CommProtocol._transports``
  * Memory snapshot -- ``MemoryPressureGate.snapshot()`` via ``get_default_gate()``
  * A1Trace hops    -- ``a1_trace._emit_ledger`` (observe-only, fail-soft)
  * Session record  -- ``SessionRecorder.save_summary(session_outcome="incomplete_kill")``

All sources are wrapped in INDEPENDENT try/except blocks so a partial failure in
one source never prevents the others or blocks teardown. Mirrors the
``local_autopsy`` timestamped-dir pattern from ``a1_live_fire_chaos_harness.py``.

Kill switch
-----------
None -- this module is the failure path, so it carries no master flag itself.
Callers (IsomorphicEnv / harness teardown) decide whether to invoke it.

Authority posture
-----------------
Observe-only; never imports orchestrator / policy / iron_gate / candidate_generator.
Wraps external calls fail-softly -- zero authority, 100% observability.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

# Maximum number of CommMessage entries captured in the causal chain.
# Most-recent tail is kept (the last _CAUSAL_CHAIN_CAP messages).
_CAUSAL_CHAIN_CAP: int = 50

# Maximum number of A1Trace emit-ledger hops captured.
_A1TRACE_HOPS_CAP: int = 50

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def capture_failure_telemetry(
    *,
    op_ctx: Any = None,
    output_dir: Path,
    reason: str,
    comm: Any = None,
    session_recorder: Any = None,
) -> Path:
    """Capture failure telemetry into a timestamped artifact directory.

    NEVER raises -- every source is wrapped in its own try/except so a
    broken source produces partial telemetry rather than blocking teardown.

    Parameters
    ----------
    op_ctx:
        An ``OperationContext`` instance or None.  FSM phase extracted via
        ``op_ctx.phase.name`` (the ``OperationPhase`` enum attribute).
    output_dir:
        Parent directory.  A timestamped sub-directory is created here.
    reason:
        Short human-readable string describing the failure trigger.
    comm:
        A ``CommProtocol`` instance or None.  Causal chain extracted from
        the first transport that exposes a ``messages`` attribute (typically
        ``LogTransport``).  Chain is bounded to the most-recent
        ``_CAUSAL_CHAIN_CAP`` messages.
    session_recorder:
        A ``SessionRecorder`` instance or None.  When provided,
        ``save_summary(session_outcome="incomplete_kill")`` is called with
        sentinel zero-cost defaults for every required positional argument.

    Returns
    -------
    Path
        The artifact directory path (attempted creation; returned even if
        mkdir failed, so callers can log the intended location).
    """
    output_dir = Path(output_dir)
    # -- local_autopsy pattern: timestamped sub-dir, fail-soft, never blocks --
    stamp = time.strftime("%Y%m%d-%H%M%S")
    artifact_dir = output_dir / f"failure_telemetry_{stamp}"

    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 -- autopsy must not block teardown
        logger.warning("[failure_telemetry] mkdir error (proceeding): %r", exc)

    telemetry: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "reason": reason,
        "ts": time.time(),
    }

    # ------------------------------------------------------------------
    # Source 1 -- FSM phase from op_ctx.phase
    # ------------------------------------------------------------------
    try:
        if op_ctx is not None:
            telemetry["fsm_phase"] = op_ctx.phase.name
        else:
            telemetry["fsm_phase"] = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[failure_telemetry] fsm_phase error: %r", exc)
        telemetry["fsm_phase"] = None

    # ------------------------------------------------------------------
    # Source 2 -- Causal-parent chain from CommProtocol / LogTransport
    # ------------------------------------------------------------------
    try:
        if comm is not None:
            messages: List[Any] = []
            for transport in comm._transports:  # type: ignore[attr-defined]
                if hasattr(transport, "messages"):
                    messages = list(transport.messages)
                    break
            # Bound: take the most-recent _CAUSAL_CHAIN_CAP entries (tail)
            capped = messages[-_CAUSAL_CHAIN_CAP:]
            chain: List[Dict[str, Any]] = [
                {
                    "seq": m.seq,
                    "causal_parent_seq": m.causal_parent_seq,
                    "msg_type": m.msg_type.value,
                    "op_id": m.op_id,
                    "timestamp": m.timestamp,
                }
                for m in capped
            ]
            telemetry["causal_chain"] = chain
        else:
            telemetry["causal_chain"] = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[failure_telemetry] causal_chain error: %r", exc)
        telemetry["causal_chain"] = None

    # ------------------------------------------------------------------
    # Source 3 -- Memory pressure snapshot via MemoryPressureGate
    # ------------------------------------------------------------------
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate,
        )
        telemetry["memory_snapshot"] = get_default_gate().snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[failure_telemetry] memory_snapshot error: %r", exc)
        telemetry["memory_snapshot"] = None

    # ------------------------------------------------------------------
    # Source 4 -- A1Trace emit-ledger hops (observe-only, fail-soft)
    # ------------------------------------------------------------------
    try:
        from backend.core.ouroboros.governance import a1_trace as _a1_trace_mod
        raw_hops = list(_a1_trace_mod._emit_ledger.items())[:_A1TRACE_HOPS_CAP]
        telemetry["a1trace_hops"] = [
            {
                "goal_id": gid,
                "emit_ts": record[0],
                "source": record[1],
            }
            for gid, record in raw_hops
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[failure_telemetry] a1trace_hops error: %r", exc)
        telemetry["a1trace_hops"] = None

    # ------------------------------------------------------------------
    # Write JSON artifact
    # ------------------------------------------------------------------
    try:
        (artifact_dir / "failure_telemetry.json").write_text(
            json.dumps(telemetry, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[failure_telemetry] write json error: %r", exc)

    # ------------------------------------------------------------------
    # Source 5 -- SessionRecorder.save_summary (session_outcome=incomplete_kill)
    # ------------------------------------------------------------------
    try:
        if session_recorder is not None:
            session_recorder.save_summary(
                output_dir=artifact_dir,
                stop_reason=reason,
                duration_s=0.0,
                cost_total=0.0,
                cost_breakdown={},
                branch_stats={},
                convergence_state="INSUFFICIENT_DATA",
                convergence_slope=0.0,
                convergence_r2=0.0,
                session_outcome="incomplete_kill",
                last_activity_ts=time.time(),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[failure_telemetry] save_summary error: %r", exc)

    logger.info("[failure_telemetry] artifact captured -> %s", artifact_dir)
    return artifact_dir
