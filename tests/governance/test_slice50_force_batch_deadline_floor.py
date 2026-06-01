"""Slice 50 Phase 2 — force-batch GENERATE-deadline floor.

Forensic basis (v45 probe, session bt-2026-06-01-034745, 2026-05-31):
    op-019e8158-e944 was route=standard, complexity=trivial. Its GENERATE
    deadline was the standard route base (JARVIS_GEN_TIMEOUT_STANDARD_S=220s);
    the R1 thinking-cap floor that would raise it to 360s only fires for
    "likely thinking" ops, and trivial ops do not qualify. The op force-batched
    (Slice 36: Claude disabled + standard route), so

        primary_budget = _compute_primary_budget(remaining=220, force_batch=True)
                       = min(220, JARVIS_DW_BATCH_TIMEOUT_S=300) = 220

    The debug.log confirms it: "primary_budget=220.0s ... remaining=220.0s" at
    the instant GENERATE dispatched. The async batch poll was then severed by
    the 220s outer deadline at 21:06:13 (TimeoutError elapsed=220.00s) while its
    own 300s lease still had runway. There was NO doomed RT streaming attempt —
    Slice 36 already skips RT for force-batch; the 98.5s StreamRender entry is
    the telemetry wrapper timing the batch poll (tokens=0).

Root cause: when an op force-batches but its route-base GENERATE deadline is
shorter than the batch lease (JARVIS_DW_BATCH_TIMEOUT_S), the outer deadline
severs the batch before its own lease expires.

Fix: floor a force-batch op's GENERATE deadline to batch_cap + a small overhead
so the outer window strictly exceeds the inner batch lease (mirror of the R1
thinking-cap floor). Non-force-batch ops pass through unchanged. This is safe by
construction: Slice 36 force-batch only engages when Claude is disabled (pure-DW
mode), so there is no Claude-cascade calibration to regress.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    apply_force_batch_deadline_floor,
    force_batch_gen_timeout_floor_s,
)


def test_floor_value_is_batch_cap_plus_overhead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "300")
    monkeypatch.setenv("JARVIS_FORCE_BATCH_GEN_OVERHEAD_S", "30")
    assert force_batch_gen_timeout_floor_s() == pytest.approx(330.0)


def test_floor_tracks_slice43_batch_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor is derived from the Slice 43 batch-timeout constant, not
    a second hardcoded value — change one, the floor follows."""
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "450")
    monkeypatch.setenv("JARVIS_FORCE_BATCH_GEN_OVERHEAD_S", "30")
    assert force_batch_gen_timeout_floor_s() == pytest.approx(480.0)


def test_non_force_batch_op_passes_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero regression: a non-force-batch op's deadline is never touched."""
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "300")
    monkeypatch.setenv("JARVIS_FORCE_BATCH_GEN_OVERHEAD_S", "30")
    assert apply_force_batch_deadline_floor(220.0, force_batch=False) == 220.0
    assert apply_force_batch_deadline_floor(120.0, force_batch=False) == 120.0


def test_force_batch_floors_short_standard_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact v45 case: standard base 220s force-batch -> floored to 330s
    so min(remaining, batch_cap=300) yields the FULL 300s lease."""
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "300")
    monkeypatch.setenv("JARVIS_FORCE_BATCH_GEN_OVERHEAD_S", "30")
    assert apply_force_batch_deadline_floor(220.0, force_batch=True) == pytest.approx(330.0)


def test_force_batch_never_shrinks_a_wider_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Floor is a max(): an already-wide window (e.g. COMPLEX R1-floored to
    360s) is preserved, never reduced."""
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "300")
    monkeypatch.setenv("JARVIS_FORCE_BATCH_GEN_OVERHEAD_S", "30")
    assert apply_force_batch_deadline_floor(360.0, force_batch=True) == pytest.approx(360.0)


def test_outer_window_strictly_exceeds_inner_batch_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coherence invariant: after the floor, the outer GENERATE deadline is
    STRICTLY greater than the inner batch lease, so the batch is never severed
    by the outer deadline at exactly its own expiry."""
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "300")
    monkeypatch.setenv("JARVIS_FORCE_BATCH_GEN_OVERHEAD_S", "30")
    batch_cap = 300.0
    floored = apply_force_batch_deadline_floor(220.0, force_batch=True)
    assert floored > batch_cap, "outer window must exceed inner batch lease"


# ---------------------------------------------------------------------------
# Wiring pins — Slice 45 dead-code lesson: the floor MUST be wired into the
# LIVE phase-dispatcher path (generate_runner.py), not just the orchestrator
# parity copy, or it never runs in production.
# ---------------------------------------------------------------------------


def test_floor_wired_in_live_generate_runner() -> None:
    import inspect

    import backend.core.ouroboros.governance.phase_runners.generate_runner as gr

    src = inspect.getsource(gr)
    assert "_slice36_should_force_batch" in src, "predicate not consulted in live path"
    assert "apply_force_batch_deadline_floor" in src, "floor not applied in live path"
    assert "force-batch deadline floor" in src, "Slice 50 floor block missing in live path"


def test_floor_wired_in_orchestrator_parity_path() -> None:
    import inspect

    import backend.core.ouroboros.governance.orchestrator as orch

    src = inspect.getsource(orch)
    assert "apply_force_batch_deadline_floor" in src, "floor not applied in orchestrator parity path"
    assert "force-batch deadline floor" in src, "Slice 50 floor block missing in orchestrator parity path"
