"""
PerformanceRegressionSensor — Continuous performance monitoring for Ouroboros.

P2 Gap: No continuous benchmarking. PatchBenchmarker exists but runs on-demand.
This sensor periodically analyzes PerformanceRecordPersistence data to detect
latency drift, success rate degradation, and code quality score drops.

Boundary Principle:
  Deterministic: Statistical comparison of recent vs baseline windows.
  Agentic: Optimization (code changes to fix regressions) routed through pipeline.

Emits IntentEnvelopes when performance regression exceeds threshold.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = float(os.environ.get("JARVIS_PERF_REGRESSION_INTERVAL_S", "3600"))
_BASELINE_WINDOW = int(os.environ.get("JARVIS_PERF_BASELINE_WINDOW", "20"))
_RECENT_WINDOW = int(os.environ.get("JARVIS_PERF_RECENT_WINDOW", "5"))
_LATENCY_REGRESSION_FACTOR = float(
    os.environ.get("JARVIS_PERF_LATENCY_REGRESSION_FACTOR", "1.5")
)
_SUCCESS_RATE_DROP_THRESHOLD = float(
    os.environ.get("JARVIS_PERF_SUCCESS_DROP_THRESHOLD", "0.15")
)
_QUALITY_DROP_THRESHOLD = float(
    os.environ.get("JARVIS_PERF_QUALITY_DROP_THRESHOLD", "0.10")
)

# --- Gap #4 migration: CI webhook primary path (Slice 6) -------------------
#
# When ``JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED=true``, CI systems push
# completion events to ``POST /webhook/ci`` and the EventChannelServer
# short-circuits relevant ones (failures + explicit benchmark payloads)
# to ``ingest_webhook`` → ``scan_once``. The 1h poll demotes to a 4h
# fallback that only catches dropped CI webhooks.
#
# Shadow pattern: flag defaults OFF so current poll-every-1h behavior
# is preserved exactly until a 3-session battle-test arc graduates this
# flag (matches the CrossRepoDriftSensor Slice 5 precedent rather than
# the graduated-4 pattern — this is the first CI-surface sensor).
_PERF_REGRESSION_FALLBACK_INTERVAL_S: float = float(
    os.environ.get("JARVIS_PERF_REGRESSION_FALLBACK_INTERVAL_S", "14400")
)


def webhook_enabled() -> bool:
    """Re-read ``JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED`` at call-time."""
    return os.environ.get(
        "JARVIS_PERF_REGRESSION_WEBHOOK_ENABLED", "true",
    ).lower() in ("true", "1", "yes")


@dataclass
class RegressionFinding:
    """One detected performance regression."""
    metric: str            # "latency", "success_rate", "code_quality"
    severity: str          # "high", "normal"
    summary: str
    baseline_value: float
    current_value: float
    delta_pct: float       # Percentage change
    task_type: str         # Which operation type regressed
    details: Dict[str, Any] = field(default_factory=dict)


class PerformanceRegressionSensor:
    """Continuous performance regression sensor for Ouroboros intake.

    Queries PerformanceRecordPersistence on a schedule, compares recent
    window against baseline window, and emits findings when regressions
    exceed configurable thresholds.

    Follows the implicit sensor protocol: start(), stop(), scan_once().
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_findings: set[str] = set()
        # Gap #4 migration captured at __init__. When True, the poll
        # loop demotes to the fallback interval and CI webhook deliveries
        # become the reactive hot path. FS/other paths are unchanged.
        self._webhook_mode: bool = webhook_enabled()
        self._webhooks_handled: int = 0
        self._webhooks_ignored: int = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"perf_regression_sensor_{self._repo}"
        )
        effective = (
            _PERF_REGRESSION_FALLBACK_INTERVAL_S
            if self._webhook_mode
            else self._poll_interval_s
        )
        mode = (
            "ci-webhook-primary (CI completion → scan_once; poll=fallback)"
            if self._webhook_mode
            else "poll-primary"
        )
        logger.info(
            "[PerfSensor] Started for repo=%s poll_interval=%ds mode=%s",
            self._repo, int(effective), mode,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[PerfSensor] Stopped for repo=%s", self._repo)

    async def _poll_loop(self) -> None:
        # Delay to let performance data accumulate
        await asyncio.sleep(120.0)
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[PerfSensor] Poll error")
            effective_interval = (
                _PERF_REGRESSION_FALLBACK_INTERVAL_S
                if self._webhook_mode
                else self._poll_interval_s
            )
            try:
                await asyncio.sleep(effective_interval)
            except asyncio.CancelledError:
                break

    async def ingest_webhook(self, payload: Dict[str, Any]) -> bool:
        """Handle one CI system webhook delivery.

        Alternative entry point to the poll loop. Reacts to CI completion
        events that might indicate a performance regression. Triggers
        ``scan_once()`` only for **interesting** events:

          * ``status`` (or ``conclusion``) ∈ ``{failure, failed, error}`` —
            the CI run didn't pass, which by itself is worth re-scanning
            performance data against (the failure may have produced
            partial benchmark results, or the broken build may have
            aborted the suite mid-run).
          * Payload carries ``benchmark`` / ``metrics`` / ``perf_data``
            — explicit performance signal sent by a CI job that ran a
            benchmark suite.
          * GitHub Actions-as-CI: embedded ``workflow_run.conclusion``
            is ``failure``.

        Benign events (passing run, no perf data) return ``False`` without
        triggering a scan — polling the local ``PerformanceRecordPersistence``
        on every passing build would be as wasteful as the 1h poll it
        replaces.

        Never raises. Returns ``True`` iff the triggered scan produced
        at least one new regression finding.
        """
        try:
            if not isinstance(payload, dict):
                self._webhooks_ignored += 1
                return False

            status = str(
                payload.get("status", payload.get("conclusion", "") or "")
            ).lower()
            has_benchmark = bool(
                payload.get("benchmark")
                or payload.get("metrics")
                or payload.get("perf_data")
            )

            # GitHub Actions wraps job state under workflow_run.conclusion
            workflow_run = payload.get("workflow_run")
            if isinstance(workflow_run, dict):
                wr_conclusion = str(
                    workflow_run.get("conclusion", "") or ""
                ).lower()
                if wr_conclusion and not status:
                    status = wr_conclusion

            is_failure = status in {"failure", "failed", "error"}
            interesting = is_failure or has_benchmark

            if not interesting:
                self._webhooks_ignored += 1
                logger.debug(
                    "[PerfSensor] CI webhook ignored — status=%s "
                    "has_benchmark=%s (not regression-relevant)",
                    status or "(n/a)", has_benchmark,
                )
                return False

            self._webhooks_handled += 1
            logger.info(
                "[PerfSensor] CI webhook status=%s benchmark=%s — "
                "triggering scan_once",
                status or "(n/a)", has_benchmark,
            )
            findings = await self.scan_once()
            return bool(findings)
        except Exception:
            logger.debug(
                "[PerfSensor] CI webhook ingest failed", exc_info=True,
            )
            self._webhooks_ignored += 1
            return False

    async def scan_once(self) -> List[RegressionFinding]:
        """Analyze performance records for regressions."""
        findings: List[RegressionFinding] = []

        try:
            from backend.core.ouroboros.integration import get_performance_persistence
            persistence = get_performance_persistence()
        except Exception:
            logger.debug("[PerfSensor] PerformanceRecordPersistence not available")
            return []

        # Get all records, grouped by task_type
        try:
            all_records = persistence.get_all()
        except Exception:
            logger.debug("[PerfSensor] Failed to read performance records")
            return []

        if len(all_records) < _BASELINE_WINDOW + _RECENT_WINDOW:
            return []  # Insufficient data

        # Group by task_type
        by_type: Dict[str, list] = {}
        for record in all_records:
            task_type = getattr(record, "task_type", "unknown")
            by_type.setdefault(task_type, []).append(record)

        for task_type, records in by_type.items():
            if len(records) < _BASELINE_WINDOW + _RECENT_WINDOW:
                continue

            # Sort by timestamp (oldest first)
            records.sort(key=lambda r: getattr(r, "timestamp", 0))

            baseline = records[-(_BASELINE_WINDOW + _RECENT_WINDOW):-_RECENT_WINDOW]
            recent = records[-_RECENT_WINDOW:]

            # Check latency regression
            finding = self._check_latency_regression(task_type, baseline, recent)
            if finding:
                findings.append(finding)

            # Check success rate drop
            finding = self._check_success_rate_drop(task_type, baseline, recent)
            if finding:
                findings.append(finding)

            # Check code quality drop
            finding = self._check_quality_drop(task_type, baseline, recent)
            if finding:
                findings.append(finding)

        # Emit envelopes
        emitted = 0
        for finding in findings:
            dedup_key = f"{finding.metric}:{finding.task_type}"
            if dedup_key in self._seen_findings:
                continue
            self._seen_findings.add(dedup_key)

            try:
                envelope = make_envelope(
                    source="performance_regression",
                    description=finding.summary,
                    target_files=("backend/core/ouroboros/governance/orchestrator.py",),
                    repo=self._repo,
                    confidence=0.85,
                    urgency=finding.severity,
                    evidence={
                        "category": "performance_regression",
                        "metric": finding.metric,
                        "task_type": finding.task_type,
                        "baseline": finding.baseline_value,
                        "current": finding.current_value,
                        "delta_pct": finding.delta_pct,
                        "sensor": "PerformanceRegressionSensor",
                    },
                    requires_human_ack=False,
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
            except Exception:
                logger.exception("[PerfSensor] Failed to emit finding")

        if findings:
            logger.info(
                "[PerfSensor] Scan: %d regressions found, %d emitted",
                len(findings), emitted,
            )
        return findings

    # ------------------------------------------------------------------
    # Regression detection (all deterministic — statistical comparison)
    # ------------------------------------------------------------------

    def _check_latency_regression(
        self, task_type: str, baseline: list, recent: list
    ) -> Optional[RegressionFinding]:
        """Detect P50 latency regression: recent > baseline * factor."""
        base_latencies = sorted(
            getattr(r, "latency_ms", 0) for r in baseline if getattr(r, "latency_ms", 0) > 0
        )
        recent_latencies = sorted(
            getattr(r, "latency_ms", 0) for r in recent if getattr(r, "latency_ms", 0) > 0
        )

        if not base_latencies or not recent_latencies:
            return None

        base_p50 = base_latencies[len(base_latencies) // 2]
        recent_p50 = recent_latencies[len(recent_latencies) // 2]

        if base_p50 > 0 and recent_p50 > base_p50 * _LATENCY_REGRESSION_FACTOR:
            delta_pct = ((recent_p50 - base_p50) / base_p50) * 100
            return RegressionFinding(
                metric="latency",
                severity="high" if delta_pct > 100 else "normal",
                summary=(
                    f"P50 latency regression in '{task_type}': "
                    f"{base_p50:.0f}ms -> {recent_p50:.0f}ms "
                    f"(+{delta_pct:.0f}%)"
                ),
                baseline_value=base_p50,
                current_value=recent_p50,
                delta_pct=delta_pct,
                task_type=task_type,
            )
        return None

    def _check_success_rate_drop(
        self, task_type: str, baseline: list, recent: list
    ) -> Optional[RegressionFinding]:
        """Detect success rate drop beyond threshold."""
        base_success = sum(1 for r in baseline if getattr(r, "success", False))
        recent_success = sum(1 for r in recent if getattr(r, "success", False))

        base_rate = base_success / max(1, len(baseline))
        recent_rate = recent_success / max(1, len(recent))
        drop = base_rate - recent_rate

        if drop >= _SUCCESS_RATE_DROP_THRESHOLD:
            return RegressionFinding(
                metric="success_rate",
                severity="high" if drop > 0.30 else "normal",
                summary=(
                    f"Success rate drop in '{task_type}': "
                    f"{base_rate:.0%} -> {recent_rate:.0%} "
                    f"(-{drop:.0%})"
                ),
                baseline_value=base_rate,
                current_value=recent_rate,
                delta_pct=-drop * 100,
                task_type=task_type,
            )
        return None

    def _check_quality_drop(
        self, task_type: str, baseline: list, recent: list
    ) -> Optional[RegressionFinding]:
        """Detect code quality score drop beyond threshold."""
        base_scores = [
            getattr(r, "code_quality_score", 0)
            for r in baseline if getattr(r, "code_quality_score", 0) > 0
        ]
        recent_scores = [
            getattr(r, "code_quality_score", 0)
            for r in recent if getattr(r, "code_quality_score", 0) > 0
        ]

        if not base_scores or not recent_scores:
            return None

        base_avg = sum(base_scores) / len(base_scores)
        recent_avg = sum(recent_scores) / len(recent_scores)
        drop = base_avg - recent_avg

        if drop >= _QUALITY_DROP_THRESHOLD:
            return RegressionFinding(
                metric="code_quality",
                severity="normal",
                summary=(
                    f"Code quality drop in '{task_type}': "
                    f"{base_avg:.2f} -> {recent_avg:.2f} "
                    f"(-{drop:.2f})"
                ),
                baseline_value=base_avg,
                current_value=recent_avg,
                delta_pct=-drop * 100,
                task_type=task_type,
            )
        return None

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "PerformanceRegressionSensor",
            "repo": self._repo,
            "running": self._running,
            "findings_seen": len(self._seen_findings),
            "poll_interval_s": self._poll_interval_s,
        }
