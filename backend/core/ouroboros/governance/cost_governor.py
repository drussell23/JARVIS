"""Per-operation cost governor for the Ouroboros governance pipeline.

Motivation
----------
Per-call cost limits (``claude_max_cost_per_op`` in ClaudeProvider,
``max_cost_per_op`` in DoublewordProvider) only bound a **single** provider
call. But a single Ouroboros op can make many provider calls:

  * Tier 0 RT (DW) attempt       -> $0.05
  * Tier 1 fallback (Claude)     -> $0.40
  * GENERATE_RETRY #1 (Claude)   -> $0.35
  * L2 repair iteration #1       -> $0.30
  * L2 repair iteration #2       -> $0.30
  * ...

Each individual call stays under its per-provider cap, but the op as a whole
silently runs away to several dollars. This module adds a **cumulative**
per-op ceiling, enforced post-hoc after each charge.

Design principles (user directive: "robust, advanced, dynamic, no hardcoding")
-----------------------------------------------------------------------------
1.  **Dynamic cap derivation.** The per-op ceiling is computed from:

        cap = baseline * route_factor * complexity_factor * retry_headroom

    Every factor is resolved from environment variables (with safe defaults).
    No hardcoded scalar multipliers inside Python. Operators tune behaviour
    with env vars; tests override by passing a ``CostGovernorConfig``.

2.  **Route-aware.** A SPECULATIVE op (IntentDiscovery pre-compute) gets a
    much tighter ceiling than a COMPLEX refactor. The router taxonomy from
    Manifesto §5 drives the multiplier table.

3.  **Complexity-aware.** A ``trivial`` task caps tighter than ``heavy_code``.
    Honors the taxonomy stamped by ComplexityClassifier at CLASSIFY.

4.  **Post-hoc enforcement.** Cost is charged after each provider call
    returns its real ``cost_usd``. Before initiating the *next* call, the
    orchestrator checks ``is_exceeded(op_id)``; if true, the op is aborted
    with ``terminal_reason_code="op_cost_cap_exceeded"``. This avoids the
    need to predict pre-call costs.

5.  **Phase-aware abort.** The governor exposes ``is_exceeded()``; the
    orchestrator decides whether the resulting abort lands in CANCELLED
    (pre-apply) or POSTMORTEM (post-apply) via ``_l2_escape_terminal``.

6.  **Observable.** Every charge is logged at DEBUG; exceeds are logged at
    WARNING with a full breakdown (per-provider totals, cap, route, factors).
    ``summary(op_id)`` returns a structured dict for telemetry.

7.  **Leak-proof.** Entries TTL out; ``finish(op_id)`` is optional. Tests
    validate that a runaway op cannot accumulate entries beyond the TTL.

8.  **Asyncio-safe.** Pure Python dict operations under a single-threaded
    asyncio event loop. No locks needed (all access is from the pipeline
    coroutine) but the API is designed to be trivially lockable if moved
    to a multi-worker model later.

Compliance
----------
* Manifesto §5 — Intelligence-driven routing: the cap respects route/complexity.
* Manifesto §7 — Absolute observability: every charge + abort is logged.
* Zero-shortcut mandate: caps are *enforced*, not *recommended*. An op over
  cap cannot silently keep spending.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Env-var helpers
# -----------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    """Read a float env var with a safe default. Negative values fall back."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[CostGovernor] Env %s=%r is not a float; using default %.4f",
            name, raw, default,
        )
        return default
    if val < 0:
        logger.warning(
            "[CostGovernor] Env %s=%.4f is negative; using default %.4f",
            name, val, default,
        )
        return default
    return val


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class CostGovernorConfig:
    """Immutable config for CostGovernor.

    All factors are resolved from env vars by default but can be overridden
    in tests by instantiating explicitly.

    Cap formula
    -----------
        cap = baseline_usd * route_factor * complexity_factor * retry_headroom

    Defaults are tuned to the current 3-tier provider chain:
      * A typical DW 397B call costs ~$0.02-$0.05.
      * A typical Claude Sonnet call costs ~$0.10-$0.40.
      * An IMMEDIATE op (Claude direct) baseline: $0.50.
      * A STANDARD op (DW-first): $0.15.
      * A BACKGROUND op: $0.05.

    The ``retry_headroom`` multiplier accounts for GENERATE retries + L2
    repair iterations. 3.0x means the op can spend 3x the single-attempt
    baseline before being aborted — enough for 2 retries and a couple of
    L2 iterations, not enough for a cost-runaway cascade.

    Entries older than ``ttl_s`` are pruned on every charge. This protects
    the in-memory dict from leaks if an op crashes without calling finish().
    """

    baseline_usd: float = field(
        default_factory=lambda: _env_float("JARVIS_OP_BASELINE_COST_USD", 0.10)
    )
    retry_headroom: float = field(
        default_factory=lambda: _env_float("JARVIS_OP_RETRY_HEADROOM", 3.0)
    )
    route_factors: Mapping[str, float] = field(
        default_factory=lambda: {
            "immediate":   _env_float("JARVIS_OP_COST_ROUTE_IMMEDIATE", 5.0),
            "standard":    _env_float("JARVIS_OP_COST_ROUTE_STANDARD", 1.5),
            "complex":     _env_float("JARVIS_OP_COST_ROUTE_COMPLEX", 4.0),
            "background":  _env_float("JARVIS_OP_COST_ROUTE_BACKGROUND", 0.5),
            "speculative": _env_float("JARVIS_OP_COST_ROUTE_SPECULATIVE", 0.25),
        }
    )
    complexity_factors: Mapping[str, float] = field(
        default_factory=lambda: {
            "trivial":    _env_float("JARVIS_OP_COST_COMPLEXITY_TRIVIAL", 0.5),
            "simple":     _env_float("JARVIS_OP_COST_COMPLEXITY_SIMPLE", 0.8),
            "light":      _env_float("JARVIS_OP_COST_COMPLEXITY_LIGHT", 1.0),
            "heavy_code": _env_float("JARVIS_OP_COST_COMPLEXITY_HEAVY", 2.0),
            "complex":    _env_float("JARVIS_OP_COST_COMPLEXITY_ARCH", 3.0),
        }
    )
    # Absolute floor + ceiling; the derived cap is clamped into this band.
    # Prevents config typos from producing a $0.0001 cap (starves every op)
    # or a $1000 cap (defeats the point).
    min_cap_usd: float = field(
        default_factory=lambda: _env_float("JARVIS_OP_COST_MIN_CAP_USD", 0.05)
    )
    max_cap_usd: float = field(
        default_factory=lambda: _env_float("JARVIS_OP_COST_MAX_CAP_USD", 5.00)
    )
    # Read-only multiplier (Session 10, Derek 2026-04-17). Read-only
    # cartography ops fan out to N parallel subagents and then run a
    # Claude synthesis over the rolled-up findings. Session 10's Trinity
    # op: 3 subagents × 4-6 tool_calls each + ~50KB synthesis output =
    # $0.3446 on a route[background]=0.5 * complexity[moderate]=~1.0 *
    # headroom=3.0 * baseline=$0.10 = $0.15 cap (observed cap). 2.3×
    # overrun. The right fix is a scoped multiplier for read-only ops
    # rather than raising the BG route or moderate complexity factors,
    # which would also loosen mutating-op budgets.
    readonly_factor: float = field(
        default_factory=lambda: _env_float("JARVIS_OP_COST_READONLY_FACTOR", 5.0)
    )
    # Default multiplier if route/complexity key is unknown (unknown token
    # taxonomy shouldn't starve the op — default to standard/light).
    default_route_factor: float = 1.5
    default_complexity_factor: float = 1.0
    # TTL for pruning abandoned entries (seconds).
    ttl_s: float = field(
        default_factory=lambda: _env_float("JARVIS_OP_COST_GOVERNOR_TTL_S", 3600.0)
    )
    # Master switch — allows operators to disable without changing code.
    enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "JARVIS_OP_COST_GOVERNOR_ENABLED", "true"
        ).lower() == "true"
    )


# -----------------------------------------------------------------------------
# Per-op ledger entry
# -----------------------------------------------------------------------------

@dataclass
class _OpCostEntry:
    """Mutable per-op cost accumulator."""

    op_id: str
    route: str
    complexity: str
    cap_usd: float
    created_at: float
    cumulative_usd: float = 0.0
    call_count: int = 0
    provider_totals: Dict[str, float] = field(default_factory=dict)
    exceeded: bool = False
    # Factors frozen at start() for postmortem transparency.
    baseline_usd: float = 0.0
    route_factor: float = 1.0
    complexity_factor: float = 1.0
    retry_headroom: float = 1.0
    # Per-phase cost instrumentation — drill-down arc Slice 2.
    # phase_totals: {phase_name: cumulative_usd} rolling per-phase.
    # phase_by_provider: {phase_name: {provider: cumulative_usd}}.
    # unknown_phase_usd: charges that arrived without a phase tag —
    # preserved separately so the budget-cap path stays oblivious to
    # phase data (cumulative_usd remains the sole budget axis).
    phase_totals: Dict[str, float] = field(default_factory=dict)
    phase_by_provider: Dict[str, Dict[str, float]] = field(
        default_factory=dict,
    )
    unknown_phase_usd: float = 0.0


# -----------------------------------------------------------------------------
# CostGovernor
# -----------------------------------------------------------------------------

class CostGovernor:
    """Tracks and enforces cumulative per-op provider cost.

    Lifecycle
    ---------
    1. ``start(op_id, route, complexity)`` — call once after CLASSIFY/ROUTE
       have stamped ``provider_route`` and ``task_complexity`` on the ctx.
       Computes the dynamic cap and registers an entry.

    2. ``charge(op_id, cost_usd, provider)`` — call after every provider
       call that reports a non-zero ``cost_usd``.  Returns the updated
       cumulative total.  If the entry was never ``start()``ed, logs a
       warning and creates an entry with the default cap (graceful).

    3. ``is_exceeded(op_id)`` — check **before** initiating the next
       provider call. If ``True``, the caller must abort the op via the
       orchestrator's phase-aware terminal picker.

    4. ``finish(op_id)`` — optional; call at terminal phases for cleaner
       logs. Entries left over are pruned via TTL on the next charge.
    """

    def __init__(self, config: Optional[CostGovernorConfig] = None) -> None:
        self._config = config or CostGovernorConfig()
        self._entries: Dict[str, _OpCostEntry] = {}
        # W3(7) Slice 3 — Class E cancel hook surfaces. The registry +
        # session_dir are attached lazily by GovernedLoopService when
        # available; both default None so unit tests / standalone callers
        # don't need to provide them. When None, ``_emit_class_e_cancel``
        # is a silent no-op (matches the master-off invariant).
        self._cancel_token_registry = None  # type: ignore[assignment]
        self._cancel_session_dir = None  # type: ignore[assignment]

    def attach_cancel_surface(
        self,
        *,
        registry: Any,
        session_dir: Optional[Any] = None,
    ) -> None:
        """Wire the Class E cancel surface (registry + optional session dir).

        Called by GovernedLoopService after construction. Slice 3 (W3(7)).
        Idempotent — re-attaching just overwrites the previous handles.
        """
        self._cancel_token_registry = registry
        self._cancel_session_dir = session_dir

    def _emit_class_e_cancel(
        self,
        op_id: str,
        *,
        cumulative_usd: float,
        cap_usd: float,
    ) -> None:
        """Emit a Class E:cost cancel record on cap exceeded.

        Best-effort. No registry attached → silent no-op. Master flag off
        OR Class E sub-flag off → ``emit_watchdog_cancel`` returns None.
        Never raises into the charge() path (cost accounting must not be
        blocked by cancel-side failures).
        """
        if self._cancel_token_registry is None:
            return
        try:
            from backend.core.ouroboros.governance.cancel_token import (
                emit_watchdog_cancel as _emit_watchdog_cancel,
            )
            _emit_watchdog_cancel(
                watchdog="cost",
                op_id=op_id,
                registry=self._cancel_token_registry,
                session_dir=self._cancel_session_dir,
                phase_at_trigger="unknown",  # cost charge can fire from any phase
                reason=(
                    f"per-op cost cap exceeded: "
                    f"cumulative=${cumulative_usd:.4f} >= cap=${cap_usd:.4f}"
                ),
                initiator_task="cost_governor",
            )
        except Exception:  # noqa: BLE001 — emit is best-effort, never blocks
            pass

    # --------------------------------------------------------------
    # Cap derivation
    # --------------------------------------------------------------

    def _derive_cap(
        self, route: str, complexity: str, is_read_only: bool = False,
    ) -> Tuple[float, float, float]:
        """Compute ``(cap_usd, route_factor, complexity_factor)`` dynamically.

        No hardcoded scalars — every component is either from env-resolved
        config or a documented default-factor fallback for unknown keys.

        When ``is_read_only=True`` the cap is multiplied by
        ``cfg.readonly_factor`` (default 5.0) BEFORE the min/max clamp
        to account for subagent fan-out + Claude synthesis payload sizes.
        Still bounded by max_cap_usd so runaway costs remain capped.
        """
        cfg = self._config
        route_key = (route or "").strip().lower() or "standard"
        complexity_key = (complexity or "").strip().lower() or "light"

        route_factor = cfg.route_factors.get(route_key, cfg.default_route_factor)
        complexity_factor = cfg.complexity_factors.get(
            complexity_key, cfg.default_complexity_factor,
        )

        raw_cap = (
            cfg.baseline_usd
            * route_factor
            * complexity_factor
            * cfg.retry_headroom
        )
        if is_read_only:
            raw_cap *= cfg.readonly_factor

        # Clamp into [min_cap_usd, max_cap_usd]; protects against env typos.
        cap = max(cfg.min_cap_usd, min(cfg.max_cap_usd, raw_cap))
        return cap, route_factor, complexity_factor

    # --------------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------------

    def start(
        self,
        op_id: str,
        route: str,
        complexity: str,
        is_read_only: bool = False,
    ) -> float:
        """Register a new op and return its dynamic cap.

        Idempotent: calling ``start`` twice for the same op refreshes the
        cap (in case route or complexity was updated post-CLASSIFY) but
        preserves the cumulative spend.

        When ``is_read_only=True`` the cap is raised by
        ``cfg.readonly_factor`` (default 5×) to cover subagent fan-out +
        Claude synthesis — Session 10 (bt-2026-04-18-050658) charged
        $0.3446 against a $0.15 cap, a clean 2.3× overrun that the new
        multiplier absorbs.
        """
        if not self._config.enabled:
            return float("inf")

        self._prune_stale()

        cap, route_factor, complexity_factor = self._derive_cap(
            route, complexity, is_read_only=is_read_only,
        )
        existing = self._entries.get(op_id)
        if existing is not None:
            existing.route = route
            existing.complexity = complexity
            existing.cap_usd = cap
            existing.route_factor = route_factor
            existing.complexity_factor = complexity_factor
            logger.debug(
                "[CostGovernor] Refreshed op=%s cap=$%.4f route=%s complexity=%s",
                op_id[:12], cap, route, complexity,
            )
            return cap

        self._entries[op_id] = _OpCostEntry(
            op_id=op_id,
            route=route,
            complexity=complexity,
            cap_usd=cap,
            created_at=time.monotonic(),
            baseline_usd=self._config.baseline_usd,
            route_factor=route_factor,
            complexity_factor=complexity_factor,
            retry_headroom=self._config.retry_headroom,
        )
        logger.debug(
            "[CostGovernor] Started op=%s cap=$%.4f "
            "(base=$%.4f * route[%s]=%.2f * complexity[%s]=%.2f * headroom=%.2f)",
            op_id[:12], cap,
            self._config.baseline_usd,
            route, route_factor,
            complexity, complexity_factor,
            self._config.retry_headroom,
        )
        return cap

    def charge(
        self,
        op_id: str,
        cost_usd: float,
        provider: str = "",
        phase: Optional[str] = None,
    ) -> float:
        """Charge a provider call to the op's ledger. Returns cumulative_usd.

        Non-positive charges are a no-op (some providers report 0.0 on
        cache hits or fallback paths). Negative charges are refused.

        If ``start()`` was never called for ``op_id``, a default-cap entry
        is created on the fly so cost tracking never silently drops data.

        Per-Phase Cost Drill-Down arc (Slice 2)
        ---------------------------------------

        The optional ``phase`` argument tags this charge with the
        orchestrator phase that produced it (e.g. ``"GENERATE"`` /
        ``"VALIDATE"`` / ``"VERIFY"``). When supplied the amount is
        also added to ``entry.phase_totals[phase]`` and
        ``entry.phase_by_provider[phase][provider]`` so
        :meth:`get_phase_breakdown` can render a per-op drill-down.

        **Budget contract:** phase tagging is pure accounting — it
        does NOT change ``entry.cumulative_usd`` or the cap-check
        logic. Callers that omit ``phase`` see byte-for-byte the
        pre-Slice-2 behavior (grep-pinned at graduation).
        """
        if not self._config.enabled:
            return 0.0
        if cost_usd is None or cost_usd <= 0.0:
            return self._cumulative(op_id)

        entry = self._entries.get(op_id)
        if entry is None:
            # Late-registration path: op was started before governor was
            # wired, or CLASSIFY didn't stamp a route. Use the default
            # factors so we still track spend.
            logger.debug(
                "[CostGovernor] Charge for unstarted op=%s — auto-registering",
                op_id[:12],
            )
            self.start(op_id, route="standard", complexity="light")
            entry = self._entries[op_id]

        entry.cumulative_usd += float(cost_usd)
        entry.call_count += 1
        key = provider or "unknown"
        entry.provider_totals[key] = entry.provider_totals.get(key, 0.0) + float(cost_usd)
        # Per-phase accounting (additive, never affects budget enforcement).
        phase_tag = (phase or "").strip()
        if phase_tag:
            entry.phase_totals[phase_tag] = (
                entry.phase_totals.get(phase_tag, 0.0) + float(cost_usd)
            )
            entry.phase_by_provider.setdefault(phase_tag, {})
            entry.phase_by_provider[phase_tag][key] = (
                entry.phase_by_provider[phase_tag].get(key, 0.0)
                + float(cost_usd)
            )
        else:
            entry.unknown_phase_usd += float(cost_usd)

        if entry.cumulative_usd >= entry.cap_usd and not entry.exceeded:
            entry.exceeded = True
            logger.warning(
                "[CostGovernor] op=%s EXCEEDED cap: $%.4f >= $%.4f "
                "(route=%s complexity=%s calls=%d providers=%s)",
                op_id[:12],
                entry.cumulative_usd, entry.cap_usd,
                entry.route, entry.complexity,
                entry.call_count,
                {k: round(v, 4) for k, v in entry.provider_totals.items()},
            )
            # W3(7) Slice 3 — Class E watchdog cancel hook (best-effort).
            # When master + watchdog sub-flag are both on, emit a Class E
            # cancel record so the dispatcher's pre-iteration cancel-check
            # routes the op to POSTMORTEM cleanly. Master-off OR
            # sub-flag-off → no-op (byte-for-byte pre-W3(7) — existing
            # `entry.exceeded=True` flag remains the authoritative signal
            # the orchestrator already consults at line ~3402).
            self._emit_class_e_cancel(op_id, cumulative_usd=entry.cumulative_usd, cap_usd=entry.cap_usd)
        else:
            logger.debug(
                "[CostGovernor] op=%s charge +$%.4f (%s) cumulative=$%.4f / $%.4f",
                op_id[:12], cost_usd, provider or "unknown",
                entry.cumulative_usd, entry.cap_usd,
            )
        return entry.cumulative_usd

    def is_exceeded(self, op_id: str) -> bool:
        """Return True if the op's cumulative spend has reached the cap."""
        if not self._config.enabled:
            return False
        entry = self._entries.get(op_id)
        if entry is None:
            return False
        return entry.exceeded

    def remaining(self, op_id: str) -> float:
        """Return remaining budget for the op, or +inf if no entry."""
        if not self._config.enabled:
            return float("inf")
        entry = self._entries.get(op_id)
        if entry is None:
            return float("inf")
        return max(0.0, entry.cap_usd - entry.cumulative_usd)

    def finish(self, op_id: str) -> Optional[Mapping[str, object]]:
        """Finalize and remove the op entry. Returns summary or None.

        Per-Phase Cost Drill-Down arc (Slice 3): after building the
        summary, dispatch it to every registered finalize observer
        (see :func:`register_finalize_observer`). Observers see the
        authoritative per-phase breakdown before the entry is pruned —
        SessionRecorder persists it into ``summary.json``.
        """
        if not self._config.enabled:
            return None
        entry = self._entries.pop(op_id, None)
        if entry is None:
            return None
        summary = self._summary(entry)
        logger.debug(
            "[CostGovernor] op=%s finished: $%.4f / $%.4f (%d calls)",
            op_id[:12], entry.cumulative_usd, entry.cap_usd, entry.call_count,
        )
        _dispatch_finalize_observers(op_id, summary)
        return summary

    def summary(self, op_id: str) -> Optional[Mapping[str, object]]:
        """Return a structured summary without removing the entry."""
        entry = self._entries.get(op_id)
        if entry is None:
            return None
        return self._summary(entry)

    # --------------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------------

    def _cumulative(self, op_id: str) -> float:
        entry = self._entries.get(op_id)
        return entry.cumulative_usd if entry else 0.0

    @staticmethod
    def _summary(entry: _OpCostEntry) -> Mapping[str, object]:
        return {
            "op_id": entry.op_id,
            "route": entry.route,
            "complexity": entry.complexity,
            "cap_usd": round(entry.cap_usd, 6),
            "cumulative_usd": round(entry.cumulative_usd, 6),
            "remaining_usd": round(max(0.0, entry.cap_usd - entry.cumulative_usd), 6),
            "call_count": entry.call_count,
            "exceeded": entry.exceeded,
            "provider_totals": {
                k: round(v, 6) for k, v in entry.provider_totals.items()
            },
            "factors": {
                "baseline_usd": round(entry.baseline_usd, 6),
                "route_factor": round(entry.route_factor, 4),
                "complexity_factor": round(entry.complexity_factor, 4),
                "retry_headroom": round(entry.retry_headroom, 4),
            },
            # Slice 2 additive keys — consumers that don't know about
            # them safely ignore unknown mapping entries.
            "phase_totals": {
                k: round(v, 6) for k, v in entry.phase_totals.items()
            },
            "phase_by_provider": {
                phase: {p: round(v, 6) for p, v in providers.items()}
                for phase, providers in entry.phase_by_provider.items()
            },
            "unknown_phase_usd": round(entry.unknown_phase_usd, 6),
        }

    # --------------------------------------------------------------
    # Phase drill-down (Per-Phase Cost Drill-Down arc Slice 2)
    # --------------------------------------------------------------

    def get_phase_breakdown(self, op_id: str) -> Optional[Any]:
        """Project an op's current cost state into a
        :class:`PhaseCostBreakdown`. Returns ``None`` when the op
        is not tracked.

        The projection is a snapshot — it does not remove the entry.
        Safe to call multiple times during an op's lifecycle.
        """
        entry = self._entries.get(op_id)
        if entry is None:
            return None
        # Late import avoids a module-load cycle — phase_cost is a
        # leaf module; cost_governor is a prod-critical one.
        from backend.core.ouroboros.governance.phase_cost import (
            breakdown_from_mappings,
        )
        return breakdown_from_mappings(
            op_id=op_id,
            phase_totals=dict(entry.phase_totals),
            phase_by_provider={
                phase: dict(providers)
                for phase, providers in entry.phase_by_provider.items()
            },
            call_count=entry.call_count,
            unknown_phase_usd=entry.unknown_phase_usd,
        )

    def snapshot_all_phase_breakdowns(self) -> Dict[str, Any]:
        """Snapshot every live op's phase breakdown.

        Returns a ``{op_id: PhaseCostBreakdown}`` dict. Empty dict when
        governor is disabled or no ops are active.
        """
        if not self._config.enabled:
            return {}
        return {
            op_id: self.get_phase_breakdown(op_id)  # type: ignore[misc]
            for op_id in list(self._entries.keys())
        }

    def _prune_stale(self) -> int:
        """Prune entries older than ``ttl_s``. Returns count pruned."""
        if not self._entries:
            return 0
        now = time.monotonic()
        ttl = self._config.ttl_s
        stale = [
            op_id for op_id, entry in self._entries.items()
            if now - entry.created_at > ttl
        ]
        for op_id in stale:
            self._entries.pop(op_id, None)
        if stale:
            logger.debug(
                "[CostGovernor] Pruned %d stale entries (ttl=%.0fs)",
                len(stale), ttl,
            )
        return len(stale)

    # --------------------------------------------------------------
    # Test/diagnostic helpers
    # --------------------------------------------------------------

    def active_op_count(self) -> int:
        """Return the number of currently tracked ops (for tests/diagnostics)."""
        return len(self._entries)


# -----------------------------------------------------------------------------
# Finalize observer registry (Per-Phase Cost Drill-Down arc Slice 3)
# -----------------------------------------------------------------------------

# Module-level list — observers are process-global. SessionRecorder is
# the canonical consumer; future surfaces (IDE observability, telemetry
# exporters) can register additional observers. Observers are called
# inside :meth:`CostGovernor.finish` with ``(op_id, summary_mapping)``.
# Exceptions are swallowed — the finalize path is authoritative and
# must never fail because a listener raised.
_finalize_observers: List[
    Callable[[str, Mapping[str, object]], None]
] = []


def register_finalize_observer(
    observer: Callable[[str, Mapping[str, object]], None],
) -> Callable[[], None]:
    """Subscribe to ``CostGovernor.finish`` completion events.

    Returns an unsubscribe callable. Idempotent — the same observer
    may be registered once; subsequent registrations are no-ops.
    Observers receive the authoritative per-phase breakdown
    (``phase_totals`` / ``phase_by_provider`` / ``unknown_phase_usd``)
    embedded in the summary mapping.
    """
    if observer not in _finalize_observers:
        _finalize_observers.append(observer)

    def _unsub() -> None:
        try:
            _finalize_observers.remove(observer)
        except ValueError:
            pass

    return _unsub


def _dispatch_finalize_observers(
    op_id: str, summary: Mapping[str, object],
) -> None:
    for obs in list(_finalize_observers):
        try:
            obs(op_id, summary)
        except Exception:  # noqa: BLE001 — must never escape finalize path
            logger.debug(
                "[CostGovernor] finalize observer raised", exc_info=True,
            )


def reset_finalize_observers() -> None:
    """Test helper — drop every registered observer."""
    _finalize_observers.clear()


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class OpCostCapExceeded(RuntimeError):
    """Raised by orchestrator callers when a cost cap is exceeded.

    Carries the op_id and the governor's structured summary so that the
    caller can route through the phase-aware terminal picker and emit
    full telemetry on abort.
    """

    def __init__(self, op_id: str, summary: Mapping[str, object]) -> None:
        self.op_id = op_id
        self.summary = dict(summary)
        cum = self.summary.get("cumulative_usd", 0.0)
        cap = self.summary.get("cap_usd", 0.0)
        super().__init__(
            f"op_cost_cap_exceeded: op={op_id[:12]} cumulative=${cum} cap=${cap}"
        )
