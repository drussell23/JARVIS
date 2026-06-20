"""Sovereign Cognitive Crucible — the mathematical graduation veto (2026-06-20).

Pure-evaluation helpers that turn a soak's parsed :class:`telemetry_parse.Metrics`
into the two veto signals the autonomic crucible demands, on top of the existing
``default_clean_predicate`` (which already catches FSM-exhaustion-class faults via
the runner-bucket: ``phase_runner_error`` / ``candidate_validate_error`` /
``fsm_state_corruption`` / …):

  * **TTFT degradation** — did the cognitive feature under test slow first-token
    latency past an absolute ceiling, or past ``ratio×`` an EWMA baseline?
  * **AST corruption** — did it emit structurally-broken candidate code during
    a soak (explicit validator/syntax markers beyond the runner-bucket)?

If either fires across a flag's 3 soaks, the crucible MUST veto graduation.

## Authority posture (locked)
  * **Pure + stdlib-only** (``os`` for env knobs, ``statistics`` for the mean).
    No I/O, no logger, no network. Mirrors ``telemetry_parse`` — this module
    evaluates, it never observes state.
  * **NEVER raises** — every helper returns a safe default on malformed input.
  * **Fail-OPEN on absence** — if a soak emitted NO ttft samples we cannot
    *prove* degradation, so we do not veto on missing instrumentation (same
    graceful-degrade contract as the metrics-aware predicates). A veto fires
    only on POSITIVE evidence of harm.
  * **All thresholds env-tunable** — zero hardcoded magic in the decision.
"""
from __future__ import annotations

import os
import statistics
from typing import Any, Dict, List, Optional, Tuple

# Env knobs (defaults are conservative ceilings, not hardcoded decisions).
_TTFT_CEILING_ENV = "JARVIS_CRUCIBLE_TTFT_CEILING_MS"
_TTFT_RATIO_ENV = "JARVIS_CRUCIBLE_TTFT_DEGRADE_RATIO"
_DEFAULT_TTFT_CEILING_MS = 30000.0   # absolute first-token ceiling
_DEFAULT_TTFT_DEGRADE_RATIO = 1.5    # vs EWMA baseline when one is supplied


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _nonzero_ttft(metrics: Any) -> List[int]:
    """Non-zero ttft samples (0 = the failure/timeout sentinel — a *no-first-
    token*, governed by the FSM/recovery gate, not the latency gate)."""
    raw = getattr(metrics, "ttft_samples_ms", None)
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[int] = []
    for v in raw:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            out.append(iv)
    return out


def ttft_stats(metrics: Any) -> Dict[str, float]:
    """Summary stats over the non-zero ttft samples. NEVER raises."""
    samples = _nonzero_ttft(metrics)
    n = len(samples)
    if n == 0:
        return {"n": 0, "mean_ms": 0.0, "max_ms": 0.0}
    return {
        "n": float(n),
        "mean_ms": float(statistics.fmean(samples)),
        "max_ms": float(max(samples)),
    }


def ttft_degraded(
    metrics: Any, *, baseline_ms: Optional[float] = None,
) -> Tuple[bool, str]:
    """True iff first-token latency degraded — past the absolute ceiling OR
    (when a baseline EWMA is supplied) past ``ratio × baseline``.

    Fail-OPEN: no samples → (False, "no_ttft_samples") (cannot prove harm).
    NEVER raises."""
    stats = ttft_stats(metrics)
    if stats["n"] == 0:
        return (False, "no_ttft_samples")
    mean_ms = stats["mean_ms"]
    ceiling = _env_float(_TTFT_CEILING_ENV, _DEFAULT_TTFT_CEILING_MS)
    if mean_ms > ceiling:
        return (True, f"mean_ttft {mean_ms:.0f}ms > ceiling {ceiling:.0f}ms")
    if baseline_ms is not None and baseline_ms > 0:
        ratio = _env_float(_TTFT_RATIO_ENV, _DEFAULT_TTFT_DEGRADE_RATIO)
        if mean_ms > baseline_ms * ratio:
            return (
                True,
                f"mean_ttft {mean_ms:.0f}ms > {ratio:g}x baseline "
                f"{baseline_ms:.0f}ms",
            )
    return (False, f"mean_ttft {mean_ms:.0f}ms within bounds")


def _ast_signal_count(metrics: Any) -> int:
    """Guarded read of the AST-corruption signal count. NEVER raises."""
    try:
        return int(getattr(metrics, "ast_corruption_signals", 0) or 0)
    except (TypeError, ValueError):
        return 0


def ast_corrupted(metrics: Any) -> Tuple[bool, str]:
    """True iff the soak surfaced explicit candidate AST/syntax corruption.
    NEVER raises."""
    n = _ast_signal_count(metrics)
    if n > 0:
        return (True, f"{n} AST/syntax corruption signal(s)")
    return (False, "zero AST corruption")


def crucible_evidence(
    metrics: Any, *, baseline_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """Structured evidence bundle for the Sovereign Telemetry Manifest — the
    empirical proof rendered into the [SOVEREIGN GRADUATION] PR body. Pure;
    NEVER raises."""
    stats = ttft_stats(metrics)
    ttft_bad, ttft_detail = ttft_degraded(metrics, baseline_ms=baseline_ms)
    ast_bad, ast_detail = ast_corrupted(metrics)
    return {
        "ttft_n": int(stats["n"]),
        "ttft_mean_ms": round(stats["mean_ms"], 1),
        "ttft_max_ms": round(stats["max_ms"], 1),
        "ttft_ceiling_ms": _env_float(_TTFT_CEILING_ENV, _DEFAULT_TTFT_CEILING_MS),
        "ttft_baseline_ms": baseline_ms,
        "ttft_degraded": ttft_bad,
        "ttft_detail": ttft_detail,
        "ast_corruption_signals": _ast_signal_count(metrics),
        "ast_corrupted": ast_bad,
        "ast_detail": ast_detail,
        "recovered": bool(getattr(metrics, "recovered", False)),
        "oom": bool(getattr(metrics, "oom", False)),
        "livefire_fired": list(getattr(metrics, "livefire_fired", []) or []),
        "session_outcome": str(getattr(metrics, "session_outcome", "")),
        "stop_reason": str(getattr(metrics, "stop_reason", "")),
    }


__all__ = [
    "ttft_stats",
    "ttft_degraded",
    "ast_corrupted",
    "crucible_evidence",
]
