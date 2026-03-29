"""REM Epoch — single explore → analyze → patch cycle (Zone 7.0, Phase 3).

A ``RemEpoch`` is the atomic unit of REM Sleep work.  Each epoch:
1. **EXPLORING** — runs oracle checks and fleet agents in parallel.
2. **STREAMING** — streams findings upstream via the SpinalCord.
3. **PATCHING** — converts top-N findings to IntentEnvelopes and submits
   them to the Unified Intake Router, stopping on backpressure.

The epoch is cooperative-cancellable via :class:`CancellationToken`.
All errors are captured and returned in :class:`EpochResult`; no exceptions
escape ``run()``.

Usage::

    from backend.core.ouroboros.cancellation_token import CancellationToken
    from backend.core.ouroboros.daemon_config import DaemonConfig
    from backend.core.ouroboros.rem_epoch import RemEpoch

    epoch = RemEpoch(
        epoch_id=1,
        oracle=oracle,
        fleet=fleet,
        spinal_cord=cord,
        intake_router=router,
        doubleword=dw,
        config=DaemonConfig(),
    )
    result = await epoch.run(token)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.daemon_config import DaemonConfig
from backend.core.ouroboros.exploration_envelope_factory import findings_to_envelopes
from backend.core.ouroboros.finding_ranker import RankedFinding, merge_and_rank

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EpochResult
# ---------------------------------------------------------------------------


@dataclass
class EpochResult:
    """Outcome of a single REM epoch run.

    Attributes
    ----------
    epoch_id:
        Monotonically increasing identifier matching the epoch's ``epoch_id``.
    findings_count:
        Total number of ranked findings produced (after merge/dedup).
    envelopes_submitted:
        Number of IntentEnvelopes successfully accepted by the intake router
        (i.e., ``ingest()`` returned ``"enqueued"``).
    envelopes_backpressured:
        Number of IntentEnvelopes rejected due to backpressure.
    duration_s:
        Wall-clock seconds from ``run()`` entry to return.
    completed:
        True if the epoch reached a clean terminal state (including graceful
        backpressure stop).
    cancelled:
        True if the CancellationToken was set and the epoch stopped early.
    error:
        Non-None string description when an unexpected exception was caught.
    """

    epoch_id: int
    findings_count: int = 0
    envelopes_submitted: int = 0
    envelopes_backpressured: int = 0
    duration_s: float = 0.0
    completed: bool = False
    cancelled: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# RemEpoch
# ---------------------------------------------------------------------------


class RemEpoch:
    """Single REM epoch: explore → stream → patch.

    Parameters
    ----------
    epoch_id:
        Identifier for this epoch.  Passed through to ``EpochResult`` and
        embedded in envelope evidence for correlation.
    oracle:
        Codebase knowledge graph.  ``find_dead_code()`` and
        ``find_circular_dependencies()`` are synchronous methods.
    fleet:
        ``ExplorationFleet`` instance.  ``deploy()`` is asynchronous.
    spinal_cord:
        ``SpinalCord`` for streaming findings upstream.  ``stream_up()`` is
        asynchronous.
    intake_router:
        ``UnifiedIntakeRouter``.  ``ingest()`` is asynchronous and returns
        one of ``"enqueued"``, ``"deduplicated"``, ``"pending_ack"``,
        ``"backpressure"``.
    doubleword:
        Doubleword provider (reserved for future deep analysis; currently
        accepted to satisfy the DI contract but not called in v1).
    config:
        Daemon configuration controlling timeouts and per-epoch caps.
    """

    def __init__(
        self,
        epoch_id: int,
        oracle: Any,
        fleet: Any,
        spinal_cord: Any,
        intake_router: Any,
        doubleword: Any,
        config: DaemonConfig,
        hypothesis_cache_dir: Any = None,
        architect: Any = None,
    ) -> None:
        self._epoch_id = epoch_id
        self._oracle = oracle
        self._fleet = fleet
        self._spinal_cord = spinal_cord
        self._intake_router = intake_router
        self._doubleword = doubleword
        self._config = config
        self._hypothesis_cache_dir = hypothesis_cache_dir
        self._architect = architect

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, token: CancellationToken) -> EpochResult:
        """Execute the full epoch lifecycle and return an :class:`EpochResult`.

        Never raises — all exceptions are caught and encoded in
        ``EpochResult.error``.

        Parameters
        ----------
        token:
            Cooperative cancellation token.  Checked at each phase boundary.
        """
        t0 = time.monotonic()
        result = EpochResult(epoch_id=self._epoch_id)

        try:
            # ---- Early cancellation check ----
            if token.is_cancelled:
                result.cancelled = True
                return result

            # ---- Phase 1: EXPLORING ----
            logger.info("[REM epoch=%d] EXPLORING started", self._epoch_id)
            try:
                findings: List[RankedFinding] = await asyncio.wait_for(
                    self._explore(token),
                    timeout=self._config.rem_cycle_timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[REM epoch=%d] exploration timed out after %.1fs",
                    self._epoch_id,
                    self._config.rem_cycle_timeout_s,
                )
                findings = []

            # ---- Post-explore cancellation check ----
            if token.is_cancelled:
                result.cancelled = True
                return result

            result.findings_count = len(findings)
            logger.info(
                "[REM epoch=%d] EXPLORING complete — %d findings",
                self._epoch_id,
                result.findings_count,
            )

            if not findings:
                result.completed = True
                return result

            # ---- Phase 2: STREAMING findings upstream ----
            logger.info("[REM epoch=%d] STREAMING %d findings", self._epoch_id, len(findings))
            await self._stream_findings(findings)

            # ---- Post-stream cancellation check ----
            if token.is_cancelled:
                result.cancelled = True
                return result

            # ---- Phase 3: PATCHING ----
            logger.info("[REM epoch=%d] PATCHING started", self._epoch_id)
            top_findings = findings[: self._config.rem_max_findings_per_epoch]
            envelopes = findings_to_envelopes(top_findings, epoch_id=self._epoch_id)

            for finding, envelope in zip(top_findings, envelopes):
                if token.is_cancelled:
                    result.cancelled = True
                    return result

                # Architect routing: intercept roadmap findings that signal
                # missing capabilities or manifesto violations before they
                # enter the normal envelope path.  In v1 the architect logs
                # and returns None (model bridge pending); when the bridge is
                # built it will produce plans that feed into SagaOrchestrator.
                if (
                    self._architect is not None
                    and hasattr(finding, "source_check")
                    and finding.source_check.startswith("roadmap:")
                    and finding.category in ("missing_capability", "manifesto_violation")
                    and self._architect.should_design(finding)
                ):
                    logger.info(
                        "[RemEpoch] Routing to architect: %s", finding.description
                    )
                    continue  # skip normal envelope path — architect handles this

                ingest_result = await self._intake_router.ingest(envelope)

                if ingest_result == "backpressure":
                    result.envelopes_backpressured += 1
                    logger.info(
                        "[REM epoch=%d] backpressure — stopping after %d submitted",
                        self._epoch_id,
                        result.envelopes_submitted,
                    )
                    break
                elif ingest_result == "enqueued":
                    result.envelopes_submitted += 1
                # deduplicated / pending_ack are silently skipped

            result.completed = True
            logger.info(
                "[REM epoch=%d] PATCHING complete — submitted=%d backpressured=%d",
                self._epoch_id,
                result.envelopes_submitted,
                result.envelopes_backpressured,
            )

        except asyncio.CancelledError:
            result.cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            logger.exception("[REM epoch=%d] unexpected error: %s", self._epoch_id, exc)

        finally:
            result.duration_s = time.monotonic() - t0

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _explore(self, token: CancellationToken) -> List[RankedFinding]:
        """Run oracle checks and fleet agents in parallel, then rank results.

        Returns a merged, deduplicated, ranked list of :class:`RankedFinding`.
        """
        oracle_task = asyncio.ensure_future(self._run_oracle_checks(token))
        fleet_task = asyncio.ensure_future(self._run_fleet(token))

        oracle_findings: List[RankedFinding] = []
        fleet_findings: List[RankedFinding] = []

        done, pending = await asyncio.wait(
            [oracle_task, fleet_task],
            return_when=asyncio.ALL_COMPLETED,
        )

        for task in done:
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "[REM epoch=%d] explore sub-task failed: %s", self._epoch_id, exc
                )
                # Cancel the other task if still running
                for p in pending:
                    p.cancel()
                raise exc
            task_result = task.result()
            if task is oracle_task:
                oracle_findings = task_result
            else:
                fleet_findings = task_result

        for task in pending:
            task.cancel()

        hypothesis_findings = self._load_cached_hypotheses()
        all_findings = oracle_findings + fleet_findings + hypothesis_findings
        return merge_and_rank(all_findings)

    async def _run_oracle_checks(self, token: CancellationToken) -> List[RankedFinding]:
        """Run synchronous oracle checks and convert to RankedFinding objects.

        ``find_dead_code`` and ``find_circular_dependencies`` are sync — we
        call them directly (they are fast graph operations, not I/O).
        """
        findings: List[RankedFinding] = []

        # --- Dead code ---
        try:
            dead_nodes = self._oracle.find_dead_code()
            for node in dead_nodes:
                if token.is_cancelled:
                    break
                file_path = getattr(node, "file_path", "") or ""
                name = getattr(node, "name", str(node))
                findings.append(
                    RankedFinding(
                        description=f"Potentially unused symbol: {name}",
                        category="dead_code",
                        file_path=file_path,
                        blast_radius=0.1,
                        confidence=0.85,
                        urgency="low",
                        last_modified=time.time(),
                        repo="jarvis",
                        source_check="oracle.find_dead_code",
                    )
                )
        except Exception as exc:
            logger.warning(
                "[REM epoch=%d] oracle.find_dead_code failed: %s", self._epoch_id, exc
            )
            raise

        if token.is_cancelled:
            return findings

        # --- Circular dependencies ---
        try:
            cycles = self._oracle.find_circular_dependencies()
            for cycle in cycles:
                if token.is_cancelled:
                    break
                # Represent the cycle by the file_path of the first node
                first = cycle[0] if cycle else None
                file_path = getattr(first, "file_path", "") if first is not None else ""
                names = ", ".join(
                    getattr(n, "name", str(n)) for n in cycle[:3]
                )
                findings.append(
                    RankedFinding(
                        description=f"Circular dependency detected: {names}",
                        category="circular_dep",
                        file_path=file_path,
                        blast_radius=0.6,
                        confidence=1.0,
                        urgency="high",
                        last_modified=time.time(),
                        repo="jarvis",
                        source_check="oracle.find_circular_dependencies",
                    )
                )
        except Exception as exc:
            logger.warning(
                "[REM epoch=%d] oracle.find_circular_dependencies failed: %s",
                self._epoch_id,
                exc,
            )
            # Non-fatal for circular deps: dead-code findings already collected

        return findings

    async def _run_fleet(self, token: CancellationToken) -> List[RankedFinding]:
        """Deploy the ExplorationFleet and convert its findings to RankedFinding."""
        findings: List[RankedFinding] = []

        try:
            report = await self._fleet.deploy(
                goal=(
                    "Identify architecture gaps, unwired components, "
                    "test coverage holes, and performance bottlenecks"
                ),
                repos=("jarvis", "jarvis-prime", "reactor"),
                max_agents=self._config.rem_max_agents,
            )
        except Exception as exc:
            logger.warning("[REM epoch=%d] fleet.deploy failed: %s", self._epoch_id, exc)
            raise

        for fleet_finding in report.findings:
            if token.is_cancelled:
                break
            category = getattr(fleet_finding, "category", None) or "architecture_gap"
            description = getattr(fleet_finding, "description", str(fleet_finding))
            file_path = getattr(fleet_finding, "file_path", "") or ""
            relevance = float(getattr(fleet_finding, "relevance", 0.5))
            # Clamp relevance to [0, 1]
            confidence = max(0.0, min(1.0, relevance))

            findings.append(
                RankedFinding(
                    description=description,
                    category=category,
                    file_path=file_path,
                    blast_radius=confidence * 0.5,  # blast radius inferred from relevance
                    confidence=confidence,
                    urgency="normal",
                    last_modified=time.time(),
                    repo="jarvis",
                    source_check="fleet.deploy",
                )
            )

        return findings

    async def _stream_findings(self, findings: List[RankedFinding]) -> None:
        """Stream all findings upstream via the SpinalCord."""
        for finding in findings:
            try:
                await self._spinal_cord.stream_up(
                    "rem_finding",
                    {
                        "epoch_id": self._epoch_id,
                        "category": finding.category,
                        "file_path": finding.file_path,
                        "description": finding.description,
                        "score": finding.score,
                        "blast_radius": finding.blast_radius,
                        "confidence": finding.confidence,
                        "urgency": finding.urgency,
                        "repo": finding.repo,
                        "source_check": finding.source_check,
                    },
                )
            except Exception as exc:
                # Non-fatal: log and continue streaming remaining findings
                logger.warning(
                    "[REM epoch=%d] stream_up failed for %s: %s",
                    self._epoch_id,
                    finding.file_path,
                    exc,
                )

    def _load_cached_hypotheses(self) -> List[RankedFinding]:
        """Load cached FeatureHypotheses and convert to RankedFinding for ranking."""
        if self._hypothesis_cache_dir is None:
            return []
        try:
            from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
            cache = HypothesisCache(cache_dir=self._hypothesis_cache_dir)
            hypotheses = cache.load()
            findings = []
            _BLAST_RADIUS = {
                "missing_capability": 0.5,
                "incomplete_wiring": 0.3,
                "stale_implementation": 0.2,
                "manifesto_violation": 0.7,
            }
            for h in hypotheses:
                if getattr(h, "status", "active") != "active":
                    continue
                findings.append(RankedFinding(
                    description=h.description,
                    category=h.gap_type,
                    file_path=h.suggested_scope,
                    blast_radius=_BLAST_RADIUS.get(h.gap_type, 0.3),
                    confidence=h.confidence,
                    urgency=h.urgency,
                    last_modified=h.synthesized_at,
                    repo=h.suggested_repos[0] if h.suggested_repos else "jarvis",
                    source_check=f"roadmap:{h.provenance}",
                ))
            return findings
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RemEpoch] Hypothesis load failed: %s", exc)
            return []
