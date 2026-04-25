"""Cost-governor parallel-stream cap fix (rooted-problem follow-up 2026-04-25).

Pin the `bump_for_parallel_streams` behavior added to fix the F1 Slice 4
S2 post-fix bottleneck:

  Cost summary op=op-019dc369-982e phase=CLASSIFY spent=$0.4914 / cap=$0.4500
  [ParallelDispatch enforce_cancelled] phase=wait elapsed_s=53.3

3-stream PLAN-EXPLOIT cost ($0.49) vs single-stream cap ($0.45) caused
the cost-governor to (correctly) cancel the in-flight fan-out. The
financial circuit breaker fired EXACTLY as designed — the bug was in
the cap derivation, not the enforcement.

Operator binding 2026-04-25: "A 3-stream parallel fan-out (PLAN-EXPLOIT)
should not be bottlenecked by a static, single-stream CLASSIFY cap...
dynamicize the cost cap based on the n_allowed parallel streams."

Pin coverage:

A. Default-singleton accessor — set/get round-trip.
B. `bump_for_parallel_streams` is no-op on n_streams<=1 (single-stream
   = pre-fix behavior, byte-for-byte).
C. `bump_for_parallel_streams` raises cap by `n_streams * parallel_stream_factor`.
D. Cap NEVER shrinks — calls with lower n_streams return current cap.
E. Idempotent — same n_streams produces same cap (no compounding).
F. Cap never exceeds `max_cap_usd` (financial circuit-breaker invariant).
G. Master-off (governor disabled) → bump returns None.
H. No entry (op not started) → bump returns None gracefully.
I. Reset of `exceeded` flag when bump retroactively rescues cap.
J. Source-grep pin — PLAN-EXPLOIT calls bump before its gather.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
    get_default_cost_governor,
    set_default_cost_governor,
)


# ---------------------------------------------------------------------------
# (A) Default-singleton accessor
# ---------------------------------------------------------------------------


def test_default_singleton_round_trip() -> None:
    """set_default_cost_governor + get_default_cost_governor round-trip."""
    governor = CostGovernor(CostGovernorConfig())
    set_default_cost_governor(governor)
    try:
        assert get_default_cost_governor() is governor
    finally:
        set_default_cost_governor(None)  # type: ignore[arg-type]


def test_default_singleton_default_none() -> None:
    """Before set, get returns None — pure helpers tolerate it."""
    set_default_cost_governor(None)  # type: ignore[arg-type]
    assert get_default_cost_governor() is None


# ---------------------------------------------------------------------------
# (B) Single-stream is no-op
# ---------------------------------------------------------------------------


def test_bump_n_streams_1_is_noop() -> None:
    """n_streams=1 → bump returns None, cap unchanged (pre-fix parity)."""
    g = CostGovernor(CostGovernorConfig())
    g.start("op-test", route="standard", complexity="moderate")
    pre_cap = g._entries["op-test"].cap_usd
    result = g.bump_for_parallel_streams("op-test", n_streams=1)
    assert result is None
    assert g._entries["op-test"].cap_usd == pre_cap


def test_bump_n_streams_0_is_noop() -> None:
    """Defensive: n_streams=0 → no-op (shouldn't happen but guard)."""
    g = CostGovernor(CostGovernorConfig())
    g.start("op-test", route="standard", complexity="moderate")
    pre_cap = g._entries["op-test"].cap_usd
    assert g.bump_for_parallel_streams("op-test", n_streams=0) is None
    assert g._entries["op-test"].cap_usd == pre_cap


# ---------------------------------------------------------------------------
# (C) Multi-stream raises cap proportionally
# ---------------------------------------------------------------------------


def test_bump_3_streams_raises_cap_by_3x_with_safety_margin() -> None:
    """The F1 Slice 4 S2 scenario: 3-stream fan-out gets 3x baseline cap
    × safety margin (default 1.1)."""
    g = CostGovernor(CostGovernorConfig())
    # standard + moderate (= "light" default) → cap=$0.10*1.5*1.0*3.0 = $0.45
    g.start("op-test", route="standard", complexity="moderate")
    pre_cap = g._entries["op-test"].cap_usd
    new_cap = g.bump_for_parallel_streams("op-test", n_streams=3)
    assert new_cap is not None
    # 3 streams × 1.1 factor = 3.3x multiplier
    assert new_cap == pytest.approx(pre_cap * 3 * 1.1, rel=0.01)
    assert g._entries["op-test"].parallel_factor == pytest.approx(3.3, rel=0.01)


def test_bump_5_streams_higher_cap() -> None:
    g = CostGovernor(CostGovernorConfig())
    g.start("op-test", route="standard", complexity="moderate")
    pre_cap = g._entries["op-test"].cap_usd
    new_cap = g.bump_for_parallel_streams("op-test", n_streams=5)
    assert new_cap is not None
    # 5 streams × 1.1 = 5.5x — but max_cap is $5.00 so will clamp
    expected_unclamped = pre_cap * 5 * 1.1
    cfg = g._config
    expected_clamped = min(cfg.max_cap_usd, expected_unclamped)
    assert new_cap == pytest.approx(expected_clamped, rel=0.01)


# ---------------------------------------------------------------------------
# (D) Caps never shrink — only grow
# ---------------------------------------------------------------------------


def test_bump_lower_n_streams_does_not_shrink_cap() -> None:
    """After 5-stream bump, calling with 2-stream must NOT reduce cap.
    The op already committed to a higher concurrency; retroactively
    starving it would be incorrect."""
    g = CostGovernor(CostGovernorConfig())
    g.start("op-test", route="standard", complexity="moderate")
    high_cap = g.bump_for_parallel_streams("op-test", n_streams=5)
    lower_cap = g.bump_for_parallel_streams("op-test", n_streams=2)
    assert lower_cap == high_cap  # No-op — caps never shrink


# ---------------------------------------------------------------------------
# (E) Idempotent
# ---------------------------------------------------------------------------


def test_bump_idempotent_same_n_streams() -> None:
    """Calling bump twice with same n_streams produces same cap."""
    g = CostGovernor(CostGovernorConfig())
    g.start("op-test", route="standard", complexity="moderate")
    cap_a = g.bump_for_parallel_streams("op-test", n_streams=3)
    cap_b = g.bump_for_parallel_streams("op-test", n_streams=3)
    assert cap_a == cap_b


# ---------------------------------------------------------------------------
# (F) Financial circuit-breaker invariant — never exceeds max_cap
# ---------------------------------------------------------------------------


def test_bump_clamps_to_max_cap() -> None:
    """Even with absurd n_streams, cap never exceeds max_cap_usd."""
    g = CostGovernor(CostGovernorConfig())
    g.start("op-test", route="complex", complexity="complex")  # high baseline
    new_cap = g.bump_for_parallel_streams("op-test", n_streams=100)
    cfg = g._config
    assert new_cap <= cfg.max_cap_usd


# ---------------------------------------------------------------------------
# (G) Master-off — governor disabled → bump no-op
# ---------------------------------------------------------------------------


def test_bump_governor_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When master flag off, bump returns None without side effects."""
    monkeypatch.setenv("JARVIS_OP_COST_GOVERNOR_ENABLED", "false")
    g = CostGovernor(CostGovernorConfig())
    assert g.bump_for_parallel_streams("op-test", n_streams=3) is None


# ---------------------------------------------------------------------------
# (H) Defensive — bump on unstarted op returns None gracefully
# ---------------------------------------------------------------------------


def test_bump_unstarted_op_returns_none() -> None:
    """If start() wasn't called for op_id, bump is a graceful no-op."""
    g = CostGovernor(CostGovernorConfig())
    assert g.bump_for_parallel_streams("op-not-started", n_streams=3) is None


# ---------------------------------------------------------------------------
# (I) Bump retroactively rescues op that just exceeded
# ---------------------------------------------------------------------------


def test_bump_resets_exceeded_when_new_cap_accommodates() -> None:
    """If charge() set exceeded=True at the old cap, but the bump raises
    the cap above cumulative_usd, exceeded must reset (retroactive
    rescue — the F1 Slice 4 S2 fix scenario)."""
    g = CostGovernor(CostGovernorConfig())
    g.start("op-test", route="standard", complexity="moderate")
    # Charge $0.49 against $0.45 cap — sets exceeded=True
    g.charge("op-test", 0.49, provider="claude-api", phase="GENERATE")
    assert g.is_exceeded("op-test") is True
    # Now PLAN-EXPLOIT calls bump for the 3-stream gather
    new_cap = g.bump_for_parallel_streams("op-test", n_streams=3)
    assert new_cap > 0.49
    # Critical: exceeded flag MUST reset (cumulative now < new cap)
    assert g.is_exceeded("op-test") is False


# ---------------------------------------------------------------------------
# (J) Source-grep pin — PLAN-EXPLOIT calls bump before gather
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_pin_plan_exploit_calls_bump_before_gather() -> None:
    """plan_exploit.py imports get_default_cost_governor + calls
    bump_for_parallel_streams BEFORE the asyncio.gather()."""
    src = _read("backend/core/ouroboros/governance/plan_exploit.py")
    assert "get_default_cost_governor" in src
    assert "bump_for_parallel_streams" in src
    # Must appear BEFORE the gather (sequence pin)
    bump_idx = src.find("bump_for_parallel_streams(_op_id")
    gather_idx = src.find("asyncio.gather(*(_generate_unit")
    assert bump_idx > 0, "bump call site missing"
    assert gather_idx > 0, "gather call site missing"
    assert bump_idx < gather_idx, (
        "bump_for_parallel_streams must be called BEFORE asyncio.gather "
        "(rooted-problem fix: cap must be sized for fan-out before the "
        "concurrent provider calls fire)"
    )


def test_pin_orchestrator_registers_default_cost_governor() -> None:
    """Orchestrator wires set_default_cost_governor at __init__ so pure
    helpers can look it up."""
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    assert "set_default_cost_governor" in src


def test_pin_cost_governor_has_bump_method() -> None:
    """CostGovernor exposes bump_for_parallel_streams with correct signature."""
    g = CostGovernor(CostGovernorConfig())
    assert hasattr(g, "bump_for_parallel_streams")
    assert callable(g.bump_for_parallel_streams)


def test_pin_parallel_stream_factor_default_1_1() -> None:
    """Default safety margin 1.1× per stream — env-overridable."""
    cfg = CostGovernorConfig()
    assert cfg.parallel_stream_factor == 1.1
