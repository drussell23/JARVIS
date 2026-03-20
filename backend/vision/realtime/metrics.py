"""
Vision action metrics — structured record builder for telemetry.

Every completed ``execute_action()`` call in :class:`VisionActionLoop` emits
a :func:`build_action_record` dict through the ``on_action_record`` callback.

Fields are intentionally flat (no nested dicts) so they can be sent directly
to Langfuse / Helicone / any JSON-compatible sink without transformation.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple


def build_action_record(
    *,
    action_id: str,
    plan_id: str = "",
    step_id: str = "",
    target_description: str = "",
    coords: Optional[Tuple[int, int]] = None,
    confidence: float = 0.0,
    precheck_passed: bool = True,
    failed_guards: Optional[List[str]] = None,
    action_type: str = "",
    backend_used: str = "",
    latency_ms: float = 0.0,
    verification_result: str = "",
    retry_count: int = 0,
    tier_used: str = "",
    success: bool = False,
    error: Optional[str] = None,
) -> Dict:
    """Build a flat action-record dict for telemetry emission.

    All parameters are keyword-only to prevent positional mistakes.

    Returns
    -------
    dict
        Flat dict suitable for JSON serialisation and telemetry sinks.
    """
    return {
        "action_id": action_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "target_description": target_description,
        "coords": coords,
        "confidence": confidence,
        "precheck_passed": precheck_passed,
        "failed_guards": failed_guards or [],
        "action_type": action_type,
        "backend_used": backend_used,
        "latency_ms": latency_ms,
        "verification_result": verification_result,
        "retry_count": retry_count,
        "tier_used": tier_used,
        "success": success,
        "error": error,
        "timestamp": time.time(),
    }
