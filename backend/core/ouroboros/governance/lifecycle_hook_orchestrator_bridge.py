"""Lifecycle Hook Registry — Slice 4 orchestrator bridge.

Five typed gate helpers (one per :class:`LifecycleEvent`) plus a
typed :class:`LifecycleHookGate` result mirroring the established
:class:`DeployGate.preflight` shape (returns result with ``.passed``
field; orchestrator branches on it). No exceptions thrown across
the bridge — every error path maps to a closed-vocabulary gate
result so the orchestrator's existing branch-on-result idiom
composes cleanly.

Architectural reuse — three existing surfaces compose with ZERO
duplication:

  * Slice 1 :class:`HookContext` / :class:`LifecycleEvent` —
    bridge constructs HookContext from orchestrator args
    (op_id + phase + payload) and dispatches via the canonical
    event vocabulary.
  * Slice 3 :func:`fire_hooks` — bridge composes the async
    coordinator. NEVER bypasses; gate semantics derived from
    aggregate.
  * :class:`DeployGate.preflight` (orchestrator existing pattern)
    — bridge mirrors the result-shape contract: caller does
    ``if not gate.passed: ...`` exactly like the deploy gate.

Why a bridge module instead of inlining
---------------------------------------

Each phase boundary needs one async-aware call that returns a
typed gate result. Inlining 5 copies of "build context + call
fire_hooks + interpret aggregate" into orchestrator.py at 5 sites
would be invasive (orchestrator.py is 9725 lines). The bridge
extracts the construction + interpretation logic so each
orchestrator call site is one line:

    gate = await gate_pre_apply(op_id, target_files, ...)
    if not gate.passed:
        # orchestrator routes to CANCELLED via existing
        # CancelToken substrate (mirrors DeployGate failure)

Direct-solve principles
-----------------------

* **Asynchronous-ready** — every gate helper is async; calls
  ``fire_hooks`` directly without re-wrapping.
* **Dynamic** — payload composition is per-event so each gate's
  caller passes only the fields that event needs (PRE_APPLY:
  target_files + diff_summary; POST_VERIFY: pass/fail + duration;
  etc.).
* **Adaptive** — gate helpers NEVER raise. Garbage inputs
  coerced. fire_hooks failures (impossible per its contract,
  but defense-in-depth) → ``passed=True`` with diagnostic
  detail (fail-open: a broken hook system can't block the
  orchestrator).
* **Intelligent** — gate result carries full audit detail
  (blocking_hooks / warning_hooks / failed_hooks names) so the
  orchestrator's logging + SSE bridge see WHO blocked, not just
  THAT something blocked.
* **Robust** — fail-open semantics on bridge errors mirrors the
  Slice 3 executor's "FAILED is non-blocking" contract: a buggy
  hook substrate cannot stop the autonomous loop.
* **No hardcoding** — all 5 gate helpers share one
  ``_compute_gate_from_aggregate`` function; gate semantics
  derived from the aggregate, not duplicated per event.

Authority invariants (AST-pinned by Slice 5):

* MAY import: ``lifecycle_hook`` (Slice 1 primitive),
  ``lifecycle_hook_executor`` (Slice 3 fire_hooks).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor / semantic_guardian /
  semantic_firewall / risk_engine.
* No exec/eval/compile.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.lifecycle_hook import (
    AggregateHookDecision,
    HookContext,
    HookOutcome,
    LifecycleEvent,
)
from backend.core.ouroboros.governance.lifecycle_hook_executor import (
    fire_hooks,
)

logger = logging.getLogger(__name__)


LIFECYCLE_HOOK_BRIDGE_SCHEMA_VERSION: str = (
    "lifecycle_hook_orchestrator_bridge.1"
)


# ---------------------------------------------------------------------------
# Gate result — mirrors DeployGate.preflight shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleHookGate:
    """Typed gate result. Mirrors the operational shape of
    :class:`DeployGate.preflight` so orchestrator call sites
    branch on ``gate.passed`` exactly like existing gates.

    ``passed=True`` means the orchestrator MAY proceed. Even
    when WARN hooks fired, ``passed`` stays True (advisory,
    not blocking).

    ``passed=False`` only on BLOCK aggregate. The orchestrator
    routes to CANCELLED via the existing CancelToken substrate
    (mirrors DeployGate failure).

    Audit detail (blocking_hooks / warning_hooks / failed_hooks)
    flows from Slice 1's :class:`AggregateHookDecision` so
    operators see WHO blocked / warned / failed, not just THAT
    something happened.
    """

    event: LifecycleEvent
    passed: bool
    aggregate: HookOutcome
    total_hooks: int = 0
    blocking_hooks: Tuple[str, ...] = ()
    warning_hooks: Tuple[str, ...] = ()
    failed_hooks: Tuple[str, ...] = ()
    detail: str = ""
    elapsed_ms: float = 0.0
    monotonic_tightening_verdict: str = ""
    schema_version: str = LIFECYCLE_HOOK_BRIDGE_SCHEMA_VERSION

    @property
    def should_warn(self) -> bool:
        """True iff aggregate is WARN — orchestrator should emit
        an SSE warning event but proceed normally."""
        return self.aggregate is HookOutcome.WARN

    @property
    def is_tightening(self) -> bool:
        """True iff this gate represents operator-inserted
        tightening (BLOCK only). Matches Phase C semantics."""
        return self.aggregate is HookOutcome.BLOCK

    def to_dict(self) -> dict:
        return {
            "event": self.event.value,
            "passed": self.passed,
            "aggregate": self.aggregate.value,
            "total_hooks": self.total_hooks,
            "blocking_hooks": list(self.blocking_hooks),
            "warning_hooks": list(self.warning_hooks),
            "failed_hooks": list(self.failed_hooks),
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Sentinel detail for fail-open paths
# ---------------------------------------------------------------------------

#: Detail substring stamped on gate results from defensive
#: fail-open paths (bridge crashed, fire_hooks raised — neither
#: should happen but defense-in-depth). Operators grep for this.
_FAIL_OPEN_DETAIL_PREFIX: str = "bridge_fail_open:"


# ---------------------------------------------------------------------------
# Aggregate → Gate translation
# ---------------------------------------------------------------------------


def _compute_gate_from_aggregate(
    event: LifecycleEvent,
    aggregate: AggregateHookDecision,
    elapsed_ms: float,
) -> LifecycleHookGate:
    """Pure transformer. NEVER raises.

    Decision rule:
      * BLOCK aggregate → ``passed=False``.
      * WARN / CONTINUE → ``passed=True``.

    The fail-isolated executor guarantees we never see something
    other than CONTINUE / BLOCK / WARN here (DISABLED / FAILED
    are per-hook outcomes that aggregate to one of the three),
    but defense-in-depth: any non-BLOCK aggregate is treated as
    passed (fail-open philosophy)."""
    try:
        passed = aggregate.aggregate is not HookOutcome.BLOCK
        detail = (
            f"event={event.value} aggregate={aggregate.aggregate.value} "
            f"hooks={aggregate.total_hooks} "
            f"block={len(aggregate.blocking_hooks)} "
            f"warn={len(aggregate.warning_hooks)} "
            f"failed={len(aggregate.failed_hooks)}"
        )
        return LifecycleHookGate(
            event=event,
            passed=passed,
            aggregate=aggregate.aggregate,
            total_hooks=aggregate.total_hooks,
            blocking_hooks=tuple(aggregate.blocking_hooks),
            warning_hooks=tuple(aggregate.warning_hooks),
            failed_hooks=tuple(aggregate.failed_hooks),
            detail=detail,
            elapsed_ms=max(0.0, float(elapsed_ms)),
            monotonic_tightening_verdict=(
                aggregate.monotonic_tightening_verdict
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[LifecycleHookBridge] _compute_gate_from_aggregate "
            "degraded: %s — fail-open", exc,
        )
        return LifecycleHookGate(
            event=event if isinstance(event, LifecycleEvent)
            else LifecycleEvent.PRE_APPLY,
            passed=True,  # FAIL-OPEN — broken bridge can't block
            aggregate=HookOutcome.CONTINUE,
            detail=f"{_FAIL_OPEN_DETAIL_PREFIX}translator_error:{exc}",
            elapsed_ms=max(0.0, float(elapsed_ms)),
        )


# ---------------------------------------------------------------------------
# Generic gate dispatcher
# ---------------------------------------------------------------------------


async def _gate_event(
    event: LifecycleEvent,
    *,
    op_id: str = "",
    phase: str = "",
    payload: Optional[Mapping[str, Any]] = None,
) -> LifecycleHookGate:
    """Internal — every public gate helper composes through here.
    NEVER raises out (asyncio.CancelledError propagates per
    convention). Defensive fail-open on any bridge-side crash."""
    started_mono = time.monotonic()
    try:
        context = HookContext(
            event=event,
            op_id=str(op_id or ""),
            phase=str(phase or ""),
            payload=dict(payload or {}),
            started_ts=started_mono,
        )
        aggregate = await fire_hooks(event, context)
        elapsed_ms = (time.monotonic() - started_mono) * 1000.0
        return _compute_gate_from_aggregate(event, aggregate, elapsed_ms)
    except Exception as exc:  # noqa: BLE001 — defensive contract
        elapsed_ms = (time.monotonic() - started_mono) * 1000.0
        logger.warning(
            "[LifecycleHookBridge] gate_event %s degraded: %s — "
            "fail-open", event, exc,
        )
        return LifecycleHookGate(
            event=event if isinstance(event, LifecycleEvent)
            else LifecycleEvent.PRE_APPLY,
            passed=True,  # FAIL-OPEN
            aggregate=HookOutcome.CONTINUE,
            detail=f"{_FAIL_OPEN_DETAIL_PREFIX}fire_hooks_error:{exc}",
            elapsed_ms=elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Public per-event gate helpers — orchestrator-callable surface
# ---------------------------------------------------------------------------


async def gate_pre_generate(
    op_id: str,
    *,
    route: str = "",
    cost_estimate_usd: float = 0.0,
    extra: Optional[Mapping[str, Any]] = None,
) -> LifecycleHookGate:
    """Fire PRE_GENERATE hooks. Use case: pre-spend check,
    generation-quality A/B routing.

    Caller may BLOCK to skip generation entirely (e.g., budget
    exceeded). NEVER raises."""
    payload = dict(extra or {})
    payload["route"] = str(route or "")
    payload["cost_estimate_usd"] = float(cost_estimate_usd or 0.0)
    return await _gate_event(
        LifecycleEvent.PRE_GENERATE,
        op_id=op_id, phase="GENERATE",
        payload=payload,
    )


async def gate_pre_apply(
    op_id: str,
    *,
    target_files: Optional[Tuple[str, ...]] = None,
    diff_summary: str = "",
    risk_tier: str = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> LifecycleHookGate:
    """Fire PRE_APPLY hooks. THE most common operator gate.
    Caller routes to CANCELLED on BLOCK via CancelToken.

    Use case: license-check, security-scan, slack-notify-then-block.
    NEVER raises."""
    payload = dict(extra or {})
    payload["target_files"] = list(target_files or ())
    payload["diff_summary"] = str(diff_summary or "")[:1000]
    payload["risk_tier"] = str(risk_tier or "")
    return await _gate_event(
        LifecycleEvent.PRE_APPLY,
        op_id=op_id, phase="APPLY",
        payload=payload,
    )


async def gate_post_apply(
    op_id: str,
    *,
    applied_files: Optional[Tuple[str, ...]] = None,
    apply_mode: str = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> LifecycleHookGate:
    """Fire POST_APPLY hooks. Files are written; this is
    notification-shaped (BLOCK is structurally moot — files
    already on disk). Use case: external indexer refresh,
    audit trail emission, Slack notification.

    NEVER raises."""
    payload = dict(extra or {})
    payload["applied_files"] = list(applied_files or ())
    payload["apply_mode"] = str(apply_mode or "")
    return await _gate_event(
        LifecycleEvent.POST_APPLY,
        op_id=op_id, phase="APPLY",
        payload=payload,
    )


async def gate_post_verify(
    op_id: str,
    *,
    verify_passed: bool = True,
    duration_s: float = 0.0,
    extra: Optional[Mapping[str, Any]] = None,
) -> LifecycleHookGate:
    """Fire POST_VERIFY hooks. Use case: completion webhook,
    Datadog metric, Slack notification.

    NEVER raises."""
    payload = dict(extra or {})
    payload["verify_passed"] = bool(verify_passed)
    payload["duration_s"] = float(duration_s or 0.0)
    return await _gate_event(
        LifecycleEvent.POST_VERIFY,
        op_id=op_id, phase="VERIFY",
        payload=payload,
    )


async def gate_on_operator_action(
    op_id: str,
    *,
    action: str,
    actor: str = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> LifecycleHookGate:
    """Fire ON_OPERATOR_ACTION hooks. Operator typed
    /cancel /allow /deny /pause via REPL or HTTP. Use case:
    audit external user actions.

    NEVER raises."""
    payload = dict(extra or {})
    payload["action"] = str(action or "")
    payload["actor"] = str(actor or "")
    return await _gate_event(
        LifecycleEvent.ON_OPERATOR_ACTION,
        op_id=op_id, phase="OPERATOR",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "LIFECYCLE_HOOK_BRIDGE_SCHEMA_VERSION",
    "LifecycleHookGate",
    "gate_on_operator_action",
    "gate_post_apply",
    "gate_post_verify",
    "gate_pre_apply",
    "gate_pre_generate",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Register Slice 4's structural invariants. Discovered
    automatically. Returns :class:`ShippedCodeInvariant` instances."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_authority_allowlist(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 4 may import only Slice 1 + Slice 3."""
        violations: list = []
        allowed = {
            "backend.core.ouroboros.governance.lifecycle_hook",
            "backend.core.ouroboros.governance.lifecycle_hook_executor",
        }
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
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                for ban in banned_substrings:
                    if ban in module:
                        violations.append(
                            f"line {lineno}: BANNED orchestrator-tier "
                            f"substring {ban!r} in {module!r}"
                        )
                if "backend." in module or (
                    "governance" in module and module
                ):
                    if module not in allowed:
                        violations.append(
                            f"line {lineno}: import outside Slice 4 "
                            f"allowlist: {module!r}"
                        )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"MUST NOT {node.func.id}()"
                        )
        return tuple(violations)

    def _validate_fail_open_sentinel(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        """Critical safety property: Slice 4 bridge MUST stamp
        ``_FAIL_OPEN_DETAIL_PREFIX`` on the gate result when the
        bridge crashes (broken hook substrate cannot block the
        autonomous loop)."""
        violations: list = []
        if "_FAIL_OPEN_DETAIL_PREFIX" not in source:
            violations.append(
                "bridge must define _FAIL_OPEN_DETAIL_PREFIX "
                "sentinel for fail-open detail stamping"
            )
        if 'passed=True' not in source:
            violations.append(
                "bridge must contain a passed=True path "
                "(fail-open philosophy)"
            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "lifecycle_hook_orchestrator_bridge.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_bridge_authority_allowlist",
            target_file=target,
            description=(
                "Slice 4 bridge imports stay within "
                "{lifecycle_hook, lifecycle_hook_executor} (+ "
                "registration-contract exemption). Banned: "
                "orchestrator-tier."
            ),
            validate=_validate_authority_allowlist,
        ),
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_bridge_fail_open",
            target_file=target,
            description=(
                "Slice 4 bridge must implement fail-open: gate "
                "returns passed=True with _FAIL_OPEN_DETAIL_PREFIX "
                "sentinel on bridge crash. Critical safety "
                "property — broken hook substrate cannot block."
            ),
            validate=_validate_fail_open_sentinel,
        ),
    ]
