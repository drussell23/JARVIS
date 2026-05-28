"""DW Adaptive Timeout — Slice 34 substrate (Phase 0).

Observation-driven timeout for DW primary calls. **Extends** Slice 27
Phase 3's static ``_compute_adaptive_tier0_timeout_s`` rather than
replacing it — the static formula is the cold-start prior, observed
rolling p99 is the refinement once sufficient samples accrue.

# Composition

  * Reads from :class:`DWPerShapeStats` (which reads from
    :class:`DWCapacityLedger`).
  * Falls through to ``candidate_generator._compute_adaptive_tier0_timeout_s``
    (Slice 27 Phase 3 — already prompt-size + heavy-model aware)
    when stats are absent or sample count is below threshold.
  * NEVER returns less than the static formula (safety floor) —
    observation-driven adjustments only *raise* the timeout, never
    lower it below what Slice 27's calibrated math would compute.

# Formula

```
static_floor    = _compute_adaptive_tier0_timeout_s(...)   # Slice 27
stats           = per_shape.stats_for_shape(...)
if stats is None or stats.sample_count < min_samples:
    return static_floor                                     # cold start
observed_budget = stats.p99_ms / 1000 × safety_factor       # default ×1.5
final           = clamp(max(static_floor, observed_budget), static_floor, cap)
```

# Why p99 not p95

The dispatch decision is "how long should we wait before giving up
on THIS call." A p95 budget kills 5% of legitimate calls that would
have succeeded — wasted dispatch. p99 with safety_factor 1.5 gives
~99.5% tail coverage while staying bounded.

# Why max(static, observed)

If observed is FASTER than static (DW endpoint is healthy), we still
honor the static prior because the dispatcher's static math was
calibrated for the worst case it could reason about analytically.
Lowering below static risks cutting off cold-MoE-warmup calls that
LATER succeed.

# Master flag

``JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED`` (default **FALSE** —
substrate ships first, behavior change graduates after v30 soak
proves adaptive math doesn't regress static performance).

# What this module does NOT do

  * Does NOT decide WHICH model to dispatch (that's
    :mod:`dw_adaptive_rotator` future arc).
  * Does NOT learn parameters (no gradient descent, no ML training).
    Per §48.10's caveat — non-stationary DW behavior makes any
    "learned" model unstable. This is *adaptive heuristic*, not ML.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from backend.core.ouroboros.governance.dw_per_shape_stats import (
    DWPerShapeStats,
    ShapeStats,
    get_default_stats,
)


logger = logging.getLogger("Ouroboros.DWAdaptiveTimeout")


# ============================================================================
# Env knobs
# ============================================================================


_ENABLED_ENV: str = "JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED"
_SAFETY_FACTOR_ENV: str = "JARVIS_DW_ADAPTIVE_TIMEOUT_SAFETY_FACTOR"
_CAP_S_ENV: str = "JARVIS_DW_ADAPTIVE_TIMEOUT_CAP_S"

_DEFAULT_SAFETY_FACTOR: float = 1.5
_DEFAULT_CAP_S: float = 600.0      # 10 minute hard ceiling


def is_enabled() -> bool:
    """Default FALSE — substrate ships before behaviour change.
    Graduates after v30 soak proves adaptive math doesn't regress."""
    raw = os.environ.get(_ENABLED_ENV, "").strip().lower()
    if not raw:
        return False
    return raw in ("1", "true", "yes", "on")


def _safety_factor() -> float:
    try:
        raw = os.environ.get(_SAFETY_FACTOR_ENV, "").strip()
        if not raw:
            return _DEFAULT_SAFETY_FACTOR
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SAFETY_FACTOR


def _cap_s() -> float:
    try:
        raw = os.environ.get(_CAP_S_ENV, "").strip()
        if not raw:
            return _DEFAULT_CAP_S
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_CAP_S


# ============================================================================
# Core function
# ============================================================================


def compute_adaptive_timeout(
    *,
    model_id: str,
    route: str,
    prompt_chars: int,
    static_floor_s: float,
    stats: Optional[DWPerShapeStats] = None,
) -> float:
    """Observation-driven timeout. NEVER returns less than ``static_floor_s``.

    Args:
      model_id: DW model identifier.
      route: ``immediate`` / ``standard`` / ``complex`` / ``background``
        / ``speculative``.
      prompt_chars: input prompt size for shape-bucketing.
      static_floor_s: the timeout Slice 27 Phase 3's static formula
        would compute for this call. ALWAYS the floor — observation
        only raises above it.
      stats: optional per-shape stats instance (defaults to module
        singleton). Tests pass a custom instance.

    Returns:
      Timeout in seconds, clamped to ``[static_floor_s, _cap_s()]``.

    Behavior matrix:
      master_flag=False → returns static_floor_s (no behavior change)
      master_flag=True + no stats → returns static_floor_s
      master_flag=True + stats present → returns max(static_floor,
                                                     p99 × safety_factor)
                                         clamped to cap.
    """
    if not is_enabled():
        return static_floor_s

    s = stats or get_default_stats()
    try:
        shape_stats = s.stats_for_shape(
            model_id=model_id,
            route=route,
            prompt_chars=prompt_chars,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed to static
        logger.warning(
            "[DWAdaptiveTimeout] stats_for_shape failed: %s — using static floor",
            exc,
        )
        return static_floor_s

    if shape_stats is None:
        # Insufficient samples — Slice 27 static prior wins
        return static_floor_s

    safety = _safety_factor()
    observed_budget_s = (shape_stats.p99_ms / 1000.0) * safety
    final = max(static_floor_s, observed_budget_s)
    final = min(final, _cap_s())
    logger.debug(
        "[DWAdaptiveTimeout] model=%s route=%s prompt_chars=%d "
        "static_floor=%.2fs observed_p99=%.0fms × %.2f = %.2fs "
        "(samples=%d) → final=%.2fs",
        model_id, route, prompt_chars,
        static_floor_s, shape_stats.p99_ms, safety,
        observed_budget_s, shape_stats.sample_count, final,
    )
    return final


__all__ = [
    "compute_adaptive_timeout",
    "is_enabled",
]
