"""InvariantRegistry and MVP invariant factory functions.

The InvariantRegistry holds a list of named invariant checks that are
evaluated against a StateOracle snapshot.  Flapping suppression prevents
the same invariant from flooding violation reports when it fires
repeatedly within a debounce window.

MVP invariant factories return closures suitable for registration:
    epoch_monotonic, single_routing_target, fault_isolation, terminal_is_final
"""

from __future__ import annotations

import time
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple


# Type alias for an invariant check function.
# Takes an oracle, returns None if OK, or an error string if violated.
InvariantCheckFn = Callable[..., Optional[str]]


# ---------------------------------------------------------------------------
# InvariantRegistry
# ---------------------------------------------------------------------------

class InvariantRegistry:
    """Registry of named invariant checks with optional flapping suppression.

    Parameters
    ----------
    debounce_window_s:
        If an invariant with ``suppress_flapping=True`` fires again within
        this many seconds of its previous violation, the second report is
        suppressed and the ``suppressed_counts`` counter is incremented.
    """

    def __init__(self, debounce_window_s: float = 5.0) -> None:
        self._debounce_window_s = debounce_window_s
        self._invariants: List[Tuple[str, InvariantCheckFn, bool]] = []
        self._last_violation: Dict[str, float] = {}
        self.suppressed_counts: Dict[str, int] = {}

    def register(
        self,
        name: str,
        check: InvariantCheckFn,
        suppress_flapping: bool = True,
    ) -> None:
        """Register an invariant check.

        Parameters
        ----------
        name:
            Human-readable identifier shown in violation messages.
        check:
            Callable ``(oracle) -> Optional[str]``.  Returns ``None`` when
            the invariant holds, or an error string when it is violated.
        suppress_flapping:
            When ``True``, repeated violations within the debounce window
            are suppressed and counted in ``suppressed_counts``.
        """
        self._invariants.append((name, check, suppress_flapping))

    def check_all(self, oracle: object) -> List[str]:
        """Evaluate every registered invariant against *oracle*.

        Returns a list of formatted violation strings.  Suppressed
        violations are silently counted in ``suppressed_counts``.
        """
        violations: List[str] = []
        now = time.monotonic()
        debounce = self._debounce_window_s

        for name, check, suppress_flapping in self._invariants:
            result = check(oracle)
            if result is None:
                continue

            # Flapping suppression
            if suppress_flapping:
                last = self._last_violation.get(name)
                if last is not None and (now - last) < debounce:
                    self.suppressed_counts[name] = (
                        self.suppressed_counts.get(name, 0) + 1
                    )
                    continue

            self._last_violation[name] = now
            violations.append(f"[{name}] {result}")

        return violations


# ---------------------------------------------------------------------------
# MVP invariant factory functions
# ---------------------------------------------------------------------------

def epoch_monotonic() -> InvariantCheckFn:
    """Return a check that verifies the oracle epoch never decreases.

    Uses a closure to track the last-seen epoch across calls.
    """
    last_epoch: List[Optional[int]] = [None]

    def _check(oracle: object) -> Optional[str]:
        current = oracle.epoch()  # type: ignore[union-attr]
        prev = last_epoch[0]
        if prev is not None and current < prev:
            result = f"Epoch decreased from {prev} to {current}"
            last_epoch[0] = current
            return result
        last_epoch[0] = current
        return None

    return _check


def single_routing_target() -> InvariantCheckFn:
    """Return a check that verifies routing_decision is in the valid set."""
    valid = {"LOCAL_PRIME", "GCP_PRIME", "CLOUD_CLAUDE", "HYBRID", "CACHED", "DEGRADED"}

    def _check(oracle: object) -> Optional[str]:
        obs = oracle.routing_decision()  # type: ignore[union-attr]
        value = obs.value
        if value not in valid:
            return f"Routing decision '{value}' not in valid set {sorted(valid)}"
        return None

    return _check


def fault_isolation(
    affected: FrozenSet[str],
    unaffected: FrozenSet[str],
) -> InvariantCheckFn:
    """Return a check that verifies unaffected components are not FAILED/LOST.

    Parameters
    ----------
    affected:
        Components expected to be impacted by the fault (not checked).
    unaffected:
        Components that must remain healthy (not FAILED or LOST).
    """
    from tests.harness.types import ComponentStatus

    bad_statuses = {ComponentStatus.FAILED, ComponentStatus.LOST}

    def _check(oracle: object) -> Optional[str]:
        broken: List[str] = []
        for comp in sorted(unaffected):
            obs = oracle.component_status(comp)  # type: ignore[union-attr]
            if obs.value in bad_statuses:
                broken.append(f"{comp}={obs.value.value}")
        if broken:
            return f"Unaffected components in bad state: {', '.join(broken)}"
        return None

    return _check


def terminal_is_final() -> InvariantCheckFn:
    """Return a check that verifies no component transitions from STOPPED/FAILED
    to anything other than STARTING, STOPPED, or FAILED.

    Scans the full event_log for ``state_change`` events.
    """
    terminal_states = {"STOPPED", "FAILED"}
    allowed_from_terminal = {"STARTING", "STOPPED", "FAILED"}

    def _check(oracle: object) -> Optional[str]:
        events = oracle.event_log()  # type: ignore[union-attr]
        violations: List[str] = []
        for ev in events:
            if ev.event_type != "state_change":
                continue
            if ev.old_value in terminal_states and ev.new_value not in allowed_from_terminal:
                violations.append(
                    f"{ev.component}: {ev.old_value} -> {ev.new_value}"
                )
        if violations:
            return f"Terminal state violated: {'; '.join(violations)}"
        return None

    return _check
