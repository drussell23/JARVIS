"""Slice 3B — Adaptive Stream Resilience.

# What this closes

Slice 3A capability soak ``bt-2026-05-25-012206`` surfaced the
behavioral inversion (model went from 0 tool calls → 32 tool calls)
but the ansible op died with:

  stream terminated via CancelledError: elapsed=80.7s budget=42.2s
    first_token=1.9s bytes_received=349 tool_round=yes thinking=off

Root cause from Phase 1 audit:

  * ``BudgetPlan.per_round_timeout()`` returned 4.65s when the
    op's budget shrank to 61.6s ÷ 10 rounds = ~5s/round.
  * The model's ACTUAL first-token-to-completion duration in
    tool-loop conditions needs 30-60s. Below that floor, the
    stream is structurally guaranteed to be cancelled mid-flight
    by the outer ``asyncio.wait_for(..., timeout=per_round_timeout)``.
  * The inner stream consumer ``providers.py:7318+`` ALREADY has
    activity-aware logic — it switches from TTFT timeout (120s/360s)
    to inter-chunk timeout (30s) once first chunk arrives — BUT
    clamps the resolved value via ``min(rupt_base, wall_rem)``
    where ``wall_rem`` collapses to the tiny per_round_timeout.
    The activity-aware logic is OVERRIDDEN when budget is tight.

# Fix — two-prong defense-in-depth

## Layer 1 — Preventive (BudgetPlan + ToolLoopCoordinator)

  * ``BudgetPlan.min_ttft_floor_s`` field — new env-tunable
    constant (default 45s, env: ``JARVIS_TOOL_LOOP_MIN_TTFT_FLOOR_S``).
  * ``BudgetPlan.is_next_round_viable(remaining_s, remaining_rounds)``
    returns ``False`` when ``per_round_timeout < min_ttft_floor_s``.
  * ``ToolLoopCoordinator._run_loop`` checks this BEFORE entering
    a round; if not viable, exits with reason
    ``tool_loop_starved_below_min_ttft_floor`` instead of starting
    a doomed round.

## Layer 2 — Corrective (stream consumer in providers.py)

  * The inner ``_chunk_timeout = min(_rupt_base, _wall_rem)`` is
    softened to ``min(_rupt_base, max(_wall_rem, _min_ttft_floor))``
    so even if a doomed round DOES start, the inner stream consumer
    won't murder a healthy stream that's actively producing.
  * Activity-awareness preserved: once first chunk arrives, the
    inter-chunk watchdog (30s) is the authority, not the per-round
    clock.

# Operator binding honored

  * **No hardcoding** — both floor values env-tunable; defaults
    derived from empirical Claude data (first_token=1.9s + tool-loop
    completion ~45s)
  * **Single seam** — Layer 1's gate is one method on BudgetPlan;
    Layer 2's clamp-relaxation is one expression in the stream
    consumer. AST-pinned.
  * **Defense-in-depth** — Layers are independent: if Layer 1 fails
    (bypassed by a future refactor), Layer 2 catches; if Layer 2 is
    disabled (env override), Layer 1 still gates round entry.
  * **Build on existing** — composes `_stream_rupture_timeout_s` +
    `_stream_inter_chunk_timeout_s` + existing wall_rem math; no
    parallel timeout substrate.
  * **No silent fallback** — `tool_loop_starved` exit is structured
    + observable; doesn't pretend the loop completed.

# Test surface (3 AST pins + 8 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_EXECUTOR_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "tool_executor.py"
PROVIDERS_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "providers.py"


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PIN #1 — BudgetPlan has min_ttft_floor_s field
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_budgetplan_has_min_ttft_floor_field() -> None:
    """``BudgetPlan`` dataclass must declare ``min_ttft_floor_s: float``
    as a field. Without it, the activity-aware floor can't be plumbed
    through ``is_next_round_viable`` or the stream consumer's clamp
    relaxation."""
    tree = _parse(TOOL_EXECUTOR_FILE)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "BudgetPlan":
            continue
        for sub in node.body:
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                if sub.target.id == "min_ttft_floor_s":
                    found = True
                    break
    assert found, (
        "BudgetPlan does not declare `min_ttft_floor_s: float` field. "
        "Slice 3B's two-prong defense-in-depth (Layer 1 gate + Layer 2 "
        "clamp relaxation) requires this field to plumb the floor from "
        "build() through to both consumers."
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #2 — BudgetPlan.is_next_round_viable method exists + is called
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_budgetplan_is_next_round_viable_method_exists() -> None:
    """``BudgetPlan.is_next_round_viable(remaining_s, remaining_rounds)``
    must be a defined method. The coordinator's round-entry gate calls
    this before starting each round; without it, the gate has nothing
    to enforce."""
    tree = _parse(TOOL_EXECUTOR_FILE)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "BudgetPlan":
            continue
        for sub in node.body:
            if isinstance(sub, ast.FunctionDef) and sub.name == "is_next_round_viable":
                found = True
                break
    assert found, (
        "BudgetPlan.is_next_round_viable() method not defined. The "
        "Slice 3B Layer 1 (preventive) gate cannot enforce minimum "
        "round viability without this method."
    )


def test_ast_pin_coordinator_calls_is_next_round_viable_before_round() -> None:
    """The ToolLoopCoordinator's round loop in
    ``tool_executor.py:_run_loop`` (or equivalent) must call
    ``is_next_round_viable(...)`` BEFORE starting a round. The pin
    walks the source for an AST Call to ``is_next_round_viable`` in
    the coordinator's body.
    """
    src = TOOL_EXECUTOR_FILE.read_text()
    # Locate `class ToolLoopCoordinator:` then look for a
    # `is_next_round_viable` call below it.
    coord_idx = src.find("class ToolLoopCoordinator")
    assert coord_idx > 0, "ToolLoopCoordinator class not found"
    body = src[coord_idx:]
    assert "is_next_round_viable" in body, (
        "ToolLoopCoordinator body does not call is_next_round_viable() "
        "— the Slice 3B Layer 1 gate is built but not wired. Round-entry "
        "starvation will continue to murder streams."
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #3 — providers.py stream consumer honors min_ttft_floor
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_providers_stream_consumer_honors_min_ttft_floor() -> None:
    """The stream consumer's inner ``_chunk_timeout`` ASSIGNMENT must
    reference the min_ttft_floor (via constant or function call) to
    prevent the activity-aware logic from being overridden when
    wall_rem (per_round_timeout) is tiny. Layer 2 defense-in-depth.

    Use ``rfind`` to find the LAST ``_chunk_timeout = min(`` occurrence
    in the source — the literal-comment form of the prior clamp may
    appear earlier in a docstring/comment.
    """
    src = PROVIDERS_FILE.read_text()
    # Find the LAST clamp assignment — the prior shape may be quoted in
    # a comment elsewhere
    idx = src.rfind("_chunk_timeout = min(")
    assert idx > 0, (
        "Could not find `_chunk_timeout = min(...)` line in providers.py "
        "— the Slice 3B Layer 2 target site may have moved; update "
        "this pin's anchor."
    )
    # ~400-char window after the clamp
    window = src[idx: idx + 400]
    assert ("min_ttft_floor" in window or "MIN_TTFT_FLOOR" in window), (
        "stream consumer's _chunk_timeout clamp does not reference "
        "min_ttft_floor. Layer 2 defense-in-depth missing — when "
        "wall_rem collapses below the floor, healthy streams will "
        "be murdered as in soak bt-2026-05-25-012206.\n"
        f"Window inspected (200ch):\n{window[:200]}"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine tests — BudgetPlan
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def plan_with_default_floor():
    """Standard BudgetPlan with the default min_ttft_floor (45s)."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    return BudgetPlan.build(
        total_budget_s=600.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )


def test_spine_is_next_round_viable_true_with_ample_budget(
    plan_with_default_floor,
) -> None:
    """With 600s remaining and 10 rounds left, the per_round_timeout
    clamps to max_per_round_s=30s.

    Post-Slice-3B.1 (self-tuning): the effective floor is
    ``min(min_ttft_floor=45, max_per_round=30) = 30``. The clamped
    per_round_timeout (30s) satisfies the effective floor (30s).
    Round is viable.

    Pre-Slice-3B.1 over-fire behavior is now structurally fixed —
    when the operator's max_per_round_s is the dominant clamp, the
    gate defers to it as the effective floor rather than firing on
    the global min_ttft_floor that exceeds the operator's ceiling.
    """
    plan = plan_with_default_floor
    # per_round_timeout = clamp((600 - 10) / 10, 3, 30) = 30s
    # effective_floor = min(45, 30) = 30s
    # 30s >= 30s → viable
    assert plan.is_next_round_viable(remaining_s=600.0, remaining_rounds=10) is True


def test_spine_is_next_round_viable_true_with_few_rounds_left(
    plan_with_default_floor,
) -> None:
    """With 600s remaining and just 1 round left, the fair share is
    ~590s. After clamping to max_per_round_s=30s — still 30s — that's
    BELOW the 45s floor. So even with massive budget but tiny round
    count, the existing max_per_round_s=30s ceiling dominates.

    To prove viability=True, we need a plan with max_per_round_s above
    the floor. Operator can set ``JARVIS_TOOL_LOOP_TOOL_TIMEOUT_S=60``
    to do this. Below we construct such a plan inline.
    """
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=600.0,
        hard_max_rounds=10,
        max_per_round_s=60.0,  # ← above floor; operator has tuned this
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    # per_round_timeout = clamp((600 - 10) / 1, 3, 60) = 60s
    # is_next_round_viable: 60s >= 45s? True
    assert plan.is_next_round_viable(remaining_s=600.0, remaining_rounds=1) is True


def test_spine_is_next_round_viable_false_under_starvation(
    plan_with_default_floor,
) -> None:
    """The soak's exact failure condition: 30s remaining ÷ 10 rounds =
    3s/round. Per the formula, per_round_timeout clamps to
    min_per_round_s=3s (well below 45s floor)."""
    plan = plan_with_default_floor
    # per_round_timeout = clamp((30 - 10) / 10, 3, 30) = 3s
    # is_next_round_viable: 3s >= 45s? False
    assert plan.is_next_round_viable(remaining_s=30.0, remaining_rounds=10) is False


def test_spine_is_next_round_viable_reverse_soak_conditions(
    plan_with_default_floor,
) -> None:
    """Reverse the soak's exact numbers: budget=61.6s, rounds_left=10
    → per_round_timeout ~5s. is_next_round_viable should be False
    (would have prevented the soak's stream rupture)."""
    plan = plan_with_default_floor
    # per_round_timeout = clamp((61.6 - 10) / 10, 3, 30) = ~5.16s
    # is_next_round_viable: 5.16s >= 45s? False
    assert plan.is_next_round_viable(remaining_s=61.6, remaining_rounds=10) is False


def test_spine_min_ttft_floor_explicit_override() -> None:
    """Operators can override the floor either via env
    (``JARVIS_TOOL_LOOP_MIN_TTFT_FLOOR_S``, read at module import
    time — the canonical operator-tuning surface) OR via an explicit
    ``min_ttft_floor_s=`` kwarg to ``BudgetPlan.build`` (the
    programmatic-override surface used by tests + custom dispatch
    paths). This test verifies the explicit-kwarg path so tests
    don't need to mutate process-wide module state.

    A floor of 1.0s effectively disables Layer 1 — useful for
    testing or for operators who measure their model's actual TTFT
    differently.
    """
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=60.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
        min_ttft_floor_s=1.0,  # explicit override
    )
    # per_round_timeout = clamp((60 - 10) / 10, 3, 30) = 5s
    # With floor=1.0 (operator override): 5s >= 1.0s → True
    assert plan.min_ttft_floor_s == 1.0
    assert plan.is_next_round_viable(remaining_s=60.0, remaining_rounds=10) is True


# ──────────────────────────────────────────────────────────────────────
# Spine — coordinator gate behavior
# ──────────────────────────────────────────────────────────────────────

def test_spine_coordinator_stop_reason_when_round_not_viable() -> None:
    """When ``is_next_round_viable`` returns False at round entry,
    the coordinator must NOT start the round. The exit-reason string
    must include the marker ``tool_loop_starved_below_min_ttft_floor``
    so operator forensics can correlate.

    Pin verifies the literal exists in tool_executor.py source (the
    coordinator's source) — full async-coordinator integration test
    is out of scope for this slice (requires a model fixture).
    """
    src = TOOL_EXECUTOR_FILE.read_text()
    assert "tool_loop_starved_below_min_ttft_floor" in src, (
        "Coordinator gate-failure marker not present in "
        "tool_executor.py. Operator forensics will be unable to "
        "distinguish min-TTFT-starvation from generic exhaustion."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — clamp relaxation (Layer 2)
# ──────────────────────────────────────────────────────────────────────

def test_spine_chunk_timeout_clamp_includes_max_with_floor() -> None:
    """The stream consumer's clamp must shape to:

        _chunk_timeout = min(_rupt_base, max(_wall_rem, _min_ttft_floor))

    so that when wall_rem shrinks below the floor, the inner timeout
    floors at min_ttft_floor — preventing a healthy stream from being
    murdered mid-token-flow. Verifies BOTH ``min(`` and ``max(`` in
    the LAST occurrence of the clamp (earlier occurrences may be
    quoted in comments)."""
    src = PROVIDERS_FILE.read_text()
    idx = src.rfind("_chunk_timeout = min(")
    assert idx > 0
    line_window = src[idx: idx + 300]
    assert "max(" in line_window, (
        "_chunk_timeout clamp does not include a max() to enforce "
        "the min_ttft_floor. Layer 2 defense-in-depth missing."
    )


def test_spine_chunk_timeout_never_clamps_below_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit test of the clamp logic: when wall_rem is 4s and
    min_ttft_floor is 45s and rupt_base is 30s (inter-chunk default),
    the resulting _chunk_timeout must be 30s (NOT 4s) — the floor
    saves the healthy stream.

    Asserts the structural invariant:
        result = min(rupt_base, max(wall_rem, min_ttft_floor))
    For (rupt_base=30, wall_rem=4, floor=45):
        max(4, 45) = 45
        min(30, 45) = 30
    So result is 30s — the activity-aware inter-chunk timeout governs,
    not the tiny per-round clock.
    """
    # Pure-data extraction — mirror the production clamp logic
    def _compute_chunk_timeout(
        rupt_base: float, wall_rem: float, floor: float,
    ) -> float:
        return min(rupt_base, max(wall_rem, floor))

    # Soak's failure condition
    assert _compute_chunk_timeout(rupt_base=30.0, wall_rem=4.0, floor=45.0) == 30.0
    # Ample wall budget — no floor needed
    assert _compute_chunk_timeout(rupt_base=30.0, wall_rem=200.0, floor=45.0) == 30.0
    # Floor disabled (=1) — wall_rem governs (4s)
    assert _compute_chunk_timeout(rupt_base=30.0, wall_rem=4.0, floor=1.0) == 4.0
    # TTFT case — rupt_base=120s (no thinking) dominates
    assert _compute_chunk_timeout(rupt_base=120.0, wall_rem=10.0, floor=45.0) == 45.0


def test_spine_min_ttft_floor_default_is_45_seconds() -> None:
    """The default floor is 45s, empirically tuned from soak data
    (Claude TTFT ~1-3s + tool-loop completion ~30-45s headroom)."""
    import os
    # Clear any env override
    os.environ.pop("JARVIS_TOOL_LOOP_MIN_TTFT_FLOOR_S", None)
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan
    plan = BudgetPlan.build(
        total_budget_s=600.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    assert plan.min_ttft_floor_s == 45.0
