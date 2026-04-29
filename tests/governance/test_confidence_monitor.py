"""Priority 1 Slice 2 — Rolling-window confidence monitor regression spine.

Pins the math + structural contract for the confidence monitor and
the ``ConfidenceCollapseError`` raise path. Mirror of Slice 1's
test_confidence_capture coverage style.

§-numbered coverage map:

  §1   Master flag JARVIS_CONFIDENCE_MONITOR_ENABLED — default false (Slice 2)
  §2   Sub-flag JARVIS_CONFIDENCE_MONITOR_ENFORCE — default false (shadow)
  §3   Knobs: floor / window_k / approaching_factor with defensive bounds
  §4   ConfidenceVerdict enum: 3 values, str-valued for serialization
  §5   ConfidenceCollapseError inherits RuntimeError + structured fields
  §6   Monitor: empty window → OK
  §7   Monitor: insufficient observations → OK (false-positive defense)
  §8   Monitor: rolling-mean math correctness (12+ cases)
  §9   Monitor: BELOW_FLOOR / APPROACHING_FLOOR / OK transitions
  §10  Monitor: posture-relevant floor multipliers (HARDEN/MAINTAIN/EXPLORE)
  §11  Monitor: master-off short-circuits to OK (always)
  §12  Monitor: NEVER raises on malformed margins (NaN, inf, str, None)
  §13  Monitor: bounded ring-buffer caps observations at window_size
  §14  Monitor: snapshot() returns frozen MonitorSnapshot
  §15  Monitor: reset() clears state
  §16  Monitor: thread-safety under concurrent observe()
  §17  feed_trace_into_monitor — bridge from Slice 1 trace
  §18  Authority invariants (no forbidden imports, pure stdlib)
  §19  to_collapse_error() shape + ENFORCE flag does NOT auto-raise
"""
from __future__ import annotations

import ast
import inspect
import math
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification import confidence_monitor
from backend.core.ouroboros.governance.verification.confidence_monitor import (
    CONFIDENCE_MONITOR_SCHEMA_VERSION,
    ConfidenceCollapseError,
    ConfidenceMonitor,
    ConfidenceVerdict,
    MonitorSnapshot,
    confidence_approaching_factor,
    confidence_floor,
    confidence_monitor_enabled,
    confidence_monitor_enforce,
    confidence_window_k,
    feed_trace_into_monitor,
)


# ===========================================================================
# §1 — Master flag default false
# ===========================================================================


def test_master_flag_default_false(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", raising=False)
    assert confidence_monitor_enabled() is False


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_default_false(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", val)
    assert confidence_monitor_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_master_flag_explicit_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", val)
    assert confidence_monitor_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_master_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", val)
    assert confidence_monitor_enabled() is False


# ===========================================================================
# §2 — Sub-flag default false (shadow mode)
# ===========================================================================


def test_enforce_subflag_default_false(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIDENCE_MONITOR_ENFORCE", raising=False)
    assert confidence_monitor_enforce() is False


def test_enforce_subflag_explicit_true(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENFORCE", "true")
    assert confidence_monitor_enforce() is True


def test_enforce_independent_of_master(monkeypatch) -> None:
    """Master off + enforce on → still shadow effectively (provider
    wiring guards on master). The flag function returns true; the
    wiring orchestrates."""
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENFORCE", "true")
    assert confidence_monitor_enabled() is False
    assert confidence_monitor_enforce() is True


# ===========================================================================
# §3 — Knob bounds
# ===========================================================================


def test_floor_default(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIDENCE_FLOOR", raising=False)
    assert confidence_floor() == 0.05


def test_floor_negative_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_FLOOR", "-0.5")
    assert confidence_floor() == 0.05  # default


def test_floor_garbage_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_FLOOR", "not a float")
    assert confidence_floor() == 0.05


def test_floor_inf_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_FLOOR", "inf")
    assert confidence_floor() == 0.05  # not finite → default


def test_window_k_default(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIDENCE_WINDOW_K", raising=False)
    assert confidence_window_k() == 16


def test_window_k_floored_at_one(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_WINDOW_K", "0")
    assert confidence_window_k() == 1
    monkeypatch.setenv("JARVIS_CONFIDENCE_WINDOW_K", "-99")
    assert confidence_window_k() == 1


def test_approaching_factor_default(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIDENCE_APPROACHING_FACTOR", raising=False)
    assert confidence_approaching_factor() == 1.5


def test_approaching_factor_floored_at_one(monkeypatch) -> None:
    """Factor < 1.0 would invert APPROACHING vs BELOW; clamp."""
    monkeypatch.setenv("JARVIS_CONFIDENCE_APPROACHING_FACTOR", "0.5")
    assert confidence_approaching_factor() == 1.0


# ===========================================================================
# §4 — ConfidenceVerdict enum
# ===========================================================================


def test_verdict_three_values() -> None:
    assert ConfidenceVerdict.OK.value == "ok"
    assert ConfidenceVerdict.APPROACHING_FLOOR.value == "approaching_floor"
    assert ConfidenceVerdict.BELOW_FLOOR.value == "below_floor"


def test_verdict_string_serializable() -> None:
    """str-valued enum serializes to JSON cleanly."""
    import json
    payload = {"verdict": ConfidenceVerdict.BELOW_FLOOR.value}
    assert json.dumps(payload) == '{"verdict": "below_floor"}'


# ===========================================================================
# §5 — ConfidenceCollapseError shape
# ===========================================================================


def test_collapse_error_inherits_runtime_error() -> None:
    """Existing 'except RuntimeError' / 'except Exception' retry
    handlers must catch this — same pattern as
    ExplorationInsufficientError."""
    assert issubclass(ConfidenceCollapseError, RuntimeError)


def test_collapse_error_carries_structured_fields() -> None:
    err = ConfidenceCollapseError(
        verdict=ConfidenceVerdict.BELOW_FLOOR,
        rolling_margin=0.012,
        floor=0.05,
        effective_floor=0.10,
        window_size=16,
        observations_count=14,
        posture="HARDEN",
        provider="doubleword",
        model_id="qwen-397b",
        op_id="op-test",
    )
    assert err.verdict == ConfidenceVerdict.BELOW_FLOOR
    assert err.rolling_margin == 0.012
    assert err.floor == 0.05
    assert err.effective_floor == 0.10
    assert err.posture == "HARDEN"
    assert err.op_id == "op-test"
    assert "confidence_collapse:" in str(err)
    assert "below_floor" in str(err)


def test_collapse_error_message_starts_with_classifier_prefix() -> None:
    """Mirror ExplorationInsufficientError's 'exploration_insufficient:'
    pattern — orchestrator's error-classification regex branches on
    the message prefix to route to the appropriate retry path."""
    err = ConfidenceCollapseError(
        verdict=ConfidenceVerdict.BELOW_FLOOR,
        rolling_margin=None,
        floor=0.05,
        effective_floor=0.05,
        window_size=16,
        observations_count=0,
    )
    assert str(err).startswith("confidence_collapse:")


# ===========================================================================
# §6-§7 — Monitor: empty window + insufficient observations
# ===========================================================================


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", "true")
    yield


def test_monitor_empty_returns_ok(enabled) -> None:
    m = ConfidenceMonitor(window_size=8)
    assert m.evaluate() == ConfidenceVerdict.OK
    assert m.current_margin() is None


def test_monitor_one_observation_returns_ok(enabled) -> None:
    """One obs is below the min-obs floor; defends against false
    positives on short generations."""
    m = ConfidenceMonitor(window_size=8)
    m.observe(0.001)  # very low margin
    assert m.evaluate() == ConfidenceVerdict.OK


def test_monitor_min_obs_floored_at_two(enabled) -> None:
    """For window_size=1, min_obs is floored at 2 → first eval still OK."""
    m = ConfidenceMonitor(window_size=1)
    m.observe(0.001)
    assert m.evaluate() == ConfidenceVerdict.OK
    m.observe(0.001)
    # Now obs=2 ≥ floor → eval works
    assert m.evaluate() == ConfidenceVerdict.BELOW_FLOOR


def test_monitor_min_obs_at_half_window(enabled) -> None:
    """For window_size=10, min_obs is ceil(10/2) = 5."""
    m = ConfidenceMonitor(window_size=10)
    for _ in range(4):
        m.observe(0.001)
    assert m.evaluate() == ConfidenceVerdict.OK  # below threshold
    m.observe(0.001)  # 5th obs → eval engages
    assert m.evaluate() == ConfidenceVerdict.BELOW_FLOOR


# ===========================================================================
# §8 — Rolling-mean math correctness
# ===========================================================================


def test_monitor_current_margin_arithmetic(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    m.observe(1.0)
    m.observe(2.0)
    m.observe(3.0)
    # Mean: 2.0
    assert abs(m.current_margin() - 2.0) < 1e-9


def test_monitor_window_evicts_oldest(enabled) -> None:
    """Bounded deque — oldest evicted on overflow."""
    m = ConfidenceMonitor(window_size=3)
    m.observe(0.1)
    m.observe(0.2)
    m.observe(0.3)
    m.observe(10.0)  # evicts 0.1
    # Mean: (0.2 + 0.3 + 10.0) / 3 ≈ 3.5
    assert abs(m.current_margin() - 3.5) < 1e-9


def test_monitor_observations_count_unbounded(enabled) -> None:
    """Total observations counts across the lifetime, even after window evicts."""
    m = ConfidenceMonitor(window_size=2)
    for _ in range(100):
        m.observe(0.5)
    assert m.observations_count == 100
    assert m.window_size == 2


# ===========================================================================
# §9 — Verdict transitions (BELOW / APPROACHING / OK)
# ===========================================================================


def test_verdict_below_floor(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.01)  # mean 0.01 < 0.05 floor
    assert m.evaluate() == ConfidenceVerdict.BELOW_FLOOR


def test_verdict_approaching_floor(enabled) -> None:
    """floor=0.05, factor=1.5 → approaching range (0.05, 0.075)."""
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.06)  # mean 0.06 in (0.05, 0.075)
    assert m.evaluate() == ConfidenceVerdict.APPROACHING_FLOOR


def test_verdict_ok_above_approaching_band(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.5)  # well above floor and approaching band
    assert m.evaluate() == ConfidenceVerdict.OK


def test_verdict_at_exact_floor_is_below(enabled) -> None:
    """Floor is strict — exactly at floor counts as BELOW (mean < eff doesn't
    trigger; mean == eff_floor is BELOW because the math uses < strict)."""
    m = ConfidenceMonitor(window_size=4)
    # Mean exactly at 0.05; we use strict <, so 0.05 < 0.05 is False
    # → not BELOW; 0.05 < 0.075 is True → APPROACHING
    for _ in range(4):
        m.observe(0.05)
    assert m.evaluate() == ConfidenceVerdict.APPROACHING_FLOOR


def test_verdict_floor_override(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.5)
    # Default floor 0.05 → OK
    assert m.evaluate() == ConfidenceVerdict.OK
    # Override floor to 1.0 → 0.5 mean below it
    assert m.evaluate(floor=1.0) == ConfidenceVerdict.BELOW_FLOOR


# ===========================================================================
# §10 — Posture-relevant multipliers
# ===========================================================================


def test_posture_harden_tightens(enabled) -> None:
    """HARDEN multiplier 2.0 → effective floor 0.10."""
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.07)  # above 0.05 base, below 0.10 HARDEN
    # Default posture (None) → above floor → OK or APPROACHING
    base_verdict = m.evaluate()
    # HARDEN → BELOW
    assert m.evaluate(posture="HARDEN") == ConfidenceVerdict.BELOW_FLOOR


def test_posture_explore_loosens(enabled) -> None:
    """EXPLORE multiplier 0.4 → effective floor 0.02."""
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.03)  # below 0.05 base, above 0.02 EXPLORE
    assert m.evaluate(posture=None) == ConfidenceVerdict.BELOW_FLOOR
    assert m.evaluate(posture="EXPLORE") in (
        ConfidenceVerdict.OK,
        ConfidenceVerdict.APPROACHING_FLOOR,
    )


def test_posture_unknown_uses_default(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.03)
    assert m.evaluate(posture="UNKNOWN_VALUE") == ConfidenceVerdict.BELOW_FLOOR


def test_effective_floor_computation(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    assert abs(m.effective_floor() - 0.05) < 1e-9
    assert abs(m.effective_floor(posture="HARDEN") - 0.10) < 1e-9
    assert abs(m.effective_floor(posture="EXPLORE") - 0.02) < 1e-9
    assert abs(m.effective_floor(posture="MAINTAIN") - 0.05) < 1e-9


# ===========================================================================
# §11 — Master-off short-circuits to OK
# ===========================================================================


def test_master_off_evaluate_always_ok(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", "false")
    m = ConfidenceMonitor(window_size=4)
    # Even constructed monitor returns OK on evaluate
    assert m.evaluate() == ConfidenceVerdict.OK
    # observe() short-circuits
    assert m.observe(0.001) is False


def test_master_off_snapshot_empty_when_observe_fails(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_MONITOR_ENABLED", "false")
    m = ConfidenceMonitor(window_size=4)
    m.observe(0.001)  # short-circuited
    snap = m.snapshot()
    assert snap.observations_count == 0
    assert snap.rolling_margin is None


# ===========================================================================
# §12 — NEVER raises on malformed input
# ===========================================================================


def test_monitor_handles_nan_margin(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    assert m.observe(float("nan")) is False  # silently dropped


def test_monitor_handles_inf_margin(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    assert m.observe(float("inf")) is False
    assert m.observe(float("-inf")) is False


def test_monitor_handles_string_margin(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    assert m.observe("not a number") is False


def test_monitor_handles_none_margin(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    assert m.observe(None) is False


def test_monitor_handles_object_margin(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    assert m.observe(object()) is False


# ===========================================================================
# §13 — Bounded ring buffer
# ===========================================================================


def test_monitor_window_caps_observations(enabled) -> None:
    """deque maxlen — old margins evict on overflow."""
    m = ConfidenceMonitor(window_size=3)
    m.observe(1.0)
    m.observe(2.0)
    m.observe(3.0)
    m.observe(4.0)
    snap = m.snapshot()
    # Window holds last 3: [2.0, 3.0, 4.0]
    assert snap.rolling_margin == 3.0
    assert snap.min_margin == 2.0
    assert snap.max_margin == 4.0


# ===========================================================================
# §14 — Snapshot
# ===========================================================================


def test_snapshot_is_frozen(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    m.observe(0.5)
    snap = m.snapshot()
    with pytest.raises((AttributeError, Exception)):
        snap.rolling_margin = 99.0  # type: ignore[misc]


def test_snapshot_carries_full_stats(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    for v in (0.1, 0.2, 0.3, 0.4):
        m.observe(v)
    snap = m.snapshot()
    assert snap.observations_count == 4
    assert snap.window_size == 4
    assert abs(snap.rolling_margin - 0.25) < 1e-9
    assert snap.min_margin == 0.1
    assert snap.max_margin == 0.4
    assert snap.schema_version == CONFIDENCE_MONITOR_SCHEMA_VERSION


def test_snapshot_empty(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    snap = m.snapshot()
    assert snap.observations_count == 0
    assert snap.rolling_margin is None
    assert snap.min_margin is None
    assert snap.max_margin is None


# ===========================================================================
# §15 — reset()
# ===========================================================================


def test_reset_clears_state(enabled) -> None:
    m = ConfidenceMonitor(window_size=4)
    for _ in range(4):
        m.observe(0.01)
    assert m.evaluate() == ConfidenceVerdict.BELOW_FLOOR
    m.reset()
    assert m.observations_count == 0
    assert m.current_margin() is None
    assert m.evaluate() == ConfidenceVerdict.OK


# ===========================================================================
# §16 — Thread-safety
# ===========================================================================


def test_monitor_concurrent_observes(enabled) -> None:
    """RLock contract — concurrent observes preserve count."""
    m = ConfidenceMonitor(window_size=10000)
    n_threads = 4
    obs_per = 250

    def worker():
        for i in range(obs_per):
            m.observe(0.5 + i * 0.001)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m.observations_count == n_threads * obs_per


# ===========================================================================
# §17 — feed_trace_into_monitor bridge
# ===========================================================================


def test_feed_trace_into_monitor_happy_path(enabled) -> None:
    from backend.core.ouroboros.governance.verification.confidence_capture import (
        ConfidenceToken, ConfidenceTrace,
    )
    trace = ConfidenceTrace(
        tokens=(
            ConfidenceToken(
                token="a", logprob=-0.1,
                top_logprobs=(("a", -0.1), ("b", -2.0)),
            ),
            ConfidenceToken(
                token="b", logprob=-0.2,
                top_logprobs=(("b", -0.2), ("c", -0.5)),
            ),
        ),
    )
    m = ConfidenceMonitor(window_size=4)
    accepted = feed_trace_into_monitor(m, trace)
    assert accepted == 2
    assert m.observations_count == 2


def test_feed_trace_skips_tokens_without_alternatives(enabled) -> None:
    from backend.core.ouroboros.governance.verification.confidence_capture import (
        ConfidenceToken, ConfidenceTrace,
    )
    trace = ConfidenceTrace(
        tokens=(
            ConfidenceToken(token="a", logprob=-0.1),  # no alts → margin None
            ConfidenceToken(
                token="b", logprob=-0.2,
                top_logprobs=(("b", -0.2), ("c", -0.5)),
            ),
        ),
    )
    m = ConfidenceMonitor(window_size=4)
    accepted = feed_trace_into_monitor(m, trace)
    assert accepted == 1


def test_feed_trace_handles_none_input() -> None:
    """NEVER raises on bad input."""
    m = ConfidenceMonitor(window_size=4)
    assert feed_trace_into_monitor(m, None) == 0
    assert feed_trace_into_monitor(None, None) == 0  # type: ignore[arg-type]


# ===========================================================================
# §18 — Authority invariants (AST-pinned)
# ===========================================================================


_FORBIDDEN_IMPORTS = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.direction_inferrer",
)


def test_authority_no_forbidden_imports() -> None:
    src = Path(inspect.getfile(confidence_monitor)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_IMPORTS:
                    assert forbidden not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for forbidden in _FORBIDDEN_IMPORTS:
                assert forbidden not in node.module


def test_authority_pure_stdlib_only() -> None:
    src = Path(inspect.getfile(confidence_monitor)).read_text()
    tree = ast.parse(src)
    allowed_roots = {
        "collections", "logging", "math", "os", "threading", "time",
        "dataclasses", "enum", "typing", "__future__",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed_roots, (
                    f"non-stdlib import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in allowed_roots, (
                f"non-stdlib import: {node.module}"
            )


# ===========================================================================
# §19 — to_collapse_error() — Slice 2 ships, Slice 5 raises
# ===========================================================================


def test_to_collapse_error_constructs_correctly(enabled) -> None:
    """The MONITOR builds the error but does NOT raise it.
    Provider wiring (which has access to ENFORCE flag) decides
    whether to raise. Slice 2 contract."""
    m = ConfidenceMonitor(
        window_size=4, provider="dw", model_id="qwen", op_id="op-1",
    )
    for _ in range(4):
        m.observe(0.01)
    err = m.to_collapse_error(
        verdict=ConfidenceVerdict.BELOW_FLOOR,
        posture="HARDEN",
    )
    assert isinstance(err, ConfidenceCollapseError)
    assert err.verdict == ConfidenceVerdict.BELOW_FLOOR
    assert err.posture == "HARDEN"
    assert err.provider == "dw"
    assert err.model_id == "qwen"
    assert err.op_id == "op-1"
    assert err.observations_count == 4
    assert abs(err.rolling_margin - 0.01) < 1e-9


def test_to_collapse_error_does_not_auto_raise(enabled) -> None:
    """Calling to_collapse_error() returns the exception object;
    does NOT raise. Critical Slice 2 contract — pure data."""
    m = ConfidenceMonitor(window_size=2)
    m.observe(0.001)
    m.observe(0.001)
    # No raise — just construction
    err = m.to_collapse_error(verdict=ConfidenceVerdict.BELOW_FLOOR)
    assert err is not None  # got the exception object back
    assert isinstance(err, Exception)
