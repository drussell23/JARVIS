"""Phase R1 — COMPLEX outer/inner timeout coherence spine.

Soak bt-2026-05-18-015317: the OUTER Iron-Gate ``_gen_timeout`` for
COMPLEX was 240s + ``_OUTER_GATE_GRACE_S`` 15s = 255s, while the INNER
fallback widened its cap to the 360s thinking window. The outer gate
killed GENERATE with ``CancelledError`` at ~255s before the inner
window could finish — psf never produced a candidate, no rubric.

Root fix (single source of truth, no duplication, no per-path drift):
``candidate_generator.gen_call_likely_thinking`` +
``fallback_thinking_cap_s`` are the ONE predicate + cap consumed by
BOTH the inner fallback AND the outer Iron-Gate ``_gen_timeout`` in
the LIVE path (``generate_runner``) and its dead twin
(``orchestrator``). The invariant ``outer >= inner`` therefore holds
by construction.

These pins are load-bearing: a future edit that re-inlines the
predicate on either path, or drops the floor before ``deadline=``,
silently re-opens the 255-vs-360 vector.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    gen_call_likely_thinking,
    fallback_thinking_cap_s,
)
from backend.core.ouroboros.governance import candidate_generator as _cg
from backend.core.ouroboros.governance import orchestrator as _orch
from backend.core.ouroboros.governance.phase_runners import (
    generate_runner as _gr,
)


# ---------------------------------------------------------------------------
# Shared predicate / cap — behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route,tc,want", [
    ("complex", "complex", True),    # the psf SWE-bench case
    ("complex", "simple", True),
    ("standard", "moderate", True),
    ("complex", "heavy_code", True),
    ("immediate", "complex", False),  # reflex path: thinking off
    ("complex", "trivial", False),    # trivial: no thinking
    ("background", "", False),        # empty complexity: no thinking
    ("COMPLEX", "Complex", True),     # case-insensitive
])
def test_gen_call_likely_thinking_truth_table(route, tc, want):
    assert gen_call_likely_thinking(route, tc) is want


def test_fallback_thinking_cap_default_and_env(monkeypatch):
    monkeypatch.delenv("JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S",
                       raising=False)
    assert fallback_thinking_cap_s() == 360.0
    monkeypatch.setenv("JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S", "500")
    assert fallback_thinking_cap_s() == 500.0
    monkeypatch.setenv("JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S", "junk")
    assert fallback_thinking_cap_s() == 360.0  # invalid → safe default


def test_coherence_invariant_complex_outer_ge_inner(monkeypatch):
    """The exact psf failure geometry: COMPLEX route base (240s) <
    inner thinking cap (360s). After the floor max(240, cap) the outer
    window is >= the inner cap → the 255s CancelledError cannot
    recur."""
    monkeypatch.delenv("JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S",
                       raising=False)
    complex_route_base = 240.0  # JARVIS_GEN_TIMEOUT_COMPLEX_S default
    assert gen_call_likely_thinking("complex", "complex") is True
    floored = max(complex_route_base, fallback_thinking_cap_s())
    assert floored >= fallback_thinking_cap_s()
    assert floored == 360.0  # > 255s outer-gate death point


# ---------------------------------------------------------------------------
# AST pins — single source of truth + floor placement
# ---------------------------------------------------------------------------


def test_ast_candidate_generator_uses_shared_helper_not_inline():
    """_call_fallback MUST consume the shared helper — the old inline
    `_likely_thinking = (... not in ("","trivial") ...)` predicate must
    be gone (single source of truth, no duplication)."""
    src = inspect.getsource(_cg)
    assert "def gen_call_likely_thinking(" in src
    assert "def fallback_thinking_cap_s(" in src
    # The historical inline predicate literal must no longer exist.
    assert '_task_complexity not in ("", "trivial")' not in src, (
        "inline thinking predicate re-introduced — duplicates the "
        "shared helper (Phase R1 single-source invariant)"
    )


def _floor_before_deadline(module) -> None:
    src = inspect.getsource(module)
    assert "gen_call_likely_thinking" in src, (
        f"{module.__name__} MUST consume the shared thinking predicate"
    )
    assert "fallback_thinking_cap_s" in src
    # The floor (max with the cap) must precede the GENERATE-block
    # deadline the outer wait_for derives from. NOTE: orchestrator.py
    # has many unrelated `deadline = datetime.now(` sites — search
    # *from the floor* so we pin the one the floor actually feeds.
    i_floor = src.index("gen_call_likely_thinking")
    i_deadline = src.index(
        "deadline = datetime.now(tz=timezone.utc) + timedelta(",
        i_floor,
    )
    assert i_floor < i_deadline, (
        f"{module.__name__}: thinking-cap floor MUST run BEFORE the "
        "GENERATE deadline= (outer wait_for must be >= inner cap)"
    )
    # The floor must actually mutate _gen_timeout via max() between
    # the predicate check and that deadline.
    assert "_gen_timeout = max(_gen_timeout, " in src[i_floor:i_deadline], (
        f"{module.__name__}: floor must `_gen_timeout = max(...)` "
        "before the deadline it feeds"
    )
    # No duplicated inline predicate literal on this path.
    assert '_task_complexity not in ("", "trivial")' not in src


def test_ast_generate_runner_floors_before_deadline_live_path():
    """generate_runner is the LIVE phase-dispatcher path — the floor
    MUST be here (orchestrator inline is the dead twin)."""
    _floor_before_deadline(_gr)


def test_ast_orchestrator_parity_floors_before_deadline():
    _floor_before_deadline(_orch)


def test_ast_both_paths_floor_before_their_adaptive_scale():
    """The thinking-cap floor MUST precede scale_gen_timeout on BOTH
    paths so adaptive scales the FLOORED value (one coherent number
    propagates to deadline + outer wait_for + tool-loop budget)."""
    for mod in (_gr, _orch):
        src = inspect.getsource(mod)
        i_floor = src.index("gen_call_likely_thinking")
        i_adaptive = src.index("scale_gen_timeout")
        assert i_floor < i_adaptive, (
            f"{mod.__name__}: thinking-cap floor MUST precede adaptive "
            "scale_gen_timeout"
        )
