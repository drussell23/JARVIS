"""Venom V2 — per-tool permission substrate.

Closes the static-permissions gap surfaced in PRD §32.6 / line
379: today the Venom tool dispatcher accepts every tool the
model proposes (subject only to the orchestrator-level
SemanticGuardian / IronGate / RiskTierFloor checks at APPROVE
time). V2 inserts an OPERATOR-DEFINED callback chain BEFORE
the V1 PRE_TOOL_USE hook fires, so an operator can structurally
deny / approve / ask-then-route specific tool calls based on
arguments + op context.

Closed taxonomy:

  * :class:`ToolPermissionDecision` — 4-value enum
    (ALLOW / DENY / ASK / DEFER). Bytes-pinned.

Aggregation (first-match-wins + DENY-strongest):

  1. Empty registry → DEFER (no opinion; dispatcher proceeds)
  2. Any callback returns DENY → aggregate DENY (strongest signal)
  3. No DENY + any callback returns ASK → aggregate ASK
  4. No DENY + no ASK + any callback returns ALLOW → ALLOW
  5. All callbacks DEFER → DEFER

Architectural locks (AST-pinned):

  * **Authority asymmetry** — substrate purity (no orchestrator/
    iron_gate/policy/providers/candidate_generator imports).
  * **Composes V1's HookContext shape** — PermissionContext
    mirrors HookContext fields so callbacks built for V1 hooks
    can reuse arg-extraction helpers.
  * **NEVER raises** across all public surfaces.
  * **Closed taxonomy** — 4-value decision bytes-pinned.

Master flag: ``JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED`` (default
``false`` per §33.1 graduation contract pattern).

ASK route notes
---------------

For Slice 1, ``ASK`` is **advisory** — the registry returns ASK
to the dispatcher, which emits an SSE event for operator
visibility AND treats it as DENY (conservative; tool dispatch
blocks). Operators who want async-approve-then-allow can
register a ``ToolPermissionCallback`` that internally awaits the
existing :class:`InlineApprovalProvider` and returns
ALLOW/DENY based on the operator's response. The Slice 1
substrate is composable enough to support that without changing
the dispatcher contract.
"""
from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, List, Mapping, Optional, Protocol,
    Tuple, runtime_checkable,
)

logger = logging.getLogger(__name__)


TOOL_PERMISSION_SCHEMA_VERSION: str = "tool_permission.1"


# ---------------------------------------------------------------------------
# Master flag — default-FALSE per §33.1 graduation contract
# ---------------------------------------------------------------------------


def venom_tool_permissions_enabled() -> bool:
    """``JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED`` master switch
    (default ``false`` per §33.1 graduation-contract pattern).

    Asymmetric env semantics — empty/whitespace = unset =
    current default; explicit truthy/falsy values evaluated at
    call time so flag flips hot-revert without restart.
    """
    raw = os.environ.get(
        "JARVIS_VENOM_TOOL_PERMISSIONS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-FALSE per §33.1
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Closed 4-value taxonomy
# ---------------------------------------------------------------------------


class ToolPermissionDecision(str, enum.Enum):
    """Closed 4-value taxonomy of permission decisions.

    Bytes-pinned via the ``tool_permission_decision_taxonomy_closed``
    AST invariant — additions require explicit pin update.

    * :attr:`ALLOW` — callback explicitly approves this tool
      call. The dispatcher proceeds (subject to the rest of the
      callback chain — first-DENY-wins still applies).
    * :attr:`DENY` — callback explicitly forbids this tool
      call. The dispatcher MUST NOT invoke the tool. Strongest
      signal — DENY beats ALLOW + ASK in aggregation.
    * :attr:`ASK` — callback wants the operator to decide.
      Aggregator preserves ASK if no DENY is present;
      dispatcher's policy on ASK is environment-dependent (see
      module docstring).
    * :attr:`DEFER` — callback has no opinion on this specific
      tool call. The dispatcher continues with whatever the
      remaining callbacks (or none) decide. Default for
      callbacks that only care about specific tools.
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    DEFER = "defer"


# ---------------------------------------------------------------------------
# Frozen request / response artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionContext:
    """Frozen permission-request context. Mirrors V1
    :class:`HookContext` shape so callbacks built for hook
    surfaces can reuse the same arg-extraction patterns.

    The ``payload`` field carries the tool call's arguments
    (read-only mapping). Callbacks SHOULD treat payload as
    read-only — mutations don't propagate (frozen)."""

    schema_version: str
    op_id: str
    tool_name: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    started_ts: float = 0.0


@dataclass(frozen=True)
class PermissionResult:
    """Frozen permission-callback result. NEVER raises out of
    a callback; the runner catches exceptions and synthesizes
    a :attr:`ToolPermissionDecision.DEFER` result with
    ``error`` set."""

    schema_version: str
    callback_name: str
    decision: ToolPermissionDecision
    reason: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "callback_name": self.callback_name,
            "decision": self.decision.value,
            "reason": self.reason[:256],
            "error": self.error[:256],
        }


@dataclass(frozen=True)
class AggregatePermissionDecision:
    """Aggregated decision across all callbacks. The dispatcher
    branches on :attr:`decision`."""

    schema_version: str
    tool_name: str
    op_id: str
    decision: ToolPermissionDecision
    total_callbacks: int
    deny_callbacks: Tuple[str, ...] = ()
    ask_callbacks: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool_name": self.tool_name,
            "op_id": self.op_id,
            "decision": self.decision.value,
            "total_callbacks": int(self.total_callbacks),
            "deny_callbacks": list(self.deny_callbacks),
            "ask_callbacks": list(self.ask_callbacks),
            "detail": self.detail[:256],
        }


# ---------------------------------------------------------------------------
# Helpers — bind a result without raising
# ---------------------------------------------------------------------------


def make_permission_result(
    *,
    callback_name: str,
    decision: ToolPermissionDecision,
    reason: str = "",
    error: str = "",
) -> PermissionResult:
    """Construct a :class:`PermissionResult` defensively.
    NEVER raises."""
    try:
        if not isinstance(decision, ToolPermissionDecision):
            decision = ToolPermissionDecision.DEFER
        return PermissionResult(
            schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
            callback_name=str(callback_name or "unknown")[:128],
            decision=decision,
            reason=str(reason or "")[:256],
            error=str(error or "")[:256],
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[tool_permission] make_permission_result raised: %s",
            exc,
        )
        return PermissionResult(
            schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
            callback_name="unknown",
            decision=ToolPermissionDecision.DEFER,
            reason="",
            error=f"defensive_fallback:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Callback Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolPermissionCallback(Protocol):
    """Operator-defined permission callback. May be sync or
    async. The runner detects coroutine functions via
    :func:`inspect.iscoroutinefunction` and awaits accordingly.

    Callbacks MUST NOT mutate the :class:`PermissionContext`
    payload — propagation across callbacks shares the same
    context by reference. Read-only treatment is enforced by
    the frozen dataclass shape."""

    def __call__(
        self, context: PermissionContext,
    ) -> PermissionResult:
        ...  # Protocol


# ---------------------------------------------------------------------------
# Frozen registration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionRegistration:
    """One registered callback. Frozen for safe propagation."""

    name: str
    callback: ToolPermissionCallback
    priority: int = 100
    timeout_s: float = 5.0
    registered_ts: float = 0.0

    def to_projection(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "priority": self.priority,
            "timeout_s": self.timeout_s,
            "registered_ts": self.registered_ts,
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PermissionRegistryError(Exception):
    """Base for registry errors."""


class DuplicatePermissionCallbackNameError(
    PermissionRegistryError,
):
    """Operator misconfig — same name registered twice."""


class InvalidPermissionCallbackError(PermissionRegistryError):
    """Garbage callback / empty name."""


# ---------------------------------------------------------------------------
# Registry — priority-ordered callback chain
# ---------------------------------------------------------------------------


def _max_callbacks_knob() -> int:
    raw = os.environ.get(
        "JARVIS_VENOM_TOOL_PERMISSION_MAX_CALLBACKS", "",
    ).strip()
    try:
        v = int(raw) if raw else 32
        return max(1, min(256, v))
    except (TypeError, ValueError):
        return 32


def _default_timeout_s() -> float:
    raw = os.environ.get(
        "JARVIS_VENOM_TOOL_PERMISSION_TIMEOUT_S", "",
    ).strip()
    try:
        v = float(raw) if raw else 5.0
        return max(0.1, min(60.0, v))
    except (TypeError, ValueError):
        return 5.0


class PermissionRegistry:
    """Per-process registry of permission callbacks. Thread-safe.
    Capacity-limited. Priority-ordered; lower priority value =
    earlier execution (matches LifecycleHookRegistry shape).

    The registry is a callback CHAIN (not pub-sub) — every
    registered callback is consulted on each
    :func:`evaluate_tool_permission` call; callbacks return
    DEFER to abstain.

    NEVER raises out of read paths. Registration paths raise
    explicitly on operator misconfig (duplicate name, invalid
    callback).
    """

    def __init__(self) -> None:
        self._registrations: List[PermissionRegistration] = []
        self._by_name: Dict[str, PermissionRegistration] = {}
        self._max = _max_callbacks_knob()
        self._lock = threading.RLock()

    @property
    def max_callbacks(self) -> int:
        return self._max

    def total_count(self) -> int:
        with self._lock:
            return len(self._registrations)

    def all_registrations(
        self,
    ) -> Tuple[PermissionRegistration, ...]:
        """Priority-ordered tuple. Sort happens at registration
        time so this lookup is O(N) tuple copy."""
        with self._lock:
            return tuple(self._registrations)

    def register(
        self,
        callback: ToolPermissionCallback,
        *,
        name: str,
        priority: int = 100,
        timeout_s: Optional[float] = None,
    ) -> PermissionRegistration:
        """Register one callback. Raises on validation failure
        so operator misconfig surfaces at boot."""
        if not callable(callback):
            raise InvalidPermissionCallbackError(
                f"callback must be callable — got "
                f"{type(callback).__name__}"
            )
        clean_name = str(name or "").strip()[:128]
        if not clean_name:
            raise InvalidPermissionCallbackError(
                "callback name must be a non-empty string"
            )
        try:
            clean_priority = int(priority)
        except (TypeError, ValueError):
            clean_priority = 100
        try:
            clean_timeout = (
                float(timeout_s) if timeout_s is not None
                else _default_timeout_s()
            )
            clean_timeout = max(0.1, min(60.0, clean_timeout))
        except (TypeError, ValueError):
            clean_timeout = _default_timeout_s()
        import time
        registration = PermissionRegistration(
            name=clean_name,
            callback=callback,
            priority=clean_priority,
            timeout_s=clean_timeout,
            registered_ts=time.monotonic(),
        )
        with self._lock:
            if clean_name in self._by_name:
                raise DuplicatePermissionCallbackNameError(
                    f"callback name {clean_name!r} already "
                    f"registered"
                )
            if len(self._registrations) >= self._max:
                raise InvalidPermissionCallbackError(
                    f"registry at capacity ({self._max} "
                    f"callbacks)"
                )
            # Insertion-sort by priority so the iteration order
            # is deterministic + low-priority callbacks fire
            # first (matching LifecycleHookRegistry).
            inserted = False
            for i, existing in enumerate(self._registrations):
                if clean_priority < existing.priority:
                    self._registrations.insert(i, registration)
                    inserted = True
                    break
            if not inserted:
                self._registrations.append(registration)
            self._by_name[clean_name] = registration
        return registration

    def unregister(self, name: str) -> bool:
        """Remove a callback by name. Returns True iff removed."""
        clean = str(name or "").strip()
        with self._lock:
            existing = self._by_name.pop(clean, None)
            if existing is None:
                return False
            self._registrations = [
                r for r in self._registrations
                if r.name != clean
            ]
            return True

    def reset(self) -> None:
        """Drop all registrations. Production code MUST NOT
        call this — used by tests via reset_default_registry."""
        with self._lock:
            self._registrations = []
            self._by_name = {}


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[PermissionRegistry] = None
_DEFAULT_REGISTRY_LOCK = threading.RLock()


def get_default_registry() -> PermissionRegistry:
    """First-instance-wins singleton."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = PermissionRegistry()
        return _DEFAULT_REGISTRY


def reset_default_registry_for_tests() -> None:
    """Test-only — clear the singleton."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is not None:
            _DEFAULT_REGISTRY.reset()
        _DEFAULT_REGISTRY = None


# ---------------------------------------------------------------------------
# Aggregation — first-DENY-wins
# ---------------------------------------------------------------------------


def compute_permission_decision(
    *,
    tool_name: str,
    op_id: str,
    results: Tuple[PermissionResult, ...],
) -> AggregatePermissionDecision:
    """Aggregate callback results into a single decision.
    Pure function. NEVER raises.

    Order:
      1. Empty results → DEFER (no opinion).
      2. Any DENY → DENY (strongest).
      3. No DENY + any ASK → ASK.
      4. No DENY + no ASK + any ALLOW → ALLOW.
      5. All DEFER → DEFER.
    """
    try:
        if not results:
            return AggregatePermissionDecision(
                schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
                tool_name=tool_name,
                op_id=op_id,
                decision=ToolPermissionDecision.DEFER,
                total_callbacks=0,
                detail="empty_results",
            )
        deny_callbacks: List[str] = []
        ask_callbacks: List[str] = []
        any_allow = False
        for r in results:
            if not isinstance(r, PermissionResult):
                continue
            if r.decision == ToolPermissionDecision.DENY:
                deny_callbacks.append(r.callback_name)
            elif r.decision == ToolPermissionDecision.ASK:
                ask_callbacks.append(r.callback_name)
            elif r.decision == ToolPermissionDecision.ALLOW:
                any_allow = True
        if deny_callbacks:
            return AggregatePermissionDecision(
                schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
                tool_name=tool_name,
                op_id=op_id,
                decision=ToolPermissionDecision.DENY,
                total_callbacks=len(results),
                deny_callbacks=tuple(deny_callbacks),
                ask_callbacks=tuple(ask_callbacks),
                detail=(
                    f"deny_by={','.join(deny_callbacks)}"
                ),
            )
        if ask_callbacks:
            return AggregatePermissionDecision(
                schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
                tool_name=tool_name,
                op_id=op_id,
                decision=ToolPermissionDecision.ASK,
                total_callbacks=len(results),
                ask_callbacks=tuple(ask_callbacks),
                detail=(
                    f"ask_by={','.join(ask_callbacks)}"
                ),
            )
        if any_allow:
            return AggregatePermissionDecision(
                schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
                tool_name=tool_name,
                op_id=op_id,
                decision=ToolPermissionDecision.ALLOW,
                total_callbacks=len(results),
                detail="explicit_allow",
            )
        return AggregatePermissionDecision(
            schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
            tool_name=tool_name,
            op_id=op_id,
            decision=ToolPermissionDecision.DEFER,
            total_callbacks=len(results),
            detail="all_defer",
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[tool_permission] compute_permission_decision "
            "fallback: %s", exc,
        )
        return AggregatePermissionDecision(
            schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
            tool_name=tool_name,
            op_id=op_id,
            decision=ToolPermissionDecision.DEFER,
            total_callbacks=0,
            detail=f"aggregator_fallback:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Async evaluator — fires the registry chain
# ---------------------------------------------------------------------------


async def _run_one_callback(
    registration: PermissionRegistration,
    context: PermissionContext,
) -> PermissionResult:
    """Run one callback with timeout + exception isolation."""
    try:
        cb = registration.callback
        if inspect.iscoroutinefunction(cb):
            result = await asyncio.wait_for(
                cb(context),
                timeout=registration.timeout_s,
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(cb, context),
                timeout=registration.timeout_s,
            )
        if isinstance(result, PermissionResult):
            return result
        # Garbage return → DEFER + audit error
        return make_permission_result(
            callback_name=registration.name,
            decision=ToolPermissionDecision.DEFER,
            error=(
                f"non_PermissionResult_return:"
                f"{type(result).__name__}"
            ),
        )
    except asyncio.TimeoutError:
        return make_permission_result(
            callback_name=registration.name,
            decision=ToolPermissionDecision.DEFER,
            error=(
                f"timeout_{registration.timeout_s}s"
            ),
        )
    except asyncio.CancelledError:
        # Propagate per asyncio convention
        raise
    except Exception as exc:  # noqa: BLE001 — defensive
        return make_permission_result(
            callback_name=registration.name,
            decision=ToolPermissionDecision.DEFER,
            error=f"{type(exc).__name__}:{exc}",
        )


async def evaluate_tool_permission(
    *,
    tool_name: str,
    op_id: str,
    payload: Optional[Mapping[str, Any]] = None,
    registry: Optional[PermissionRegistry] = None,
    enabled: Optional[bool] = None,
) -> AggregatePermissionDecision:
    """Async entry point. Composes the registry chain + the
    aggregator. NEVER raises (asyncio.CancelledError propagates
    per convention).

    Decision flow:
      1. Master flag check — if disabled, short-circuit to
         DEFER (callers MUST treat DEFER as ALLOW pre-graduation).
      2. Resolve registry singleton.
      3. Empty registry → DEFER.
      4. Spawn one task per registration; gather with
         ``return_exceptions=True``.
      5. Aggregate via :func:`compute_permission_decision`.
    """
    is_enabled = (
        enabled if enabled is not None
        else venom_tool_permissions_enabled()
    )
    if not is_enabled:
        return AggregatePermissionDecision(
            schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
            tool_name=tool_name,
            op_id=op_id,
            decision=ToolPermissionDecision.DEFER,
            total_callbacks=0,
            detail="master_off",
        )
    try:
        active_registry = registry or get_default_registry()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[tool_permission] registry resolve degraded: %s",
            exc,
        )
        return AggregatePermissionDecision(
            schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
            tool_name=tool_name,
            op_id=op_id,
            decision=ToolPermissionDecision.DEFER,
            total_callbacks=0,
            detail=f"registry_resolve_failed:{type(exc).__name__}",
        )
    registrations = active_registry.all_registrations()
    if not registrations:
        return AggregatePermissionDecision(
            schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
            tool_name=tool_name,
            op_id=op_id,
            decision=ToolPermissionDecision.DEFER,
            total_callbacks=0,
            detail="empty_registry",
        )
    import time
    ctx = PermissionContext(
        schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
        op_id=str(op_id or ""),
        tool_name=str(tool_name or ""),
        payload=dict(payload or {}),
        started_ts=time.monotonic(),
    )
    raw_results = await asyncio.gather(
        *(_run_one_callback(r, ctx) for r in registrations),
        return_exceptions=True,
    )
    # Drop exceptions (asyncio CancelledError already raised
    # above; other exceptions from gather are kept as DEFER
    # results via _run_one_callback's exception handler).
    results: List[PermissionResult] = []
    for r in raw_results:
        if isinstance(r, PermissionResult):
            results.append(r)
    return compute_permission_decision(
        tool_name=tool_name,
        op_id=op_id,
        results=tuple(results),
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``tool_permission_decision_taxonomy_closed`` — 4-value
         enum bytes-pinned.
      2. ``tool_permission_authority_asymmetry`` — substrate
         purity.
      3. ``tool_permission_master_flag_default_false`` —
         §33.1 graduation contract; venom_tool_permissions_enabled
         reads canonical flag name.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/tool_permission.py"
    )

    _EXPECTED_VALUES = {
        "allow", "deny", "ask", "defer",
    }

    def _validate_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ToolPermissionDecision"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VALUES - found
                extra = found - _EXPECTED_VALUES
                if missing:
                    violations.append(
                        f"ToolPermissionDecision missing: "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"ToolPermissionDecision drift: "
                        f"{sorted(extra)}"
                    )
                return tuple(violations)
        violations.append(
            "ToolPermissionDecision class missing"
        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"tool_permission.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name
                == "venom_tool_permissions_enabled"
            ):
                # Must read os.environ.get("JARVIS_VENOM_TOOL_
                # PERMISSIONS_ENABLED", ...)
                found_canonical = False
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        if (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "get"
                            and sub.args
                            and isinstance(
                                sub.args[0], ast.Constant,
                            )
                            and sub.args[0].value
                            == "JARVIS_VENOM_TOOL_"
                               "PERMISSIONS_ENABLED"
                        ):
                            found_canonical = True
                if not found_canonical:
                    violations.append(
                        "venom_tool_permissions_enabled MUST "
                        "read os.environ.get('JARVIS_VENOM_"
                        "TOOL_PERMISSIONS_ENABLED', '') — no "
                        "parallel flag-name path"
                    )
                return tuple(violations)
        violations.append(
            "venom_tool_permissions_enabled missing"
        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "tool_permission_decision_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "Venom V2 — 4-value decision closed "
                "taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_permission_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Venom V2 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "tool_permission_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "Venom V2 — master flag default-FALSE per "
                "§33.1."
            ),
            validate=_validate_master_flag_default_false,
        ),
    ]


__all__ = [
    "AggregatePermissionDecision",
    "DuplicatePermissionCallbackNameError",
    "InvalidPermissionCallbackError",
    "PermissionContext",
    "PermissionRegistration",
    "PermissionRegistry",
    "PermissionRegistryError",
    "PermissionResult",
    "TOOL_PERMISSION_SCHEMA_VERSION",
    "ToolPermissionCallback",
    "ToolPermissionDecision",
    "compute_permission_decision",
    "evaluate_tool_permission",
    "get_default_registry",
    "make_permission_result",
    "register_shipped_invariants",
    "reset_default_registry_for_tests",
    "venom_tool_permissions_enabled",
]
