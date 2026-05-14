"""
Task #88b spine — thinking-aware outer-cascade budget widening.

v14-rev6 graduation soak proved: Task #88's inner rupture widening
(120s -> 360s) fires too late because the OUTER asyncio.wait_for
budget computed in ``CandidateGenerator._call_fallback`` (was 192.8s
for STANDARD route) terminates the Claude stream before the inner
matters.

Task #88b widens the outer cap when the op's task_complexity +
provider_route would produce a thinking-enabled call.  Single
policy with #88: outer >= inner for thinking.  Applied via ``max()``
so it never SHRINKS route-specific caps.

This spine pins:

  * Thinking-likely ops on non-immediate routes get the widened cap
    (default 360s).
  * IMMEDIATE-route ops skip the widening (reflex path, thinking off).
  * Trivial-complexity ops skip the widening (thinking off).
  * Empty/unstamped task_complexity skips the widening (conservative
    — won't engage until ComplexityClassifier has stamped the ctx).
  * Existing route-specific caps (COMPLEX=180s, read-only-BG~480+s)
    are NEVER shrunk by the widening (max() invariant).
  * Env-tunable via JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S.
  * FlagRegistry seed present.

The spine extracts the decision logic into a testable surface by
walking the source AST — same pattern as Task #88's thinking-aware
TTFT pin.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest


_CG_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# AST pins — Task #88b wiring + invariant
# ---------------------------------------------------------------------------


def test_ast_pin_call_fallback_has_thinking_aware_widening():
    """``_call_fallback`` MUST include the Task #88b widening logic.

    Without this, the outer budget remains at the route-base cap
    (120s for STANDARD) and Task #88's inner rupture widening can't
    engage.  v14-rev6 soak proved this empirically — outer fires
    first at 192.8-218.7s with first_token=NEVER.
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    assert "JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S" in src, (
        "_call_fallback MUST consult JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S "
        "— the operator-tunable outer cap for thinking-on calls (Task #88b)"
    )
    assert "_likely_thinking" in src, (
        "_call_fallback MUST compute _likely_thinking from task_complexity + "
        "provider_route (Task #88b signal)"
    )


def test_ast_pin_widening_uses_max_clamp():
    """The widening MUST use ``max()`` so it never SHRINKS existing
    route-specific caps (COMPLEX 180s, read-only-BG 480s+).

    This is the same invariant the PLAN-EXPLOIT override uses
    (line ~3785).  We pin it on the Task #88b path too.
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    # The widening line: _max_cap = max(_max_cap, _thinking_cap)
    assert "_max_cap = max(_max_cap, _thinking_cap)" in src, (
        "Task #88b widening MUST use max() against the existing "
        "_max_cap — never shrink route-specific caps"
    )


def test_ast_pin_widening_excludes_immediate_route():
    """IMMEDIATE-route ops MUST NOT trigger the widening.

    IMMEDIATE is the reflex path where thinking is intentionally
    OFF (per ``_resolve_thinking_budget`` in providers.py).  Widening
    the cap there would burn budget without benefit.
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    # The exclusion: _op_route not in ("immediate",)
    assert '_op_route not in ("immediate",)' in src, (
        "Task #88b MUST exclude IMMEDIATE route (no thinking on reflex path)"
    )


def test_ast_pin_widening_excludes_trivial_complexity():
    """Trivial-complexity ops MUST NOT trigger the widening.

    Trivial gets thinking=off per _resolve_thinking_budget — widening
    the outer cap would just slow trivial ops down.
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    assert '_task_complexity not in ("", "trivial")' in src, (
        "Task #88b MUST skip widening for trivial complexity AND "
        "empty (unstamped) complexity"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed pin
# ---------------------------------------------------------------------------


def test_seed_has_thinking_cap_flag():
    """JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S MUST be FlagRegistry-seeded."""
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S" in src, (
        "FlagRegistry must seed JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S"
    )
    idx = src.find("JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S")
    window = src[idx:idx + 1500]
    assert "Category.TIMING" in window, (
        "JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S MUST be Category.TIMING"
    )
    assert "default=360" in window, (
        "Default MUST be 360s per Task #88b design (matches Task #88's "
        "inner default for the single outer>=inner policy)"
    )
    assert "candidate_generator.py" in window, (
        "source_file MUST point at candidate_generator.py"
    )


# ---------------------------------------------------------------------------
# Single-policy invariant: Task #88b cap >= Task #88 inner cap
# ---------------------------------------------------------------------------


def test_thinking_caps_share_default_value():
    """Task #88 inner cap and Task #88b outer cap MUST share the
    same default value (360s).

    This is the load-bearing "outer >= inner" invariant from the
    operator binding 2026-05-13: 'single policy for thinking on
    ceilings, outer >= inner'.  If a future refactor splits the
    defaults, this pin fails.
    """
    # Read both defaults from the env-resolution call sites
    from backend.core.ouroboros.governance.stream_rupture import (
        stream_rupture_timeout_s,
    )
    # Clear env so we read defaults
    for k in ("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S",
              "JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S"):
        if k in os.environ:
            del os.environ[k]
    inner_default = stream_rupture_timeout_s(thinking_enabled=True)
    # Read the outer default from the candidate_generator constant
    src = _CG_SRC.read_text(encoding="utf-8")
    m = re.search(
        r'JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S", "(\d+(?:\.\d+)?)"',
        src,
    )
    assert m is not None, (
        "Could not locate JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S default"
    )
    outer_default = float(m.group(1))
    assert outer_default >= inner_default, (
        f"Task #88b outer cap ({outer_default}s) MUST be >= Task #88 "
        f"inner cap ({inner_default}s) — operator binding: "
        f"'outer >= inner for thinking-on'.  If diverging, document "
        f"the rationale + update this pin."
    )
    # And by design they share the same default value
    assert outer_default == inner_default == 360.0, (
        "Task #88b + #88 thinking defaults MUST share 360s for "
        "single-policy alignment"
    )
