"""Operator-Independent Recursion-Depth Gate — Slice 104 (RRD §23.5 / §43).

Upgrades the PATH-based governance boundary predicate into a MATHEMATICAL,
depth-bounded gate. ``governance_boundary_gate`` answers a 1-bit question — "does
this op touch the cage?". This gate answers the deeper Reverse-Russian-Doll
question: "how many ORDERS deep is this self-modification chain?" — and enforces
an absolute ceiling.

A self-modification CHAIN is a run of causally-consecutive APPLYs that EACH touch
the ``governance/`` cage. Order 0 = the operator. Order 1 = O+V modifies the body.
Order 2 = O+V modifies its own cognition (``governance/``). A chain that recurses
governance→governance→governance without bound is the unbounded-RSI failure mode
the RRD thesis must prevent. When the consecutive governance-touching depth would
exceed ``MAX_RECURSION_DEPTH``, the op is HALTED — routed to the un-bypassable
``blocked`` tier via :mod:`risk_tier_floor`.

OPERATOR-INDEPENDENT: this is pure arithmetic over a causal depth counter, not a
human approval. The human remains the structural authority (the zero-order doll),
but this gate severs a runaway self-modification chain AUTONOMOUSLY — even with
the operator out of the loop. That is the structural prerequisite for *safe*
Order-2 RSI, and the formal core of the "Thermodynamic Containment of Agentic
Entropy / Bounded RSI" thesis.

The depth counter (:class:`RecursionDepthTracker`) is a process singleton: a
governance-touching APPLY increments it; a non-governance APPLY resets it to 0
(the chain is broken). It is in-memory by design — a reboot resets the chain,
which is correct (a self-modification chain lives within a running session).

Master ``JARVIS_RECURSION_DEPTH_GATE_ENABLED`` — DEFAULT-TRUE (load-bearing safety
gate, mirroring ``governance_boundary_gate``). NEVER raises.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Optional, Sequence

logger = logging.getLogger("ouroboros.recursion_depth_gate")

_ENV_MASTER = "JARVIS_RECURSION_DEPTH_GATE_ENABLED"
_ENV_MAX_DEPTH = "JARVIS_MAX_RECURSION_DEPTH"
_TRUTHY = ("1", "true", "yes", "on")

_DEFAULT_MAX_DEPTH = 3
_MIN_MAX_DEPTH = 1
_MAX_MAX_DEPTH = 16

RECURSION_DEPTH_GATE_SCHEMA_VERSION = "recursion_depth_gate.1"


def master_enabled() -> bool:
    """§43 safety gate — DEFAULT-TRUE (mirrors governance_boundary_gate). The
    recursion bound is load-bearing; it is on unless explicitly disabled."""
    raw = os.environ.get(_ENV_MASTER)
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


def max_recursion_depth() -> int:
    """The absolute ceiling on consecutive governance-touching self-modification
    depth. A chain whose depth would EXCEED this is halted. Clamped to a sane
    range; never raises."""
    try:
        raw = int(os.environ.get(_ENV_MAX_DEPTH, str(_DEFAULT_MAX_DEPTH)))
        return max(_MIN_MAX_DEPTH, min(_MAX_MAX_DEPTH, raw))
    except Exception:  # noqa: BLE001
        return _DEFAULT_MAX_DEPTH


class RecursionVerdict(str, enum.Enum):
    """Closed taxonomy. Bytes-pinned."""

    ALLOWED = "allowed"      # not a self-mod, or chain within the ceiling
    HALT = "halt"            # chain would exceed MAX_RECURSION_DEPTH → sever
    DISABLED = "disabled"    # master off


@dataclass(frozen=True)
class RecursionReport:
    master_enabled: bool
    verdict: RecursionVerdict
    touches_governance: bool
    chain_depth_before: int     # depth of the already-applied governance chain
    effective_depth: int        # chain_depth_before + 1 (this pending op)
    max_depth: int
    diagnostic: str
    schema_version: str = RECURSION_DEPTH_GATE_SCHEMA_VERSION


def _touches_governance(target_files: Optional[Sequence[Any]]) -> bool:
    """Compose the canonical governance boundary predicate. NEVER raises."""
    if not target_files:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (
            BoundaryVerdict,
            evaluate_target_files,
        )
        report = evaluate_target_files(target_files)
        return report.verdict is BoundaryVerdict.BOUNDARY_CROSSED
    except Exception:  # noqa: BLE001
        return False


def self_modification_depth(lineage_governance_flags: Sequence[bool]) -> int:
    """Pure: given the per-op governance-touch flags of a causal chain ordered
    OLDEST→CURRENT (last element = the current op), return the length of the
    maximal run of consecutive ``True`` ending at the current op. NEVER raises.

    This is the mathematical "Order depth" of the self-modification chain.
    """
    try:
        depth = 0
        for flag in reversed(list(lineage_governance_flags)):
            if bool(flag):
                depth += 1
            else:
                break
        return depth
    except Exception:  # noqa: BLE001
        return 0


class RecursionDepthTracker:
    """Process-singleton consecutive-governance-APPLY depth counter. Thread-safe.
    A governance-touching APPLY increments; a non-governance APPLY breaks the
    chain (reset to 0). In-memory by design (a reboot resets the chain)."""

    def __init__(self) -> None:
        self._depth = 0
        self._lock = threading.Lock()

    def note_apply(self, *, touched_governance: bool) -> int:
        """Record one APPLY. Returns the new chain depth. NEVER raises."""
        try:
            with self._lock:
                if touched_governance:
                    self._depth += 1
                else:
                    self._depth = 0
                return self._depth
        except Exception:  # noqa: BLE001
            return 0

    def current_depth(self) -> int:
        try:
            with self._lock:
                return self._depth
        except Exception:  # noqa: BLE001
            return 0

    def reset(self) -> None:
        try:
            with self._lock:
                self._depth = 0
        except Exception:  # noqa: BLE001
            pass


_TRACKER = RecursionDepthTracker()


def get_tracker() -> RecursionDepthTracker:
    return _TRACKER


def note_apply(target_files: Optional[Sequence[Any]]) -> int:
    """Production hook: call at the APPLY terminal seam. Increments the chain on
    a governance-touching apply, resets it otherwise. NEVER raises. Inert (depth
    0) when the master flag is off."""
    if not master_enabled():
        return 0
    return _TRACKER.note_apply(touched_governance=_touches_governance(target_files))


def evaluate_recursion_gate(
    target_files: Optional[Sequence[Any]],
    *,
    chain_depth: Optional[int] = None,
) -> RecursionReport:
    """Evaluate the pending op against the recursion ceiling. PURE + NEVER raises.

    ``chain_depth`` = the depth of the already-applied consecutive governance
    chain (defaults to the live tracker). A non-governance op is always ALLOWED
    (the chain is irrelevant). A governance-touching op whose effective depth
    (chain_depth + 1) EXCEEDS ``max_recursion_depth()`` is HALTED.
    """
    mx = max_recursion_depth()
    if not master_enabled():
        return RecursionReport(
            master_enabled=False, verdict=RecursionVerdict.DISABLED,
            touches_governance=False, chain_depth_before=0, effective_depth=0,
            max_depth=mx, diagnostic="recursion depth gate disabled",
        )
    touches = _touches_governance(target_files)
    if not touches:
        return RecursionReport(
            master_enabled=True, verdict=RecursionVerdict.ALLOWED,
            touches_governance=False, chain_depth_before=0, effective_depth=0,
            max_depth=mx, diagnostic="non-governance op — chain not applicable",
        )
    before = _TRACKER.current_depth() if chain_depth is None else max(0, int(chain_depth))
    effective = before + 1
    if effective > mx:
        return RecursionReport(
            master_enabled=True, verdict=RecursionVerdict.HALT,
            touches_governance=True, chain_depth_before=before,
            effective_depth=effective, max_depth=mx,
            diagnostic=(
                f"HALT: self-modification chain depth {effective} would exceed "
                f"MAX_RECURSION_DEPTH={mx} — severing the loop (RRD bound)"
            ),
        )
    return RecursionReport(
        master_enabled=True, verdict=RecursionVerdict.ALLOWED,
        touches_governance=True, chain_depth_before=before,
        effective_depth=effective, max_depth=mx,
        diagnostic=f"within bound: depth {effective}/{mx}",
    )


def recursion_depth_floor(
    target_files: Optional[Sequence[Any]],
    *,
    chain_depth: Optional[int] = None,
) -> Optional[str]:
    """Risk-tier-floor composition: returns ``"blocked"`` (the strictest, un-
    bypassable tier) when the recursion gate HALTs, else ``None``. NEVER raises.
    """
    try:
        report = evaluate_recursion_gate(target_files, chain_depth=chain_depth)
        if report.verdict is RecursionVerdict.HALT:
            logger.warning("[RecursionGate] %s", report.diagnostic)
            return "blocked"
    except Exception:  # noqa: BLE001 — the floor must stay robust
        return None
    return None
