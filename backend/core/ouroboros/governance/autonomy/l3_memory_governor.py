"""L3 worktree-RAM-budget governor (pure math).

Composes ON TOP of MemoryPressureGate's free-%-based fan-out caps:
the gate answers "is the box under pressure?"; this module answers
"given the absolute RAM cost of a worktree, how many fit right now?".
Strictest-wins between the two. No IO, no scheduler import — every
decision is a deterministic function of its arguments so it can be
proven at all pressure levels in isolation.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def governor_enabled() -> bool:
    """Master flag. Default TRUE; inert until an L3 graph actually runs."""
    return _env_bool("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", True)


def worktree_ram_budget_mb() -> int:
    """Assumed peak RAM per concurrent worktree. Default 1500MB."""
    return _env_int("JARVIS_L3_WORKTREE_RAM_BUDGET_MB", 1500, minimum=64)


@dataclass(frozen=True)
class GovernorDecision:
    requested: int
    ram_cap: int
    level_cap: int
    n_allowed: int
    avail_mb: float
    budget_mb: int
    disposition: str  # compute_worktree_cap emits "allow"|"clamp"; the
    # scheduler layer may instead report "disabled"/"probe_fail" (this pure
    # function never produces those — it is only reached with a live probe).


def compute_worktree_cap(
    *,
    requested: int,
    avail_mb: float,
    budget_mb: int,
    level_cap: int,
) -> GovernorDecision:
    """Pure clamp. ``ram_cap = floor(avail_mb / budget_mb)`` (>=1);
    final allowance is the strictest of requested / ram_cap / level_cap."""
    # Fail-safe: a bad/non-positive avail_mb (e.g. a garbage probe reading)
    # floors to ram_cap=1 — the most conservative non-zero fan-out — rather
    # than 0 or negative. Clamping down on bad input is the safe direction.
    ram_cap = max(1, int(math.floor(avail_mb / float(budget_mb))))
    n_allowed = max(0, min(requested, ram_cap, level_cap))
    disposition = "clamp" if n_allowed < requested else "allow"
    return GovernorDecision(
        requested=requested,
        ram_cap=ram_cap,
        level_cap=level_cap,
        n_allowed=n_allowed,
        avail_mb=avail_mb,
        budget_mb=budget_mb,
        disposition=disposition,
    )
