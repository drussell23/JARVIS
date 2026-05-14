"""
Task #88c spine — thinking-aware Claude-floor reservation.

v14-rev7 surfaced the third budget layer: even with Task #88 (inner
rupture 360s) and Task #88b (outer _max_cap 360s) widened, the Claude
fallback was given only 90s because the DW cascade consumed ~140s of
the ~200s op deadline.  The post-acquire refresh's floor
(``_FALLBACK_MIN_GUARANTEED_S=90s``) was the binding constraint.

Task #88c promotes the floor to 360s when the call is likely-thinking
(reusing the ``_likely_thinking`` signal Task #88b already computes).
The math then becomes:

    _min_guaranteed_s = 360s (was 90s)
    _budget_target = max(parent_remaining=60.1s, 360s) = 360s
    remaining = min(360s, _max_cap=360s) = 360s

Claude gets a guaranteed 360s even when the DW cascade exhausted the
op's parent_remaining to near-zero.  This is the operator-mandated
"Claude-floor reservation against op global deadline" — DW cannot
force Claude below the floor.

Single-policy invariant pinned: ``thinking floor >= max(inner cap,
outer cap)`` so the math is achievable.

This spine pins:

  * Floor selection uses ``_likely_thinking`` (Task #88b's signal).
  * Thinking floor default = 360s (matches #88 inner + #88b outer).
  * Non-thinking floor = 90s (legacy ``_FALLBACK_MIN_GUARANTEED_S``)
    — preserved bit-identical for IMMEDIATE / trivial paths.
  * Env override via ``JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S``.
  * FlagRegistry seed present.
  * Log line surfaces ``thinking=yes|no`` for observability.
  * Single-policy invariant: thinking floor >= max(inner, outer).
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
# AST pins — Task #88c wiring + invariant
# ---------------------------------------------------------------------------


def test_ast_pin_floor_selection_uses_likely_thinking():
    """``_call_fallback`` MUST gate the floor selection on ``_likely_thinking``.

    Reusing Task #88b's signal (instead of duplicating the
    task_complexity + provider_route check) keeps the single-policy
    invariant: same signal -> same widening across all three layers
    (inner / outer / floor).
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    assert "JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S" in src, (
        "_call_fallback must consult JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S "
        "(Task #88c env knob)"
    )
    assert "if _likely_thinking" in src, (
        "Floor selection MUST gate on _likely_thinking (Task #88b's signal). "
        "Duplicating the check would risk inconsistent dispatch."
    )
    # The conditional expression that picks the floor value
    assert "_min_guaranteed_s = (" in src, (
        "_call_fallback MUST compute _min_guaranteed_s with the thinking-aware "
        "conditional expression"
    )


def test_ast_pin_non_thinking_path_keeps_legacy_floor():
    """The else-branch of the floor selection MUST resolve to
    ``_FALLBACK_MIN_GUARANTEED_S`` (the 90s legacy default).

    Non-thinking IMMEDIATE / trivial paths MUST NOT inherit the 360s
    thinking widening — that would over-pay budget on reflex calls
    where thinking is intentionally off.
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    # The else fallback in the conditional expression
    assert "else _FALLBACK_MIN_GUARANTEED_S" in src, (
        "Non-thinking path MUST fall back to _FALLBACK_MIN_GUARANTEED_S "
        "(legacy 90s default) — Task #88c invariant"
    )


def test_ast_pin_log_line_surfaces_thinking_flag():
    """The post-acquire refresh log line MUST include a thinking=yes|no
    field for observability.  Without this, soak triage can't
    distinguish thinking-on vs thinking-off floor selection.
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    assert "thinking=%s" in src, (
        "Fallback refresh log line MUST include thinking=%s format spec"
    )
    assert '"yes" if _likely_thinking else "no"' in src, (
        "Fallback refresh log line MUST surface thinking=yes/no based on "
        "_likely_thinking"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed pin
# ---------------------------------------------------------------------------


def test_seed_has_min_guaranteed_thinking_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S" in src, (
        "FlagRegistry must seed JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S"
    )
    idx = src.find("JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S")
    window = src[idx:idx + 1500]
    assert "Category.TIMING" in window
    assert "default=360" in window, (
        "Default MUST be 360s per Task #88c — matches #88 inner + #88b outer "
        "(single-policy invariant)"
    )
    assert "candidate_generator.py" in window


# ---------------------------------------------------------------------------
# Single-policy invariant: thinking floor >= max(inner, outer)
# ---------------------------------------------------------------------------


def test_single_policy_invariant_thinking_floor_matches_caps(
    monkeypatch: pytest.MonkeyPatch,
):
    """The CORE Task #88c invariant: when thinking is enabled, the
    Claude-floor budget MUST be >= max(inner rupture cap, outer
    _max_cap) so the math ``remaining = min(_budget_target, _max_cap)``
    can actually deliver the full thinking budget.

    If a future refactor breaks this invariant, the Claude call will
    be silently clamped down to the smaller cap, and v14-rev7's
    failure mode returns.
    """
    from backend.core.ouroboros.governance.stream_rupture import (
        stream_rupture_timeout_s,
    )
    # Clear all three layer env vars to read defaults
    for k in ("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S",
              "JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S",
              "JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S"):
        monkeypatch.delenv(k, raising=False)

    inner = stream_rupture_timeout_s(thinking_enabled=True)
    # Read the outer + floor defaults from the candidate_generator source
    src = _CG_SRC.read_text(encoding="utf-8")
    m_outer = re.search(
        r'JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S", "(\d+(?:\.\d+)?)"', src,
    )
    m_floor = re.search(
        r'JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S", "(\d+(?:\.\d+)?)"', src,
    )
    assert m_outer is not None and m_floor is not None, (
        "Failed to locate JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S "
        "and JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S defaults"
    )
    outer = float(m_outer.group(1))
    floor = float(m_floor.group(1))

    # The invariant: floor MUST be >= max(inner, outer) so the floor
    # actually delivers a usable budget after the min() clamps.
    assert floor >= max(inner, outer), (
        f"Task #88c single-policy invariant VIOLATED: "
        f"thinking floor ({floor}s) must be >= max(inner={inner}s, "
        f"outer={outer}s).  If diverging, document the rationale + "
        f"update this pin.  Otherwise v14-rev7's failure mode returns: "
        f"DW cascade exhausts parent_remaining, post-acquire refresh "
        f"caps Claude below the inner/outer 360s thinking window."
    )

    # And by design all three share 360s for clean alignment
    assert inner == outer == floor == 360.0, (
        f"Task #88/#88b/#88c thinking defaults MUST share 360s for "
        f"single-policy alignment.  Got: inner={inner} outer={outer} "
        f"floor={floor}"
    )


def test_legacy_floor_default_preserved():
    """The legacy _FALLBACK_MIN_GUARANTEED_S=90 MUST stay 90 for
    non-thinking paths — backward compat.
    """
    src = _CG_SRC.read_text(encoding="utf-8")
    # The legacy constant declaration
    assert 'OUROBOROS_FALLBACK_MIN_GUARANTEED_S", "90"' in src, (
        "Legacy _FALLBACK_MIN_GUARANTEED_S default MUST stay 90s "
        "(non-thinking floor)"
    )
