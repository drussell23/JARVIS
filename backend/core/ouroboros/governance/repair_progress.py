"""L2 Repair Engine completion — Phase 2 (divergence escape) + Phase 3 (progress v1.1 / velocity).

Pure, deterministic, testable helpers that upgrade the L2 self-repair loop from flat early-stops to a
dynamic, path-aware execution fabric:

- **Stochastic State-Mutation Escape (Phase 2):** when the loop stalls (identical failure signature or
  identical patch over sequential iterations = a local minimum), instead of a flat `_stopped` the loop
  mutates its own strategy — escalating the generation *paradigm* (localized patch → full-method
  encapsulation rewrite → module-level redesign) and widening the dependency-cone lookahead so the
  model gets more architectural telemetry. Bounded by an escalation budget so it always terminates.
- **Granular Progress v1.1 (Phase 3):** progress is no longer just "fewer failing tests" — a shrinking
  *set* of failing-test signatures (even at constant count) counts as progress, and an Operational
  Velocity Score over the repair timeline detects thrashing (velocity ≤ 0 while errors persist) so the
  loop can throttle graph-cache memory before a token-limit failure.

All thresholds env-tunable (no hardcoding). All gated default-OFF (the loop is byte-identical when off).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Tuple

__all__ = [
    "diverge_escape_enabled",
    "progress_v11_enabled",
    "EscalationDirective",
    "next_escalation",
    "RepairProgressTracker",
]


# --------------------------------------------------------------------------- flags
def diverge_escape_enabled() -> bool:
    """``JARVIS_L2_DIVERGE_ESCAPE_ENABLED`` (default OFF) — convert flat oscillation/no-progress
    stops into bounded stochastic strategy escalations."""
    return os.environ.get("JARVIS_L2_DIVERGE_ESCAPE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def progress_v11_enabled() -> bool:
    """``JARVIS_L2_PROGRESS_V11_ENABLED`` (default OFF) — sig-set-narrowing progress signal +
    Operational Velocity Score + memory-throttle."""
    return os.environ.get("JARVIS_L2_PROGRESS_V11_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _diverge_window() -> int:
    try:
        return max(2, int(os.environ.get("JARVIS_L2_DIVERGE_WINDOW", "2")))
    except ValueError:
        return 2


def max_escalations() -> int:
    """``JARVIS_L2_MAX_ESCALATIONS`` (default 2) — escalation budget before the loop falls back to a
    terminal stop. Guarantees termination."""
    try:
        return max(1, int(os.environ.get("JARVIS_L2_MAX_ESCALATIONS", "2")))
    except ValueError:
        return 2


# --------------------------------------------------------------------------- escalation ladder
@dataclass(frozen=True)
class EscalationDirective:
    """A strategy mutation injected into the next generation when the loop is diverged."""

    level: int
    paradigm: str          # prompt directive that switches the generation paradigm
    cone_depth_bump: int   # widen the dependency-cone lookahead by this many graph levels


# Deterministic escalation ladder (no RNG — Math.random is banned and determinism is auditable).
# Each level abandons a finer-grained strategy that stalled for a coarser, more structural one.
_LADDER: Tuple[str, ...] = (
    "ESCALATION (loop diverged): localized line-level patching has stalled in a local minimum. "
    "ABANDON the targeted diff. REWRITE the entire failing function/method from its signature + "
    "docstring contract (full-method encapsulation) — reconsider the algorithm, not just the lines.",
    "ESCALATION L2 (still diverged): the function-level rewrite also stalled. Step up to the "
    "MODULE level — reconsider the class/module design and the contracts between the failing symbol "
    "and the dependents shown in the (now-widened) dependency cone. A larger structural change is "
    "authorized; preserve the public interface unless the cone shows it is the fault.",
    "ESCALATION L3 (persistent divergence): treat this as a design defect, not a bug. Re-derive the "
    "failing component from first principles against the cone's call-chain and dependents; you may "
    "restructure internal helpers freely as long as the dependency cone's boundary contracts hold.",
)


def next_escalation(count: int) -> Optional[EscalationDirective]:
    """Return the escalation for the *count*-th divergence (1-based), or ``None`` once the budget
    (``max_escalations``) is spent → caller falls back to a terminal stop."""
    if count < 1 or count > max_escalations():
        return None
    idx = min(count - 1, len(_LADDER) - 1)
    return EscalationDirective(level=count, paradigm=_LADDER[idx], cone_depth_bump=count)


# --------------------------------------------------------------------------- progress tracker
@dataclass
class _Sample:
    fail_sig: str
    patch_sig: str
    failing_sigs: FrozenSet[str]
    diff_lines: int


@dataclass
class RepairProgressTracker:
    """Per-iteration repair telemetry → divergence + granular-progress + velocity signals."""

    samples: List[_Sample] = field(default_factory=list)

    def record(self, *, fail_sig: str, patch_sig: str,
               failing_sigs: FrozenSet[str], diff_lines: int) -> None:
        self.samples.append(_Sample(fail_sig, patch_sig, frozenset(failing_sigs), int(diff_lines)))

    # ---- Phase 2: divergence ----
    def is_diverged(self, window: Optional[int] = None) -> bool:
        """True when the last *window* iterations share one identical failure signature OR one
        identical patch signature — a local minimum the model can't escape with the same strategy."""
        w = window if window is not None else _diverge_window()
        if len(self.samples) < w:
            return False
        tail = self.samples[-w:]
        same_fail = len({s.fail_sig for s in tail}) == 1
        same_patch = len({s.patch_sig for s in tail}) == 1
        return same_fail or same_patch

    # ---- Phase 3: granular progress ----
    def sig_set_narrowed(self) -> bool:
        """True when the *set* of failing-test signatures strictly shrank vs the previous iteration
        (progress even when the raw count is unchanged — a fix that resolved one and surfaced none)."""
        if len(self.samples) < 2:
            return False
        prev, cur = self.samples[-2].failing_sigs, self.samples[-1].failing_sigs
        return cur < prev  # proper subset → strictly narrowed

    def velocity_score(self, window: int = 3) -> float:
        """Operational Velocity Score over the recent timeline. Positive = converging (failures and/or
        signature-set and/or churn shrinking); negative = thrashing. Normalized, deterministic."""
        if len(self.samples) < 2:
            return 0.0
        tail = self.samples[-(window + 1):] if len(self.samples) > window else self.samples
        deltas: List[float] = []
        for a, b in zip(tail, tail[1:]):
            d_count = len(a.failing_sigs) - len(b.failing_sigs)        # +ve = fewer failures
            d_set = len(a.failing_sigs - b.failing_sigs)               # +ve = some resolved
            d_diff = abs(a.diff_lines) - abs(b.diff_lines)             # +ve = less churn
            base = max(1, len(a.failing_sigs))
            deltas.append((2.0 * d_count + 1.0 * d_set + 0.25 * (d_diff / base)) / base)
        return sum(deltas) / len(deltas) if deltas else 0.0

    def should_throttle_memory(self, window: int = 3) -> bool:
        """True when velocity is non-positive while failures persist — the loop is thrashing and may
        be heading for a token/memory blow-up, so the graph cache should contract pre-emptively."""
        if not self.samples or not self.samples[-1].failing_sigs:
            return False
        return self.velocity_score(window) <= 0.0
