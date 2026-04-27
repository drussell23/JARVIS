"""Phase 9.1 — Live-Fire Graduation Soak Harness regression spine.

Pins:
  * CADENCE_POLICY extension (24 substrate flags incl. 9 new from v2.52)
  * Dependency map shape + bit-rot guard
  * Pick-next algorithm (substrate-before-surface)
  * Outcome classification (CLEAN/INFRA/RUNNER/MIGRATION decision tree)
  * Subprocess runner injection + master/pause flag matrix
  * Evidence row persistence (JSONL + flock)
  * Authority/cage invariants
  * NEVER-raises smoke
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.adaptation import (
    graduation_ledger as _ledger_mod,
)
from backend.core.ouroboros.governance.graduation import (
    live_fire_soak as _soak,
)
from backend.core.ouroboros.governance.graduation.live_fire_soak import (
    BATTLE_TEST_SCRIPT_REL,
    DEFAULT_COST_CAP_USD,
    DEFAULT_MAX_WALL_SECONDS,
    DEFAULT_SUBPROCESS_TIMEOUT_S,
    EVIDENCE_SCHEMA_VERSION,
    EvidenceRow,
    HarnessResult,
    HarnessStatus,
    LiveFireSoakHarness,
    MAX_FAILURE_CLASS_COUNT_KEYS,
    MAX_HISTORY_FILE_BYTES,
    MAX_HISTORY_RECORDS_LOADED,
    MAX_NOTES_CHARS,
    all_dependency_flags,
    classify_outcome,
    get_default_harness,
    get_dependencies,
    history_path,
    is_paused,
    is_soak_harness_enabled,
    reset_default_harness,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch):
    """Clean live-fire + graduation-ledger env per test."""
    keys = [
        k for k in os.environ.keys()
        if (
            k.startswith("JARVIS_LIVE_FIRE_GRADUATION_")
            or k.startswith("JARVIS_GRADUATION_LEDGER_")
        )
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    reset_default_harness()
    _ledger_mod.reset_default_ledger()
    yield
    reset_default_harness()
    _ledger_mod.reset_default_ledger()


@pytest.fixture
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point both the graduation ledger AND the live-fire history at
    isolated tmp paths."""
    target = tmp_path / "graduation_ledger.jsonl"
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH", str(target),
    )
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    history = tmp_path / "live_fire_history.jsonl"
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH", str(history),
    )
    _ledger_mod.reset_default_ledger()
    reset_default_harness()
    return {"ledger_path": target, "history_path": history}


@pytest.fixture
def harness(isolated_ledger):
    """Fresh harness pointed at the isolated paths."""
    return get_default_harness()


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_schema_version():
    assert EVIDENCE_SCHEMA_VERSION == "1.0"


def test_caps_sane():
    assert MAX_HISTORY_FILE_BYTES >= 1024 * 1024
    assert MAX_HISTORY_RECORDS_LOADED >= 1000
    assert MAX_NOTES_CHARS >= 500
    assert MAX_FAILURE_CLASS_COUNT_KEYS >= 16


def test_battle_test_script_path_constant():
    assert BATTLE_TEST_SCRIPT_REL == Path("scripts") / "ouroboros_battle_test.py"


def test_default_subprocess_params_sane():
    assert 0.10 <= DEFAULT_COST_CAP_USD <= 5.00
    assert 600 <= DEFAULT_MAX_WALL_SECONDS <= 7200
    assert DEFAULT_SUBPROCESS_TIMEOUT_S > DEFAULT_MAX_WALL_SECONDS


# ---------------------------------------------------------------------------
# Master flag matrix
# ---------------------------------------------------------------------------


def test_master_flag_default_off():
    assert is_soak_harness_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_master_truthy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", val,
    )
    assert is_soak_harness_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
def test_master_falsy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", val,
    )
    assert is_soak_harness_enabled() is False


def test_pause_flag_default_off():
    assert is_paused() is False


def test_pause_flag_truthy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED", "true")
    assert is_paused() is True


# ---------------------------------------------------------------------------
# CADENCE_POLICY — 24-flag pin (bit-rot guard)
# ---------------------------------------------------------------------------


def test_cadence_policy_count_pinned_at_24():
    """Bit-rot guard: any added flag must update this pin so the
    graduation surface is reviewed."""
    assert len(_ledger_mod.CADENCE_POLICY) == 24


def test_cadence_policy_includes_phase_8_substrate():
    names = {e.flag_name for e in _ledger_mod.CADENCE_POLICY}
    for f in [
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        "JARVIS_MULTI_OP_TIMELINE_ENABLED",
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
    ]:
        assert f in names


def test_cadence_policy_includes_phase_8_surface():
    names = {e.flag_name for e in _ledger_mod.CADENCE_POLICY}
    for f in [
        "JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED",
        "JARVIS_PHASE8_SSE_BRIDGE_ENABLED",
        "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED",
    ]:
        assert f in names


def test_cadence_policy_includes_curiosity_engine():
    names = {e.flag_name for e in _ledger_mod.CADENCE_POLICY}
    assert "JARVIS_CURIOSITY_ENGINE_ENABLED" in names


def test_cadence_policy_no_duplicate_flags():
    names = [e.flag_name for e in _ledger_mod.CADENCE_POLICY]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Dependency map
# ---------------------------------------------------------------------------


def test_get_dependencies_unknown_flag_empty():
    assert get_dependencies("JARVIS_DOES_NOT_EXIST") == frozenset()


def test_get_dependencies_phase_8_surface_depends_on_substrate():
    deps = get_dependencies("JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED")
    assert "JARVIS_DECISION_TRACE_LEDGER_ENABLED" in deps
    assert "JARVIS_LATENT_CONFIDENCE_RING_ENABLED" in deps
    assert "JARVIS_FLAG_CHANGE_EMITTER_ENABLED" in deps
    assert "JARVIS_LATENCY_SLO_DETECTOR_ENABLED" in deps
    assert "JARVIS_MULTI_OP_TIMELINE_ENABLED" in deps


def test_get_dependencies_curiosity_depends_on_hypothesis_probe():
    deps = get_dependencies("JARVIS_CURIOSITY_ENGINE_ENABLED")
    assert "JARVIS_HYPOTHESIS_PROBE_ENABLED" in deps


def test_get_dependencies_pass_c_activation_depends_on_loader_and_writer():
    """Pass C mining-surface flags require BOTH the corresponding
    loader flag AND the meta-governor YAML writer to be graduated."""
    deps = get_dependencies("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED")
    assert "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS" in deps
    assert "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED" in deps


def test_all_dependency_flags_subset_of_known_flags():
    """Bit-rot guard: every flag named as a dependency must itself
    be in CADENCE_POLICY (else dependency can never resolve)."""
    known = _ledger_mod.known_flags()
    for dep in all_dependency_flags():
        assert dep in known, (
            f"dependency {dep!r} is not a known graduation flag"
        )


def test_substrate_flags_have_no_dependencies():
    """The 5 Phase 8 substrate flags + 5 Phase 7 loader flags + 1
    Phase 7.6 hypothesis probe flag are all leaf-level — no deps."""
    leaf_flags = [
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
        "JARVIS_MULTI_OP_TIMELINE_ENABLED",
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS",
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
        "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
        "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
        "JARVIS_HYPOTHESIS_PROBE_ENABLED",
    ]
    for f in leaf_flags:
        assert get_dependencies(f) == frozenset(), (
            f"{f} should be a leaf-level substrate flag"
        )


# ---------------------------------------------------------------------------
# Outcome classification — decision tree
# ---------------------------------------------------------------------------


def test_classify_complete_no_failures_clean():
    summary = {
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
    }
    outcome, runner_attr, _ = classify_outcome(summary)
    assert outcome == "clean"
    assert runner_attr is False


def test_classify_runner_class_failure_blocks():
    summary = {
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {"phase_runner_error": 1},
    }
    outcome, runner_attr, notes = classify_outcome(summary)
    assert outcome == "runner"
    assert runner_attr is True
    assert "phase_runner_error" in notes


def test_classify_infra_class_failure_waiver():
    summary = {
        "session_outcome": "incomplete_kill",
        "stop_reason": "sigterm",
        "failure_class_counts": {"provider_rate_limited": 2},
    }
    outcome, runner_attr, _ = classify_outcome(summary)
    assert outcome == "infra"
    assert runner_attr is False


def test_classify_shutdown_noise_infra():
    """sigterm/sighup/sigint/wall_clock_cap are infra-class waivers
    even with no other failure_class_counts."""
    for stop_reason in [
        "sigterm", "sighup", "sigint",
        "wall_clock_cap", "harness_idle_timeout",
    ]:
        summary = {
            "session_outcome": "incomplete_kill",
            "stop_reason": stop_reason,
            "failure_class_counts": {},
        }
        outcome, _, _ = classify_outcome(summary)
        assert outcome == "infra", f"{stop_reason} should be infra"


def test_classify_migration_stop_reason():
    summary = {
        "session_outcome": "incomplete",
        "stop_reason": "schema_version_skew",
        "failure_class_counts": {},
    }
    outcome, runner_attr, _ = classify_outcome(summary)
    assert outcome == "migration"
    assert runner_attr is False


def test_classify_unknown_default_runner():
    """Conservative default — unknown stop_reason WITH NO failure
    counts is conservatively classified RUNNER (blocks rather
    than silently waivers)."""
    summary = {
        "session_outcome": "incomplete",
        "stop_reason": "mystery_unknown_reason",
        "failure_class_counts": {},
    }
    outcome, runner_attr, _ = classify_outcome(summary)
    assert outcome == "runner"
    assert runner_attr is True


def test_classify_runner_takes_priority_over_infra():
    """When BOTH runner-class AND infra-class failures present,
    runner blocks (more conservative)."""
    summary = {
        "session_outcome": "incomplete",
        "stop_reason": "sigterm",
        "failure_class_counts": {
            "phase_runner_error": 1,
            "provider_rate_limited": 1,
        },
    }
    outcome, runner_attr, _ = classify_outcome(summary)
    assert outcome == "runner"
    assert runner_attr is True


def test_classify_non_dict_summary_runner_fault():
    outcome, runner_attr, notes = classify_outcome("not a dict")  # type: ignore[arg-type]
    assert outcome == "runner"
    assert runner_attr is True
    assert "non_dict" in notes


def test_classify_non_dict_failure_counts_treated_as_empty():
    """If failure_class_counts isn't a dict, treat as empty
    rather than crashing."""
    summary = {
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": "garbage",
    }
    outcome, _, _ = classify_outcome(summary)
    assert outcome == "clean"


def test_classify_zero_count_runner_class_does_not_block():
    """Counter at 0 doesn't trigger blocking — only positive."""
    summary = {
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {"phase_runner_error": 0},
    }
    outcome, _, _ = classify_outcome(summary)
    assert outcome == "clean"


# ---------------------------------------------------------------------------
# Pick-next algorithm
# ---------------------------------------------------------------------------


def _seed_clean_sessions(
    flag: str, *, n: int = 3,
) -> None:
    ledger = _ledger_mod.get_default_ledger()
    for i in range(n):
        ledger.record_session(
            flag_name=flag, session_id=f"sid-{flag}-{i}",
            outcome=_ledger_mod.SessionOutcome.CLEAN,
            recorded_by="test",
        )


def test_pick_next_no_graduations_returns_substrate_flag(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    """With nothing graduated, pick-next must return a leaf-level
    substrate flag (no deps to satisfy)."""
    flag = harness.pick_next_flag()
    assert flag is not None
    deps = get_dependencies(flag)
    assert deps == frozenset(), f"{flag} should be substrate-level"


def test_pick_next_alpha_stable(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    """Two consecutive pick_next() calls without state change return
    the same flag."""
    f1 = harness.pick_next_flag()
    f2 = harness.pick_next_flag()
    assert f1 == f2


def test_pick_next_skips_graduated(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    """Once a flag is graduated, pick_next must skip it."""
    first = harness.pick_next_flag()
    assert first is not None
    _seed_clean_sessions(first, n=3)
    second = harness.pick_next_flag()
    assert second != first


def test_pick_next_unblocks_dependent_after_dep_graduation(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    """Phase 8 surface flag becomes pickable only after all its
    Phase 8 substrate deps are graduated."""
    surface = "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED"
    deps = get_dependencies(surface)
    assert deps  # sanity
    # Initially blocked: not in pick-next output.
    seen_before: set = set()
    for _ in range(50):
        f = harness.pick_next_flag()
        if f is None:
            break
        seen_before.add(f)
        _seed_clean_sessions(f, n=3)
        if f == surface:
            break
    # Surface should appear in seen_before only AFTER all deps were
    # graduated. Since we always seed-3-clean as we pick, by the
    # time surface appears, deps must already be graduated.
    if surface in seen_before:
        ledger = _ledger_mod.get_default_ledger()
        for d in deps:
            assert ledger.is_eligible(d), (
                f"surface {surface} picked before dep {d} graduated"
            )


def test_pick_next_returns_none_when_all_graduated(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    for entry in _ledger_mod.CADENCE_POLICY:
        _seed_clean_sessions(
            entry.flag_name,
            n=entry.required_clean_sessions,
        )
    assert harness.pick_next_flag() is None


# ---------------------------------------------------------------------------
# queue_view
# ---------------------------------------------------------------------------


def test_queue_view_returns_24_flags(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    rows = harness.queue_view()
    assert len(rows) == 24


def test_queue_view_marks_graduated_correctly(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    _seed_clean_sessions("JARVIS_HYPOTHESIS_PROBE_ENABLED", n=3)
    rows = harness.queue_view()
    by_name = {r["flag_name"]: r for r in rows}
    assert by_name["JARVIS_HYPOTHESIS_PROBE_ENABLED"]["graduated"] is True
    assert by_name[
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    ]["graduated"] is False


def test_queue_view_deps_satisfied_flag(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    rows = harness.queue_view()
    by_name = {r["flag_name"]: r for r in rows}
    # Substrate flags always have deps_satisfied=True.
    assert by_name[
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    ]["deps_satisfied"] is True
    # Surface flag pre-graduation has deps_satisfied=False.
    assert by_name[
        "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED"
    ]["deps_satisfied"] is False


# ---------------------------------------------------------------------------
# run_soak — short-circuit paths (no subprocess invocation)
# ---------------------------------------------------------------------------


def test_run_soak_skipped_disabled_when_master_off(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    # master flag default off via _reset_env autouse fixture
    result = harness.run_soak()
    assert result.status == HarnessStatus.SKIPPED_DISABLED


def test_run_soak_skipped_paused(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED", "true",
    )
    result = harness.run_soak()
    assert result.status == HarnessStatus.SKIPPED_PAUSED


def test_run_soak_skipped_unknown_flag(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    result = harness.run_soak(flag_name="JARVIS_DOES_NOT_EXIST")
    assert result.status == HarnessStatus.SKIPPED_UNKNOWN_FLAG


def test_run_soak_skipped_when_no_eligible_flag(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    for entry in _ledger_mod.CADENCE_POLICY:
        _seed_clean_sessions(
            entry.flag_name, n=entry.required_clean_sessions,
        )
    result = harness.run_soak()
    assert result.status == HarnessStatus.SKIPPED_NO_FLAG


def test_run_soak_skipped_deps_not_graduated_for_explicit_surface_flag(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    # No deps graduated; explicit surface flag must be rejected.
    result = harness.run_soak(
        flag_name="JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED",
    )
    assert result.status == HarnessStatus.SKIPPED_DEPS_NOT_GRADUATED


# ---------------------------------------------------------------------------
# run_soak with injected subprocess runner — clean / infra / runner
# ---------------------------------------------------------------------------


def _fake_runner_returning(
    summary: Dict[str, Any], *, debug_tail: str = "",
):
    """Build a subprocess-runner stub returning the given summary."""

    def runner(
        *,
        env: Dict[str, str],
        cost_cap_usd: float,
        max_wall_seconds: int,
        timeout_s: int,
        project_root: Path,
    ) -> Tuple[int, Dict[str, Any], str]:
        runner.last_env = env  # type: ignore[attr-defined]
        runner.last_cost_cap = cost_cap_usd  # type: ignore[attr-defined]
        runner.last_max_wall = max_wall_seconds  # type: ignore[attr-defined]
        return (0, summary, debug_tail)

    return runner


def test_run_soak_clean_outcome_records(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    fake = _fake_runner_returning({
        "session_id": "bt-clean-1",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
        "cost_total": 0.42,
        "duration_s": 1234.5,
        "ops_count": 5,
    })
    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    assert result.status == HarnessStatus.OK
    assert result.evidence is not None
    assert result.evidence.outcome == "clean"
    assert result.evidence.session_id == "bt-clean-1"
    assert result.evidence.cost_total_usd == 0.42
    # Canonical ledger should now show 1 clean session.
    progress = _ledger_mod.get_default_ledger().progress(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
    )
    assert progress["clean"] == 1


def test_run_soak_runner_outcome_blocks(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    fake = _fake_runner_returning({
        "session_id": "bt-runner-1",
        "session_outcome": "incomplete",
        "stop_reason": "iron_gate_violation",
        "failure_class_counts": {"iron_gate_violation": 1},
    })
    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    assert result.status == HarnessStatus.OK
    assert result.evidence is not None
    assert result.evidence.outcome == "runner"
    assert result.evidence.runner_attributed is True
    progress = _ledger_mod.get_default_ledger().progress(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
    )
    assert progress["runner"] == 1
    assert progress["clean"] == 0


def test_run_soak_infra_outcome_waiver(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    fake = _fake_runner_returning({
        "session_id": "bt-infra-1",
        "session_outcome": "incomplete_kill",
        "stop_reason": "sigterm",
        "failure_class_counts": {},
    })
    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    assert result.status == HarnessStatus.OK
    assert result.evidence is not None
    assert result.evidence.outcome == "infra"
    progress = _ledger_mod.get_default_ledger().progress(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
    )
    assert progress["infra"] == 1
    assert progress["clean"] == 0


def test_run_soak_passes_target_flag_in_subprocess_env(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    """The subprocess env must contain ONLY the target flag (+ deps)
    set to 'true'; not other JARVIS_* flags."""
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    fake = _fake_runner_returning({
        "session_id": "bt-env-1",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
    })
    target = "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED"
    deps = get_dependencies(target)
    # Graduate deps so the surface flag is pickable.
    for d in deps:
        _seed_clean_sessions(d, n=3)
    result = harness.run_soak(
        flag_name=target, subprocess_runner=fake,
    )
    assert result.status == HarnessStatus.OK
    env = fake.last_env  # type: ignore[attr-defined]
    assert env[target] == "true"
    for d in deps:
        assert env[d] == "true"
    # An unrelated non-dep substrate flag must NOT be set to 'true'
    # by the harness (only by inheriting the parent process env).
    untouched = "JARVIS_FLAG_CHANGE_EMITTER_ENABLED"
    if untouched in deps:
        return  # skip — accidentally a dep
    if os.environ.get(untouched) is None:
        # parent doesn't set → harness must not inject 'true' here
        assert env.get(untouched) != "true"


def test_run_soak_passes_cost_cap_and_wall_seconds(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    fake = _fake_runner_returning({
        "session_id": "bt-args-1",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
    })
    harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        cost_cap_usd=1.25, max_wall_seconds=900,
        subprocess_runner=fake,
    )
    assert fake.last_cost_cap == 1.25  # type: ignore[attr-defined]
    assert fake.last_max_wall == 900  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# run_soak — failure paths
# ---------------------------------------------------------------------------


def test_run_soak_subprocess_failed(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )

    def boom(**kwargs):
        raise RuntimeError("simulated subprocess failure")

    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=boom,
    )
    assert result.status == HarnessStatus.SUBPROCESS_FAILED
    assert result.evidence is not None
    assert result.evidence.outcome == "infra"  # waiver row


def test_run_soak_subprocess_timeout(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    import subprocess as _sp
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )

    def hang(**kwargs):
        raise _sp.TimeoutExpired(cmd="x", timeout=1)

    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=hang,
    )
    assert result.status == HarnessStatus.SUBPROCESS_TIMEOUT


def test_run_soak_summary_parse_failed(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )

    def returns_garbage(**kwargs):
        return (1, "not a dict", "")

    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=returns_garbage,
    )
    assert result.status == HarnessStatus.SUMMARY_PARSE_FAILED


# ---------------------------------------------------------------------------
# Evidence persistence
# ---------------------------------------------------------------------------


def test_evidence_row_to_dict_includes_schema_version():
    row = EvidenceRow(
        schema_version="1.0",
        harness_status="ok",
        flag_name="JARVIS_X",
        session_id="sid-1",
        outcome="clean",
        runner_attributed=False,
        stop_reason="ok",
        cost_total_usd=0.5,
        duration_s=100.0,
        ops_count=3,
        failure_class_counts={},
        deps_set=["JARVIS_X"],
        started_at_iso="2026-01-01T00:00:00Z",
        started_at_epoch=1700000000.0,
        finished_at_iso="2026-01-01T00:01:00Z",
        finished_at_epoch=1700000060.0,
        notes="test",
    )
    d = row.to_dict()
    assert d["schema_version"] == "1.0"
    assert d["flag_name"] == "JARVIS_X"
    assert d["outcome"] == "clean"


def test_evidence_persisted_to_history_jsonl(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    fake = _fake_runner_returning({
        "session_id": "bt-persist-1",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
    })
    harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    history_p = isolated_ledger["history_path"]
    assert history_p.exists()
    text = history_p.read_text()
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "bt-persist-1"
    assert rows[0]["flag_name"] == "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    assert rows[0]["outcome"] == "clean"


def test_evidence_for_flag_returns_only_that_flag(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    fake = _fake_runner_returning({
        "session_id": "bt-A",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
    })
    harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    fake_b = _fake_runner_returning({
        "session_id": "bt-B",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
    })
    harness.run_soak(
        flag_name="JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        subprocess_runner=fake_b,
    )
    rows_a = harness.evidence_for_flag(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
    )
    assert len(rows_a) == 1
    assert rows_a[0]["session_id"] == "bt-A"
    rows_b = harness.evidence_for_flag(
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
    )
    assert len(rows_b) == 1
    assert rows_b[0]["session_id"] == "bt-B"


def test_history_corrupt_lines_skipped(
    isolated_ledger, harness: LiveFireSoakHarness,
):
    history_p = isolated_ledger["history_path"]
    history_p.parent.mkdir(parents=True, exist_ok=True)
    history_p.write_text(
        "not-json\n"
        '{"flag_name": "JARVIS_X", "session_id": "good"}\n'
        '{"missing_flag_name": true}\n'
        "garbage\n",
    )
    # Should not raise, should return only well-formed dict rows.
    rows = harness.all_evidence()
    assert any(r.get("session_id") == "good" for r in rows)
    assert all(isinstance(r, dict) for r in rows)


def test_failure_counts_truncated(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    huge = {f"class-{i}": 1 for i in range(MAX_FAILURE_CLASS_COUNT_KEYS + 50)}
    fake = _fake_runner_returning({
        "session_id": "bt-truncate",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": huge,
    })
    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    assert result.evidence is not None
    assert len(result.evidence.failure_class_counts) <= (
        MAX_FAILURE_CLASS_COUNT_KEYS
    )


# ---------------------------------------------------------------------------
# NEVER-raises smoke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_summary", [
    None,
    "string",
    [],
    42,
    {"session_outcome": None},
    {"failure_class_counts": "not-a-dict"},
])
def test_classify_outcome_never_raises(bad_summary):
    out = classify_outcome(bad_summary)
    assert isinstance(out, tuple)
    assert len(out) == 3


def test_run_soak_never_raises_on_any_runner_exception(
    isolated_ledger, harness: LiveFireSoakHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    for exc in [
        ValueError("v"), KeyError("k"), OSError("o"),
        RuntimeError("r"), MemoryError("m"),
    ]:
        captured_exc = exc

        def boom(_captured=captured_exc, **kwargs):
            raise _captured

        result = harness.run_soak(
            flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
            subprocess_runner=boom,
        )
        assert result.status == HarnessStatus.SUBPROCESS_FAILED
        assert isinstance(result.evidence, EvidenceRow)


# ---------------------------------------------------------------------------
# Authority / cage invariants
# ---------------------------------------------------------------------------


def test_does_not_import_gate_modules():
    import ast
    import inspect
    src = inspect.getsource(_soak)
    tree = ast.parse(src)
    banned = [
        "orchestrator", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "policy_engine",
        "candidate_generator", "tool_executor", "change_engine",
    ]
    for node in ast.walk(tree):
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module]
        for mod in names:
            for token in banned:
                assert token not in mod, (
                    f"live_fire_soak imports {mod!r} (banned token "
                    f"{token!r})"
                )


def test_top_level_imports_stdlib_only():
    """Top-level imports stdlib + typing only. Substrate imports
    happen lazily inside helper bodies."""
    import ast
    import inspect
    src = inspect.getsource(_soak)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    # The graduation_ledger import is the one allowed governance import
    # at module level — it's a sibling-package data dependency, not a
    # gate module.
    forbidden = {
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.tool_executor",
    }
    leaked = forbidden & set(top_level)
    assert not leaked


def test_no_secret_leakage_in_module_constants():
    text = repr(vars(_soak))
    for needle in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE KEY"):
        assert needle not in text


def test_public_api_count_pinned():
    """Bit-rot guard."""
    public = sorted(
        n for n in dir(_soak)
        if not n.startswith("_") and (
            callable(getattr(_soak, n))
            or n.isupper()
        )
    )
    # Allow growth but not silent removal.
    required = {
        "LiveFireSoakHarness",
        "EvidenceRow",
        "HarnessResult",
        "HarnessStatus",
        "classify_outcome",
        "get_default_harness",
        "get_dependencies",
        "all_dependency_flags",
        "history_path",
        "is_paused",
        "is_soak_harness_enabled",
        "reset_default_harness",
        "EVIDENCE_SCHEMA_VERSION",
        "DEFAULT_COST_CAP_USD",
        "DEFAULT_MAX_WALL_SECONDS",
        "DEFAULT_SUBPROCESS_TIMEOUT_S",
        "MAX_HISTORY_FILE_BYTES",
        "MAX_HISTORY_RECORDS_LOADED",
        "MAX_NOTES_CHARS",
        "MAX_FAILURE_CLASS_COUNT_KEYS",
        "BATTLE_TEST_SCRIPT_REL",
    }
    missing = required - set(public)
    assert not missing, f"public API regression: {missing}"


def test_history_path_default_under_jarvis():
    """When env is unset, history defaults under .jarvis/."""
    p = history_path()
    assert ".jarvis" in p.parts


def test_history_path_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH", "/tmp/custom.jsonl",
    )
    assert str(history_path()) == "/tmp/custom.jsonl"


# ---------------------------------------------------------------------------
# CLI integration smoke
# ---------------------------------------------------------------------------


def test_cli_imports_substrate_lazily():
    """The CLI script must lazy-import the harness module so a `--help`
    invocation doesn't pay the substrate import cost."""
    import ast
    import inspect
    import scripts.live_fire_graduation_soak as cli
    src = inspect.getsource(cli)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.graduation.live_fire_soak",
    }
    leaked = forbidden & set(top_level)
    assert not leaked


def test_cli_subcommands_present():
    import inspect
    import scripts.live_fire_graduation_soak as cli
    src = inspect.getsource(cli)
    for sub in ["queue", "evidence", "run", "status", "pause", "resume"]:
        assert f'"{sub}"' in src or f"'{sub}'" in src
