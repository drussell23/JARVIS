# [Ouroboros] Modified by Ouroboros (op=op-01a07b16-) at 2026-09-07 09:06 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a07b18-) at 2026-09-07 09:10 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a07b19-) at 2026-09-07 09:16 UTC
# Reason: First-order proof #2: author a real unit test for the untested production_oracle aggregator  AUTHOR a new pytest test fi

# Reason: First-order proof #2: author a real unit test for the untested production_oracle aggregator  AUTHOR a new pytest test fi

# Reason: First-order proof #2: author a real unit test for the untested production_oracle aggregator  AUTHOR a new pytest test fi

import enum
from typing import Any, Dict
from unittest.mock import Mock

import pytest

from backend.core.ouroboros.governance.production_oracle import (
    OracleKind,
    OracleSignal,
    OracleVerdict,
    compute_aggregate_verdict,
    project_signal_for_observability,
)

def test_compute_aggregate_verdict_empty_input():
    """Test that empty input returns INSUFFICIENT_DATA."""
    result = compute_aggregate_verdict([])
    assert result == OracleVerdict.INSUFFICIENT_DATA


def test_compute_aggregate_verdict_insufficient_signals():
    """Test that fewer signals than minimum_signals returns INSUFFICIENT_DATA."""
    signal = OracleSignal(
        oracle_name="test",
        kind=OracleKind.ERROR,
        verdict=OracleVerdict.HEALTHY,
        observed_at_ts=1.0,
        summary="test",
        payload={}
    )
    result = compute_aggregate_verdict([signal], minimum_signals=2)
    assert result == OracleVerdict.INSUFFICIENT_DATA


def test_compute_aggregate_verdict_all_disabled():
    """Test that all DISABLED signals returns DISABLED."""
    signal = OracleSignal(
        oracle_name="test",
        kind=OracleKind.ERROR,
        verdict=OracleVerdict.DISABLED,
        observed_at_ts=1.0,
        summary="test",
        payload={}
    )
    result = compute_aggregate_verdict([signal])
    assert result == OracleVerdict.DISABLED


def test_compute_aggregate_verdict_failed_signal():
    """Test that FAILED signal with severity >= fail_threshold_severity returns FAILED."""
    signal = OracleSignal(
        oracle_name="test",
        kind=OracleKind.ERROR,
        verdict=OracleVerdict.FAILED,
        observed_at_ts=1.0,
        summary="test",
        payload={},
        severity=0.9
    )
    result = compute_aggregate_verdict([signal])
    assert result == OracleVerdict.FAILED


def test_compute_aggregate_verdict_degraded_signal():
    """Test that DEGRADED signal with severity >= degrade_threshold_severity returns DEGRADED."""
    signal = OracleSignal(
        oracle_name="test",
        kind=OracleKind.ERROR,
        verdict=OracleVerdict.DEGRADED,
        observed_at_ts=1.0,
        summary="test",
        payload={},
        severity=0.6
    )
    result = compute_aggregate_verdict([signal])
    assert result == OracleVerdict.DEGRADED


def test_compute_aggregate_verdict_healthy_signal():
    """Test that HEALTHY signal with no FAILED/DEGRADED signals returns HEALTHY."""
    signal = OracleSignal(
        oracle_name="test",
        kind=OracleKind.ERROR,
        verdict=OracleVerdict.HEALTHY,
        observed_at_ts=1.0,
        summary="test",
        payload={},
        severity=0.4
    )
    result = compute_aggregate_verdict([signal])
    assert result == OracleVerdict.HEALTHY


def test_project_signal_for_observability():
    """Test that project_signal_for_observability returns correct keys with enum .value strings."""
    signal = OracleSignal(
        oracle_name="test_oracle",
        kind=OracleKind.ERROR,
        verdict=OracleVerdict.HEALTHY,
        observed_at_ts=1.0,
        summary="test_summary",
        payload={"key": "value"},
        severity=0.5
    )
    result = project_signal_for_observability(signal)
    assert result["oracle_name"] == "test_oracle"
    assert result["kind"] == OracleKind.ERROR.value
    assert result["verdict"] == OracleVerdict.HEALTHY.value
    assert result["observed_at_ts"] == 1.0
    assert result["summary"] == "test_summary"
    assert result["payload"] == {"key": "value"}
    assert result["severity"] == 0.5


def test_project_signal_for_observability_summary_truncation():
    """Test that project_signal_for_observability truncates summary to 200 chars."""
    long_summary = "x" * 250
    signal = OracleSignal(
        oracle_name="test_oracle",
        kind=OracleKind.ERROR,
        verdict=OracleVerdict.HEALTHY,
        observed_at_ts=1.0,
        summary=long_summary,
        payload={"key": "value"},
        severity=0.5
    )
    result = project_signal_for_observability(signal)
    assert len(result["summary"]) == 200
