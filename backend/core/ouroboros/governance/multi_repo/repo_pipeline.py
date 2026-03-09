"""backend/core/ouroboros/governance/multi_repo/repo_pipeline.py

Per-repo GovernedLoopService orchestration.

Routes IntentSignals to the correct repo's pipeline, enriches the
OperationContext with blast radius data, and manages lifecycle.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §3
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.core.ouroboros.governance.intent.signals import IntentSignal
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.operation_id import generate_operation_id

from .blast_radius import CrossRepoBlastRadius
from .registry import RepoRegistry

logger = logging.getLogger(__name__)


class RepoPipelineManager:
    """Manages GovernedLoopService instances per repo.

    Routes signals to the correct pipeline and enriches context
    with blast radius analysis.
    """

    def __init__(
        self,
        registry: RepoRegistry,
        pipelines: Dict[str, Any],
        blast_radius_analyzer: Optional[CrossRepoBlastRadius] = None,
    ) -> None:
        self._registry = registry
        self._pipelines = pipelines
        self._blast_analyzer = blast_radius_analyzer

    async def submit(self, signal: IntentSignal) -> Any:
        """Route signal to the correct repo's pipeline.

        1. Look up the pipeline for signal.repo
        2. Run blast radius analysis (if analyzer available)
        3. Build OperationContext
        4. Submit to the pipeline
        """
        repo_name = signal.repo
        if repo_name not in self._pipelines:
            raise KeyError(
                f"{repo_name}: no pipeline registered for this repo"
            )

        pipeline = self._pipelines[repo_name]

        # Blast radius analysis — failure escalates to approval_required
        blast_report = None
        if self._blast_analyzer is not None:
            try:
                blast_report = await self._blast_analyzer.analyze(signal)
            except Exception:
                logger.warning(
                    "Blast radius analysis failed for %s, escalating to approval_required",
                    signal.description,
                )
                from .blast_radius import BlastRadiusReport
                blast_report = BlastRadiusReport(
                    target_repo=repo_name,
                    target_files=signal.target_files,
                    affected_repos=(repo_name,),
                    affected_files=(),
                    crosses_repo_boundary=True,
                    risk_escalation="approval_required",
                    contract_impact=None,
                )

        # Build operation context
        op_id = generate_operation_id(signal.repo)
        ctx = OperationContext.create(
            target_files=signal.target_files,
            description=signal.description,
            op_id=op_id,
            primary_repo=repo_name,
            repo_scope=(repo_name,),
        )

        # Submit to the repo's pipeline
        result = await pipeline.submit(
            ctx,
            trigger_source=signal.source,
        )

        if blast_report and blast_report.crosses_repo_boundary:
            logger.info(
                "Cross-repo impact detected for op %s: affects %s",
                op_id,
                blast_report.affected_repos,
            )

        return result

    async def start_all(self) -> None:
        """Start all registered pipelines."""
        for name, pipeline in self._pipelines.items():
            try:
                await pipeline.start()
                logger.info("Pipeline started: %s", name)
            except Exception:
                logger.exception("Failed to start pipeline: %s", name)

    async def stop_all(self) -> None:
        """Stop all registered pipelines."""
        for name, pipeline in self._pipelines.items():
            try:
                await pipeline.stop()
                logger.info("Pipeline stopped: %s", name)
            except Exception:
                logger.exception("Failed to stop pipeline: %s", name)
