"""Cross-key invariant policy enforcement for the reactive state store.

Provides a lightweight, extensible rule engine that validates proposed writes
against the current state snapshot *before* they are committed.  Each rule is
a pure function with no I/O -- it receives the key, proposed value, and an
immutable snapshot of the store, and returns a :class:`PolicyResult`.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only (plus ``StateEntry``).
* All rule functions are pure: deterministic, no side-effects, no I/O.
* ``PolicyEngine.evaluate()`` short-circuits on the first rejection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from backend.core.reactive_state.types import StateEntry

# ── Type alias ────────────────────────────────────────────────────────

PolicyRuleFn = Callable[[str, Any, Dict[str, StateEntry]], "PolicyResult"]

# ── PolicyResult ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyResult:
    """Outcome of a single policy rule evaluation.

    Use the :meth:`ok` and :meth:`rejected` class methods rather than
    constructing instances directly.
    """

    allowed: bool
    reason: Optional[str] = None

    @classmethod
    def ok(cls) -> PolicyResult:
        """The proposed write is permitted by this rule."""
        return cls(allowed=True)

    @classmethod
    def rejected(cls, reason: str) -> PolicyResult:
        """The proposed write violates this rule."""
        return cls(allowed=False, reason=reason)


# ── Invariant rule functions ──────────────────────────────────────────


def gcp_offload_requires_ip(
    key: str, value: Any, snapshot: Dict[str, StateEntry]
) -> PolicyResult:
    """Offload activation requires a non-empty ``gcp.node_ip``.

    Only applies when *key* is ``"gcp.offload_active"`` and *value* is
    ``True``.  If the snapshot lacks a ``gcp.node_ip`` entry or its value
    is the empty string, the write is rejected.
    """
    if key != "gcp.offload_active" or value is not True:
        return PolicyResult.ok()

    ip_entry = snapshot.get("gcp.node_ip")
    if ip_entry is None or ip_entry.value == "":
        return PolicyResult.rejected(
            "gcp.offload_active=True requires a non-empty gcp.node_ip"
        )
    return PolicyResult.ok()


def gcp_offload_requires_port(
    key: str, value: Any, snapshot: Dict[str, StateEntry]
) -> PolicyResult:
    """Offload activation requires a ``gcp.node_port`` entry.

    Only applies when *key* is ``"gcp.offload_active"`` and *value* is
    ``True``.  If the snapshot lacks a ``gcp.node_port`` entry, the write
    is rejected.
    """
    if key != "gcp.offload_active" or value is not True:
        return PolicyResult.ok()

    port_entry = snapshot.get("gcp.node_port")
    if port_entry is None:
        return PolicyResult.rejected(
            "gcp.offload_active=True requires gcp.node_port to be set"
        )
    return PolicyResult.ok()


def hollow_requires_offload(
    key: str, value: Any, snapshot: Dict[str, StateEntry]
) -> PolicyResult:
    """Hollow client activation requires ``gcp.offload_active=True``.

    Only applies when *key* is ``"hollow.client_active"`` and *value* is
    ``True``.  If the snapshot lacks a ``gcp.offload_active`` entry or its
    value is not ``True``, the write is rejected.
    """
    if key != "hollow.client_active" or value is not True:
        return PolicyResult.ok()

    offload_entry = snapshot.get("gcp.offload_active")
    if offload_entry is None or offload_entry.value is not True:
        return PolicyResult.rejected(
            "hollow.client_active=True requires gcp.offload_active=True"
        )
    return PolicyResult.ok()


# ── PolicyEngine ──────────────────────────────────────────────────────


class PolicyEngine:
    """Evaluates an ordered list of policy rules against proposed writes.

    Rules are evaluated in insertion order.  The first rejection wins
    (short-circuit): subsequent rules are **not** called once a rejection
    is produced.
    """

    def __init__(self) -> None:
        self._rules: List[PolicyRuleFn] = []

    @property
    def rules(self) -> List[PolicyRuleFn]:
        """Return a shallow copy of the registered rule list."""
        return list(self._rules)

    def add_rule(self, rule: PolicyRuleFn) -> None:
        """Append *rule* to the evaluation chain."""
        self._rules.append(rule)

    def evaluate(
        self, key: str, value: Any, snapshot: Dict[str, StateEntry]
    ) -> PolicyResult:
        """Run all rules.  First rejection wins (short-circuit)."""
        for rule in self._rules:
            result = rule(key, value, snapshot)
            if not result.allowed:
                return result
        return PolicyResult.ok()


# ── Builder ───────────────────────────────────────────────────────────


def build_default_policy_engine() -> PolicyEngine:
    """Create a :class:`PolicyEngine` pre-loaded with the standard invariant rules."""
    engine = PolicyEngine()
    engine.add_rule(gcp_offload_requires_ip)
    engine.add_rule(gcp_offload_requires_port)
    engine.add_rule(hollow_requires_offload)
    return engine
