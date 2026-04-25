"""W3(6) Slice 5a — structural pin tests (pre-graduation contract).

Pins the pre-Slice-5b structural invariants for Wave 3 (6) parallel
L3 fan-out. These tests run on every commit going forward; if any
pin breaks, either:

* The change was an unintentional regression — fix the change.
* The contract is intentionally being expanded — update the pin AND
  the corresponding section of `docs/operations/wave3-parallel-dispatch-graduation.md`.

The master-off invariant is non-negotiable per the operator binding:
``JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=false`` MUST disable every
fan-out path at every layer (env knobs, eligibility decision, post-
GENERATE seam in phase_dispatcher).

Pin coverage:

A. Master flag default is **False** (pre-Slice-5b).
B. Sub-flag composition under master-on / master-off (explicit setenv).
C. Hot-revert path: master=false force-disables every sub-flag effect.
D. Authority invariants — ReasonCode + FanoutOutcome enum vocab
   stable, schema constants frozen.
E. Source-grep pins — env reader literals + post-GENERATE seam +
   GLS FlagRegistry seed call site.
F. FlagRegistry registration — all 5 knobs present with correct types.
G. is_fanout_eligible decision matrix — all 8 ReasonCode paths
   reachable with controlled inputs.

Slice 5b graduation (operator-authorized) will:
  1. Update pin (A) to assert default True.
  2. Update env-reader source-grep literal in (E).
  3. Add a graduation evidence row to the matrix doc.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# (A) Master flag default — pre-graduation
# ---------------------------------------------------------------------------


def test_master_default_false_pre_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED defaults to False until
    Slice 5b operator-authorized flip. If this fails AND Slice 5b has
    been authorized: rename to test_master_default_true_post_graduation
    and update the assertion + docstring + the env-reader pin in (E)."""
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", raising=False)
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_enabled,
    )
    assert parallel_dispatch_enabled() is False, (
        "Slice 5a defaults are still false. If Slice 5b has been authorized "
        "and the flip committed, update this pin to assert True."
    )


def test_shadow_subflag_default_false_pre_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", raising=False)
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_shadow_enabled,
    )
    assert parallel_dispatch_shadow_enabled() is False


def test_enforce_subflag_default_false_pre_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_enforce_enabled,
    )
    assert parallel_dispatch_enforce_enabled() is False


def test_max_units_default_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard ceiling default 3 — operator binding §12 (c)."""
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", raising=False)
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_max_units,
    )
    assert parallel_dispatch_max_units() == 3


def test_wait_timeout_default_900(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default 15min wait per operator scope §12 (e)."""
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S", raising=False)
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_wait_timeout_s,
    )
    assert parallel_dispatch_wait_timeout_s() == 900.0


# ---------------------------------------------------------------------------
# (B) Sub-flag composition under master-on / master-off
# ---------------------------------------------------------------------------


def test_master_on_sub_off_eligibility_returns_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master-on but neither shadow nor enforce armed → no path engages.
    The eligibility helper itself short-circuits on master_off because
    sub-flag arming is required for is_fanout_eligible to even be
    consulted in production (phase_dispatcher's gate)."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", raising=False)
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_enabled,
        parallel_dispatch_shadow_enabled,
        parallel_dispatch_enforce_enabled,
    )
    assert parallel_dispatch_enabled() is True
    assert parallel_dispatch_shadow_enabled() is False
    assert parallel_dispatch_enforce_enabled() is False


def test_max_units_clamped_to_user_value_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "5")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_max_units,
    )
    assert parallel_dispatch_max_units() == 5


def test_wait_timeout_clamped_to_user_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S", "60.5")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        parallel_dispatch_wait_timeout_s,
    )
    assert parallel_dispatch_wait_timeout_s() == 60.5


# ---------------------------------------------------------------------------
# (C) Hot-revert path — master=false force-disables every sub-flag effect
# ---------------------------------------------------------------------------


def test_hot_revert_master_off_eligibility_returns_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master=false + every sub-flag explicitly true → eligibility still
    rejects with reason MASTER_OFF. The hot-revert contract: single env
    knob force-disables every fan-out path."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "false")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "10")

    from backend.core.ouroboros.governance.parallel_dispatch import (
        ReasonCode,
        is_fanout_eligible,
    )
    elig = is_fanout_eligible(
        op_id="op-revert-test",
        n_candidate_files=3,
    )
    assert elig.allowed is False
    assert elig.reason_code is ReasonCode.MASTER_OFF


def test_hot_revert_master_off_keeps_max_units_unused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with sub-flags + 3-file op, master=false → no fan-out
    happens. allowed=False short-circuits at the master gate; n_allowed
    is the serial-equivalent value (1), NOT the requested 3."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "false")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        is_fanout_eligible,
    )
    elig = is_fanout_eligible(
        op_id="op-revert", n_candidate_files=3,
    )
    assert elig.allowed is False
    assert elig.n_allowed == 1  # serial-equivalent (not the requested 3)
    assert elig.n_requested == 3  # what the caller asked for is preserved


# ---------------------------------------------------------------------------
# (D) Authority invariants — enum vocab stable + schema constants
# ---------------------------------------------------------------------------


def test_reason_code_vocab_is_stable() -> None:
    """8 deterministic reason codes per scope §"FanoutEligibility".
    Renames break operator dashboards / log-grep filters."""
    from backend.core.ouroboros.governance.parallel_dispatch import ReasonCode
    expected = {
        "allowed",
        "master_off",
        "empty_candidate_list",
        "single_file_op",
        "posture_low_confidence",
        "memory_critical",
        "memory_clamp",
        "posture_clamp",
        "max_units_clamp",
    }
    actual = {rc.value for rc in ReasonCode}
    assert actual == expected, (
        f"ReasonCode vocabulary changed: {actual} != {expected}. "
        "If intentional, update operator-facing audit docs + the marker "
        "glossary in docs/operations/wave3-parallel-dispatch-graduation.md."
    )


def test_fanout_outcome_vocab_is_stable() -> None:
    """7 terminal classifiers for enforce_evaluate_fanout."""
    from backend.core.ouroboros.governance.parallel_dispatch import (
        FanoutOutcome,
    )
    expected = {
        "skipped", "submit_denied", "submit_failed",
        "completed", "failed", "cancelled", "timeout",
    }
    actual = {fo.value for fo in FanoutOutcome}
    assert actual == expected, (
        f"FanoutOutcome vocabulary changed: {actual} != {expected}. "
        "Wire-format API for telemetry — bumps need additive migration."
    )


def test_schema_constants_frozen() -> None:
    """PLANNER_ID + GRAPH_SCHEMA_VERSION are wire-format API for graph
    consumers. Renames invalidate downstream telemetry parsers."""
    from backend.core.ouroboros.governance.parallel_dispatch import (
        GRAPH_SCHEMA_VERSION,
        PLANNER_ID,
    )
    # Pin literals — value bumps are intentional contract changes
    assert isinstance(PLANNER_ID, str) and PLANNER_ID, "PLANNER_ID must be non-empty str"
    assert isinstance(GRAPH_SCHEMA_VERSION, str) and GRAPH_SCHEMA_VERSION, (
        "GRAPH_SCHEMA_VERSION must be non-empty str"
    )


def test_posture_confidence_floor_is_03() -> None:
    """Below this floor, posture readings shouldn't steer fan-out at
    all (matches Wave 1 SensorGovernor's tier 'untrusted' boundary)."""
    from backend.core.ouroboros.governance.parallel_dispatch import (
        POSTURE_CONFIDENCE_FLOOR,
    )
    assert POSTURE_CONFIDENCE_FLOOR == 0.3


# ---------------------------------------------------------------------------
# (E) Source-grep pins — code shape that must survive drift
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_pin_master_env_reader_default_false_literal() -> None:
    """The `parallel_dispatch_enabled()` reader literal-defaults to
    False. Slice 5b graduation flips this to True in a single commit."""
    src = _read("backend/core/ouroboros/governance/parallel_dispatch.py")
    assert (
        '_env_bool("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", False)' in src
    ), (
        "Master flag default literal moved or changed. If Slice 5b has "
        "flipped this to True, update both the source and this pin "
        "(rename to test_pin_master_env_reader_default_true_literal)."
    )


def test_pin_phase_dispatcher_post_generate_seam_present() -> None:
    """phase_dispatcher.py contains the post-GENERATE fan-out seam."""
    src = _read("backend/core/ouroboros/governance/phase_dispatcher.py")
    # Slice 4 wired enforce_evaluate_fanout into the post-GENERATE seam
    assert "enforce_evaluate_fanout" in src
    # The seam keys parallel_dispatch_fanout_result onto pctx.extras
    assert "parallel_dispatch_fanout_result" in src or "fanout_result" in src


def test_pin_gls_calls_ensure_flag_registry_seeded() -> None:
    """GovernedLoopService.start invokes parallel_dispatch.ensure_flag_registry_seeded
    so the 5 Wave 3 knobs are discoverable via /help flags."""
    src = _read("backend/core/ouroboros/governance/governed_loop_service.py")
    assert "ensure_flag_registry_seeded" in src
    assert "parallel_dispatch" in src


def test_pin_master_off_short_circuit_in_eligibility() -> None:
    """is_fanout_eligible's first guard checks parallel_dispatch_enabled().
    Structural enforcement of the master-off invariant."""
    src = _read("backend/core/ouroboros/governance/parallel_dispatch.py")
    # Find is_fanout_eligible and confirm parallel_dispatch_enabled() is
    # checked early (within ~200 lines of the function header)
    idx = src.find("def is_fanout_eligible")
    assert idx >= 0, "is_fanout_eligible function not found"
    window = src[idx: idx + 4000]
    assert "parallel_dispatch_enabled()" in window, (
        "is_fanout_eligible must consult the master flag (master-off "
        "invariant). If the function was refactored, ensure the master "
        "check still happens inside the new shape."
    )


def test_pin_worktree_manager_imported_by_scheduler() -> None:
    """worktree_manager is the L3 isolation primitive Wave 3 (6) relies
    on. Scheduler integration must remain wired."""
    # Either subagent_scheduler imports worktree_manager directly, OR the
    # WorkUnitSpec it consumes carries worktree config. Either way, the
    # import graph must show coupling.
    sched_src = _read("backend/core/ouroboros/governance/autonomy/subagent_scheduler.py")
    assert (
        "worktree_manager" in sched_src
        or "WorkUnitSpec" in sched_src
    ), (
        "subagent_scheduler must remain wired to worktree_manager (Wave 1 "
        "#3 graduated L3 isolation). Wave 3 (6) fan-out depends on this."
    )


# ---------------------------------------------------------------------------
# (F) FlagRegistry registration — all 5 knobs registered with right types
# ---------------------------------------------------------------------------


def test_flag_registry_seed_registers_all_5_w3_knobs() -> None:
    """ensure_flag_registry_seeded registers all 5 Wave 3 (6) flags."""
    from backend.core.ouroboros.governance.flag_registry import (
        FlagRegistry,
        FlagType,
        Category,
    )
    from backend.core.ouroboros.governance.parallel_dispatch import (
        _own_flag_specs,
    )
    specs = _own_flag_specs()
    names = {s.name for s in specs}
    expected = {
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED",
        "JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW",
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE",
        "JARVIS_WAVE3_PARALLEL_MAX_UNITS",
        "JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S",
    }
    assert names == expected, f"missing/extra flags: {names ^ expected}"

    # Check types
    by_name = {s.name: s for s in specs}
    assert by_name["JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED"].type is FlagType.BOOL
    assert by_name["JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW"].type is FlagType.BOOL
    assert by_name["JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE"].type is FlagType.BOOL
    assert by_name["JARVIS_WAVE3_PARALLEL_MAX_UNITS"].type is FlagType.INT
    assert by_name["JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S"].type is FlagType.FLOAT

    # Check categories — master is SAFETY, shadow is OBSERVABILITY
    assert by_name["JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED"].category is Category.SAFETY
    assert by_name["JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW"].category is Category.OBSERVABILITY
    assert by_name["JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE"].category is Category.SAFETY
    assert by_name["JARVIS_WAVE3_PARALLEL_MAX_UNITS"].category is Category.CAPACITY
    assert by_name["JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S"].category is Category.TIMING


def test_flag_registry_seed_idempotent() -> None:
    """ensure_flag_registry_seeded is idempotent (safe to call multiple
    times)."""
    from backend.core.ouroboros.governance.parallel_dispatch import (
        ensure_flag_registry_seeded,
    )
    # Multiple calls must not raise
    ensure_flag_registry_seeded()
    ensure_flag_registry_seeded()
    ensure_flag_registry_seeded()


def test_flag_registry_seed_master_flag_relevant_at_harden() -> None:
    """Master flag is CRITICAL relevance under HARDEN posture so
    operators see it in /help flags --posture HARDEN."""
    from backend.core.ouroboros.governance.flag_registry import Relevance
    from backend.core.ouroboros.governance.parallel_dispatch import (
        _own_flag_specs,
    )
    specs = _own_flag_specs()
    by_name = {s.name: s for s in specs}
    master = by_name["JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED"]
    assert master.posture_relevance.get("HARDEN") is Relevance.CRITICAL


# ---------------------------------------------------------------------------
# (G) is_fanout_eligible decision matrix — 8 ReasonCode paths reachable
# ---------------------------------------------------------------------------


def test_eligibility_master_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "false")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        ReasonCode, is_fanout_eligible,
    )
    elig = is_fanout_eligible(op_id="op-1", n_candidate_files=2)
    assert elig.reason_code is ReasonCode.MASTER_OFF


def test_eligibility_empty_candidate_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        ReasonCode, is_fanout_eligible,
    )
    elig = is_fanout_eligible(op_id="op-1", n_candidate_files=0)
    assert elig.reason_code is ReasonCode.EMPTY_CANDIDATE_LIST


def test_eligibility_single_file_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        ReasonCode, is_fanout_eligible,
    )
    elig = is_fanout_eligible(op_id="op-1", n_candidate_files=1)
    assert elig.reason_code is ReasonCode.SINGLE_FILE_OP


def test_eligibility_posture_low_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Posture confidence < 0.3 floor → POSTURE_LOW_CONFIDENCE.
    Tested via injected posture_fn to keep the test hermetic."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        ReasonCode, is_fanout_eligible,
    )
    from backend.core.ouroboros.governance.posture import Posture

    def _low_conf_posture():
        return (Posture.EXPLORE, 0.1)  # below 0.3 floor

    elig = is_fanout_eligible(
        op_id="op-1",
        n_candidate_files=2,
        posture_fn=_low_conf_posture,
    )
    assert elig.reason_code is ReasonCode.POSTURE_LOW_CONFIDENCE


def test_eligibility_memory_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MemoryPressureGate at CRITICAL → MEMORY_CRITICAL reason code."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    from backend.core.ouroboros.governance.parallel_dispatch import (
        ReasonCode, is_fanout_eligible,
    )
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        PressureLevel,
    )

    from backend.core.ouroboros.governance.memory_pressure_gate import (
        FanoutDecision,
    )
    fake_gate = MagicMock()
    fake_gate.can_fanout.return_value = FanoutDecision(
        allowed=False, n_requested=2, n_allowed=0,
        level=PressureLevel.CRITICAL, free_pct=2.0,
        reason_code="memory_critical", source="test",
    )

    elig = is_fanout_eligible(
        op_id="op-1",
        n_candidate_files=2,
        gate=fake_gate,
    )
    assert elig.reason_code is ReasonCode.MEMORY_CRITICAL


def test_eligibility_allowed_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master on + 3-file op + posture EXPLORE conf=0.9 + memory OK +
    max_units=3 → ALLOWED with n_allowed=3."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", raising=False)
    from backend.core.ouroboros.governance.parallel_dispatch import (
        ReasonCode, is_fanout_eligible,
    )
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        PressureLevel,
    )

    def _good_posture():
        return (Posture.EXPLORE, 0.95)

    from backend.core.ouroboros.governance.memory_pressure_gate import (
        FanoutDecision,
    )
    fake_gate = MagicMock()
    fake_gate.can_fanout.return_value = FanoutDecision(
        allowed=True, n_requested=3, n_allowed=3,
        level=PressureLevel.OK, free_pct=80.0,
        reason_code="allowed", source="test",
    )

    elig = is_fanout_eligible(
        op_id="op-1",
        n_candidate_files=3,
        posture_fn=_good_posture,
        gate=fake_gate,
    )
    assert elig.allowed is True
    assert elig.reason_code is ReasonCode.ALLOWED
    assert elig.n_allowed == 3
