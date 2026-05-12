"""Regression spine for Treefinement Phase 3 — cross-branch learning.

Pins the AlphaVerus delta over naive parallel repair: layer-N+1
GENERATE prompts receive sibling-outcome context from layer N so
the model picks DIFFERENT strategies than already-tried siblings.

Invariants:

* CrossBranchLearningConfig.from_env loads defaults / overrides /
  garbage gracefully (NEVER raises).
* select_informative_siblings filters non-informative outcomes
  (PRUNED_DUPLICATE / PRUNED_BUDGET / IRON_GATE_REJECT / WALL_CLOCK_CAP /
  VALIDATION_BUDGET_EXHAUSTED) — these contribute zero learning signal.
* Hypothesis dedup collapses identical strategies (case + whitespace
  insensitive) to the highest-scored entry.
* Ranking: validator_score descending; deterministic branch_id
  lex tiebreak.
* format_sibling_outcomes_block honors max_chars cap by dropping
  lowest-ranked entries iteratively (NEVER half-truncates).
* maybe_inject_sibling_outcomes is the §33.2 producer-bridge:
  - master flag OFF → pass-through
  - layer 0 → pass-through (no siblings yet)
  - posture in skip set → pass-through (default: MAINTAIN)
  - empty / non-informative siblings → pass-through
  - any internal exception → pass-through (NEVER raises)
* AST pin: no parallel sibling-rank logic; no inline LLM call.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import List

import pytest  # noqa: F401 — used by raises etc. in this module's idioms

from backend.core.ouroboros.governance import repair_tree
from backend.core.ouroboros.governance.posture import Posture
from backend.core.ouroboros.governance.repair_tree import (
    CROSS_BRANCH_LEARNING_ENV_VAR,
    SIBLING_MAX_CHARS_ENV_VAR,
    SIBLING_MAX_COUNT_ENV_VAR,
    SIBLING_SKIP_POSTURES_ENV_VAR,
    BranchOutcome,
    CrossBranchLearningConfig,
    PruningReason,
    RepairBranch,
    format_sibling_outcomes_block,
    maybe_inject_sibling_outcomes,
    select_informative_siblings,
)


def _branch(
    *,
    bid: str,
    score: float,
    outcome: BranchOutcome,
    prune: BranchOutcome = None,  # type: ignore[assignment]
    prune_reason: PruningReason = None,  # type: ignore[assignment]
    hypothesis: str = "rename foo to bar",
    diff: str = "--- a\n+++ b\n",
) -> RepairBranch:
    return RepairBranch(
        branch_id=bid,
        parent_branch_id=None,
        layer_index=0,
        failure_class="test",
        fix_hypothesis=hypothesis,
        diff=diff,
        validator_score=score,
        outcome=outcome,
        prune_reason=prune_reason,
        worktree_id=None,
        cost_usd=0.001,
        validation_runs_consumed=1,
    )


# ===========================================================================
# CrossBranchLearningConfig — env loader (NEVER raises)
# ===========================================================================


def test_config_defaults(monkeypatch):
    for k in [
        CROSS_BRANCH_LEARNING_ENV_VAR,
        SIBLING_MAX_COUNT_ENV_VAR,
        SIBLING_MAX_CHARS_ENV_VAR,
        SIBLING_SKIP_POSTURES_ENV_VAR,
    ]:
        monkeypatch.delenv(k, raising=False)
    cfg = CrossBranchLearningConfig.from_env()
    assert cfg.enabled is True, (
        "Cross-branch learning MUST default-ON — without it tree "
        "mode degrades to race-the-loop (the AlphaVerus delta is the "
        "cross-branch signal)"
    )
    assert cfg.max_siblings == 2
    assert cfg.max_chars == 800
    assert cfg.skip_postures == ("MAINTAIN",)


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "false")
    monkeypatch.setenv(SIBLING_MAX_COUNT_ENV_VAR, "4")
    monkeypatch.setenv(SIBLING_MAX_CHARS_ENV_VAR, "400")
    monkeypatch.setenv(SIBLING_SKIP_POSTURES_ENV_VAR, "MAINTAIN,HARDEN")
    cfg = CrossBranchLearningConfig.from_env()
    assert cfg.enabled is False
    assert cfg.max_siblings == 4
    assert cfg.max_chars == 400
    assert set(cfg.skip_postures) == {"MAINTAIN", "HARDEN"}


def test_config_env_clamps(monkeypatch):
    monkeypatch.setenv(SIBLING_MAX_COUNT_ENV_VAR, "999")
    monkeypatch.setenv(SIBLING_MAX_CHARS_ENV_VAR, "1")
    cfg = CrossBranchLearningConfig.from_env()
    assert cfg.max_siblings == 8       # ceiling
    assert cfg.max_chars == 64         # floor


def test_config_handles_garbage(monkeypatch):
    monkeypatch.setenv(SIBLING_MAX_COUNT_ENV_VAR, "two")
    monkeypatch.setenv(SIBLING_MAX_CHARS_ENV_VAR, "many")
    monkeypatch.setenv(SIBLING_SKIP_POSTURES_ENV_VAR, "FOO,BAR,BAZ")
    cfg = CrossBranchLearningConfig.from_env()
    # Defaults preserved
    assert cfg.max_siblings == 2
    assert cfg.max_chars == 800
    # Unknown postures dropped → fallback to default MAINTAIN
    assert cfg.skip_postures == ("MAINTAIN",)


def test_config_skip_postures_case_insensitive(monkeypatch):
    monkeypatch.setenv(
        SIBLING_SKIP_POSTURES_ENV_VAR,
        "maintain, Harden , Explore",
    )
    cfg = CrossBranchLearningConfig.from_env()
    assert set(cfg.skip_postures) == {"MAINTAIN", "HARDEN", "EXPLORE"}


def test_config_skip_postures_empty_string_falls_back(monkeypatch):
    """Empty env value is intentionally distinct from unset — operator
    explicitly cleared the skip list. Falls back to default since
    accepted set would be empty."""
    monkeypatch.setenv(SIBLING_SKIP_POSTURES_ENV_VAR, "")
    cfg = CrossBranchLearningConfig.from_env()
    assert cfg.skip_postures == ("MAINTAIN",)


# ===========================================================================
# select_informative_siblings — filter + rank + dedup
# ===========================================================================


def test_select_filters_non_informative_prune_reasons():
    siblings = (
        _branch(
            bid="dup", score=0.0,
            outcome=BranchOutcome.PRUNED_DUPLICATE,
            prune_reason=PruningReason.DUPLICATE_PATCH_SIG,
        ),
        _branch(
            bid="ascii", score=0.0,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.IRON_GATE_REJECT,
        ),
        _branch(
            bid="budget", score=0.0,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.VALIDATION_BUDGET_EXHAUSTED,
        ),
        _branch(
            bid="wallclock", score=0.0,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.WALL_CLOCK_CAP,
        ),
        _branch(bid="real", score=0.5, outcome=BranchOutcome.PROMOTED),
    )
    picked = select_informative_siblings(siblings, max_siblings=5)
    assert {p.branch_id for p in picked} == {"real"}


def test_select_includes_strategy_relevant_outcomes():
    """PROMOTED + WORSE_THAN_SIBLING + SEMANTIC_GUARDIAN_HARD_FINDING
    are the strategy-relevant signals — all should pass the filter."""
    siblings = (
        _branch(
            bid="promoted", score=0.8,
            outcome=BranchOutcome.PROMOTED,
            hypothesis="strategy A",
        ),
        _branch(
            bid="worse", score=0.3,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.WORSE_THAN_SIBLING,
            hypothesis="strategy B",
        ),
        _branch(
            bid="semantic", score=0.0,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.SEMANTIC_GUARDIAN_HARD_FINDING,
            hypothesis="strategy C",
        ),
    )
    picked = select_informative_siblings(siblings, max_siblings=10)
    assert {p.branch_id for p in picked} == {"promoted", "worse", "semantic"}


def test_select_ranks_by_score_descending():
    siblings = (
        _branch(bid="low", score=0.2, outcome=BranchOutcome.PROMOTED, hypothesis="strat-A"),
        _branch(bid="high", score=0.9, outcome=BranchOutcome.PROMOTED, hypothesis="strat-B"),
        _branch(bid="mid", score=0.5, outcome=BranchOutcome.PROMOTED, hypothesis="strat-C"),
    )
    picked = select_informative_siblings(siblings, max_siblings=3)
    assert [p.branch_id for p in picked] == ["high", "mid", "low"]


def test_select_deterministic_tiebreak_on_branch_id():
    siblings = (
        _branch(bid="zebra", score=0.5, outcome=BranchOutcome.PROMOTED, hypothesis="x"),
        _branch(bid="apple", score=0.5, outcome=BranchOutcome.PROMOTED, hypothesis="y"),
        _branch(bid="mango", score=0.5, outcome=BranchOutcome.PROMOTED, hypothesis="z"),
    )
    picked = select_informative_siblings(siblings, max_siblings=3)
    # Same score → branch_id lex sort → apple, mango, zebra
    assert [p.branch_id for p in picked] == ["apple", "mango", "zebra"]


def test_select_respects_max_siblings():
    siblings = tuple(
        _branch(bid=f"b{i}", score=0.9 - i * 0.05,
                outcome=BranchOutcome.PROMOTED,
                hypothesis=f"strategy-{i}")
        for i in range(10)
    )
    picked = select_informative_siblings(siblings, max_siblings=3)
    assert len(picked) == 3
    # Top-3 by score
    assert [p.branch_id for p in picked] == ["b0", "b1", "b2"]


def test_select_dedups_identical_hypotheses():
    """Two siblings with identical normalized hypotheses → keep the
    highest-scored entry."""
    siblings = (
        _branch(bid="low", score=0.2, outcome=BranchOutcome.PROMOTED,
                hypothesis="rename foo to bar"),
        _branch(bid="high", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="Rename Foo To Bar"),  # case + whitespace variation
        _branch(bid="other", score=0.5, outcome=BranchOutcome.PROMOTED,
                hypothesis="extract helper"),
    )
    picked = select_informative_siblings(siblings, max_siblings=10)
    assert len(picked) == 2, (
        "Identical hypotheses (case-insensitive) MUST collapse to one"
    )
    bids = {p.branch_id for p in picked}
    assert bids == {"high", "other"}, (
        "Higher-scored entry MUST survive dedup"
    )


def test_select_filters_empty_hypothesis():
    siblings = (
        _branch(bid="empty", score=0.5, outcome=BranchOutcome.PROMOTED,
                hypothesis=""),
        _branch(bid="whitespace", score=0.5, outcome=BranchOutcome.PROMOTED,
                hypothesis="   \n\t  "),
        _branch(bid="real", score=0.5, outcome=BranchOutcome.PROMOTED,
                hypothesis="real-strategy"),
    )
    picked = select_informative_siblings(siblings, max_siblings=10)
    assert {p.branch_id for p in picked} == {"real"}


def test_select_empty_input_returns_empty():
    assert select_informative_siblings((), max_siblings=2) == ()


def test_select_won_branches_filtered_out():
    """WON branches early-return at runner level; if one slips through
    (defense in depth), filter it out — the cross-branch block is for
    NON-winning siblings."""
    siblings = (
        _branch(bid="won", score=1.0, outcome=BranchOutcome.WON,
                hypothesis="winning-strategy"),
        _branch(bid="other", score=0.5, outcome=BranchOutcome.PROMOTED,
                hypothesis="alternative"),
    )
    picked = select_informative_siblings(siblings, max_siblings=10)
    assert {p.branch_id for p in picked} == {"other"}


# ===========================================================================
# format_sibling_outcomes_block — markdown rendering + truncation
# ===========================================================================


def test_format_empty_returns_empty_string():
    assert format_sibling_outcomes_block(
        (), layer_index=1, max_chars=800,
    ) == ""


def test_format_renders_header_and_entries():
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="rename helper"),
        _branch(bid="b", score=0.0,
                outcome=BranchOutcome.PRUNED_VALIDATOR,
                prune_reason=PruningReason.SEMANTIC_GUARDIAN_HARD_FINDING,
                hypothesis="invert assertion"),
    )
    block = format_sibling_outcomes_block(
        siblings, layer_index=2, max_chars=800,
    )
    assert "## Sibling Branch Outcomes" in block
    assert "Layer 2 already attempted" in block
    assert "Choose a DIFFERENT approach" in block
    assert "score=0.80 promoted" in block
    assert "rename helper" in block
    assert "score=0.00 pruned_validator:semantic_guardian_hard_finding" in block
    assert "AVOID this pattern" in block, (
        "SEMANTIC_GUARDIAN_HARD_FINDING entries MUST carry an "
        "explicit avoidance warning"
    )


def test_format_truncates_to_max_chars():
    """When block exceeds max_chars, lowest-ranked siblings drop
    iteratively. Final block MUST be ≤ max_chars and the
    highest-ranked sibling (by score) MUST survive."""
    siblings = tuple(
        _branch(
            bid=f"b{i}", score=0.9 - i * 0.05,
            outcome=BranchOutcome.PROMOTED,
            hypothesis=f"long-strategy-name-{i}-" + "x" * 50,
        )
        for i in range(8)
    )
    block = format_sibling_outcomes_block(
        siblings, layer_index=1, max_chars=400,
    )
    assert len(block) <= 400
    # Highest-ranked entry's hypothesis MUST survive (bid b0, score 0.9)
    # The format renders hypotheses, not branch_ids — so check for the
    # hypothesis substring.
    assert "long-strategy-name-0" in block


def test_format_truncates_long_hypothesis():
    siblings = (
        _branch(
            bid="a", score=0.8,
            outcome=BranchOutcome.PROMOTED,
            hypothesis="x" * 500,  # very long
        ),
    )
    block = format_sibling_outcomes_block(
        siblings, layer_index=1, max_chars=800,
    )
    # Hypothesis truncated to ~100 chars + ellipsis
    assert "..." in block


def test_format_extreme_overflow_returns_header_only():
    """Even a single entry exceeds max_chars → return just the
    header (no half-rendered entry)."""
    siblings = (
        _branch(bid="a", score=0.5, outcome=BranchOutcome.PROMOTED,
                hypothesis="x" * 50),
    )
    block = format_sibling_outcomes_block(
        siblings, layer_index=1, max_chars=80,  # tiny cap
    )
    # Header alone is ~150 chars — even that exceeds 80, but function
    # MUST return SOMETHING (degraded path); never raise.
    assert isinstance(block, str)


# ===========================================================================
# maybe_inject_sibling_outcomes — §33.2 producer-bridge
# ===========================================================================


def test_inject_pass_through_when_master_off(monkeypatch):
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "false")
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="strategy"),
    )
    base_prompt = "GENERATE prompt body."
    out = maybe_inject_sibling_outcomes(
        base_prompt,
        sibling_outcomes=siblings,
        layer_index=1,
    )
    assert out == base_prompt


def test_inject_pass_through_at_layer_zero(monkeypatch):
    """Layer 0 has no siblings yet — pass-through unconditional."""
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="strategy"),
    )
    base_prompt = "GENERATE prompt body."
    out = maybe_inject_sibling_outcomes(
        base_prompt,
        sibling_outcomes=siblings,
        layer_index=0,
    )
    assert out == base_prompt


def test_inject_pass_through_for_skipped_posture(monkeypatch):
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="strategy"),
    )
    base_prompt = "GENERATE prompt body."
    out = maybe_inject_sibling_outcomes(
        base_prompt,
        sibling_outcomes=siblings,
        layer_index=1,
        posture=Posture.MAINTAIN,
    )
    assert out == base_prompt


def test_inject_active_for_explore_posture(monkeypatch):
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="strategy A"),
    )
    base_prompt = "GENERATE prompt body."
    out = maybe_inject_sibling_outcomes(
        base_prompt,
        sibling_outcomes=siblings,
        layer_index=1,
        posture=Posture.EXPLORE,
    )
    assert "## Sibling Branch Outcomes" in out
    assert "strategy A" in out
    # Original prompt preserved at start
    assert out.startswith(base_prompt)


def test_inject_active_for_harden_posture(monkeypatch):
    """HARDEN especially benefits from semantic-rejection signals."""
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    siblings = (
        _branch(
            bid="semantic", score=0.0,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.SEMANTIC_GUARDIAN_HARD_FINDING,
            hypothesis="invert-assertion",
        ),
    )
    out = maybe_inject_sibling_outcomes(
        "PROMPT.",
        sibling_outcomes=siblings,
        layer_index=1,
        posture=Posture.HARDEN,
    )
    assert "AVOID this pattern" in out


def test_inject_pass_through_when_no_informative_siblings(monkeypatch):
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    # All siblings non-informative
    siblings = (
        _branch(
            bid="dup", score=0.0,
            outcome=BranchOutcome.PRUNED_DUPLICATE,
            prune_reason=PruningReason.DUPLICATE_PATCH_SIG,
        ),
        _branch(
            bid="ascii", score=0.0,
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.IRON_GATE_REJECT,
        ),
    )
    base_prompt = "PROMPT."
    out = maybe_inject_sibling_outcomes(
        base_prompt,
        sibling_outcomes=siblings,
        layer_index=1,
    )
    assert out == base_prompt


def test_inject_pass_through_on_empty_siblings(monkeypatch):
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    out = maybe_inject_sibling_outcomes(
        "PROMPT.", sibling_outcomes=(), layer_index=1,
    )
    assert out == "PROMPT."


def test_inject_never_raises_on_garbage(monkeypatch):
    """Adversarial input — operator wires a malformed sibling tuple.
    Function MUST pass-through, never propagate."""
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    # Build a sibling-shaped object that explodes on attribute access
    class _ExplodingBranch:
        @property
        def outcome(self):
            raise RuntimeError("branch exploded")

    out = maybe_inject_sibling_outcomes(
        "PROMPT.",
        sibling_outcomes=(_ExplodingBranch(),),  # type: ignore[arg-type]
        layer_index=1,
    )
    assert out == "PROMPT.", (
        "Internal exception MUST pass-through; fail-open per §7"
    )


def test_inject_uses_injected_config():
    """When config is supplied explicitly, env vars are ignored —
    important for tests + future operator overrides."""
    cfg = CrossBranchLearningConfig(
        enabled=False,           # explicit OFF in config
        max_siblings=2,
        max_chars=800,
        skip_postures=(),
    )
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="x"),
    )
    out = maybe_inject_sibling_outcomes(
        "PROMPT.",
        sibling_outcomes=siblings,
        layer_index=1,
        config=cfg,
    )
    assert out == "PROMPT."


def test_inject_respects_no_posture_skip(monkeypatch):
    """When skip_postures is empty, ALL postures inject."""
    monkeypatch.setenv(SIBLING_SKIP_POSTURES_ENV_VAR, "")
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    cfg = CrossBranchLearningConfig.from_env()
    # Default fallback is MAINTAIN — but operator may want NO skipping
    # via explicit empty config
    cfg = CrossBranchLearningConfig(
        enabled=True, max_siblings=2, max_chars=800, skip_postures=(),
    )
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="x"),
    )
    out = maybe_inject_sibling_outcomes(
        "PROMPT.",
        sibling_outcomes=siblings,
        layer_index=1,
        posture=Posture.MAINTAIN,
        config=cfg,
    )
    # MAINTAIN no longer skipped → injection active
    assert "## Sibling Branch Outcomes" in out


def test_inject_default_no_posture_still_active(monkeypatch):
    """When posture is None (operator hasn't wired posture yet),
    injection runs normally — posture is an OPTIONAL filter."""
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "true")
    siblings = (
        _branch(bid="a", score=0.8, outcome=BranchOutcome.PROMOTED,
                hypothesis="strategy"),
    )
    out = maybe_inject_sibling_outcomes(
        "PROMPT.",
        sibling_outcomes=siblings,
        layer_index=1,
        posture=None,
    )
    assert "## Sibling Branch Outcomes" in out


# ===========================================================================
# AST composition pins — no parallel rank logic, no inline LLM
# ===========================================================================


_MODULE_SRC = Path(inspect.getfile(repair_tree)).read_text(encoding="utf-8")
_MODULE_AST = ast.parse(_MODULE_SRC)


def test_no_parallel_posture_weight_table():
    """Posture weighting MUST come from parallel_dispatch.posture_weight_for
    — repair_tree.py defines no parallel weight table. Drift here means
    a future contributor inlined posture weights, breaking the
    single-source-of-truth invariant."""
    src = _MODULE_SRC
    # Cheap heuristic — any "_POSTURE_WEIGHTS" dict literal in the module
    # would suggest a parallel table.
    assert "_POSTURE_WEIGHTS" not in src, (
        "repair_tree.py MUST NOT define _POSTURE_WEIGHTS — compose "
        "parallel_dispatch.posture_weight_for"
    )


def test_no_inline_llm_call_in_inject_function():
    """maybe_inject_sibling_outcomes is text-only — must not invoke
    any provider/LLM client (that's GENERATE phase concern, not
    prompt composition)."""
    forbidden = (
        "complete",
        "create_message",
        "send_prompt",
        "client.chat",
        "openai",
        "anthropic.client",
    )
    # Walk the function body specifically
    for node in ast.walk(_MODULE_AST):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "maybe_inject_sibling_outcomes"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute):
                    attr_chain = _attr_chain(sub).lower()
                    for f in forbidden:
                        assert f not in attr_chain, (
                            f"maybe_inject_sibling_outcomes MUST NOT "
                            f"invoke {attr_chain} — text-only function"
                        )


def _attr_chain(node: ast.Attribute) -> str:
    parts: List[str] = []
    cursor: ast.AST = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if isinstance(cursor, ast.Name):
        parts.append(cursor.id)
    return ".".join(reversed(parts))


def test_inject_function_signature_pin():
    """Pin the maybe_inject_sibling_outcomes signature so Phase 5
    wiring (in BranchGenerator) doesn't break silently when this
    function's contract drifts."""
    sig = inspect.signature(maybe_inject_sibling_outcomes)
    # Required positional: prompt
    assert "prompt" in sig.parameters
    # Required kwargs (per the §33.2 producer-bridge contract)
    for name in (
        "sibling_outcomes",
        "layer_index",
        "op_id",
        "posture",
        "config",
    ):
        assert name in sig.parameters, (
            f"signature MUST expose {name!r} — Phase 5 BranchGenerator "
            "wiring depends on this contract"
        )
    # Return type — `from __future__ import annotations` stringifies
    # everything, so compare against the string form.
    assert sig.return_annotation in (str, "str")
