"""Tests for cross-repo integration test harness core data types."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Dict, FrozenSet

import pytest

from tests.harness.types import (
    ComponentStatus,
    ContractReasonCode,
    ContractStatus,
    FaultComposition,
    FaultHandle,
    FaultScope,
    ObservedEvent,
    OracleObservation,
    PhaseFailure,
    PhaseResult,
    ScenarioResult,
)


# ---------------------------------------------------------------------------
# ObservedEvent
# ---------------------------------------------------------------------------
class TestObservedEvent:
    """ObservedEvent is a frozen dataclass with all expected fields."""

    def test_all_fields_present(self) -> None:
        ev = ObservedEvent(
            oracle_event_seq=1,
            timestamp_mono=100.0,
            source="state_oracle",
            event_type="status_change",
            component="voice",
            old_value="READY",
            new_value="DEGRADED",
            epoch=3,
            scenario_phase="inject",
            trace_root_id="root-abc",
            trace_id="trace-123",
            metadata={"key": "val"},
        )
        assert ev.oracle_event_seq == 1
        assert ev.timestamp_mono == 100.0
        assert ev.source == "state_oracle"
        assert ev.event_type == "status_change"
        assert ev.component == "voice"
        assert ev.old_value == "READY"
        assert ev.new_value == "DEGRADED"
        assert ev.epoch == 3
        assert ev.scenario_phase == "inject"
        assert ev.trace_root_id == "root-abc"
        assert ev.trace_id == "trace-123"
        assert ev.metadata == {"key": "val"}

    def test_frozen(self) -> None:
        ev = ObservedEvent(
            oracle_event_seq=0,
            timestamp_mono=0.0,
            source="s",
            event_type="e",
            component=None,
            old_value=None,
            new_value="n",
            epoch=0,
            scenario_phase="p",
            trace_root_id="r",
            trace_id="t",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.source = "other"  # type: ignore[misc]

    def test_metadata_defaults_to_empty_dict(self) -> None:
        ev = ObservedEvent(
            oracle_event_seq=0,
            timestamp_mono=0.0,
            source="s",
            event_type="e",
            component=None,
            old_value=None,
            new_value="n",
            epoch=0,
            scenario_phase="p",
            trace_root_id="r",
            trace_id="t",
        )
        assert ev.metadata == {}

    def test_optional_fields_accept_none(self) -> None:
        ev = ObservedEvent(
            oracle_event_seq=0,
            timestamp_mono=0.0,
            source="s",
            event_type="e",
            component=None,
            old_value=None,
            new_value="n",
            epoch=0,
            scenario_phase="p",
            trace_root_id="r",
            trace_id="t",
        )
        assert ev.component is None
        assert ev.old_value is None


# ---------------------------------------------------------------------------
# OracleObservation
# ---------------------------------------------------------------------------
class TestOracleObservation:
    """OracleObservation carries a value with quality classification."""

    def test_quality_values(self) -> None:
        for quality in ("fresh", "stale", "timeout", "divergent"):
            obs = OracleObservation(
                value="ok",
                observed_at_mono=1.0,
                observation_quality=quality,  # type: ignore[arg-type]
                source="test",
            )
            assert obs.observation_quality == quality

    def test_frozen(self) -> None:
        obs = OracleObservation(
            value=42,
            observed_at_mono=1.0,
            observation_quality="fresh",
            source="test",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            obs.value = 99  # type: ignore[misc]

    def test_all_fields(self) -> None:
        obs = OracleObservation(
            value={"nested": True},
            observed_at_mono=55.5,
            observation_quality="stale",
            source="polling",
        )
        assert obs.value == {"nested": True}
        assert obs.observed_at_mono == 55.5
        assert obs.observation_quality == "stale"
        assert obs.source == "polling"


# ---------------------------------------------------------------------------
# FaultScope
# ---------------------------------------------------------------------------
class TestFaultScope:
    """FaultScope enum has exactly 5 members."""

    def test_exactly_5_scopes(self) -> None:
        assert len(FaultScope) == 5

    def test_expected_members(self) -> None:
        expected = {"component", "transport", "contract", "clock", "process"}
        actual = {member.value for member in FaultScope}
        assert actual == expected


# ---------------------------------------------------------------------------
# FaultComposition
# ---------------------------------------------------------------------------
class TestFaultComposition:
    """FaultComposition enum has exactly 3 policies."""

    def test_exactly_3_policies(self) -> None:
        assert len(FaultComposition) == 3

    def test_expected_members(self) -> None:
        expected = {"reject", "stack", "replace"}
        actual = {member.value for member in FaultComposition}
        assert actual == expected


# ---------------------------------------------------------------------------
# ContractStatus
# ---------------------------------------------------------------------------
class TestContractStatus:
    """ContractStatus carries compatible flag + reason code + optional detail."""

    def test_compatible(self) -> None:
        cs = ContractStatus(
            compatible=True,
            reason_code=ContractReasonCode.OK,
        )
        assert cs.compatible is True
        assert cs.reason_code == ContractReasonCode.OK
        assert cs.detail is None

    def test_incompatible_with_detail(self) -> None:
        cs = ContractStatus(
            compatible=False,
            reason_code=ContractReasonCode.SCHEMA_HASH,
            detail="hash mismatch: abc != def",
        )
        assert cs.compatible is False
        assert cs.detail == "hash mismatch: abc != def"

    def test_all_6_reason_codes(self) -> None:
        expected = {
            "ok",
            "version_window",
            "schema_hash",
            "missing_capability",
            "handshake_missing",
            "handshake_expired",
        }
        actual = {member.value for member in ContractReasonCode}
        assert actual == expected
        assert len(ContractReasonCode) == 6

    def test_frozen(self) -> None:
        cs = ContractStatus(
            compatible=True,
            reason_code=ContractReasonCode.OK,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            cs.compatible = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FaultHandle
# ---------------------------------------------------------------------------
class TestFaultHandle:
    """FaultHandle carries all fault metadata including revert callable."""

    def test_all_fields(self) -> None:
        async def _revert() -> None:
            pass

        fh = FaultHandle(
            fault_id="fault-001",
            scope=FaultScope.COMPONENT,
            target="voice",
            affected_components=frozenset({"voice", "tts"}),
            unaffected_components=frozenset({"vision"}),
            pre_fault_baseline={"voice": "READY", "tts": "READY"},
            convergence_deadline_s=30.0,
            revert=_revert,
        )
        assert fh.fault_id == "fault-001"
        assert fh.scope == FaultScope.COMPONENT
        assert fh.target == "voice"
        assert fh.affected_components == frozenset({"voice", "tts"})
        assert fh.unaffected_components == frozenset({"vision"})
        assert fh.pre_fault_baseline == {"voice": "READY", "tts": "READY"}
        assert fh.convergence_deadline_s == 30.0
        assert callable(fh.revert)

    def test_frozen(self) -> None:
        async def _revert() -> None:
            pass

        fh = FaultHandle(
            fault_id="fault-001",
            scope=FaultScope.COMPONENT,
            target="voice",
            affected_components=frozenset(),
            unaffected_components=frozenset(),
            pre_fault_baseline={},
            convergence_deadline_s=10.0,
            revert=_revert,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            fh.fault_id = "changed"  # type: ignore[misc]

    def test_pre_fault_baseline_is_dict(self) -> None:
        async def _revert() -> None:
            pass

        fh = FaultHandle(
            fault_id="f",
            scope=FaultScope.TRANSPORT,
            target="bus",
            affected_components=frozenset({"a"}),
            unaffected_components=frozenset({"b"}),
            pre_fault_baseline={"a": "READY"},
            convergence_deadline_s=5.0,
            revert=_revert,
        )
        assert isinstance(fh.pre_fault_baseline, dict)

    def test_revert_is_awaitable(self) -> None:
        async def _revert() -> None:
            pass

        fh = FaultHandle(
            fault_id="f",
            scope=FaultScope.PROCESS,
            target="proc",
            affected_components=frozenset(),
            unaffected_components=frozenset(),
            pre_fault_baseline={},
            convergence_deadline_s=1.0,
            revert=_revert,
        )
        result = fh.revert()
        assert asyncio.iscoroutine(result)
        # Clean up the coroutine to avoid RuntimeWarning
        result.close()


# ---------------------------------------------------------------------------
# ComponentStatus
# ---------------------------------------------------------------------------
class TestComponentStatus:
    """ComponentStatus enum has exactly 11 members."""

    def test_exactly_11_statuses(self) -> None:
        assert len(ComponentStatus) == 11

    def test_expected_members(self) -> None:
        expected = {
            "READY",
            "DEGRADED",
            "FAILED",
            "LOST",
            "STOPPED",
            "STARTING",
            "REGISTERED",
            "HANDSHAKING",
            "DRAINING",
            "STOPPING",
            "UNKNOWN",
        }
        actual = {member.value for member in ComponentStatus}
        assert actual == expected


# ---------------------------------------------------------------------------
# PhaseFailure
# ---------------------------------------------------------------------------
class TestPhaseFailure:
    """PhaseFailure is a typed failure with phase, failure_type, and detail."""

    def test_all_fields(self) -> None:
        pf = PhaseFailure(
            phase="converge",
            failure_type="phase_timeout",
            detail="Timed out after 30s",
        )
        assert pf.phase == "converge"
        assert pf.failure_type == "phase_timeout"
        assert pf.detail == "Timed out after 30s"

    def test_frozen(self) -> None:
        pf = PhaseFailure(
            phase="inject",
            failure_type="invariant_violation",
            detail="monotonic epoch violated",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            pf.phase = "other"  # type: ignore[misc]

    def test_failure_types_are_strings(self) -> None:
        """All four documented failure_type values are accepted as plain strings."""
        for ft in (
            "phase_timeout",
            "oracle_stale",
            "invariant_violation",
            "divergence_error",
        ):
            pf = PhaseFailure(phase="p", failure_type=ft, detail="d")
            assert pf.failure_type == ft


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------
class TestPhaseResult:
    """PhaseResult tracks duration and violations for a single phase."""

    def test_defaults(self) -> None:
        pr = PhaseResult(duration_s=1.5)
        assert pr.duration_s == 1.5
        assert pr.violations == []

    def test_frozen(self) -> None:
        pr = PhaseResult(duration_s=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            pr.duration_s = 2.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ScenarioResult
# ---------------------------------------------------------------------------
class TestScenarioResult:
    """ScenarioResult aggregates the outcome of a full scenario run."""

    def test_all_fields(self) -> None:
        phase = PhaseResult(duration_s=2.0, violations=["v1"])
        sr = ScenarioResult(
            scenario_name="s1_prime_crash",
            trace_root_id="root-xyz",
            passed=False,
            violations=["v1"],
            phases={"inject": phase},
            event_log=[{"seq": 1}],
        )
        assert sr.scenario_name == "s1_prime_crash"
        assert sr.trace_root_id == "root-xyz"
        assert sr.passed is False
        assert sr.violations == ["v1"]
        assert sr.phases == {"inject": phase}
        assert sr.event_log == [{"seq": 1}]

    def test_frozen(self) -> None:
        sr = ScenarioResult(
            scenario_name="s",
            trace_root_id="r",
            passed=True,
            violations=[],
            phases={},
            event_log=[],
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            sr.passed = False  # type: ignore[misc]
