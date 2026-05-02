"""Lifecycle Hook Registry — Slice 1 pure-stdlib decision primitive.

The pure-data foundation for operator-defined hooks that fire on
orchestrator phase boundaries (CC's PreToolUse / PostToolUse model,
adapted for O+V's autonomous loop). Slice 1 ships only the
primitive layer — closed taxonomies, frozen dataclasses, total
decision functions. Slice 2 adds the registry; Slice 3 the async
executor; Slice 4 wires into orchestrator phase boundaries;
Slice 5 graduates.

Architectural reuse pattern (no duplication)
--------------------------------------------

* :class:`LifecycleEvent` mirrors the closed-taxonomy enum shape
  used by every prior Slice 1 primitive (Move 5 ProbeOutcome,
  Move 6 ConsensusOutcome, Priority #1 BehavioralDriftKind,
  InlinePromptGate PhaseInlineVerdict, SBT-Probe Escalation
  EscalationDecision).
* :class:`HookContext` is the read-only payload an executor hands
  the hook callable — frozen dataclass propagation-safe across
  asyncio boundaries.
* :class:`HookResult` is what each hook returns; the aggregator
  composes results via :func:`compute_hook_decision` with
  BLOCK-wins semantics.
* Phase C ``MonotonicTighteningVerdict.PASSED`` stamping is
  outcome-aware: BLOCK is structural tightening (operator-inserted
  friction blocking phase transition); WARN/CONTINUE/DISABLED/FAILED
  are not.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — frozen dataclasses propagate cleanly
  through ``asyncio.gather`` / ``asyncio.wait_for``; Slice 3
  runner calls each hook concurrently bounded by per-hook timeout.
* **Dynamic** — every numeric (max-hooks-per-event, default
  timeout) clamped floor + ceiling via env helpers. NO hardcoded
  magic constants in decision logic.
* **Adaptive** — degraded inputs (None hook, garbage event,
  unknown outcome) all map to closed-taxonomy values rather than
  raises. Hooks that raise → FAILED + log.
* **Intelligent** — BLOCK-wins aggregation is total: if ANY hook
  in a same-event batch returns BLOCK, the aggregate is BLOCK.
  WARN aggregates count for SSE telemetry. CONTINUE / DISABLED /
  FAILED are non-blocking; operators see them in audit but the
  phase proceeds.
* **Robust** — every public function NEVER raises. Pure-data
  primitive callable from any context.
* **No hardcoding** — 5-value closed taxonomies; per-knob env
  helpers with floor + ceiling; event-vocabulary stamped with
  byte-parity to documented constants (Slice 5 AST pin will
  assert the 5-value invariant).

Authority invariants (AST-pinned by Slice 5):

* Imports stdlib ONLY at hot path. NEVER imports any governance
  module — strongest authority invariant. Module-owned
  ``register_flags`` / ``register_shipped_invariants`` exempt
  (registration-contract exemption from Priority #6 closure).
* No async (Slice 3 runner wraps via asyncio).
* No exec/eval/compile (mirrors every prior Slice 1 critical
  safety pin).
* Hooks themselves are NOT primitives — they live in operator
  config; the primitive only knows their VERDICT shape.

Master flag default-FALSE until Slice 5 graduation:
``JARVIS_LIFECYCLE_HOOKS_ENABLED``. Asymmetric env semantics —
empty/whitespace = unset = current default; explicit truthy/falsy
overrides at call time.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


LIFECYCLE_HOOK_SCHEMA_VERSION: str = "lifecycle_hook.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def lifecycle_hooks_enabled() -> bool:
    """``JARVIS_LIFECYCLE_HOOKS_ENABLED`` (default ``true`` —
    graduated 2026-05-02 in Lifecycle Hook Registry Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default; explicit ``0``/``false``/``no``/``off`` evaluates false;
    explicit truthy values evaluate true. Re-read on every call so
    flips hot-revert without restart.

    Graduated default-true matches established Move 5 / Move 6 /
    Priority #1-#5 / Priority #6 / InlinePromptGate / SBT-Probe
    Escalation discipline:
      * Slice 1 primitive (pure-stdlib) shipped 2026-05-02.
      * Slice 2 sync registry shipped 2026-05-02.
      * Slice 3 async executor shipped 2026-05-02.
      * Slice 4 orchestrator bridge + PRE_APPLY wire-up shipped
        2026-05-02.
      * 197/197 combined sweep + 23 wire-up smoke tests passed.
      * Two layers of fail-open by construction (Slice 3 FAILED-
        is-non-blocking + Slice 4 bridge passed=True on crash):
        a buggy hook substrate cannot block the orchestrator.
      * No hooks registered today (operators add their own via
        register_lifecycle_hooks(registry) module-owned contract);
        graduation activates the SUBSTRATE, not any specific hook.
    """
    raw = os.environ.get(
        "JARVIS_LIFECYCLE_HOOKS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5, 2026-05-02)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped (floor + ceiling)
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def max_hooks_per_event() -> int:
    """``JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT`` — defense cap on
    the number of hooks that can be registered for ONE event.
    Floor 1, ceiling 256, default 16. Prevents a misconfigured
    plugin from registering thousands of hooks and paying
    proportional cost on every phase boundary."""
    return _env_int_clamped(
        "JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT",
        default=16, floor=1, ceiling=256,
    )


def default_hook_timeout_s() -> float:
    """``JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S`` — per-hook
    wall-clock timeout default. Floor 0.1s, ceiling 60s, default
    5s. Hooks should be fast (notification / log / cheap policy
    check); long-running side effects belong elsewhere. Slice 3
    runner enforces via ``asyncio.wait_for``."""
    return _env_float_clamped(
        "JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S",
        default=5.0, floor=0.1, ceiling=60.0,
    )


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value LifecycleEvent (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class LifecycleEvent(str, enum.Enum):
    """Closed 5-value taxonomy of orchestrator lifecycle events
    that can fire hooks. Every event maps to exactly one phase
    boundary or operator action.

    Slice 4 wires these into the orchestrator at the named
    boundaries. New events require explicit scope-doc + Slice 4
    wire-up update — adding a value here without a wire-up
    silently produces dead hooks.

    * :attr:`PRE_GENERATE` — before the model is invoked. Hook
      sees the planned route + cost estimate. Use case: pre-spend
      check, generation-quality A/B routing.
    * :attr:`PRE_APPLY` — before any file write. The most common
      operator gate. Hook sees the candidate's diff summary.
      Use case: license-check, security-scan, slack-notify-then-block.
    * :attr:`POST_APPLY` — after files written, before VERIFY.
      Hook sees the applied paths. Use case: external indexer
      refresh, audit trail emission.
    * :attr:`POST_VERIFY` — after VERIFY phase. Hook sees the
      verify result (pass / fail). Use case: completion webhook,
      Datadog metric, Slack notification.
    * :attr:`ON_OPERATOR_ACTION` — operator typed /cancel /allow
      /deny /pause via REPL or HTTP. Hook sees the action +
      target op. Use case: audit external user actions.
    """

    PRE_GENERATE = "pre_generate"
    PRE_APPLY = "pre_apply"
    POST_APPLY = "post_apply"
    POST_VERIFY = "post_verify"
    ON_OPERATOR_ACTION = "on_operator_action"


_VALID_EVENTS: frozenset = frozenset({e.value for e in LifecycleEvent})


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value HookOutcome (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class HookOutcome(str, enum.Enum):
    """Closed 5-value taxonomy. Every hook return maps to exactly
    one outcome.

    * :attr:`CONTINUE` — hook ran cleanly; phase proceeds normally.
      Default for any hook that returns successfully without an
      explicit BLOCK / WARN.
    * :attr:`BLOCK` — operator-defined gate said no; phase MUST
      NOT proceed. Slice 4 orchestrator wire-up routes BLOCK to
      a CANCELLED phase via the existing CancelToken substrate.
      The strongest hook signal.
    * :attr:`WARN` — advisory only; phase proceeds but operators
      see an SSE warning event + an entry in the audit ledger.
      Use case: low-severity policy violation that shouldn't
      block but should be visible.
    * :attr:`DISABLED` — hook is registered but its own enable
      check returned false (e.g., hook-local feature flag).
      Distinct from FAILED so observability can tell "hook said
      not now" from "hook crashed".
    * :attr:`FAILED` — defensive sentinel. Hook raised an
      exception (Slice 3 runner catches at the boundary).
      Aggregator treats FAILED as non-blocking: a buggy hook
      cannot stop the orchestrator.
    """

    CONTINUE = "continue"
    BLOCK = "block"
    WARN = "warn"
    DISABLED = "disabled"
    FAILED = "failed"


_VALID_OUTCOMES: frozenset = frozenset({o.value for o in HookOutcome})

#: Outcomes that constitute structural tightening — operator-inserted
#: friction blocking the phase. BLOCK is the only one. WARN is
#: advisory; CONTINUE/DISABLED/FAILED are non-events for tightening.
_TIGHTENING_OUTCOMES: frozenset = frozenset({HookOutcome.BLOCK})

#: Outcomes that count as "hook actively did something". Used by
#: aggregator + observability to distinguish active-blocks from
#: passthrough.
_ACTIVE_OUTCOMES: frozenset = frozenset({
    HookOutcome.BLOCK, HookOutcome.WARN,
})


# ---------------------------------------------------------------------------
# Phase C MonotonicTighteningVerdict canonical string
# ---------------------------------------------------------------------------

#: Canonical string from ``adaptation.ledger.MonotonicTighteningVerdict``.
#: Slice 5 graduation pin asserts byte-parity to live enum.
_TIGHTENING_PASSED_STR: str = "passed"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookContext:
    """Read-only payload handed to a hook callable. Frozen so
    propagation across async boundaries is safe (Slice 3 runner
    hands the same context to N hooks concurrently).

    The ``payload`` field is a free-form mapping carrying
    event-specific data (for PRE_APPLY: diff summary + target paths;
    for POST_VERIFY: pass/fail + duration; for ON_OPERATOR_ACTION:
    action verb + target op). Hooks SHOULD treat payload as
    read-only — mutating it does NOT propagate (frozen dataclass)."""

    event: LifecycleEvent
    op_id: str = ""
    phase: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    started_ts: float = 0.0
    schema_version: str = LIFECYCLE_HOOK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event.value,
            "op_id": self.op_id,
            "phase": self.phase,
            "payload": dict(self.payload),
            "started_ts": self.started_ts,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "HookContext":
        try:
            ev_raw = str(
                d.get("event", LifecycleEvent.PRE_APPLY.value),
            )
            try:
                event = LifecycleEvent(ev_raw)
            except ValueError:
                event = LifecycleEvent.PRE_APPLY
            payload_raw = d.get("payload", {})
            payload = (
                dict(payload_raw)
                if isinstance(payload_raw, Mapping) else {}
            )
            return cls(
                event=event,
                op_id=str(d.get("op_id", "")),
                phase=str(d.get("phase", "")),
                payload=payload,
                started_ts=float(d.get("started_ts", 0.0) or 0.0),
                schema_version=str(
                    d.get("schema_version", LIFECYCLE_HOOK_SCHEMA_VERSION),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[LifecycleHook] HookContext from_dict degraded: %s",
                exc,
            )
            return cls(event=LifecycleEvent.PRE_APPLY)


@dataclass(frozen=True)
class HookResult:
    """One hook's terminal verdict. Frozen for safe propagation.

    ``hook_name`` flows from the registry's identifier. ``detail``
    is operator-readable + bounded.

    ``monotonic_tightening_verdict`` is the canonical Phase C
    string — populated to ``"passed"`` on BLOCK outcomes only
    (operator-inserted friction blocking phase transition). All
    other outcomes stamp empty (advisory or no-op)."""

    hook_name: str
    outcome: HookOutcome
    detail: str = ""
    elapsed_ms: float = 0.0
    monotonic_tightening_verdict: str = ""
    schema_version: str = LIFECYCLE_HOOK_SCHEMA_VERSION

    @property
    def is_blocking(self) -> bool:
        return self.outcome is HookOutcome.BLOCK

    @property
    def is_active(self) -> bool:
        """True iff the hook actively did something (BLOCK or
        WARN). CONTINUE / DISABLED / FAILED are passthrough."""
        return self.outcome in _ACTIVE_OUTCOMES

    @property
    def is_tightening(self) -> bool:
        return self.outcome in _TIGHTENING_OUTCOMES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hook_name": self.hook_name,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "HookResult":
        try:
            out_raw = str(d.get("outcome", HookOutcome.FAILED.value))
            try:
                outcome = HookOutcome(out_raw)
            except ValueError:
                outcome = HookOutcome.FAILED
            return cls(
                hook_name=str(d.get("hook_name", "")),
                outcome=outcome,
                detail=str(d.get("detail", "")),
                elapsed_ms=max(
                    0.0, float(d.get("elapsed_ms", 0.0) or 0.0),
                ),
                monotonic_tightening_verdict=str(
                    d.get("monotonic_tightening_verdict", ""),
                ),
                schema_version=str(
                    d.get("schema_version", LIFECYCLE_HOOK_SCHEMA_VERSION),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[LifecycleHook] HookResult from_dict degraded: %s",
                exc,
            )
            return cls(hook_name="", outcome=HookOutcome.FAILED)


@dataclass(frozen=True)
class AggregateHookDecision:
    """Composed verdict over a tuple of HookResults from one
    event firing. Frozen for safe propagation.

    Aggregation rules (deterministic, BLOCK-wins):
      * ANY result with outcome=BLOCK → aggregate=BLOCK
        (any blocking hook stops the phase).
      * No BLOCK + any WARN → aggregate=WARN (advisory).
      * All CONTINUE / DISABLED / FAILED → aggregate=CONTINUE
        (passthrough).
      * Empty result tuple → aggregate=CONTINUE (no hooks
        registered for this event).

    ``blocking_hooks`` carries the names of the hooks that voted
    BLOCK so operators see WHO blocked, not just THAT something
    blocked. ``warning_hooks`` similarly for WARN.
    """

    event: LifecycleEvent
    aggregate: HookOutcome
    total_hooks: int = 0
    blocking_hooks: Tuple[str, ...] = ()
    warning_hooks: Tuple[str, ...] = ()
    failed_hooks: Tuple[str, ...] = ()
    monotonic_tightening_verdict: str = ""
    schema_version: str = LIFECYCLE_HOOK_SCHEMA_VERSION

    @property
    def is_blocking(self) -> bool:
        return self.aggregate is HookOutcome.BLOCK

    @property
    def is_tightening(self) -> bool:
        return self.is_blocking

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event.value,
            "aggregate": self.aggregate.value,
            "total_hooks": self.total_hooks,
            "blocking_hooks": list(self.blocking_hooks),
            "warning_hooks": list(self.warning_hooks),
            "failed_hooks": list(self.failed_hooks),
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Total aggregation function — BLOCK-wins semantics
# ---------------------------------------------------------------------------


def compute_hook_decision(
    event: LifecycleEvent,
    results: Tuple[HookResult, ...],
) -> AggregateHookDecision:
    """Total aggregation — every (event × results-tuple) maps to
    exactly one :class:`AggregateHookDecision`. NEVER raises.

    BLOCK-wins semantics:
      1. Empty results → CONTINUE (no hooks for this event).
      2. Any BLOCK in results → BLOCK aggregate.
      3. No BLOCK + any WARN → WARN aggregate.
      4. All CONTINUE / DISABLED / FAILED → CONTINUE aggregate.

    Phase C tightening stamping:
      BLOCK → ``"passed"`` (operator-inserted friction blocking
        phase transition is structural tightening).
      WARN / CONTINUE → empty (advisory or no-op).

    Garbage input (non-tuple results, non-HookResult elements)
    handled defensively — non-conforming entries dropped, missing
    event coerced to PRE_APPLY (the most common phase boundary).
    """
    try:
        # Coerce event defensively.
        if not isinstance(event, LifecycleEvent):
            try:
                event = LifecycleEvent(str(event))
            except (ValueError, TypeError):
                logger.warning(
                    "[LifecycleHook] non-event %r — coercing to "
                    "PRE_APPLY", event,
                )
                event = LifecycleEvent.PRE_APPLY

        # Coerce results defensively.
        if results is None:
            results = ()
        if not isinstance(results, tuple):
            try:
                results = tuple(results)
            except (TypeError, ValueError):
                results = ()

        valid_results = tuple(
            r for r in results if isinstance(r, HookResult)
        )
        total_hooks = len(valid_results)

        if total_hooks == 0:
            return AggregateHookDecision(
                event=event,
                aggregate=HookOutcome.CONTINUE,
                total_hooks=0,
                monotonic_tightening_verdict="",
            )

        blocking = tuple(
            r.hook_name for r in valid_results
            if r.outcome is HookOutcome.BLOCK
        )
        warning = tuple(
            r.hook_name for r in valid_results
            if r.outcome is HookOutcome.WARN
        )
        failed = tuple(
            r.hook_name for r in valid_results
            if r.outcome is HookOutcome.FAILED
        )

        if blocking:
            aggregate = HookOutcome.BLOCK
            tightening = _TIGHTENING_PASSED_STR
        elif warning:
            aggregate = HookOutcome.WARN
            tightening = ""
        else:
            aggregate = HookOutcome.CONTINUE
            tightening = ""

        return AggregateHookDecision(
            event=event,
            aggregate=aggregate,
            total_hooks=total_hooks,
            blocking_hooks=blocking,
            warning_hooks=warning,
            failed_hooks=failed,
            monotonic_tightening_verdict=tightening,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[LifecycleHook] compute_hook_decision last-resort "
            "degraded: %s", exc,
        )
        return AggregateHookDecision(
            event=event if isinstance(event, LifecycleEvent)
            else LifecycleEvent.PRE_APPLY,
            aggregate=HookOutcome.CONTINUE,
            total_hooks=0,
            monotonic_tightening_verdict="",
        )


# ---------------------------------------------------------------------------
# Result construction helper — convenience for hook authors
# ---------------------------------------------------------------------------


def make_hook_result(
    hook_name: str,
    outcome: HookOutcome,
    *,
    detail: str = "",
    elapsed_ms: float = 0.0,
) -> HookResult:
    """Convenience constructor that auto-stamps the Phase C
    tightening verdict per outcome. Hook authors call this rather
    than constructing :class:`HookResult` directly so the
    tightening field stays consistent with the closed-taxonomy
    contract. NEVER raises."""
    try:
        tightening = (
            _TIGHTENING_PASSED_STR if outcome in _TIGHTENING_OUTCOMES
            else ""
        )
        return HookResult(
            hook_name=str(hook_name or "")[:128],
            outcome=outcome if isinstance(outcome, HookOutcome)
            else HookOutcome.FAILED,
            detail=str(detail or "")[:1000],
            elapsed_ms=max(0.0, float(elapsed_ms or 0.0)),
            monotonic_tightening_verdict=tightening,
        )
    except Exception:  # noqa: BLE001 — defensive
        return HookResult(
            hook_name="", outcome=HookOutcome.FAILED,
        )


# ---------------------------------------------------------------------------
# Public surface — Slice 5 will pin via shipped_code_invariants
# ---------------------------------------------------------------------------

__all__ = [
    "AggregateHookDecision",
    "HookContext",
    "HookOutcome",
    "HookResult",
    "LIFECYCLE_HOOK_SCHEMA_VERSION",
    "LifecycleEvent",
    "compute_hook_decision",
    "default_hook_timeout_s",
    "lifecycle_hooks_enabled",
    "make_hook_result",
    "max_hooks_per_event",
    "register_flags",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned FlagRegistry contribution (3 flags)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned :class:`FlagRegistry` registration. Discovered
    automatically by ``flag_registry_seed._discover_module_provided_flags``.
    Returns count registered."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[LifecycleHook] register_flags degraded: %s", exc,
        )
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_LIFECYCLE_HOOKS_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/lifecycle_hook.py"
            ),
            example="JARVIS_LIFECYCLE_HOOKS_ENABLED=true",
            description=(
                "Master switch for the Lifecycle Hook Registry. "
                "When on, operator-defined hooks fire on the 5 "
                "phase boundaries (PRE_GENERATE / PRE_APPLY / "
                "POST_APPLY / POST_VERIFY / ON_OPERATOR_ACTION). "
                "Two layers of fail-open by construction protect "
                "against buggy hooks blocking the autonomous loop. "
                "Graduated default-true 2026-05-02 in Slice 5."
            ),
        ),
        FlagSpec(
            name="JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT",
            type=FlagType.INT, default=16,
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/lifecycle_hook.py"
            ),
            example="JARVIS_LIFECYCLE_HOOKS_MAX_PER_EVENT=32",
            description=(
                "Defense cap on the number of hooks that can be "
                "registered for ONE event. Prevents misconfigured "
                "plugins from registering thousands. Floor 1, "
                "ceiling 256, default 16."
            ),
        ),
        FlagSpec(
            name="JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S",
            type=FlagType.FLOAT, default=5.0,
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/lifecycle_hook.py"
            ),
            example="JARVIS_LIFECYCLE_HOOKS_DEFAULT_TIMEOUT_S=10",
            description=(
                "Per-hook wall-clock timeout default. Hooks should "
                "be fast (notification / log / cheap policy check); "
                "long-running side effects belong elsewhere. Slice 3 "
                "executor enforces via asyncio.wait_for. Floor 0.1s, "
                "ceiling 60s, default 5s."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[LifecycleHook] register_flags spec %s skipped: "
                "%s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Register Slice 1's structural invariants. Discovered
    automatically. Returns :class:`ShippedCodeInvariant` instances."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_pure_stdlib(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 1 stays pure-stdlib at hot path (registration-
        contract exemption applies)."""
        violations: list = []
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    violations.append(
                        f"line {lineno}: Slice 1 must be pure-stdlib "
                        f"— found {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"MUST NOT {node.func.id}()"
                        )
            if isinstance(node, _ast.AsyncFunctionDef):
                violations.append(
                    f"line {getattr(node, 'lineno', '?')}: Slice 1 "
                    f"must remain sync — found async def {node.name!r}"
                )
        return tuple(violations)

    def _validate_event_taxonomy_5_values(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Closed-taxonomy invariant: LifecycleEvent has exactly
        5 values. New events require explicit Slice 4 wire-up."""
        violations: list = []
        required = {
            "PRE_GENERATE", "PRE_APPLY", "POST_APPLY",
            "POST_VERIFY", "ON_OPERATOR_ACTION",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                if node.name == "LifecycleEvent":
                    seen = set()
                    for stmt in node.body:
                        if isinstance(stmt, _ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, _ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"LifecycleEvent missing: {sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"LifecycleEvent unexpected values "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_outcome_taxonomy_5_values(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Closed-taxonomy: HookOutcome has exactly 5 values."""
        violations: list = []
        required = {
            "CONTINUE", "BLOCK", "WARN", "DISABLED", "FAILED",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                if node.name == "HookOutcome":
                    seen = set()
                    for stmt in node.body:
                        if isinstance(stmt, _ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, _ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"HookOutcome missing: {sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"HookOutcome unexpected values "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/lifecycle_hook.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_pure_stdlib",
            target_file=target,
            description=(
                "Slice 1 primitive stays pure-stdlib at hot path "
                "(no governance imports outside register_flags / "
                "register_shipped_invariants, no async, no "
                "exec/eval/compile)."
            ),
            validate=_validate_pure_stdlib,
        ),
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_event_taxonomy_5_values",
            target_file=target,
            description=(
                "LifecycleEvent is a 5-value closed taxonomy "
                "(PRE_GENERATE / PRE_APPLY / POST_APPLY / "
                "POST_VERIFY / ON_OPERATOR_ACTION). New events "
                "require explicit Slice 4 wire-up."
            ),
            validate=_validate_event_taxonomy_5_values,
        ),
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_outcome_taxonomy_5_values",
            target_file=target,
            description=(
                "HookOutcome is a 5-value closed taxonomy "
                "(CONTINUE / BLOCK / WARN / DISABLED / FAILED). "
                "BLOCK is the only blocking outcome."
            ),
            validate=_validate_outcome_taxonomy_5_values,
        ),
    ]
