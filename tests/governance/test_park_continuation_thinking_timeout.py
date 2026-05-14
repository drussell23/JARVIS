"""
Task #88d spine — fourth-layer coherence for park continuation timeout.

v14-rev8 surfaced: even with Task #88 (inner=360s) + #88b (outer=360s)
+ #88c (floor=360s) all firing, Claude was cancelled at elapsed=248s
while its 357.5s budget was still alive.  The cancel source: the
out-of-pool park continuation's own ``asyncio.wait_for(...,
timeout=gen_timeout + outer_grace_s)`` which inherits the legacy
GENERATE-phase wall (~200s for STANDARD + grace = ~230s).

Task #88d patches at the continuation's wait_for: when ``_likely_thinking``,
widen to JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S (default 390s =
single-policy 360s + 30s grace).  Non-thinking paths preserve the
legacy gen_timeout + outer_grace_s timeout bit-identical.

This spine pins:

  * Continuation wait_for uses ``_likely_thinking`` (reused signal
    from #88b/#88c — single-policy invariant).
  * Thinking default = 390s (≥ 360s single-policy floor + 30s grace).
  * Non-thinking path preserved bit-identical (uses gen_timeout +
    outer_grace_s, no widening).
  * Env override JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S works.
  * FlagRegistry seed present.
  * Single-policy invariant: continuation timeout >= single-policy
    (inner/outer/floor) for thinking-on so the fourth layer can't
    cancel a legitimate stream.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


_WRAPPER_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "generate_park_wrapper.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "flag_registry_seed.py"
)
_CG_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)


# ---------------------------------------------------------------------------
# AST pins — wrapper wiring
# ---------------------------------------------------------------------------


def test_ast_pin_continuation_uses_likely_thinking():
    """The wrapper MUST use _likely_thinking (same signal as Task #88b/#88c)
    to gate the continuation-timeout widening.  Reusing the signal
    keeps the single-policy invariant: one gate, one widening pattern.
    """
    src = _WRAPPER_SRC.read_text(encoding="utf-8")
    assert "_likely_thinking = (" in src, (
        "generate_park_wrapper.py must compute _likely_thinking in "
        "_spawn_park_continuation — same signal as Task #88b/#88c"
    )
    assert "JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S" in src, (
        "_spawn_park_continuation must consult "
        "JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S (Task #88d)"
    )


def test_ast_pin_non_thinking_path_preserves_legacy_timeout():
    """The else-branch MUST preserve the legacy ``gen_timeout +
    outer_grace_s`` value bit-identical for non-thinking callers.
    """
    src = _WRAPPER_SRC.read_text(encoding="utf-8")
    # The legacy computation, used in BOTH branches
    assert "_legacy_timeout = gen_timeout + outer_grace_s" in src, (
        "Task #88d must compute _legacy_timeout = gen_timeout + outer_grace_s "
        "as the baseline — non-thinking path uses it bit-identical"
    )
    # The thinking branch uses max() so it never SHRINKS
    assert "_continuation_timeout = max(_legacy_timeout, _thinking_cont_timeout)" in src, (
        "Task #88d thinking path MUST use max() against _legacy_timeout — "
        "never shrink the timeout below what non-thinking callers got"
    )
    # The else branch must use the unmodified legacy value
    assert "_continuation_timeout = _legacy_timeout" in src, (
        "Task #88d else-branch must set _continuation_timeout = _legacy_timeout "
        "for non-thinking callers (bit-identical)"
    )


def test_ast_pin_continuation_uses_widened_timeout():
    """The wait_for inside _continuation MUST consume the new
    ``_continuation_timeout`` variable (not the legacy expression).
    """
    src = _WRAPPER_SRC.read_text(encoding="utf-8")
    assert "timeout=_continuation_timeout," in src, (
        "_continuation's wait_for MUST use timeout=_continuation_timeout "
        "to consume the Task #88d widening"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed pin
# ---------------------------------------------------------------------------


def test_seed_has_continuation_thinking_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S" in src
    idx = src.find("JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S")
    window = src[idx:idx + 1500]
    assert "Category.TIMING" in window
    assert "default=390" in window, (
        "Task #88d default MUST be 390s = 360s single-policy + 30s grace "
        "(so a legitimate 360s stream completion never races the wait_for)"
    )
    assert "generate_park_wrapper.py" in window


# ---------------------------------------------------------------------------
# Single-policy invariant — 4 layers coherent for thinking-on
# ---------------------------------------------------------------------------


def test_four_layer_single_policy_invariant():
    """ALL four thinking-aware layers MUST satisfy:

        continuation timeout (Task #88d, layer 4)
            >= single-policy inner cap (Task #88, layer 1)
            == single-policy outer cap (Task #88b, layer 2)
            == single-policy floor    (Task #88c, layer 3)

    The +30s grace on the continuation timeout is what lets a
    legitimate 360s stream complete without racing the wait_for.

    If a future refactor breaks this, the v14-rev8 failure mode
    returns: continuation cancels at gen_timeout+grace (~230s) while
    a legitimate thinking stream is still active.
    """
    from backend.core.ouroboros.governance.stream_rupture import (
        stream_rupture_timeout_s,
    )
    inner = stream_rupture_timeout_s(thinking_enabled=True)
    cg_src = _CG_SRC.read_text(encoding="utf-8")
    m_outer = re.search(
        r'JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S", "(\d+(?:\.\d+)?)"', cg_src,
    )
    m_floor = re.search(
        r'JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S", "(\d+(?:\.\d+)?)"', cg_src,
    )
    wrapper_src = _WRAPPER_SRC.read_text(encoding="utf-8")
    m_cont = re.search(
        r'JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S", "(\d+(?:\.\d+)?)"',
        wrapper_src,
    )
    assert m_outer is not None and m_floor is not None and m_cont is not None, (
        "Failed to locate one of the three thinking-aware defaults"
    )
    outer = float(m_outer.group(1))
    floor = float(m_floor.group(1))
    cont = float(m_cont.group(1))

    # Invariant: continuation timeout >= single-policy 360s + small grace
    assert cont >= max(inner, outer, floor), (
        f"Four-layer invariant VIOLATED: continuation timeout ({cont}s) "
        f"must be >= max(inner={inner}, outer={outer}, floor={floor}). "
        f"If diverging, the v14-rev8 failure mode returns: "
        f"continuation cancels legitimate thinking streams before the "
        f"inner budget completes."
    )
    # Specific design: inner=outer=floor=360, cont=390 (30s grace)
    assert inner == outer == floor == 360.0, (
        f"Layers 1-3 MUST share 360s default.  Got: inner={inner}, "
        f"outer={outer}, floor={floor}"
    )
    assert cont == 390.0, (
        f"Continuation timeout MUST be 360s + 30s grace = 390s.  Got: {cont}"
    )


def test_non_thinking_continuation_timeout_unchanged():
    """The non-thinking branch MUST preserve ``gen_timeout +
    outer_grace_s`` bit-identical.  Walk the AST to assert this.
    """
    src = _WRAPPER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Find the _legacy_timeout assignment + its non-conditional usage
    found_legacy = False
    found_else_assign = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == "_legacy_timeout":
                        # Should be gen_timeout + outer_grace_s
                        rhs = ast.unparse(node.value)
                        if "gen_timeout + outer_grace_s" in rhs:
                            found_legacy = True
                    elif target.id == "_continuation_timeout":
                        rhs = ast.unparse(node.value)
                        if rhs == "_legacy_timeout":
                            found_else_assign = True
    assert found_legacy, (
        "Task #88d must assign _legacy_timeout = gen_timeout + outer_grace_s"
    )
    assert found_else_assign, (
        "Task #88d non-thinking else-branch must assign "
        "_continuation_timeout = _legacy_timeout (bit-identical preservation)"
    )
