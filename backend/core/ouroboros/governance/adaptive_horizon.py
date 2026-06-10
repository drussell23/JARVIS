"""Slice 195 — Adaptive Horizon Governor: the end of the 360s magic number.

The background pool's per-op watchdog ceiling was a static, env-tunable table
(base 360s; read_only / ≥4-file complex / swe_bench 900s) with a hard 4-file
cliff. This module replaces the magic numbers with a derivation: the temporal
runway is computed from the operation's actual shape —

  * **input context size** — a longer description/prompt means more tokens in
    flight at every phase (size factor, capped),
  * **complexity vector** — continuous per-target-file scaling, eradicating
    the ≥4-file cliff (2 files now earns more runway than 1),
  * **the active model's catalog profile** — a heavy model (param count ≥
    threshold, parsed by ``dw_catalog_client.parse_parameter_count`` — ZERO
    hardcoded model names) earns the heavy factor, mirroring the Slice 28
    heavy-TTFT doctrine.

WATCHDOG DOCTRINE (Slice 47 — load-bearing, do not weaken):

  The horizon is computed ONCE at worker pickup from STATIC envelope signals.
  It is NOT an activity-gated mid-run extension — the rejected "adaptive
  budget waiver" failure mode coupled the watchdog to the inner state-ledger
  and deadlocked WITH the system it guarded. This governor reads no ledger,
  no phase, no in-flight activity signal, and pulls in no async machinery; a
  wedged op still dies at its precomputed ceiling. The governor can only RAISE above the legacy
  max-aggregated floor and is hard-clamped by ``JARVIS_HORIZON_MAX_S``, so
  the anti-hang purpose survives any input. OFF → byte-identical legacy.

Env surface (all tunable, defaults conservative):
  JARVIS_ADAPTIVE_HORIZON_ENABLED   master (default true — raise-only + clamped)
  JARVIS_HORIZON_MAX_S              hard ceiling (default 1800)
  JARVIS_HORIZON_SIZE_KNEE_CHARS    chars at which the size factor saturates'
                                    growth midpoint (default 50000)
  JARVIS_HORIZON_SIZE_FACTOR_MAX    size factor ceiling (default 2.0)
  JARVIS_HORIZON_PER_FILE_FACTOR    additive factor per target file (default 0.15)
  JARVIS_HORIZON_FILE_FACTOR_CAP    files counted at most (default 8)
  JARVIS_HORIZON_HEAVY_PARAMS_B     heavy-model param threshold in B (default 100)
  JARVIS_HORIZON_HEAVY_MODEL_FACTOR heavy-model multiplier (default 1.5)
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_ADAPTIVE_HORIZON_ENABLED"


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v > 0 else default
    except Exception:  # noqa: BLE001
        return default


def adaptive_horizon_enabled() -> bool:
    """Master gate (default TRUE — the governor is raise-only above the legacy
    floor and hard-clamped, so it cannot weaken the watchdog). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _size_factor(context_chars: int) -> float:
    """1.0 + chars/knee, hard-capped at SIZE_FACTOR_MAX (linear, monotone,
    bounded — reaches the cap exactly so the clamp is auditable)."""
    knee = _envf("JARVIS_HORIZON_SIZE_KNEE_CHARS", 50_000.0)
    cap = max(1.0, _envf("JARVIS_HORIZON_SIZE_FACTOR_MAX", 2.0))
    chars = max(0.0, float(context_chars or 0))
    return min(cap, 1.0 + chars / knee)


def _file_factor(target_file_count: int) -> float:
    """Continuous per-file scaling — no cliff. 1 file = 1+f, 2 files = 1+2f…
    capped at FILE_FACTOR_CAP files."""
    per_file = _envf("JARVIS_HORIZON_PER_FILE_FACTOR", 0.15)
    file_cap = _envf("JARVIS_HORIZON_FILE_FACTOR_CAP", 8.0)
    files = min(max(0.0, float(target_file_count or 0)), file_cap)
    return 1.0 + per_file * files


def _model_factor(model_id: Optional[str]) -> float:
    """Catalog-profile factor: param count parsed by the EXISTING dw_catalog
    heuristic (curated map + size-token regex — zero hardcoded names here).
    Unknown / unparseable → 1.0 (conservative)."""
    try:
        if not model_id or not isinstance(model_id, str):
            return 1.0
        from backend.core.ouroboros.governance.dw_catalog_client import (
            parse_parameter_count,
        )
        params_b = parse_parameter_count(model_id)
        if params_b is None:
            return 1.0
        threshold = _envf("JARVIS_HORIZON_HEAVY_PARAMS_B", 100.0)
        if params_b >= threshold:
            return max(1.0, _envf("JARVIS_HORIZON_HEAVY_MODEL_FACTOR", 1.5))
        return 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def compute_horizon(
    *,
    legacy_floor_s: float,
    legacy_reason: str,
    context_chars: int = 0,
    target_file_count: int = 0,
    model_id: Optional[str] = None,
) -> Tuple[float, str]:
    """Derive the per-op watchdog ceiling from the operation's static shape.

    Returns ``(timeout_s, reason)``. Disabled → the legacy pair untouched.
    Enabled → ``legacy_floor × size × files × model``, clamped to
    ``[legacy_floor, JARVIS_HORIZON_MAX_S]``. Computed once at worker pickup;
    NEVER raises (any internal failure → legacy pair)."""
    try:
        if not adaptive_horizon_enabled():
            return float(legacy_floor_s), str(legacy_reason)
        floor = max(0.0, float(legacy_floor_s))
        sf = _size_factor(context_chars)
        ff = _file_factor(target_file_count)
        mf = _model_factor(model_id)
        horizon = floor * sf * ff * mf
        hard_max = _envf("JARVIS_HORIZON_MAX_S", 1800.0)
        horizon = min(max(horizon, floor), max(hard_max, floor))
        if horizon <= floor:
            # All factors 1.0 — keep the legacy reason so logs stay familiar
            # for unscaled ops.
            return floor, str(legacy_reason)
        reason = (
            f"adaptive({legacy_reason};size={sf:.2f},files={ff:.2f},"
            f"model={mf:.2f})"
        )
        return horizon, reason
    except Exception:  # noqa: BLE001
        return float(legacy_floor_s), str(legacy_reason)
