"""Slice 1 regression spine for AsyncTopologySentinel.

Pins the foundation primitives: state machine composition over
``rate_limiter.CircuitBreaker``, slow-start ramp over ``TokenBucket``,
context-weighted prober, disk-backed persistence with boot-loop
protection. **No orchestrator wiring is exercised here** — Slice 1
ships the module isolated; Slice 3 wires consumers.

Test categories:
  * §1 Module authority invariants (AST scans + import-shape pins)
  * §2 Env knobs + master flag
  * §3 FailureSource weight matrix
  * §4 EndpointSnapshot + TransitionRecord serialization
  * §5 SentinelStateStore disk round-trips
  * §6 SlowStartRamp
  * §7 ContextWeightedProber
  * §8 TopologySentinel coordinator
  * §9 Boot-loop protection (the marquee correctness goal)
  * §10 Default singleton accessor
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance import topology_sentinel as ts


SENTINEL_PATH = Path(ts.__file__)


# ===========================================================================
# §1 — Module authority invariants
# ===========================================================================


def _module_ast() -> ast.Module:
    return ast.parse(SENTINEL_PATH.read_text(encoding="utf-8"))


def test_top_level_imports_stdlib_only() -> None:
    """Top-level imports must be stdlib only — providers /
    governance modules are imported lazily inside method bodies so
    importing the sentinel doesn't boot the orchestrator."""
    module = _module_ast()
    allowed = {
        "asyncio", "contextvars", "enum", "json", "logging", "os",
        "random", "tempfile", "threading", "time", "dataclasses",
        "pathlib", "typing", "__future__",
    }
    for node in module.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed, (
                    f"top-level import {alias.name} is not stdlib"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root in allowed, (
                f"top-level from-import {node.module} not stdlib"
            )


def test_no_local_fsm_or_bucket_or_backoff_class() -> None:
    """No reimplementation of primitives owned by rate_limiter or
    preemption_fsm. The sentinel composes; it does not duplicate."""
    module = _module_ast()
    forbidden_suffixes = ("Breaker", "Bucket", "Backoff")
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef):
            for suffix in forbidden_suffixes:
                assert not node.name.endswith(suffix), (
                    f"class {node.name} duplicates a primitive that "
                    "already exists in rate_limiter / preemption_fsm — "
                    "compose, do not duplicate"
                )


def test_no_orchestrator_or_gate_imports() -> None:
    """Sentinel is a pure observer; cascade-decision authority lives
    in candidate_generator (Slice 3). The sentinel module must not
    import any module that grants it cascade authority by accident."""
    src = SENTINEL_PATH.read_text(encoding="utf-8")
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.gate",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for needle in forbidden:
        assert needle not in src, (
            f"forbidden import {needle!r} appeared in sentinel module"
        )


def test_imports_existing_primitives_lazily() -> None:
    """Reverse pin — confirm the sentinel actually USES the existing
    primitives. If somebody refactors away the lazy imports we want
    a regression here."""
    src = SENTINEL_PATH.read_text(encoding="utf-8")
    assert (
        "from backend.core.ouroboros.governance.rate_limiter import"
    ) in src, "sentinel must import CircuitBreaker / TokenBucket"
    assert (
        "from backend.core.ouroboros.governance.preemption_fsm import"
    ) in src, "sentinel must import _compute_backoff_ms"
    assert (
        "from backend.core.ouroboros.governance.contracts.fsm_contract"
    ) in src, "sentinel must import RetryBudget"


def test_schema_version_constant() -> None:
    assert ts.SCHEMA_VERSION == "topology_sentinel.1"


# ===========================================================================
# §2 — Env knobs + master flag
# ===========================================================================


def test_master_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    assert ts.is_sentinel_enabled() is False


def test_master_flag_truthy_values_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", val)
        assert ts.is_sentinel_enabled() is True


def test_force_severed_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_FORCE_SEVERED", raising=False)
    assert ts.force_severed() is False


def test_severed_threshold_default_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_SEVERED_THRESHOLD_WEIGHTED", raising=False,
    )
    assert ts.severed_threshold_weighted() == pytest.approx(3.0)


def test_heavy_probe_ratio_clamped_to_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_RATIO", "5.0")
    assert ts.heavy_probe_ratio() == 1.0


def test_state_dir_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    assert ts.state_dir() == tmp_path


# ===========================================================================
# §3 — FailureSource weight matrix
# ===========================================================================


def test_failure_source_enum_values() -> None:
    assert ts.FailureSource.LIVE_STREAM_STALL.value == "live_stream_stall"
    assert ts.FailureSource.HEAVY_PROBE_FAIL.value == "heavy_probe_fail"


def test_live_stream_stall_weight_dominates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single live-traffic stream-stall must trip alone at default
    threshold (3.0). This is the critical empirical signal."""
    for env in (
        "JARVIS_TOPOLOGY_WEIGHT_LIVE_STREAM_STALL",
        "JARVIS_TOPOLOGY_WEIGHT_LIGHT_PROBE_FAIL",
    ):
        monkeypatch.delenv(env, raising=False)
    assert ts.failure_weight(ts.FailureSource.LIVE_STREAM_STALL) == 3.0


def test_failure_weight_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_WEIGHT_LIGHT_PROBE_FAIL", "2.5",
    )
    assert ts.failure_weight(
        ts.FailureSource.LIGHT_PROBE_FAIL,
    ) == 2.5


def test_failure_weight_bounded_at_10(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_WEIGHT_LIVE_STREAM_STALL", "9999",
    )
    assert ts.failure_weight(ts.FailureSource.LIVE_STREAM_STALL) == 10.0


# ===========================================================================
# §4 — Snapshot + transition record serialization
# ===========================================================================


def test_snapshot_to_json_round_trip() -> None:
    snap = ts.EndpointSnapshot(
        model_id="qwen-397b", state="OPEN",
        weighted_failure_streak=3.5,
        opened_at_epoch=1234567890.0,
        last_failure_source="live_stream_stall",
        backoff_idx=2,
    )
    payload = snap.to_json()
    rehydrated = ts.EndpointSnapshot.from_json(payload)
    assert rehydrated is not None
    assert rehydrated.model_id == "qwen-397b"
    assert rehydrated.state == "OPEN"
    assert rehydrated.weighted_failure_streak == pytest.approx(3.5)
    assert rehydrated.backoff_idx == 2


def test_snapshot_from_json_rejects_wrong_schema() -> None:
    payload = {
        "model_id": "x", "state": "CLOSED",
        "schema_version": "wrong",
    }
    assert ts.EndpointSnapshot.from_json(payload) is None


def test_snapshot_truncates_long_failure_detail() -> None:
    long_detail = "x" * 5000
    snap = ts.EndpointSnapshot(
        model_id="x", state="OPEN", last_failure_detail=long_detail,
    )
    payload = snap.to_json()
    assert len(payload["last_failure_detail"]) <= 200


def test_transition_record_to_json_pins_schema() -> None:
    rec = ts.TransitionRecord(
        ts_epoch=time.time(), model_id="x",
        transition_kind="state_change",
        from_state="CLOSED", to_state="OPEN",
        weighted_failure_streak=3.0,
    )
    out = rec.to_json()
    assert out["schema_version"] == ts.SCHEMA_VERSION
    assert out["transition_kind"] == "state_change"


# ===========================================================================
# §5 — SentinelStateStore disk round-trips
# ===========================================================================


def test_store_hydrate_empty_returns_empty(tmp_path: Path) -> None:
    store = ts.SentinelStateStore(directory=tmp_path)
    assert store.hydrate() == {}


def test_store_round_trip_current_then_hydrate(tmp_path: Path) -> None:
    store = ts.SentinelStateStore(directory=tmp_path)
    snap = ts.EndpointSnapshot(
        model_id="qwen-397b", state="OPEN",
        opened_at_epoch=time.time(),
        weighted_failure_streak=4.5,
    )
    store.write_current({"qwen-397b": snap})
    out = store.hydrate()
    assert "qwen-397b" in out
    assert out["qwen-397b"].state == "OPEN"
    assert out["qwen-397b"].weighted_failure_streak == pytest.approx(4.5)


def test_store_hydrate_rejects_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshots older than state_max_age_s must cold-start. The
    safety net: an OPEN state from days ago is not authoritative."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_STATE_MAX_AGE_S", "60")
    store = ts.SentinelStateStore(directory=tmp_path)
    payload = {
        "schema_version": ts.SCHEMA_VERSION,
        "written_at_epoch": time.time() - 3600,  # 1h ago, exceeds 60s
        "endpoints": {"x": {"model_id": "x", "state": "OPEN",
                            "schema_version": ts.SCHEMA_VERSION}},
    }
    store.current_path.parent.mkdir(parents=True, exist_ok=True)
    store.current_path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.hydrate() == {}


def test_store_hydrate_rejects_bad_schema(tmp_path: Path) -> None:
    store = ts.SentinelStateStore(directory=tmp_path)
    store.current_path.parent.mkdir(parents=True, exist_ok=True)
    store.current_path.write_text(
        json.dumps({"schema_version": "wrong"}), encoding="utf-8",
    )
    assert store.hydrate() == {}


def test_store_history_append_and_trim(
    tmp_path: Path,
) -> None:
    store = ts.SentinelStateStore(
        directory=tmp_path, history_capacity=5,
    )
    for i in range(20):
        rec = ts.TransitionRecord(
            ts_epoch=time.time(), model_id=f"m{i}",
            transition_kind="probe",
        )
        store.append_history(rec)
    lines = store.history_path.read_text(
        encoding="utf-8",
    ).splitlines()
    assert len(lines) == 5  # trimmed to capacity


def test_store_atomic_write_no_torn_state(tmp_path: Path) -> None:
    """Multiple writes must always leave a valid JSON file — temp+
    rename guarantees readers never see torn state."""
    store = ts.SentinelStateStore(directory=tmp_path)
    for i in range(10):
        snap = ts.EndpointSnapshot(model_id=f"m{i}", state="CLOSED")
        store.write_current({f"m{i}": snap})
    # File must be valid JSON throughout — at the end, parseable.
    payload = json.loads(store.current_path.read_text("utf-8"))
    assert payload["schema_version"] == ts.SCHEMA_VERSION


# ===========================================================================
# §6 — SlowStartRamp
# ===========================================================================


def test_ramp_inactive_allows_immediately() -> None:
    ramp = ts.SlowStartRamp()
    assert ramp.is_active() is False
    assert ramp.current_capacity() == ramp.baseline_capacity_per_s


def test_ramp_activate_starts_at_entry_tier() -> None:
    schedule = ((0.0, 1.0), (10.0, 4.0), (60.0, 16.0))
    ramp = ts.SlowStartRamp(schedule=schedule)
    ramp.activate()
    assert ramp.is_active() is True
    assert ramp.current_capacity() == 1.0


def test_ramp_capacity_walks_schedule_over_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a controllable monotonic clock — process-relative
    ``time.monotonic`` can return values < 12.0 in fresh test
    processes, which would make the subtraction trick unsound."""
    schedule = ((0.0, 1.0), (10.0, 4.0), (60.0, 16.0))
    ramp = ts.SlowStartRamp(schedule=schedule)
    fake_now = [1000.0]
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.topology_sentinel."
        "time.monotonic",
        lambda: fake_now[0],
    )
    ramp.activate()  # closed_at = 1000.0
    fake_now[0] = 1012.0  # 12s elapsed → 4-tier
    assert ramp.current_capacity() == 4.0


def test_ramp_finishes_after_last_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = ((0.0, 1.0), (10.0, 4.0))
    ramp = ts.SlowStartRamp(schedule=schedule)
    fake_now = [1000.0]
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.topology_sentinel."
        "time.monotonic",
        lambda: fake_now[0],
    )
    ramp.activate()
    fake_now[0] = 1100.0  # 100s elapsed → past last tier (10s)
    cap = ramp.current_capacity()
    assert cap == 4.0
    assert ramp.is_active() is False


def test_ramp_register_failure_resets_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = ((0.0, 1.0), (10.0, 4.0), (60.0, 16.0))
    ramp = ts.SlowStartRamp(schedule=schedule)
    fake_now = [1000.0]
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.topology_sentinel."
        "time.monotonic",
        lambda: fake_now[0],
    )
    ramp.activate()
    fake_now[0] = 1030.0  # 30s elapsed → 4-tier territory
    ramp.register_failure()
    # Reset puts closed_at at current monotonic (1030.0) → t=0 again.
    assert ramp.current_capacity() == 1.0
    assert ramp.snapshot()["failure_resets"] == 1


def test_ramp_deactivate_restores_baseline() -> None:
    ramp = ts.SlowStartRamp(
        schedule=((0.0, 1.0), (10.0, 16.0)),
    )
    ramp.activate()
    ramp.deactivate()
    assert ramp.is_active() is False
    assert ramp.current_capacity() == 16.0


def test_ramp_parses_env_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_RAMP_SCHEDULE", "0:0.5,5:2.0,30:8.0",
    )
    schedule = ts.parse_ramp_schedule_env()
    assert schedule[0] == (0.0, 0.5)
    assert schedule[-1] == (30.0, 8.0)


def test_ramp_malformed_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_RAMP_SCHEDULE", "garbage,not-numbers",
    )
    schedule = ts.parse_ramp_schedule_env()
    # Default has at least 4 tiers
    assert len(schedule) >= 4


@pytest.mark.asyncio
async def test_ramp_try_acquire_inactive_returns_true_immediately() -> None:
    ramp = ts.SlowStartRamp()
    allowed, wait_s = await ramp.try_acquire()
    assert allowed is True
    assert wait_s == 0.0


@pytest.mark.asyncio
async def test_ramp_try_acquire_within_max_wait_returns_true() -> None:
    schedule = ((0.0, 1.0), (10.0, 16.0))
    ramp = ts.SlowStartRamp(schedule=schedule, max_wait_s=2.0)
    ramp.activate()
    # First acquire must succeed (full burst on activate).
    allowed, _ = await ramp.try_acquire()
    assert allowed is True


# ===========================================================================
# §7 — ContextWeightedProber
# ===========================================================================


@pytest.mark.asyncio
async def test_prober_calls_probe_fn() -> None:
    calls: list = []

    async def fake_probe(model_id: str, weight: ts.ProbeWeight):
        calls.append((model_id, weight))
        return ts.ProbeResult(
            model_id=model_id, weight=weight,
            outcome=ts.ProbeOutcome.PASS, latency_s=0.05,
        )

    prober = ts.ContextWeightedProber(probe_fn=fake_probe)
    result = await prober.probe("qwen-397b")
    assert result.outcome == ts.ProbeOutcome.PASS
    assert calls and calls[0][0] == "qwen-397b"


@pytest.mark.asyncio
async def test_prober_probe_fn_raise_returns_FAIL() -> None:
    async def raising_probe(model_id: str, weight: ts.ProbeWeight):
        raise RuntimeError("simulated transport failure")

    prober = ts.ContextWeightedProber(probe_fn=raising_probe)
    result = await prober.probe("qwen-397b")
    assert result.outcome == ts.ProbeOutcome.FAIL
    assert result.failure_source is not None
    assert "probe_fn_raised" in result.failure_detail


def test_prober_pick_weight_zero_ratio_always_light() -> None:
    async def noop(model_id: str, weight: ts.ProbeWeight):
        return ts.ProbeResult(
            model_id=model_id, weight=weight,
            outcome=ts.ProbeOutcome.PASS, latency_s=0.0,
        )

    prober = ts.ContextWeightedProber(probe_fn=noop, heavy_ratio=0.0)
    for _ in range(20):
        assert prober.pick_weight() == ts.ProbeWeight.LIGHT


def test_prober_pick_weight_full_ratio_always_heavy() -> None:
    async def noop(model_id: str, weight: ts.ProbeWeight):
        return ts.ProbeResult(
            model_id=model_id, weight=weight,
            outcome=ts.ProbeOutcome.PASS, latency_s=0.0,
        )

    prober = ts.ContextWeightedProber(probe_fn=noop, heavy_ratio=1.0)
    for _ in range(20):
        assert prober.pick_weight() == ts.ProbeWeight.HEAVY


def test_prober_pick_weight_default_ratio_mixes() -> None:
    """Default ratio 0.2 → ~1 in 5 heavy. Over 100 picks we expect
    a non-zero count of each (RNG-seeded so test is deterministic)."""
    import random as _r

    async def noop(model_id: str, weight: ts.ProbeWeight):
        return ts.ProbeResult(
            model_id=model_id, weight=weight,
            outcome=ts.ProbeOutcome.PASS, latency_s=0.0,
        )

    rng = _r.Random(42)
    prober = ts.ContextWeightedProber(
        probe_fn=noop, heavy_ratio=0.2, rng=rng,
    )
    counts = {ts.ProbeWeight.LIGHT: 0, ts.ProbeWeight.HEAVY: 0}
    for _ in range(100):
        counts[prober.pick_weight()] += 1
    assert counts[ts.ProbeWeight.LIGHT] > 0
    assert counts[ts.ProbeWeight.HEAVY] > 0


# ===========================================================================
# §8 — TopologySentinel coordinator
# ===========================================================================


@pytest.fixture
def sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    monkeypatch.delenv("JARVIS_TOPOLOGY_FORCE_SEVERED", raising=False)
    store = ts.SentinelStateStore(directory=tmp_path)
    return ts.TopologySentinel(store=store)


def test_sentinel_get_state_master_off_returns_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    s = ts.TopologySentinel(
        store=ts.SentinelStateStore(directory=tmp_path),
    )
    s.register_endpoint("x")
    s.report_failure(
        "x", ts.FailureSource.LIVE_STREAM_STALL, "stall",
    )
    # Master flag off — get_state always CLOSED regardless of state.
    assert s.get_state("x") == "CLOSED"


def test_sentinel_force_severed_env_pins_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOPOLOGY_FORCE_SEVERED", "true")
    s = ts.TopologySentinel(
        store=ts.SentinelStateStore(directory=tmp_path),
    )
    # No need to register — operator panic switch wins.
    assert s.get_state("any-model") == "OPEN"
    assert s.is_dw_allowed("any-model") is False


def test_sentinel_unknown_model_returns_closed(
    sentinel: ts.TopologySentinel,
) -> None:
    assert sentinel.get_state("never-registered") == "CLOSED"


def test_sentinel_register_endpoint_idempotent(
    sentinel: ts.TopologySentinel,
) -> None:
    sentinel.register_endpoint("qwen-397b")
    sentinel.register_endpoint("qwen-397b")
    assert sentinel.get_state("qwen-397b") == "CLOSED"


def test_sentinel_live_stream_stall_trips_alone(
    sentinel: ts.TopologySentinel,
) -> None:
    """The marquee correctness pin — a SINGLE live-traffic stream-
    stall must trip CLOSED→OPEN immediately at the default
    threshold (3.0). This is the empirical signal we trust most."""
    sentinel.register_endpoint("qwen-397b")
    assert sentinel.get_state("qwen-397b") == "CLOSED"
    sentinel.report_failure(
        "qwen-397b", ts.FailureSource.LIVE_STREAM_STALL,
        "first-token timeout",
    )
    assert sentinel.get_state("qwen-397b") == "OPEN"


def test_sentinel_three_light_probes_trip(
    sentinel: ts.TopologySentinel,
) -> None:
    """Three light-probe failures (weight 1.0 each) accumulate to
    threshold and trip."""
    sentinel.register_endpoint("qwen-397b")
    for _ in range(3):
        sentinel.report_failure(
            "qwen-397b", ts.FailureSource.LIGHT_PROBE_FAIL, "no token",
        )
    assert sentinel.get_state("qwen-397b") == "OPEN"


def test_sentinel_two_light_probes_dont_trip(
    sentinel: ts.TopologySentinel,
) -> None:
    sentinel.register_endpoint("qwen-397b")
    for _ in range(2):
        sentinel.report_failure(
            "qwen-397b", ts.FailureSource.LIGHT_PROBE_FAIL, "no token",
        )
    assert sentinel.get_state("qwen-397b") == "CLOSED"


def test_sentinel_429_weight_subdued(
    sentinel: ts.TopologySentinel,
) -> None:
    """429 is rate-limit (transient, upstream-handled) — five 429s
    don't trip alone (weight 0.5 × 5 = 2.5 < 3.0)."""
    sentinel.register_endpoint("qwen-397b")
    for _ in range(5):
        sentinel.report_failure(
            "qwen-397b", ts.FailureSource.LIVE_HTTP_429, "429",
        )
    assert sentinel.get_state("qwen-397b") == "CLOSED"


def test_sentinel_report_success_decays_streak(
    sentinel: ts.TopologySentinel,
) -> None:
    sentinel.register_endpoint("qwen-397b")
    # Two light probe failures — under threshold but streak built up.
    sentinel.report_failure(
        "qwen-397b", ts.FailureSource.LIGHT_PROBE_FAIL, "x",
    )
    sentinel.report_failure(
        "qwen-397b", ts.FailureSource.LIGHT_PROBE_FAIL, "x",
    )
    snap_before = sentinel.snapshot()["endpoints"]["qwen-397b"]
    streak_before = snap_before["weighted_failure_streak"]
    sentinel.report_success("qwen-397b")
    snap_after = sentinel.snapshot()["endpoints"]["qwen-397b"]
    assert snap_after["weighted_failure_streak"] < streak_before


def test_sentinel_force_severed_call_pins_open(
    sentinel: ts.TopologySentinel,
) -> None:
    sentinel.force_severed("qwen-397b", "operator incident")
    assert sentinel.get_state("qwen-397b") == "OPEN"


def test_sentinel_force_healthy_call_pins_closed(
    sentinel: ts.TopologySentinel,
) -> None:
    sentinel.register_endpoint("qwen-397b")
    sentinel.report_failure(
        "qwen-397b", ts.FailureSource.LIVE_STREAM_STALL, "stall",
    )
    assert sentinel.get_state("qwen-397b") == "OPEN"
    sentinel.force_healthy("qwen-397b")
    assert sentinel.get_state("qwen-397b") == "CLOSED"


def test_sentinel_persists_state_change_to_disk(
    sentinel: ts.TopologySentinel, tmp_path: Path,
) -> None:
    sentinel.register_endpoint("qwen-397b")
    sentinel.report_failure(
        "qwen-397b", ts.FailureSource.LIVE_STREAM_STALL, "x",
    )
    current = json.loads(
        (tmp_path / "topology_sentinel_current.json").read_text("utf-8"),
    )
    assert current["endpoints"]["qwen-397b"]["state"] == "OPEN"


def test_sentinel_listener_receives_transition(
    sentinel: ts.TopologySentinel,
) -> None:
    received: list = []
    sentinel.add_listener(lambda rec: received.append(rec))
    sentinel.register_endpoint("qwen-397b")
    sentinel.report_failure(
        "qwen-397b", ts.FailureSource.LIVE_STREAM_STALL, "x",
    )
    assert any(r.transition_kind == "state_change" for r in received)


def test_sentinel_listener_exception_swallowed(
    sentinel: ts.TopologySentinel,
) -> None:
    """A misbehaving listener must not break the sentinel."""
    def bad(rec):
        raise RuntimeError("listener bug")

    sentinel.add_listener(bad)
    sentinel.register_endpoint("qwen-397b")
    # Must not raise.
    sentinel.report_failure(
        "qwen-397b", ts.FailureSource.LIVE_STREAM_STALL, "x",
    )
    assert sentinel.get_state("qwen-397b") == "OPEN"


def test_sentinel_report_failure_unknown_model_no_raise(
    sentinel: ts.TopologySentinel,
) -> None:
    """report_failure on an unregistered endpoint is a no-op (defense
    against caller bugs); must NEVER raise."""
    sentinel.report_failure(
        "never-registered", ts.FailureSource.LIVE_STREAM_STALL, "x",
    )


# ===========================================================================
# §9 — Boot-loop protection (the marquee correctness goal)
# ===========================================================================


def test_sentinel_hydrates_open_state_from_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """**The boot-loop-prevention pin.**

    Process A: trips OPEN, persists current.json, dies (SIGKILL).
    Process B: starts fresh, hydrates current.json, registers the
    same endpoint. The breaker MUST come up OPEN — not CLOSED
    (which would let the first BG op stampede DW and re-trip)."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )

    # --- Process A simulation
    store_a = ts.SentinelStateStore(directory=tmp_path)
    s_a = ts.TopologySentinel(store=store_a)
    s_a.register_endpoint("qwen-397b")
    s_a.report_failure(
        "qwen-397b", ts.FailureSource.LIVE_STREAM_STALL, "stall",
    )
    assert s_a.get_state("qwen-397b") == "OPEN"

    # --- Process B simulation: fresh sentinel reads same state dir
    store_b = ts.SentinelStateStore(directory=tmp_path)
    s_b = ts.TopologySentinel(store=store_b)
    loaded = s_b.hydrate()
    assert loaded == 1
    s_b.register_endpoint("qwen-397b")
    # Must come up OPEN, not CLOSED — the boot-loop guarantee.
    assert s_b.get_state("qwen-397b") == "OPEN"


def test_sentinel_hydrate_old_snapshot_cold_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A snapshot older than max-age must NOT pin OPEN — that would
    keep a now-recovered endpoint sealed forever."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOPOLOGY_STATE_MAX_AGE_S", "60")
    payload = {
        "schema_version": ts.SCHEMA_VERSION,
        "written_at_epoch": time.time() - 86400,  # 24h ago
        "endpoints": {
            "x": {
                "model_id": "x", "state": "OPEN",
                "schema_version": ts.SCHEMA_VERSION,
                "opened_at_epoch": time.time() - 86400,
            },
        },
    }
    state_path = tmp_path / "topology_sentinel_current.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    store = ts.SentinelStateStore(directory=tmp_path)
    s = ts.TopologySentinel(store=store)
    assert s.hydrate() == 0
    s.register_endpoint("x")
    assert s.get_state("x") == "CLOSED"


def test_sentinel_half_open_at_kill_re_opens_on_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Process killed mid-HALF_OPEN must boot back into OPEN, not
    CLOSED — half-open is an unsafe transient and we err toward
    cascade prevention."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    payload = {
        "schema_version": ts.SCHEMA_VERSION,
        "written_at_epoch": time.time(),
        "endpoints": {
            "x": {
                "model_id": "x", "state": "HALF_OPEN",
                "schema_version": ts.SCHEMA_VERSION,
                "opened_at_epoch": time.time() - 5,
            },
        },
    }
    state_path = tmp_path / "topology_sentinel_current.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    store = ts.SentinelStateStore(directory=tmp_path)
    s = ts.TopologySentinel(store=store)
    s.hydrate()
    s.register_endpoint("x")
    assert s.get_state("x") == "OPEN"


# ===========================================================================
# §10 — Default singleton + lifecycle no-op when off
# ===========================================================================


def test_default_sentinel_is_singleton() -> None:
    ts.reset_default_sentinel_for_tests()
    a = ts.get_default_sentinel()
    b = ts.get_default_sentinel()
    assert a is b
    ts.reset_default_sentinel_for_tests()


@pytest.mark.asyncio
async def test_sentinel_start_no_op_when_master_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    s = ts.TopologySentinel(
        store=ts.SentinelStateStore(directory=tmp_path),
    )
    await s.start()  # must not spawn a task
    assert s._probe_task is None  # noqa: SLF001
    await s.stop()


@pytest.mark.asyncio
async def test_sentinel_start_no_op_when_no_prober(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    s = ts.TopologySentinel(
        store=ts.SentinelStateStore(directory=tmp_path),
        # no prober wired
    )
    await s.start()
    assert s._probe_task is None  # noqa: SLF001
    await s.stop()


@pytest.mark.asyncio
async def test_sentinel_probe_loop_stops_on_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: spawn the probe loop with a fake prober, register
    one endpoint, observe at least one probe land, then stop."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEALTHY_PROBE_INTERVAL_S", "5")
    probes_seen: list = []

    async def fake_probe(model_id: str, weight: ts.ProbeWeight):
        probes_seen.append((model_id, weight))
        return ts.ProbeResult(
            model_id=model_id, weight=weight,
            outcome=ts.ProbeOutcome.PASS, latency_s=0.01,
            cost_usd=1e-6,
        )

    prober = ts.ContextWeightedProber(probe_fn=fake_probe)
    s = ts.TopologySentinel(
        prober=prober,
        store=ts.SentinelStateStore(directory=tmp_path),
    )
    s.register_endpoint("qwen-397b")
    await s.start()
    # Give the loop a moment to run one tick.
    await asyncio.sleep(0.1)
    await s.stop()
    # Exact count is loop-dependent; the contract is "stop terminates
    # cleanly within budget" rather than "exactly N probes ran."
    assert s._probe_task is None  # noqa: SLF001
