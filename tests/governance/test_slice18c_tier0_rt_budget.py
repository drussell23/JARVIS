"""Slice 18c — Route-aware Tier 0 RT budget cap (eliminates premature-timeout cascade-to-Claude).

Closes the cascade pattern surfaced by soak bt-2026-05-26-070049 (FLEET v13):
DW Tier 0 RT dispatched correctly on Qwen 397B via Slice 10B-iii promotion +
Slice 10B-ii topology bridge, but the 30s default cap clamped budgets BELOW
the 397B's actual TTFT envelope. Result: 8 EXHAUSTION events, each cascading
to Claude which then refused on credit-balance.

# Root cause

`_TIER3_REFLEX_HARD_CAP_S = 30s` (designed for IMMEDIATE-equivalent reflex
semantics per Manifesto §5) was being applied as the absolute Tier 0 RT cap
on STANDARD + COMPLEX routes. Those routes are explicitly cost-optimized
(DW primary) with no reflex-time SLA. The 30s cap was a category error.

# Fix mechanism — route-aware cap selector

  _tier0_rt_cap_for_route(route) -> float:
    if route in ("standard", "complex"):
      return _TIER0_RT_BUDGET_STANDARD_COMPLEX_S  (default 90s)
    return _TIER3_REFLEX_HARD_CAP_S                (default 30s)

Operator override via `JARVIS_DW_TIER0_RT_BUDGET_S` env. Future Slice 13B
bandit (§45.7.2) can replace this static cap with per-shape p95 envelope.

# Operator bindings honored

* IMMEDIATE / BG / SPEC preserved at 30s (byte-equivalent to pre-Slice-18c
  for cost-optimization semantics).
* STANDARD + COMPLEX get 90s default — matches the empirical 397B + Kimi
  TTFT envelope documented in §46.2.
* No new logic in the budget formula — just the 4th constraint in the
  min() switches from a global constant to a route-aware function call.
* AST-pinned: the route-aware function MUST appear in the constraint
  selector or pre-Slice-18c behavior persists.

# Test surface (2 AST pins + 5 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_route_aware_cap_function_present() -> None:
    """``_tier0_rt_cap_for_route`` MUST be a module-level function that
    routes STANDARD+COMPLEX to a different (larger) cap than the
    legacy reflex 30s. Without it, all routes inherit the 30s cap and
    the cascade pattern from FLEET v13 reproduces."""
    src = CG_FILE.read_text()
    assert "def _tier0_rt_cap_for_route(" in src, (
        "Slice 18c selector function missing — cap routing dead"
    )
    assert "_TIER0_RT_BUDGET_STANDARD_COMPLEX_S" in src, (
        "Missing _TIER0_RT_BUDGET_STANDARD_COMPLEX_S constant"
    )
    assert "JARVIS_DW_TIER0_RT_BUDGET_S" in src, (
        "Missing JARVIS_DW_TIER0_RT_BUDGET_S env knob"
    )
    # Slice 18c attribution + bt soak link
    assert "Slice 18c" in src
    assert "bt-2026-05-26-070049" in src, (
        "Missing soak attribution — future readers can't trace which "
        "FLEET v13 forensic surfaced this cascade pattern"
    )


def test_ast_pin_compute_tier0_budget_uses_route_aware_cap() -> None:
    """``_compute_tier0_budget`` MUST call ``_tier0_rt_cap_for_route``
    in its min() constraint switch. Without this call site swap, the
    function body still uses ``_TIER3_REFLEX_HARD_CAP_S`` directly
    and the cap routing is dead code."""
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    found_use = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_compute_tier0_budget"
        ):
            body_src = ast.unparse(node)
            if "_tier0_rt_cap_for_route" in body_src:
                found_use = True
                break
    assert found_use, (
        "_compute_tier0_budget does NOT call _tier0_rt_cap_for_route — "
        "Slice 18c switch is inert; pre-Slice-18c 30s cap still applies"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 5
# ──────────────────────────────────────────────────────────────────────


def test_spine_standard_and_complex_get_90s_default() -> None:
    """STANDARD + COMPLEX routes admit the new 90s default cap.
    This is the empirical floor for Qwen 397B + Kimi K2.6 TTFT
    envelope per §46.2."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _tier0_rt_cap_for_route,
    )
    assert _tier0_rt_cap_for_route("standard") == 90.0
    assert _tier0_rt_cap_for_route("complex") == 90.0


def test_spine_bg_spec_immediate_keep_30s_reflex_cap() -> None:
    """IMMEDIATE / BACKGROUND / SPECULATIVE preserve the 30s reflex
    cap (byte-equivalent to pre-Slice-18c). Cost-optimization +
    fast-reflex semantics intact."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _tier0_rt_cap_for_route,
    )
    assert _tier0_rt_cap_for_route("immediate") == 30.0
    assert _tier0_rt_cap_for_route("background") == 30.0
    assert _tier0_rt_cap_for_route("speculative") == 30.0


def test_spine_unknown_route_falls_through_to_reflex_cap() -> None:
    """Unknown / empty / None routes fall through to the 30s reflex
    cap (defensive — never grant the larger budget to an unmapped
    route)."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _tier0_rt_cap_for_route,
    )
    assert _tier0_rt_cap_for_route("") == 30.0
    assert _tier0_rt_cap_for_route("totally_made_up") == 30.0


def test_spine_compute_tier0_budget_honors_90s_on_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget formula must return a Tier 0 budget ≥ 60s for STANDARD
    when the parent budget is generous, proving the 90s cap is the
    effective constraint (NOT the legacy 30s cap)."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    # Static method — invoke with a 200s parent budget; complexity=complex
    # to use the multiplier-friendly path. We expect budget to land
    # between 60-90s (constrained by 90s cap, not 30s reflex).
    budget = CandidateGenerator._compute_tier0_budget(
        total_s=200.0,
        complexity="complex",
        provider_route="standard",
    )
    assert budget > 30.0, (
        f"STANDARD route still capped at 30s reflex cap; got {budget}s "
        f"— Slice 18c not taking effect"
    )
    assert budget <= 90.0, (
        f"STANDARD route budget exceeded the new 90s cap; got {budget}s "
        f"— constraint formula bug"
    )


def test_spine_compute_tier0_budget_unchanged_on_immediate() -> None:
    """IMMEDIATE route returns 0 (DW skipped per Manifesto §5).
    Spine guard — Slice 18c must not accidentally enable DW on
    IMMEDIATE."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    budget = CandidateGenerator._compute_tier0_budget(
        total_s=200.0,
        complexity="complex",
        provider_route="immediate",
    )
    assert budget == 0.0, (
        f"IMMEDIATE route returned non-zero Tier 0 budget {budget} — "
        f"Slice 18c violated Manifesto §5 (Claude-direct for IMMEDIATE)"
    )
