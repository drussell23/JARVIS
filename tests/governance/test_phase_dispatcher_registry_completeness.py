"""Spine — Stage 2 Fix C: PhaseRunnerRegistry completeness.

Root cause (v16 bt-2026-05-16-085224): the orchestrator's Stage 1.6
park/resume re-dispatched a ``GENERATE_RETRY``-phase ctx, but
``build_default_registry()`` registered only 9 phases and NOT
``GENERATE_RETRY`` / ``VALIDATE_RETRY`` — both first-class phases in
``OperationPhase`` + ``PHASE_TRANSITIONS``. The registry miss raised
``PhaseRunnerRegistryError`` → ``Unhandled exception in pipeline`` →
op failed with no patch (element-web → UNRESOLVED, not a scorer
judgement).

This spine pins the STRUCTURAL guarantee (not scanner appeasement):

  1. GENERATE_RETRY / VALIDATE_RETRY are registered, composing the
     SAME factory as their base phase (no new retry logic).
  2. ``assert_registry_complete`` fails fast (PhaseRunnerRegistryError
     at construction) if ANY non-terminal PHASE_TRANSITIONS target
     lacks a factory and is not on the explicit internal-only
     allowlist — derived from the canonical op_context tables, no
     hardcoded phase list.
  3. The exact v16 failure is reproduced on a deliberately-incomplete
     registry and proven closed by the real one.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import phase_dispatcher as pd
from backend.core.ouroboros.governance.op_context import (
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    OperationPhase,
)


# ---------------------------------------------------------------------------
# RETRY phases registered, composing the base factory
# ---------------------------------------------------------------------------


def test_generate_retry_registered_same_factory_as_generate():
    reg = pd.build_default_registry()
    assert reg.get(OperationPhase.GENERATE_RETRY) is pd._factory_generate
    assert reg.get(OperationPhase.GENERATE) is pd._factory_generate
    # composition, not duplication: identical factory object
    assert reg.get(OperationPhase.GENERATE_RETRY) is reg.get(
        OperationPhase.GENERATE
    )


def test_validate_retry_registered_same_factory_as_validate():
    reg = pd.build_default_registry()
    assert reg.get(OperationPhase.VALIDATE_RETRY) is pd._factory_validate
    assert reg.get(OperationPhase.VALIDATE_RETRY) is reg.get(
        OperationPhase.VALIDATE
    )


def test_build_default_registry_self_validates():
    # Construction itself runs assert_registry_complete; if it raised
    # this call would fail. Explicit re-assert for clarity.
    reg = pd.build_default_registry()
    pd.assert_registry_complete(reg)  # must NOT raise


# ---------------------------------------------------------------------------
# The structural completeness invariant (canonical-table derived)
# ---------------------------------------------------------------------------


def test_every_non_terminal_transition_phase_is_served():
    """Derived from op_context canonical tables — no hardcoded list.
    Every non-terminal phase the FSM can transition/resume into is
    EITHER registered OR explicitly internal-only."""
    reg = pd.build_default_registry()
    registered = set(reg.phases())
    reachable_non_terminal = {
        p for p in PHASE_TRANSITIONS if p not in TERMINAL_PHASES
    }
    unserved = (
        reachable_non_terminal
        - registered
        - pd.DISPATCHER_INTERNAL_ONLY_PHASES
    )
    assert unserved == set(), (
        f"non-terminal phases reachable but unserved: "
        f"{sorted(p.name for p in unserved)}"
    )


def test_internal_only_allowlist_is_real_nonterminal_and_unregistered():
    """The allowlist may not silently hide a terminal phase, a
    non-existent phase, or a phase that IS registered (which would
    make the exemption meaningless / mask drift)."""
    reg = pd.build_default_registry()
    registered = set(reg.phases())
    for p in pd.DISPATCHER_INTERNAL_ONLY_PHASES:
        assert isinstance(p, OperationPhase)
        assert p in PHASE_TRANSITIONS, f"{p} not a transition key"
        assert p not in TERMINAL_PHASES, f"{p} is terminal"
        assert p not in registered, (
            f"{p.name} is on the internal-only allowlist AND "
            f"registered — the exemption is meaningless; remove it "
            f"from one side"
        )
    # Pin the documented membership (drift tripwire).
    assert pd.DISPATCHER_INTERNAL_ONLY_PHASES == frozenset({
        OperationPhase.APPLY,
        OperationPhase.VERIFY,
        OperationPhase.VISUAL_VERIFY,
    })


# ---------------------------------------------------------------------------
# Reproduce the exact v16 failure → prove it is closed
# ---------------------------------------------------------------------------


def test_v16_failure_reproduced_on_incomplete_registry_then_closed():
    # Pre-fix shape: a registry WITHOUT GENERATE_RETRY behaves
    # exactly as the v16 soak — get() raises with the exact message.
    incomplete = pd.PhaseRunnerRegistry()
    incomplete.register(OperationPhase.GENERATE, pd._factory_generate)
    with pytest.raises(pd.PhaseRunnerRegistryError) as ei:
        incomplete.get(OperationPhase.GENERATE_RETRY)
    assert "no runner factory registered for phase GENERATE_RETRY" in str(
        ei.value
    )
    # And assert_registry_complete would have caught it at BUILD time
    # (fail-fast) — the structural guarantee that prevents it ever
    # reaching a live op again.
    with pytest.raises(pd.PhaseRunnerRegistryError) as ei2:
        pd.assert_registry_complete(incomplete)
    assert "GENERATE_RETRY" in str(ei2.value)

    # The real registry closes it.
    real = pd.build_default_registry()
    assert real.get(OperationPhase.GENERATE_RETRY) is pd._factory_generate


def test_assert_registry_complete_catches_a_dropped_core_phase():
    """Defense-in-depth: if a future refactor drops, say, GENERATE
    from build_default_registry, construction must fail loudly."""
    reg = pd.PhaseRunnerRegistry()
    # register everything EXCEPT GENERATE/GENERATE_RETRY
    reg.register(OperationPhase.CLASSIFY, pd._factory_classify)
    reg.register(OperationPhase.ROUTE, pd._factory_route)
    reg.register(
        OperationPhase.CONTEXT_EXPANSION, pd._factory_context_expansion)
    reg.register(OperationPhase.PLAN, pd._factory_plan)
    reg.register(OperationPhase.VALIDATE, pd._factory_validate)
    reg.register(OperationPhase.VALIDATE_RETRY, pd._factory_validate)
    reg.register(OperationPhase.GATE, pd._factory_gate)
    reg.register(OperationPhase.APPROVE, pd._factory_approve)
    reg.register(OperationPhase.COMPLETE, pd._factory_complete)
    with pytest.raises(pd.PhaseRunnerRegistryError) as ei:
        pd.assert_registry_complete(reg)
    msg = str(ei.value)
    assert "GENERATE" in msg and "registry incomplete" in msg
