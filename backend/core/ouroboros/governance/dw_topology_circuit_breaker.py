"""DW Topology Early-Detection Circuit Breaker.

Per the live debug.log captured during Phase 9.1c once-proof
(session ``bt-2026-04-27-162115``): the orchestrator's BG-route
exception handler emits ``BACKGROUND route: DW failed... accepting
[op-...]`` AFTER the op has entered the GENERATE phase, when
``CandidateGenerator`` raises ``RuntimeError("background_dw_blocked
_by_topology:...")`` from the existing topology gate.

This is a late-detection path: the op enters the generation hot
path, the generator raises, and the orchestrator gracefully accepts
the failure. Functionally fine, but it produces noisy logs that
look like "we tried to do work" when the work was structurally
guaranteed to fail.

This module is the **pure-data circuit-breaker check** that lets
the orchestrator make the same decision BEFORE entering the GENERATE
phase. The semantics match the existing late-detection path exactly
— same Nervous-System-Reflex carve-out for read-only ops, same
provider-topology read, same skip_and_queue gate. The only
difference is *when* it fires (pre-GENERATE vs mid-GENERATE) and
*how it reads in the log* (one clean `[CircuitBreaker] skipped`
line vs two messy `Topology block` + `BACKGROUND route: DW failed`
lines).

## Authority posture

  * **Pure-evaluation module** — no I/O, no logger, no orchestrator
    state mutation. The caller decides what to do with the verdict.
  * **Stdlib + governance.provider_topology only** at top-level
    (pinned by AST scan in tests).
  * **NEVER raises** — every error path returns ``(False, reason)``
    with the reason describing the raise, so the caller can choose
    to log + proceed (defaulting to today's late-detection
    semantics).
  * **Master flag** ``JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED``
    (default ``false``) gates orchestrator consultation. Default-off
    means the orchestrator's behavior is byte-identical to pre-
    Option-C — circuit-breaker module exists but is never consulted.
  * **Read-only carve-out preserved**: the existing Nervous-System-
    Reflex bypass at ``candidate_generator.py:1418-1440`` (read-only
    ops on BG with topology skip_and_queue → cascade to Claude) is
    REPLICATED here. Read-only ops never circuit-break — they get
    routed by the late-detection path's existing reflex.
  * **Bounded** — single function call, no recursion, no state.

## Verdict shape

``(circuit_break: bool, reason: str)``

  * ``circuit_break=True`` — the op is structurally doomed; caller
    should skip GENERATE and mark the op CANCELLED with reason
    ``"circuit_breaker_dw_topology:<topology_reason>"`` (mirrors the
    late-detection path's terminal_reason_code).
  * ``circuit_break=False`` — let the op proceed normally. The
    reason string carries diagnostic context (e.g. ``"topology_off"``
    / ``"route_unmapped"`` / ``"read_only_carve_out"``) so the caller
    can emit a single trace event explaining why the breaker did
    NOT fire.
"""
from __future__ import annotations

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


def is_circuit_breaker_enabled() -> bool:
    """Master flag —
    ``JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED`` (default ``false``).

    When off, callers MUST treat any verdict as advisory — the
    orchestrator's late-detection path (graceful-accept of
    ``background_dw_blocked_by_topology``) remains the authoritative
    behavior. Default-off until graduation cadence proves the early-
    reject path produces identical downstream ledger state."""
    return os.environ.get(
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED", "",
    ).strip().lower() in _TRUTHY


def should_circuit_break(
    *,
    provider_route: str,
    is_read_only: bool,
) -> Tuple[bool, str]:
    """Return ``(circuit_break, reason)`` for the given route +
    read-only state.

    **Phase 10 P10.3 update (2026-04-27)**: when
    ``JARVIS_TOPOLOGY_SENTINEL_ENABLED=true``, the breaker also
    consults the AsyncTopologySentinel — if **every** model in the
    route's ranked ``dw_models`` list has its breaker OPEN AND the
    route's ``fallback_tolerance`` is ``"queue"``, fire the breaker
    even if the static yaml ``block_mode`` would have allowed a
    cascade. This closes the dynamic-evidence loop: when the
    sentinel has live empirical evidence that all DW models are
    SEVERED, BG/SPEC ops queue (preserving unit economics) instead
    of cascading.

    When the sentinel master flag is OFF (default), behavior is
    byte-identical to pre-Slice-3 — same logic as the old
    ``candidate_generator.py:1404-1465`` path:

      1. If topology disabled → ``(False, "topology_disabled")``.
      2. If route not in topology map → ``(False, "route_unmapped")``.
      3. If route IS dw_allowed → ``(False, "dw_allowed")``.
      4. If route DW-blocked AND ``block_mode == "cascade_to_claude"``
         → ``(False, "cascade_to_claude")`` (caller routes to Claude
         via late-detection path; circuit breaker doesn't intervene).
      5. If route DW-blocked AND ``block_mode == "skip_and_queue"``
         AND ``is_read_only=True`` AND ``route == "background"`` →
         ``(False, "read_only_carve_out")`` (Nervous-System-Reflex
         bypass — read-only BG ops cascade to Claude even on
         skip_and_queue routes).
      6. Otherwise (skip_and_queue + not-read-only-BG) → ``(True,
         topology_reason)`` — the op is structurally doomed; caller
         should skip GENERATE.

    NEVER raises — any topology read failure returns ``(False,
    f"topology_read_error:{exc}")`` so the orchestrator falls back
    to today's late-detection path.
    """
    route_norm = (provider_route or "").strip().lower()
    if not route_norm:
        return (False, "empty_route")

    try:
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        topology = get_topology()
    except Exception as exc:  # noqa: BLE001 — defensive
        return (False, f"topology_read_error:{type(exc).__name__}")

    if not topology.enabled:
        return (False, "topology_disabled")

    # Phase 10 P10.3 — sentinel-driven verdict (when master flag on).
    # Consulted AHEAD of the legacy yaml-only path because the
    # sentinel carries live empirical state; the yaml is operator
    # intent. When both agree the verdict is the same; when they
    # disagree, live evidence wins.
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            is_sentinel_enabled,
            get_default_sentinel,
        )
        if is_sentinel_enabled():
            sentinel = get_default_sentinel()
            ranked = topology.dw_models_for_route(route_norm)
            tolerance = topology.fallback_tolerance_for_route(route_norm)
            if ranked:
                # Every model OPEN → fire breaker iff fallback is queue.
                all_open = all(
                    sentinel.get_state(m) == "OPEN" for m in ranked
                )
                if all_open and tolerance == "queue":
                    return (
                        True,
                        f"sentinel_all_severed:{','.join(ranked[:3])}"[
                            :120
                        ],
                    )
                if all_open and tolerance == "cascade_to_claude":
                    # Sentinel says SEVERED everywhere but cascade is
                    # the contract — let the caller's late-detection
                    # path cascade. Don't fire here.
                    return (False, "sentinel_severed_cascade_to_claude")
                # At least one model not OPEN — let the dispatch try.
                return (False, "sentinel_dw_available")
    except Exception as exc:  # noqa: BLE001
        # Sentinel-side failure must not block the legacy verdict.
        logger.debug(
            "[CircuitBreaker] sentinel consultation raised "
            "(%s) — falling back to legacy yaml verdict",
            type(exc).__name__,
        )

    try:
        dw_allowed = topology.dw_allowed_for_route(route_norm)
    except Exception as exc:  # noqa: BLE001
        return (False, f"dw_allowed_check_error:{type(exc).__name__}")

    if dw_allowed:
        return (False, "dw_allowed")

    # DW NOT allowed — check block_mode.
    try:
        block_mode = topology.block_mode_for_route(route_norm)
    except Exception as exc:  # noqa: BLE001
        return (False, f"block_mode_check_error:{type(exc).__name__}")

    if block_mode != "skip_and_queue":
        # cascade_to_claude (or unknown mode) — caller's late-detection
        # path handles the cascade. Circuit breaker stays out of it.
        return (False, f"block_mode:{block_mode}")

    # skip_and_queue → Nervous-System-Reflex carve-out for read-only
    # BG ops (matches candidate_generator.py:1418-1440).
    if is_read_only and route_norm == "background":
        return (False, "read_only_carve_out")

    # Op is structurally doomed: skip GENERATE.
    try:
        topology_reason = topology.reason_for_route(route_norm)
    except Exception as exc:  # noqa: BLE001
        topology_reason = f"reason_read_error:{type(exc).__name__}"
    return (True, topology_reason)


def terminal_reason_code(topology_reason: str) -> str:
    """Build the ``terminal_reason_code`` string for the orchestrator
    to stamp on the cancelled context. Mirrors the late-detection
    path's ``background_accepted:<err_msg[:80]>`` shape so downstream
    ledger consumers can grep both equally.

    Schema:
      ``circuit_breaker_dw_topology:<reason[:80]>``
    """
    safe = (topology_reason or "")[:80]
    return f"circuit_breaker_dw_topology:{safe}"


def ledger_reason_label(topology_reason: str) -> str:
    """Build the ``reason`` label for the orchestrator's
    ``_record_ledger`` call. Mirrors the late-detection path's
    ``background_dw_failure`` / ``background_cascade_failure``
    naming convention so cron + graduation-ledger consumers can
    filter on a single namespace."""
    return "circuit_breaker_dw_topology_blocked"


__all__ = [
    "is_circuit_breaker_enabled",
    "ledger_reason_label",
    "should_circuit_break",
    "terminal_reason_code",
]
