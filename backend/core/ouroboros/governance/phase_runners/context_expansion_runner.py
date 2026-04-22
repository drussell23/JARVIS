"""ContextExpansionRunner — Slice 3 of Wave 2 item (5).

Extracts orchestrator.py lines ~2143-2254 (CONTEXT_EXPANSION body +
advance to PLAN) into a :class:`PhaseRunner` behind
``JARVIS_PHASE_RUNNER_CONTEXT_EXPANSION_EXTRACTED`` (default ``false``).

## What CONTEXT_EXPANSION does

* Build expansion deadline
* Construct optional helpers: SkillRegistry / DocFetcher / WebSearchCapability /
  VisualCodeComprehension / CodeExplorationTool
* Run ContextExpander.expand(ctx, deadline) via asyncio.wait_for
* Run ExplorationFleet (optional parallel codebase exploration)
* Inject Oracle dependency summary (P2.1)
* Broad try/except wraps the whole body — expansion failure is a WARNING,
  not a terminal. Pipeline continues to GENERATE via PLAN.
* Final unconditional ``ctx.advance(PLAN)``

## Single success path

``next_phase = PLAN`` — always, even on expansion failure.

## Dependencies injected via constructor

* ``orchestrator`` — reads:
    - ``_config.{project_root, context_expansion_timeout_s}``
    - ``_generator``
    - ``_stack.oracle`` (optional)
    - ``_dialogue_store`` (optional)
    - ``_exploration_fleet`` (optional)
    - ``_build_dependency_summary``
* ``serpent`` — not used in this runner (PreActionNarrator +
  serpent.update_phase already fired in ROUTERunner pre-advance)

## Authority invariant

Imports: ``op_context``, ``phase_runner``, plus function-local imports
matching the inline block (``skill_registry`` / ``doc_fetcher`` /
``web_search`` / ``visual_comprehension`` / ``code_exploration``).
No execution-authority widening.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)

if TYPE_CHECKING:  # pragma: no cover
    from backend.core.ouroboros.governance.orchestrator import Orchestrator


logger = logging.getLogger("Ouroboros.Orchestrator")


class ContextExpansionRunner(PhaseRunner):
    """Verbatim transcription of orchestrator.py CONTEXT_EXPANSION block (~2143-2254)."""

    phase = OperationPhase.CONTEXT_EXPANSION

    def __init__(
        self,
        orchestrator: "Orchestrator",
        serpent: Optional[Any],
    ) -> None:
        self._orchestrator = orchestrator
        self._serpent = serpent

    async def run(self, ctx: OperationContext) -> PhaseResult:
        orch = self._orchestrator

        # ---- VERBATIM transcription of orchestrator.py 2143-2254 ----
        # ---- Phase 2b: CONTEXT_EXPANSION ----
        try:
            expansion_deadline = datetime.now(tz=timezone.utc) + timedelta(
                seconds=orch._config.context_expansion_timeout_s
            )
            from backend.core.ouroboros.governance.skill_registry import SkillRegistry as _SkillRegistry
            _skill_registry = _SkillRegistry(orch._config.project_root)
            # DocFetcher: bounded external doc retrieval (P3 — Boundary Principle)
            _doc_fetcher = None
            try:
                from backend.core.ouroboros.governance.doc_fetcher import DocFetcher
                _doc_fetcher = DocFetcher()
            except ImportError:
                pass

            # WebSearchCapability: structured search with epistemic allowlist
            _web_search = None
            try:
                from backend.core.ouroboros.governance.web_search import WebSearchCapability
                _ws = WebSearchCapability()
                if _ws.is_available:
                    _web_search = _ws
                    logger.debug(
                        "[Orchestrator] WebSearchCapability available (backend=%s)",
                        _ws.backend_name,
                    )
            except ImportError:
                pass

            # VisualCodeComprehension: screenshot-based analysis
            _visual = None
            try:
                from backend.core.ouroboros.governance.visual_comprehension import (
                    VisualCodeComprehension,
                )
                _vc = VisualCodeComprehension()
                if _vc.is_available:
                    _visual = _vc
            except ImportError:
                pass

            # CodeExplorationTool: sandboxed hypothesis testing
            _explorer = None
            try:
                from backend.core.ouroboros.governance.code_exploration import CodeExplorationTool
                _explorer = CodeExplorationTool(str(orch._config.project_root))
            except ImportError:
                pass

            # Resolve ContextExpander through the orchestrator module
            # namespace so test mocks (patch("orchestrator.ContextExpander"))
            # reach the runner path. Runtime-cheap (single getattr).
            from backend.core.ouroboros.governance import (
                orchestrator as _orch_mod,
            )
            expander = _orch_mod.ContextExpander(
                generator=orch._generator,
                repo_root=orch._config.project_root,
                oracle=getattr(orch._stack, "oracle", None),
                skill_registry=_skill_registry,
                doc_fetcher=_doc_fetcher,
                web_search=_web_search,
                visual_comprehension=_visual,
                code_explorer=_explorer,
                dialogue_store=orch._dialogue_store,
            )
            ctx = await asyncio.wait_for(
                expander.expand(ctx, expansion_deadline),
                timeout=orch._config.context_expansion_timeout_s,
            )

            # ExplorationFleet: parallel codebase exploration across Trinity repos
            if orch._exploration_fleet is not None:
                try:
                    _fleet_report = await asyncio.wait_for(
                        orch._exploration_fleet.deploy(
                            goal=ctx.description,
                            max_agents=8,
                        ),
                        timeout=min(30.0, orch._config.context_expansion_timeout_s / 2),
                    )
                    if _fleet_report.total_findings > 0:
                        _fleet_text = orch._exploration_fleet.format_for_prompt(_fleet_report)
                        ctx = ctx.with_expanded_files(
                            ctx.expanded_files + (f"[Fleet:{_fleet_report.total_findings}]",)
                        )
                        logger.info(
                            "[Orchestrator] ExplorationFleet: %d agents, %d findings in %.1fs",
                            _fleet_report.agents_completed,
                            _fleet_report.total_findings,
                            _fleet_report.duration_s,
                        )
                except Exception as _fleet_exc:
                    logger.debug("[Orchestrator] ExplorationFleet skipped: %s", _fleet_exc)

            # P2.1: Dependency-aware generation — inject Oracle graph summary
            _oracle_ref = getattr(orch._stack, "oracle", None)
            if _oracle_ref is not None and ctx.target_files:
                try:
                    _dep_summary = orch._build_dependency_summary(
                        _oracle_ref, ctx.target_files,
                    )
                    if _dep_summary:
                        ctx = dataclasses.replace(ctx, dependency_summary=_dep_summary)
                        logger.info(
                            "[Orchestrator] Dependency summary injected (%d chars, %d files)",
                            len(_dep_summary), len(ctx.target_files),
                        )
                except Exception as _dep_exc:
                    logger.debug("[Orchestrator] Dependency summary skipped: %s", _dep_exc)
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Context expansion failed for op=%s: %s; "
                "continuing to GENERATE",
                ctx.op_id, exc,
            )

        ctx = ctx.advance(OperationPhase.PLAN)
        # ---- end verbatim transcription ----

        return PhaseResult(
            next_ctx=ctx,
            next_phase=OperationPhase.PLAN,
            status="ok",
            reason="expanded",
        )


__all__ = ["ContextExpansionRunner"]
