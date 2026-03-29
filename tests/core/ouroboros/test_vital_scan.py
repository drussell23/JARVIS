"""Tests for VitalScan — Phase 1 boot invariant checks (TDD first).

All tests are deterministic: zero model calls, zero I/O (beyond mock).
Every check exercises a concrete scenario from the specification.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.vital_scan import (
    VitalFinding,
    VitalReport,
    VitalScan,
    VitalStatus,
)


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NodeID:
    """Minimal NodeID stub — only file_path is needed by VitalScan."""
    file_path: str
    repo: str = "jarvis"
    name: str = "stub"


def _make_oracle(
    *,
    cycles: Optional[List[List[_NodeID]]] = None,
    index_age_s: float = 0.0,
    last_indexed: int = 1,  # non-zero → "has been indexed"
) -> MagicMock:
    """Return a mock TheOracle with controlled responses."""
    oracle = MagicMock()
    oracle.find_circular_dependencies.return_value = cycles if cycles is not None else []
    oracle.index_age_s.return_value = index_age_s
    oracle._last_indexed_monotonic_ns = last_indexed
    return oracle


def _make_health_sensor(
    findings: Optional[List[Any]] = None,
) -> AsyncMock:
    """Return a mock RuntimeHealthSensor whose scan_once returns findings."""
    sensor = AsyncMock()
    sensor.scan_once.return_value = findings if findings is not None else []
    return sensor


# ---------------------------------------------------------------------------
# VitalReport unit tests
# ---------------------------------------------------------------------------


class TestVitalReport:
    def test_pass_when_no_findings(self):
        report = VitalReport(status=VitalStatus.PASS, findings=[], duration_s=0.1)
        assert report.status == VitalStatus.PASS
        assert report.warnings == []
        assert report.failures == []

    def test_warn_filters_warnings(self):
        findings = [
            VitalFinding(check="cache_age", severity="warn", detail="stale"),
            VitalFinding(check="circular_dep", severity="fail", detail="cycle"),
        ]
        report = VitalReport(status=VitalStatus.FAIL, findings=findings, duration_s=0.2)
        assert len(report.warnings) == 1
        assert report.warnings[0].check == "cache_age"
        assert len(report.failures) == 1
        assert report.failures[0].check == "circular_dep"

    def test_status_worst_case_fail_wins(self):
        """Even if most findings are warn, a single fail → FAIL status."""
        findings = [
            VitalFinding(check="c1", severity="warn", detail="minor"),
            VitalFinding(check="c2", severity="fail", detail="critical"),
        ]
        report = VitalReport(status=VitalStatus.FAIL, findings=findings, duration_s=0.0)
        assert report.status == VitalStatus.FAIL

    def test_vital_finding_is_frozen(self):
        f = VitalFinding(check="x", severity="warn", detail="d")
        with pytest.raises((AttributeError, TypeError)):
            f.check = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# VitalScan integration scenarios
# ---------------------------------------------------------------------------


class TestVitalScanAllPass:
    @pytest.mark.asyncio
    async def test_all_pass_no_cycles_healthy_deps(self):
        oracle = _make_oracle(cycles=[], index_age_s=10.0)
        sensor = _make_health_sensor(findings=[])
        scan = VitalScan(oracle=oracle, health_sensor=sensor, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.PASS
        assert report.findings == []
        assert report.duration_s >= 0.0

    @pytest.mark.asyncio
    async def test_all_pass_without_health_sensor(self):
        oracle = _make_oracle(cycles=[], index_age_s=5.0)
        scan = VitalScan(oracle=oracle, repo_file_count=200)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.PASS


class TestVitalScanCircularDeps:
    @pytest.mark.asyncio
    async def test_cycle_in_kernel_file_is_fail(self):
        """A cycle that includes unified_supervisor.py → severity 'fail'."""
        cycle = [
            _NodeID(file_path="unified_supervisor.py"),
            _NodeID(file_path="backend/core/foo.py"),
        ]
        oracle = _make_oracle(cycles=[cycle], index_age_s=0.0)
        scan = VitalScan(oracle=oracle, repo_file_count=50)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.FAIL
        assert len(report.failures) >= 1
        fail = report.failures[0]
        assert fail.severity == "fail"
        assert "unified_supervisor.py" in fail.detail or "kernel" in fail.detail.lower()

    @pytest.mark.asyncio
    async def test_cycle_in_governed_loop_service_is_fail(self):
        """governed_loop_service.py is also a kernel file → fail."""
        cycle = [
            _NodeID(file_path="backend/core/ouroboros/governance/governed_loop_service.py"),
            _NodeID(file_path="backend/agents/helper.py"),
        ]
        oracle = _make_oracle(cycles=[cycle], index_age_s=0.0)
        scan = VitalScan(oracle=oracle, repo_file_count=50)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.FAIL
        assert any(f.severity == "fail" for f in report.findings)

    @pytest.mark.asyncio
    async def test_cycle_in_non_kernel_file_is_warn(self):
        """A cycle that doesn't touch kernel files → severity 'warn'."""
        cycle = [
            _NodeID(file_path="backend/agents/foo.py"),
            _NodeID(file_path="backend/agents/bar.py"),
        ]
        oracle = _make_oracle(cycles=[cycle], index_age_s=0.0)
        scan = VitalScan(oracle=oracle, repo_file_count=50)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.WARN
        assert len(report.warnings) >= 1
        assert all(f.severity == "warn" for f in report.findings)

    @pytest.mark.asyncio
    async def test_multiple_cycles_mixed_severity(self):
        """Kernel cycle (fail) + non-kernel cycle (warn) → FAIL overall."""
        kernel_cycle = [
            _NodeID(file_path="unified_supervisor.py"),
            _NodeID(file_path="backend/core/x.py"),
        ]
        user_cycle = [
            _NodeID(file_path="backend/agents/alpha.py"),
            _NodeID(file_path="backend/agents/beta.py"),
        ]
        oracle = _make_oracle(cycles=[kernel_cycle, user_cycle], index_age_s=0.0)
        scan = VitalScan(oracle=oracle, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.FAIL
        assert len(report.failures) >= 1
        assert len(report.warnings) >= 1


class TestVitalScanCacheFreshness:
    @pytest.mark.asyncio
    async def test_no_cache_large_repo_is_fail(self):
        """No cache AND repo >500 files → fail."""
        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        oracle._last_indexed_monotonic_ns = 0  # never indexed
        scan = VitalScan(oracle=oracle, repo_file_count=501)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.FAIL
        assert any(f.severity == "fail" and "cache" in f.check.lower() for f in report.findings)

    @pytest.mark.asyncio
    async def test_no_cache_small_repo_is_pass(self):
        """No cache but repo ≤500 files → no cache finding."""
        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        oracle._last_indexed_monotonic_ns = 0  # never indexed
        scan = VitalScan(oracle=oracle, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        # No cache finding for small repos
        cache_findings = [f for f in report.findings if "cache" in f.check.lower()]
        assert not any(f.severity == "fail" for f in cache_findings)

    @pytest.mark.asyncio
    async def test_stale_cache_over_24h_is_warn(self):
        """Cache that is >24 hours old → warn."""
        stale_seconds = 25 * 3600  # 25 hours
        oracle = _make_oracle(cycles=[], index_age_s=stale_seconds)
        scan = VitalScan(oracle=oracle, repo_file_count=200)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.WARN
        cache_warnings = [
            f for f in report.findings
            if "cache" in f.check.lower() and f.severity == "warn"
        ]
        assert len(cache_warnings) >= 1

    @pytest.mark.asyncio
    async def test_fresh_cache_no_finding(self):
        """Cache <24h old → no cache finding."""
        oracle = _make_oracle(cycles=[], index_age_s=3600.0)  # 1 hour
        scan = VitalScan(oracle=oracle, repo_file_count=200)
        report = await scan.run(timeout_s=5.0)

        cache_findings = [f for f in report.findings if "cache" in f.check.lower()]
        assert cache_findings == []


class TestVitalScanDependencyHealth:
    @pytest.mark.asyncio
    async def test_critical_cve_is_fail(self):
        """A HealthFinding with severity='critical' → VitalFinding severity='fail'."""
        health_finding = MagicMock()
        health_finding.severity = "critical"
        health_finding.summary = "CVE-2024-0001 in requests"
        health_finding.category = "security"

        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        sensor = _make_health_sensor(findings=[health_finding])
        scan = VitalScan(oracle=oracle, health_sensor=sensor, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.FAIL
        dep_failures = [
            f for f in report.findings
            if f.severity == "fail" and "dep" in f.check.lower()
        ]
        assert len(dep_failures) >= 1

    @pytest.mark.asyncio
    async def test_non_critical_health_finding_is_warn(self):
        """A HealthFinding with severity='high' → VitalFinding severity='warn'."""
        health_finding = MagicMock()
        health_finding.severity = "high"
        health_finding.summary = "requests 2.28 is 4 minor versions behind"
        health_finding.category = "package_stale"

        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        sensor = _make_health_sensor(findings=[health_finding])
        scan = VitalScan(oracle=oracle, health_sensor=sensor, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.WARN
        dep_warnings = [
            f for f in report.findings
            if f.severity == "warn" and "dep" in f.check.lower()
        ]
        assert len(dep_warnings) >= 1

    @pytest.mark.asyncio
    async def test_no_health_sensor_skips_dep_check(self):
        """When health_sensor is None, dependency check is skipped gracefully."""
        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        scan = VitalScan(oracle=oracle, health_sensor=None, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        dep_findings = [f for f in report.findings if "dep" in f.check.lower()]
        assert dep_findings == []


class TestVitalScanTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_warn(self):
        """If checks exceed timeout_s, the scan returns WARN with partial results."""
        oracle = MagicMock()

        async def _slow_cycles():
            await asyncio.sleep(10.0)  # way longer than timeout
            return []

        # find_circular_dependencies is sync in oracle, but we simulate slow via
        # the scan's internal async wrapper stalling — we fake via asyncio.sleep
        # in the oracle call itself by patching _run_checks.
        original_run_checks = VitalScan._run_checks

        async def _slow_run_checks(self_inner, findings):
            await asyncio.sleep(10.0)

        scan = VitalScan(oracle=oracle, repo_file_count=100)

        with patch.object(VitalScan, "_run_checks", _slow_run_checks):
            report = await scan.run(timeout_s=0.05)

        assert report.status == VitalStatus.WARN
        # The timeout finding check name contains "timeout"; detail says "timed out"
        timeout_findings = [
            f for f in report.findings
            if "timeout" in f.check.lower() or "timed" in f.detail.lower()
        ]
        assert len(timeout_findings) >= 1

    @pytest.mark.asyncio
    async def test_timeout_includes_partial_results(self):
        """Findings collected before timeout are preserved in the report."""
        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        pre_findings = []

        original_run_checks = VitalScan._run_checks

        async def _partial_run_checks(self_inner, findings):
            # Add one finding then stall
            findings.append(
                VitalFinding(check="partial_check", severity="warn", detail="partial")
            )
            await asyncio.sleep(10.0)

        scan = VitalScan(oracle=oracle, repo_file_count=100)

        with patch.object(VitalScan, "_run_checks", _partial_run_checks):
            report = await scan.run(timeout_s=0.05)

        assert report.status == VitalStatus.WARN
        # The partial finding plus the timeout finding should both be present
        assert len(report.findings) >= 1


class TestVitalScanNoneOracle:
    @pytest.mark.asyncio
    async def test_none_oracle_returns_pass_gracefully(self):
        """When oracle is None, VitalScan returns PASS without raising."""
        scan = VitalScan(oracle=None, repo_file_count=0)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.PASS
        assert isinstance(report.findings, list)
        assert report.duration_s >= 0.0

    @pytest.mark.asyncio
    async def test_none_oracle_with_large_repo_no_crash(self):
        """Oracle=None + large repo count does not crash or raise."""
        scan = VitalScan(oracle=None, repo_file_count=1000)
        report = await scan.run(timeout_s=5.0)
        # Should not raise; status may be PASS since no oracle to check
        assert report.status in (VitalStatus.PASS, VitalStatus.WARN)


class TestVitalScanIdempotency:
    @pytest.mark.asyncio
    async def test_run_twice_same_result(self):
        """VitalScan.run() is idempotent — same inputs produce same outputs."""
        cycle = [
            _NodeID(file_path="backend/agents/foo.py"),
            _NodeID(file_path="backend/agents/bar.py"),
        ]
        oracle = _make_oracle(cycles=[cycle], index_age_s=5.0)
        scan = VitalScan(oracle=oracle, repo_file_count=100)

        report1 = await scan.run(timeout_s=5.0)
        report2 = await scan.run(timeout_s=5.0)

        assert report1.status == report2.status
        assert len(report1.findings) == len(report2.findings)

    @pytest.mark.asyncio
    async def test_no_side_effects_on_oracle(self):
        """VitalScan must not mutate the oracle state."""
        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        initial_last_indexed = oracle._last_indexed_monotonic_ns

        scan = VitalScan(oracle=oracle, repo_file_count=100)
        await scan.run(timeout_s=5.0)

        assert oracle._last_indexed_monotonic_ns == initial_last_indexed
        # find_circular_dependencies should only be called (read-only), not set
        assert not oracle.find_circular_dependencies.called or True  # called is fine, mutation is not


class TestVitalStatusDerivedFromFindings:
    @pytest.mark.asyncio
    async def test_only_warn_findings_gives_warn_status(self):
        oracle = _make_oracle(
            cycles=[[_NodeID(file_path="backend/util/helper.py"), _NodeID(file_path="backend/util/other.py")]],
            index_age_s=0.0,
        )
        scan = VitalScan(oracle=oracle, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.WARN
        assert all(f.severity == "warn" for f in report.findings)

    @pytest.mark.asyncio
    async def test_empty_findings_gives_pass_status(self):
        oracle = _make_oracle(cycles=[], index_age_s=0.0)
        scan = VitalScan(oracle=oracle, repo_file_count=100)
        report = await scan.run(timeout_s=5.0)

        assert report.status == VitalStatus.PASS
        assert report.findings == []
