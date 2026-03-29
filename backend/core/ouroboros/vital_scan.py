"""
VitalScan — Phase 1 of OuroborosDaemon.awaken()
================================================

Runs deterministic boot-time invariant checks with ZERO model calls.
All checks are synchronous in nature; the async wrapper exists only to
honour a shared timeout budget and to be awaitable from Zone 7.0.

Design contract
---------------
* No side effects — safe to re-run any number of times.
* No model or network I/O — every check is purely structural.
* Idempotent — identical inputs always produce identical outputs.
* Timeout-safe — partial results are returned with a timeout finding
  rather than raising.

Checks performed (in order)
----------------------------
1. Circular dependency detection (via TheOracle)
   - Cycle that includes a kernel file  → severity "fail"
   - Other cycle                         → severity "warn"
2. Cache freshness
   - No cache AND repo >500 files        → severity "fail"
   - Cache >24 h stale                   → severity "warn"
3. Dependency health (via RuntimeHealthSensor if provided)
   - HealthFinding.severity == "critical" → severity "fail"
   - Any other finding                    → severity "warn"
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger("Ouroboros.VitalScan")

# ---------------------------------------------------------------------------
# Kernel files — cycles touching these are escalated to "fail"
# ---------------------------------------------------------------------------

_KERNEL_FILES: frozenset[str] = frozenset(
    {
        "unified_supervisor.py",
        "backend/core/ouroboros/governance/governed_loop_service.py",
    }
)

# Cache staleness threshold: 24 hours in seconds
_CACHE_STALE_THRESHOLD_S: float = 24 * 3600.0

# Repo size threshold above which a missing cache is a hard failure
_LARGE_REPO_THRESHOLD: int = 500


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class VitalStatus(enum.Enum):
    """Aggregate status of a VitalReport."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class VitalFinding:
    """One detected invariant violation."""

    check: str       # Machine-readable check name, e.g. "circular_deps"
    severity: str    # "fail" or "warn"
    detail: str      # Human-readable description


@dataclass
class VitalReport:
    """Aggregate result of a VitalScan run."""

    status: VitalStatus
    findings: List[VitalFinding]
    duration_s: float

    @property
    def warnings(self) -> List[VitalFinding]:
        """Findings with severity 'warn'."""
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def failures(self) -> List[VitalFinding]:
        """Findings with severity 'fail'."""
        return [f for f in self.findings if f.severity == "fail"]


# ---------------------------------------------------------------------------
# VitalScan
# ---------------------------------------------------------------------------


class VitalScan:
    """Phase 1 boot invariant checker for OuroborosDaemon.

    Parameters
    ----------
    oracle:
        TheOracle instance (or duck-typed equivalent).  May be ``None``; in
        that case all oracle-dependent checks are skipped gracefully.
    health_sensor:
        Optional RuntimeHealthSensor whose ``scan_once()`` coroutine is
        called during the dependency-health check.  When ``None`` the check
        is skipped.
    repo_file_count:
        Total number of tracked files in the primary repository.  Used by
        the cache-freshness check to distinguish large repos (>500 files)
        from small ones.
    """

    def __init__(
        self,
        oracle: Any,
        health_sensor: Any = None,
        repo_file_count: int = 0,
    ) -> None:
        self._oracle = oracle
        self._health_sensor = health_sensor
        self._repo_file_count = repo_file_count

    async def run(self, timeout_s: float = 30.0) -> VitalReport:
        """Execute all invariant checks within *timeout_s* seconds.

        Returns a :class:`VitalReport` in all cases — never raises.  If the
        checks exceed *timeout_s* a WARN-severity timeout finding is appended
        and the partial results are returned.
        """
        start = time.monotonic()
        findings: List[VitalFinding] = []

        try:
            await asyncio.wait_for(
                self._run_checks(findings),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[VitalScan] checks exceeded timeout of %.1fs; returning partial results",
                timeout_s,
            )
            findings.append(
                VitalFinding(
                    check="vital_scan_timeout",
                    severity="warn",
                    detail=f"Vital scan timed out after {timeout_s:.1f}s; results are partial",
                )
            )
        except Exception:
            logger.exception("[VitalScan] unexpected error during checks")
            findings.append(
                VitalFinding(
                    check="vital_scan_error",
                    severity="warn",
                    detail="Unexpected error during vital scan; results are partial",
                )
            )

        duration = time.monotonic() - start
        status = _derive_status(findings)

        logger.info(
            "[VitalScan] completed in %.3fs — status=%s findings=%d",
            duration,
            status.value,
            len(findings),
        )

        return VitalReport(status=status, findings=findings, duration_s=duration)

    async def _run_checks(self, findings: List[VitalFinding]) -> None:
        """Run all checks, appending :class:`VitalFinding` objects to *findings*.

        This is the single extensible entry point.  All checks are
        synchronous internally; the method is ``async`` to allow future
        checks to await I/O without restructuring.
        """
        self._check_circular_deps(findings)
        self._check_cache_freshness(findings)
        await self._check_dependency_health(findings)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_circular_deps(self, findings: List[VitalFinding]) -> None:
        """Detect circular imports/dependencies via the Oracle.

        Delegates to ``oracle.find_circular_dependencies()`` when available,
        then falls back to ``oracle._graph.find_circular_dependencies()``
        for backwards compatibility with older Oracle builds that expose the
        method only on the underlying graph.
        """
        if self._oracle is None:
            return

        try:
            cycles = _call_find_circular_dependencies(self._oracle)
        except Exception:
            logger.exception("[VitalScan] circular-dep check failed")
            return

        for cycle in cycles:
            files_in_cycle = _extract_file_paths(cycle)
            is_kernel = any(_is_kernel_file(fp) for fp in files_in_cycle)
            severity = "fail" if is_kernel else "warn"

            # Surface the most relevant file path in the detail
            kernel_hit = next(
                (fp for fp in files_in_cycle if _is_kernel_file(fp)), None
            )
            representative = kernel_hit or (files_in_cycle[0] if files_in_cycle else "<unknown>")
            cycle_repr = " → ".join(files_in_cycle[:4])
            if len(files_in_cycle) > 4:
                cycle_repr += " → ..."

            detail_parts = [f"Cycle: {cycle_repr}"]
            if kernel_hit:
                detail_parts.append(f"(kernel file: {kernel_hit})")

            findings.append(
                VitalFinding(
                    check="circular_deps",
                    severity=severity,
                    detail=" ".join(detail_parts),
                )
            )

    def _check_cache_freshness(self, findings: List[VitalFinding]) -> None:
        """Assess whether the Oracle's structural cache is acceptably fresh.

        Rules:
        * No cache AND repo >500 files → "fail"
        * Cache >24 h stale             → "warn"
        """
        if self._oracle is None:
            return

        try:
            last_indexed_ns = getattr(self._oracle, "_last_indexed_monotonic_ns", None)
        except Exception:
            logger.debug("[VitalScan] could not read oracle._last_indexed_monotonic_ns")
            return

        never_indexed = (last_indexed_ns is None) or (last_indexed_ns == 0)

        if never_indexed:
            if self._repo_file_count > _LARGE_REPO_THRESHOLD:
                findings.append(
                    VitalFinding(
                        check="cache_freshness",
                        severity="fail",
                        detail=(
                            f"Oracle has never been indexed and repo has "
                            f"{self._repo_file_count} files (>{_LARGE_REPO_THRESHOLD}). "
                            "Run oracle.full_index() before booting."
                        ),
                    )
                )
            # Small repos: no finding — fresh index will be fast at first use
            return

        # Oracle has been indexed before — check age
        try:
            age_s = _get_index_age_s(self._oracle)
        except Exception:
            logger.debug("[VitalScan] could not determine oracle cache age")
            return

        if age_s > _CACHE_STALE_THRESHOLD_S:
            hours = age_s / 3600.0
            findings.append(
                VitalFinding(
                    check="cache_freshness",
                    severity="warn",
                    detail=(
                        f"Oracle cache is {hours:.1f}h old "
                        f"(threshold: {_CACHE_STALE_THRESHOLD_S / 3600:.0f}h). "
                        "Consider refreshing the index."
                    ),
                )
            )

    async def _check_dependency_health(self, findings: List[VitalFinding]) -> None:
        """Query RuntimeHealthSensor for dependency/security issues.

        Skipped gracefully when ``health_sensor`` is ``None``.
        """
        if self._health_sensor is None:
            return

        try:
            health_findings = await self._health_sensor.scan_once()
        except Exception:
            logger.exception("[VitalScan] dependency-health check failed")
            return

        for hf in health_findings:
            severity = "fail" if getattr(hf, "severity", "") == "critical" else "warn"
            summary = getattr(hf, "summary", str(hf))
            category = getattr(hf, "category", "unknown")
            findings.append(
                VitalFinding(
                    check=f"dep_health_{category}",
                    severity=severity,
                    detail=summary,
                )
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_status(findings: List[VitalFinding]) -> VitalStatus:
    """Worst-case status from a list of findings.

    Any "fail" → FAIL, any "warn" → WARN, else → PASS.
    """
    if any(f.severity == "fail" for f in findings):
        return VitalStatus.FAIL
    if any(f.severity == "warn" for f in findings):
        return VitalStatus.WARN
    return VitalStatus.PASS


def _call_find_circular_dependencies(oracle: Any) -> list:
    """Call find_circular_dependencies() on whatever object is available.

    Tries the oracle directly first, then falls back to oracle._graph.
    Returns an empty list if neither is available.
    """
    if callable(getattr(oracle, "find_circular_dependencies", None)):
        return oracle.find_circular_dependencies()
    graph = getattr(oracle, "_graph", None)
    if graph is not None and callable(getattr(graph, "find_circular_dependencies", None)):
        return graph.find_circular_dependencies()
    return []


def _get_index_age_s(oracle: Any) -> float:
    """Return oracle cache age in seconds via the best available API."""
    if callable(getattr(oracle, "index_age_s", None)):
        return oracle.index_age_s()
    # Compute manually from monotonic timestamp
    last_ns = getattr(oracle, "_last_indexed_monotonic_ns", 0) or 0
    if last_ns == 0:
        return 0.0
    return (time.monotonic_ns() - last_ns) / 1_000_000_000


def _extract_file_paths(cycle: list) -> List[str]:
    """Extract file path strings from a list of NodeID (or duck-typed) objects."""
    paths = []
    for node in cycle:
        if isinstance(node, str):
            paths.append(node)
        elif hasattr(node, "file_path"):
            paths.append(node.file_path)
        else:
            paths.append(str(node))
    return paths


def _is_kernel_file(file_path: str) -> bool:
    """Return True if *file_path* names a kernel file."""
    # Exact match or suffix match so paths like "jarvis:unified_supervisor.py:X" also match
    for kernel in _KERNEL_FILES:
        if file_path == kernel or file_path.endswith(kernel):
            return True
    return False
