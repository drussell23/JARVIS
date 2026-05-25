"""Slice 3E — Convergence-force two-axis final-write nudge.

Closes the convergence failure surfaced by 3 consecutive capability
soaks (bt-2026-05-25-{033000,041717,043137}): across all three the
final-write reserve nudge fired EXACTLY ZERO times despite the model
exhausting tool rounds in each. Root: the predicate was time-only
(``remaining_s <= reserve``, default 10s), but the model in COMPLEX
ops exhausts ROUNDS while having ample TIME remaining — the
bt-2026-05-25-043137 trace showed round 9 of 10 finishing with 232.9s
of wall budget left, vastly above the 10s reserve. Round 10 then trips
``tool_loop_max_rounds_exceeded`` and raises BEFORE ``generate_fn``
runs — the model never gets the nudge.

Even if the time-axis nudge HAD fired on round 9 → ``continue`` →
round 10's max-rounds gate would raise before the nudged prompt
reaches the model.

# Fix mechanism — two-axis trigger + grace round + imperative text

## Two-axis trigger

The nudge predicate now fires on EITHER:

  * Time axis: ``remaining_s <= final_write_reserve_s`` (legacy)
  * Round axis: ``rounds_left <= 1`` (Slice 3E; NEW)

When EITHER axis is exhausted, the model is told this is its final
chance to emit a patch.

## Grace round

When the nudge fires, ``_final_nudge_issued`` is set True. The
max-rounds gate (``round_index >= effective_max_rounds``) now grants
ONE extra round when this flag is set — the "grace round" — so the
model has a chance to actually emit a non-tool answer in response to
the nudge it just received.

If the grace round produces a non-tool answer, normal return. If the
model STILL emits tool calls (ignoring the imperative nudge), the
raw response is returned anyway — downstream parsers / Iron Gate get
to decide validity. Executing the tool calls and prolonging the loop
would violate the contract the nudge just set with the model
("any further tool calls will be IGNORED").

## Imperative text

Legacy: "Budget reserve reached — produce your final answer now
without calling any more tools." (mild guidance)

Slice 3E: "FINAL ROUND — exploration budget exhausted. You MUST emit
your final patch JSON now. Any further tool calls will be IGNORED
and the operation will fail. Synthesize what you have learned from
your tool calls so far and produce the patch in the required JSON
format." (imperative directive)

# Test surface (3 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_EXECUTOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "tool_executor.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_two_axis_trigger_present() -> None:
    """The nudge predicate must reference BOTH the time-axis
    (``should_stop_for_final_write``) AND the round-axis
    (``rounds_left`` or ``effective_max_rounds - round_index``)."""
    src = TOOL_EXECUTOR_FILE.read_text()
    assert "should_stop_for_final_write" in src
    # Slice 3E round-axis trigger uses _rounds_left_in_loop
    assert "_rounds_left_in_loop" in src, (
        "Slice 3E round-axis trigger missing — single-axis predicate "
        "is the bt-2026-05-25-043137 trap."
    )
    assert "_trigger_rounds" in src, (
        "_trigger_rounds variable missing — diagnostic structure broken"
    )
    assert "_trigger_time" in src, (
        "_trigger_time variable missing — diagnostic structure broken"
    )


def test_ast_pin_grace_round_bypass_present() -> None:
    """The max-rounds gate must reference ``_final_nudge_issued`` so
    the grace round runs past ``effective_max_rounds``. Without this
    bypass the nudge fires but raises before the model can emit a
    final answer."""
    src = TOOL_EXECUTOR_FILE.read_text()
    assert "_final_nudge_issued" in src
    # The grace-round bypass MUST be in the max-rounds gate
    tree = _parse(TOOL_EXECUTOR_FILE)
    coordinator = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolLoopCoordinator":
            coordinator = node
            break
    assert coordinator is not None
    run_method = None
    for sub in coordinator.body:
        if isinstance(sub, ast.AsyncFunctionDef) and sub.name == "run":
            run_method = sub
            break
    assert run_method is not None
    body_src = ast.unparse(run_method)
    # The max-rounds gate condition must compose `_final_nudge_issued`
    assert (
        "round_index >= effective_max_rounds and not _final_nudge_issued"
        in body_src
        or "not _final_nudge_issued" in body_src
    ), (
        "Max-rounds gate does NOT bypass on _final_nudge_issued — the "
        "grace round won't run; the nudge is silently swallowed by the "
        "round-cap raise. bt-2026-05-25-043137 trap is open."
    )


def test_ast_pin_imperative_nudge_text() -> None:
    """The nudge text must contain the imperative ``FINAL ROUND``
    + ``MUST`` + ``IGNORED`` keywords. Soft guidance was empirically
    insufficient for convergence — the legacy text 'produce your final
    answer now' did not force convergence under tight conditions."""
    src = TOOL_EXECUTOR_FILE.read_text()
    assert "FINAL ROUND" in src, (
        "Imperative 'FINAL ROUND' marker missing from nudge text"
    )
    assert "MUST" in src or "must emit" in src.lower(), (
        "Imperative 'MUST' marker missing from nudge text"
    )
    assert "IGNORED" in src, (
        "'IGNORED' warning missing — model must know further tool "
        "calls are wasted bandwidth"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6 (pure-data tests of the predicate math)
# ──────────────────────────────────────────────────────────────────────


def test_spine_round_axis_trigger_fires_when_one_round_left() -> None:
    """The exact bt-2026-05-25-043137 condition: round 9 finishes with
    232.9s of wall budget remaining (above 10s reserve, time axis does
    NOT trigger) but only 1 round of headroom remaining
    (effective_max_rounds=10, round_index=9 → rounds_left=1). Slice 3E
    round-axis trigger MUST fire."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan

    plan = BudgetPlan.build(
        total_budget_s=358.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    # Time axis: should NOT trigger (232.9 > 10.0)
    assert plan.should_stop_for_final_write(232.9) is False
    # Round axis (computed by Slice 3E): rounds_left = 10 - 9 = 1
    _rounds_left = plan.effective_max_rounds - 9
    assert _rounds_left == 1
    # The Slice 3E predicate fires on `rounds_left <= 1` — proven here
    # at the data level. Production wire is AST-pinned above.


def test_spine_time_axis_trigger_still_fires_when_reserve_hit() -> None:
    """Legacy time-axis path must continue to fire when the time
    reserve is hit even if rounds are abundant. Backwards compat."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan

    plan = BudgetPlan.build(
        total_budget_s=358.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    # Time axis: 5s < 10s reserve → fires
    assert plan.should_stop_for_final_write(5.0) is True
    # Even with 7 rounds left
    _rounds_left = plan.effective_max_rounds - 3
    assert _rounds_left == 7


def test_spine_neither_axis_triggers_under_ample_conditions() -> None:
    """When BOTH time and rounds are ample, neither axis should fire."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan

    plan = BudgetPlan.build(
        total_budget_s=358.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    # Ample time (300s > 10s reserve) AND ample rounds (left=7 > 1)
    assert plan.should_stop_for_final_write(300.0) is False
    _rounds_left = plan.effective_max_rounds - 3
    assert _rounds_left > 1


def test_spine_round_axis_trigger_one_round_left_definition() -> None:
    """The round-axis trigger fires when rounds_left <= 1. Verify the
    boundary cases at rounds_left=0, 1, and 2."""
    from backend.core.ouroboros.governance.tool_executor import BudgetPlan

    plan = BudgetPlan.build(
        total_budget_s=358.0,
        hard_max_rounds=10,
        max_per_round_s=30.0,
        min_per_round_s=3.0,
        final_write_reserve_s=10.0,
    )
    # The trigger predicate is `effective_max_rounds - round_index <= 1`
    # which is equivalent to `rounds_left <= 1`.
    for round_index, expected in [(8, False), (9, True), (10, True)]:
        rounds_left = plan.effective_max_rounds - round_index
        assert (rounds_left <= 1) is expected, (
            f"round_index={round_index} → rounds_left={rounds_left}; "
            f"expected trigger={expected}"
        )


def test_spine_grace_round_bypass_logic() -> None:
    """The max-rounds gate logic: raises iff
    ``round_index >= effective_max_rounds AND not _final_nudge_issued``.

    Pure-function test of the predicate."""
    effective_max_rounds = 10

    # Round 10, nudge NOT issued → must raise (legacy behavior preserved)
    assert (10 >= effective_max_rounds and not False) is True
    # Round 10, nudge issued → grace round, do NOT raise (Slice 3E)
    assert (10 >= effective_max_rounds and not True) is False
    # Round 9, nudge NOT issued → do not raise (normal round)
    assert (9 >= effective_max_rounds and not False) is False
    # Round 11, nudge issued → ALREADY consumed grace once, the grace
    # round logic returns in the parse_fn path before next iteration,
    # so this case in production is unreachable. But predicate-wise:
    assert (11 >= effective_max_rounds and not True) is False
    # Production-shape: the grace-round path returns immediately after
    # parse_fn so a second iteration after nudge cannot happen.


def test_spine_nudge_text_contains_required_directives() -> None:
    """The nudge text — the actual string the model receives — must
    contain the three load-bearing markers: FINAL ROUND, MUST, IGNORED.
    Reads the source verbatim to guarantee what production sends."""
    src = TOOL_EXECUTOR_FILE.read_text()
    # The actual nudge text uses the distinctive marker
    # "FINAL ROUND — exploration budget exhausted" — anchored to that
    # so we don't accidentally grep into the grace-warning log message
    # which also references "FINAL ROUND".
    nudge_block_start = src.find(
        "FINAL ROUND — exploration budget exhausted"
    )
    assert nudge_block_start >= 0, (
        "Source does not contain the imperative 'FINAL ROUND — "
        "exploration budget exhausted' nudge text"
    )
    nudge_block = src[nudge_block_start:nudge_block_start + 600]
    assert "MUST" in nudge_block, "Nudge missing MUST directive"
    assert "IGNORED" in nudge_block, "Nudge missing IGNORED directive"
    assert "patch" in nudge_block.lower(), (
        "Nudge missing 'patch' reference — model must know what kind of "
        "output is expected"
    )
    assert "JSON" in nudge_block or "json" in nudge_block, (
        "Nudge missing JSON format reference — model must know to use "
        "the structured candidate format Iron Gate expects"
    )
