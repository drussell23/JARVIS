"""Synthetic diagnostic probe — the daemon-side half of ``ov doctor --live``.

Fires ONE real, read-only ``web_search`` through the REAL governance
ToolBackend under a strict Trace Context protocol, then emits the traversal
edges every consumer can observe — WITHOUT any state mutation anywhere:

  * TRACE CONTEXT (mandate 2): every artifact of the probe carries
    ``trace_class="synthetic_probe"`` and an op_id under the
    ``doctor-probe-`` prefix. Stateful subsystems (ConsciousnessBridge →
    MemoryEngine) inspect for the flag and structurally no-op.
  * OBSERVABILITY EDGES: the probe emits the SAME ``actor_edge`` envelope
    shape the Step 2 tool shim produces (subsystem="web", intent
    "tool_call") — tagged synthetic — so the full emitter → aggregator →
    cockpit chain is exercised end-to-end; plus a best-effort
    ``command.doctor_probe_completed`` TrinityEventBus event so the bus
    fabric broadcasts the traversal too.
  * NO ACTUATION: ``web_search`` is a read-only capability. MCP servers are
    NEVER executed by the probe (configuration is reported by the doctor's
    static edge instead) — the actuator short-circuit applied at its
    strongest.

Bounded, NEVER raises, and returns a structured verdict dict the REPL verb
relays to attached ``ov doctor --live`` watchers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict

logger = logging.getLogger("Ouroboros.DoctorProbe")

#: The trace-context flag every synthetic artifact carries (mandate 2).
TRACE_CLASS_SYNTHETIC = "synthetic_probe"

#: op_id prefix — a second, structural marker (guards match EITHER).
SYNTHETIC_OP_PREFIX = "doctor-probe-"


def _probe_timeout_s() -> float:
    try:
        raw = os.environ.get("JARVIS_DOCTOR_PROBE_TIMEOUT_S", "")
        return float(raw) if raw else 20.0
    except (TypeError, ValueError):
        return 20.0


def is_synthetic_trace(*, op_id: str = "", trace_class: str = "") -> bool:
    """The ONE trace-inspection predicate every stateful seam reuses."""
    try:
        return (trace_class == TRACE_CLASS_SYNTHETIC
                or str(op_id).startswith(SYNTHETIC_OP_PREFIX))
    except Exception:  # noqa: BLE001
        return False


async def run_synthetic_tool_probe(tool_backend: Any = None) -> Dict[str, Any]:
    """Execute the synthetic ``web_search`` probe. Returns a verdict dict:
    ``{"ok": bool, "op_id", "duration_ms", "status", "detail"}``.

    ``tool_backend`` is injectable for tests; when None the probe resolves
    the process ToolBackend lazily. NEVER raises.
    """
    op_id = f"{SYNTHETIC_OP_PREFIX}{uuid.uuid4().hex[:12]}"
    started = time.monotonic()
    verdict: Dict[str, Any] = {
        "ok": False, "op_id": op_id, "duration_ms": 0.0,
        "status": "not_run", "detail": "",
        "trace_class": TRACE_CLASS_SYNTHETIC,
    }
    try:
        from pathlib import Path

        from backend.core.ouroboros.governance.tool_executor import (
            AsyncProcessToolBackend, PolicyContext, ToolCall,
        )
        backend = tool_backend
        if backend is None:
            try:
                backend = AsyncProcessToolBackend(asyncio.Semaphore(1))
            except Exception as exc:  # noqa: BLE001
                verdict["status"] = "backend_unavailable"
                verdict["detail"] = str(exc)[:160]
                return verdict

        call = ToolCall(
            name="web_search",
            arguments={"query": "example.com connectivity probe"},
        )
        policy_ctx = PolicyContext(
            repo="jarvis", repo_root=Path(os.getcwd()), op_id=op_id,
            call_id=f"{op_id}:r0:web_search", round_index=0,
        )
        deadline = time.monotonic() + _probe_timeout_s()
        try:
            result = await asyncio.wait_for(
                backend.execute_async(call, policy_ctx, deadline),
                timeout=_probe_timeout_s() + 2.0)
            status = str(getattr(
                getattr(result, "status", ""), "value",
                getattr(result, "status", "")) or "unknown")
            out_bytes = len((getattr(result, "output", "") or "").encode())
            verdict["status"] = status
            verdict["ok"] = "success" in status.lower()
            verdict["detail"] = f"{out_bytes}B"
        except asyncio.TimeoutError:
            verdict["status"] = "timeout"
        except Exception as exc:  # noqa: BLE001
            verdict["status"] = "exec_error"
            verdict["detail"] = str(exc)[:160]

        verdict["duration_ms"] = round((time.monotonic() - started) * 1000, 1)

        # ---- observability edges (both fabrics, both tagged synthetic) ----
        try:
            from backend.api.hive_emitter import hive_emit
            hive_emit(
                actor_id="web.search", subsystem="web", intent="tool_call",
                summary=(f"[synthetic probe] web_search {verdict['status']} "
                         f"{verdict['duration_ms']:.0f}ms"),
                severity="info" if verdict["ok"] else "warn",
                trace_id=op_id,
                detail={"trace_class": TRACE_CLASS_SYNTHETIC,
                        "status": verdict["status"],
                        "duration_ms": verdict["duration_ms"]},
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            from backend.core.trinity_event_bus import get_event_bus_if_exists
            bus = get_event_bus_if_exists()
            if bus is not None:
                await bus.publish_raw(
                    "command.doctor_probe_completed",
                    {"op_id": op_id, "trace_class": TRACE_CLASS_SYNTHETIC,
                     "status": verdict["status"],
                     "duration_ms": verdict["duration_ms"], "ts": time.time()},
                    persist=False,
                )
        except Exception:  # noqa: BLE001
            pass

        return verdict
    except Exception as exc:  # noqa: BLE001
        verdict["status"] = "probe_error"
        verdict["detail"] = str(exc)[:160]
        return verdict


__all__ = [
    "TRACE_CLASS_SYNTHETIC", "SYNTHETIC_OP_PREFIX",
    "is_synthetic_trace", "run_synthetic_tool_probe",
]
