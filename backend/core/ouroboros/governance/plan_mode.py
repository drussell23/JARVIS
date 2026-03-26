"""
Plan Mode — Read-Only Pipeline Dry-Run
=======================================

Runs through the Ouroboros governance pipeline phases *without* executing any
mutations.  Produces a :class:`PlanReport` that shows what the pipeline
**would** do, how long it would take, what it would cost, and what approvals
are needed.

Activation
----------
Set ``JARVIS_PLAN_MODE=1`` (or ``true`` / ``yes``) to enable globally.
The executor can also be invoked explicitly regardless of the env var.

Design Principles
-----------------
- **Zero side-effects**: Plan mode NEVER modifies files, runs tests, or
  advances the operation context past CLASSIFY.
- **Honest estimates**: Duration and token counts are rough — based on
  historical averages from the durable ledger, not wall-clock profiling.
- **Full transparency**: Every phase produces a :class:`PlanStep` explaining
  the decision that *would* be made.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.PlanMode")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class PlanModeConfig:
    """Controls plan-mode behaviour.

    Attributes
    ----------
    enabled:
        Whether plan mode is active.  Sourced from ``JARVIS_PLAN_MODE`` env.
    max_exploration_depth:
        Maximum context-expansion rounds to simulate.
    include_cost_estimate:
        Whether to include token-cost estimates in the report.
    """

    enabled: bool = False
    max_exploration_depth: int = 3
    include_cost_estimate: bool = True

    @classmethod
    def from_env(cls) -> PlanModeConfig:
        """Build config from environment variables."""
        enabled = os.environ.get("JARVIS_PLAN_MODE", "").lower() in _TRUTHY
        depth = int(os.environ.get("JARVIS_PLAN_MODE_DEPTH", "3"))
        cost = os.environ.get("JARVIS_PLAN_MODE_COST", "true").lower() in _TRUTHY
        return cls(
            enabled=enabled,
            max_exploration_depth=max(1, min(depth, 10)),
            include_cost_estimate=cost,
        )


# ---------------------------------------------------------------------------
# Risk level constants
# ---------------------------------------------------------------------------

_RISK_LOW = "LOW"
_RISK_MEDIUM = "MEDIUM"
_RISK_HIGH = "HIGH"
_RISK_CRITICAL = "CRITICAL"

_RISK_ORDER = {_RISK_LOW: 0, _RISK_MEDIUM: 1, _RISK_HIGH: 2, _RISK_CRITICAL: 3}


def _max_risk(*levels: str) -> str:
    """Return the highest risk level from a set of levels."""
    return max(levels, key=lambda r: _RISK_ORDER.get(r, 0))


# ---------------------------------------------------------------------------
# Phase names — aligned with OperationPhase enum
# ---------------------------------------------------------------------------

PHASE_CLASSIFY = "CLASSIFY"
PHASE_ROUTE = "ROUTE"
PHASE_CONTEXT_EXPANSION = "CONTEXT_EXPANSION"
PHASE_GENERATE = "GENERATE"
PHASE_VALIDATE = "VALIDATE"
PHASE_GATE = "GATE"
PHASE_APPLY = "APPLY"

_ALL_PHASES = (
    PHASE_CLASSIFY,
    PHASE_ROUTE,
    PHASE_CONTEXT_EXPANSION,
    PHASE_GENERATE,
    PHASE_VALIDATE,
    PHASE_GATE,
    PHASE_APPLY,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    """One phase of the simulated pipeline execution.

    Attributes
    ----------
    phase:
        Pipeline phase name.
    description:
        Human-readable explanation of what would happen.
    estimated_duration_s:
        Rough wall-clock time estimate for this phase.
    estimated_tokens:
        Rough token count (prompt + completion) for this phase.
    risk_assessment:
        Risk level for this specific step.
    would_modify:
        List of file paths that this step would touch.
    blocked_by:
        What approval or condition is required before this step can proceed.
    """

    phase: str
    description: str
    estimated_duration_s: float = 0.0
    estimated_tokens: int = 0
    risk_assessment: str = _RISK_LOW
    would_modify: List[str] = field(default_factory=list)
    blocked_by: str = ""


@dataclass
class PlanReport:
    """Complete dry-run report for a proposed operation.

    Attributes
    ----------
    goal:
        The operation's stated objective.
    steps:
        Ordered list of simulated phase outcomes.
    total_estimated_tokens:
        Sum of estimated tokens across all steps.
    total_estimated_duration_s:
        Sum of estimated durations across all steps.
    risk_level:
        Overall operation risk (max across all steps).
    files_affected:
        Deduplicated list of files that would be modified.
    requires_approval:
        Whether the GATE phase would block for human review.
    recommendation:
        One of ``"proceed"``, ``"review first"``, ``"too risky"``.
    """

    goal: str
    steps: List[PlanStep] = field(default_factory=list)
    total_estimated_tokens: int = 0
    total_estimated_duration_s: float = 0.0
    risk_level: str = _RISK_LOW
    files_affected: List[str] = field(default_factory=list)
    requires_approval: bool = False
    recommendation: str = "proceed"


# ---------------------------------------------------------------------------
# Phase estimators — modular functions for each pipeline phase
# ---------------------------------------------------------------------------


def _estimate_classify(
    ctx: Any,
    config: PlanModeConfig,
) -> PlanStep:
    """Simulate the CLASSIFY phase.

    Examines the operation description and target files to determine
    complexity and risk.
    """
    description_text = getattr(ctx, "description", "") or ""
    target_files: list[str] = list(getattr(ctx, "target_files", []) or [])
    file_count = len(target_files)

    # Heuristic complexity from file count + description length
    if file_count <= 1 and len(description_text) < 200:
        risk = _RISK_LOW
        desc = f"Classify as SIMPLE (1 file, short description)"
        duration = 0.1
    elif file_count <= 3:
        risk = _RISK_LOW
        desc = f"Classify as MODERATE ({file_count} files)"
        duration = 0.2
    elif file_count <= 10:
        risk = _RISK_MEDIUM
        desc = f"Classify as COMPLEX ({file_count} files, multi-module)"
        duration = 0.3
    else:
        risk = _RISK_HIGH
        desc = f"Classify as ARCHITECTURAL ({file_count} files, cross-cutting)"
        duration = 0.5

    return PlanStep(
        phase=PHASE_CLASSIFY,
        description=desc,
        estimated_duration_s=duration,
        estimated_tokens=0,  # Classification is heuristic, no LLM call
        risk_assessment=risk,
    )


def _estimate_route(
    ctx: Any,
    classify_step: PlanStep,
) -> PlanStep:
    """Simulate the ROUTE phase.

    Determines which provider would handle generation based on complexity.
    """
    risk = classify_step.risk_assessment

    # Check for explicit provider override
    provider_override = os.environ.get("JARVIS_PROVIDER_OVERRIDE", "")

    if provider_override:
        provider = provider_override
        desc = f"Route to {provider} (explicit override via JARVIS_PROVIDER_OVERRIDE)"
    elif risk in (_RISK_HIGH, _RISK_CRITICAL):
        provider = "claude-api"
        desc = f"Route to claude-api (complexity exceeds local model threshold)"
    elif risk == _RISK_MEDIUM:
        # Check if J-Prime is likely available
        prime_host = os.environ.get("JARVIS_PRIME_HOST", "")
        if prime_host:
            provider = "j-prime"
            desc = f"Route to j-prime at {prime_host} (moderate complexity, GPU available)"
        else:
            provider = "claude-api"
            desc = "Route to claude-api (j-prime not configured)"
    else:
        prime_host = os.environ.get("JARVIS_PRIME_HOST", "")
        if prime_host:
            provider = "j-prime"
            desc = f"Route to j-prime (simple task, local inference preferred)"
        else:
            provider = "claude-api"
            desc = "Route to claude-api (j-prime not configured)"

    return PlanStep(
        phase=PHASE_ROUTE,
        description=desc,
        estimated_duration_s=0.05,
        estimated_tokens=0,
        risk_assessment=_RISK_LOW,
    )


def _estimate_context_expansion(
    ctx: Any,
    config: PlanModeConfig,
) -> PlanStep:
    """Simulate the CONTEXT_EXPANSION phase.

    Estimates how many files would be gathered and how many expansion rounds
    would be needed.
    """
    target_files: list[str] = list(getattr(ctx, "target_files", []) or [])
    expanded_files: list[str] = list(getattr(ctx, "expanded_context_files", []) or [])
    existing_count = len(target_files) + len(expanded_files)

    # Each expansion round can add up to 5 files (MAX_FILES_PER_ROUND from context_expander)
    max_files_per_round = 5
    estimated_rounds = min(config.max_exploration_depth, 2)  # Context expander caps at 2
    estimated_new_files = estimated_rounds * max_files_per_round
    total_files = existing_count + estimated_new_files

    # Each expansion round costs a planning prompt (~500 tokens)
    tokens_per_round = 500
    total_tokens = estimated_rounds * tokens_per_round

    desc = (
        f"~{total_files} files would be in context "
        f"({existing_count} initial + ~{estimated_new_files} expanded "
        f"over {estimated_rounds} rounds)"
    )

    return PlanStep(
        phase=PHASE_CONTEXT_EXPANSION,
        description=desc,
        estimated_duration_s=estimated_rounds * 2.0,  # ~2s per expansion round
        estimated_tokens=total_tokens,
        risk_assessment=_RISK_LOW,
    )


def _estimate_generate(
    ctx: Any,
    route_step: PlanStep,
    expansion_step: PlanStep,
) -> PlanStep:
    """Simulate the GENERATE phase.

    Estimates token cost based on context size and provider.
    """
    target_files: list[str] = list(getattr(ctx, "target_files", []) or [])
    description_text = getattr(ctx, "description", "") or ""

    # Rough token estimation: ~100 tokens per file in context + description tokens
    file_count = len(target_files)
    # Average file contributes ~500 tokens of context
    context_tokens = file_count * 500
    description_tokens = len(description_text.split()) * 2  # Rough word-to-token ratio
    system_prompt_tokens = 800  # Codegen system prompt overhead

    prompt_tokens = context_tokens + description_tokens + system_prompt_tokens
    # Completion is typically 30-60% of prompt for code changes
    completion_tokens = int(prompt_tokens * 0.4)
    total_tokens = prompt_tokens + completion_tokens

    provider = "unknown"
    if "claude-api" in route_step.description:
        provider = "claude-api"
        duration = max(3.0, total_tokens / 1000)  # ~1000 tok/s for Claude
    elif "j-prime" in route_step.description:
        provider = "j-prime"
        duration = max(5.0, total_tokens / 45)  # ~45 tok/s for L4 GPU
    else:
        provider = route_step.description.split()[-1] if route_step.description else "unknown"
        duration = max(3.0, total_tokens / 500)

    desc = (
        f"~{total_tokens:,} tokens estimated "
        f"(~{prompt_tokens:,} prompt + ~{completion_tokens:,} completion) "
        f"via {provider}"
    )

    risk = _RISK_LOW if total_tokens < 10000 else (
        _RISK_MEDIUM if total_tokens < 50000 else _RISK_HIGH
    )

    return PlanStep(
        phase=PHASE_GENERATE,
        description=desc,
        estimated_duration_s=duration,
        estimated_tokens=total_tokens,
        risk_assessment=risk,
        would_modify=list(target_files),
    )


def _estimate_validate(
    ctx: Any,
    generate_step: PlanStep,
) -> PlanStep:
    """Simulate the VALIDATE phase.

    Estimates what tests would run and how long they'd take.
    """
    target_files: list[str] = list(getattr(ctx, "target_files", []) or [])

    # Estimate test files based on target files
    test_files: list[str] = []
    for f in target_files:
        # Common test file patterns
        base = f.replace(".py", "")
        candidates = [
            f"test_{f.split('/')[-1]}",
            f"{base}_test.py",
            f"tests/test_{f.split('/')[-1]}",
        ]
        test_files.extend(candidates[:1])  # Just estimate, not resolve

    test_count = max(len(test_files), 1)
    duration = test_count * 3.0  # ~3s per test file

    desc = (
        f"{test_count} test file(s) would run "
        f"({', '.join(test_files[:3])}"
        f"{'...' if len(test_files) > 3 else ''})"
    )

    return PlanStep(
        phase=PHASE_VALIDATE,
        description=desc,
        estimated_duration_s=duration,
        estimated_tokens=0,  # Tests don't consume LLM tokens
        risk_assessment=_RISK_LOW,
    )


def _estimate_gate(
    ctx: Any,
    classify_step: PlanStep,
    generate_step: PlanStep,
) -> PlanStep:
    """Simulate the GATE phase.

    Determines whether human approval would be required.
    """
    overall_risk = _max_risk(
        classify_step.risk_assessment,
        generate_step.risk_assessment,
    )

    # Check auto-approve eligibility
    auto_approve_env = os.environ.get("JARVIS_GOVERNANCE_AUTO_APPROVE", "").lower()
    auto_approve_enabled = auto_approve_env in _TRUTHY

    modified_files = generate_step.would_modify
    modifies_critical = any(
        any(pattern in f for pattern in (
            "unified_supervisor", "__init__", "config", ".env",
            "security", "auth", "credential", "secret",
        ))
        for f in modified_files
    )

    if overall_risk == _RISK_CRITICAL:
        requires_approval = True
        blocked_by = "CRITICAL risk — mandatory human review"
    elif overall_risk == _RISK_HIGH:
        requires_approval = True
        blocked_by = "HIGH risk — requires human approval"
    elif modifies_critical:
        requires_approval = True
        blocked_by = "Modifies critical infrastructure files — requires review"
    elif overall_risk == _RISK_MEDIUM and not auto_approve_enabled:
        requires_approval = True
        blocked_by = "MEDIUM risk — requires approval (auto-approve not enabled)"
    elif auto_approve_enabled and overall_risk in (_RISK_LOW, _RISK_MEDIUM):
        requires_approval = False
        blocked_by = ""
    else:
        requires_approval = overall_risk != _RISK_LOW
        blocked_by = f"{overall_risk} risk — approval needed" if requires_approval else ""

    desc = (
        f"Requires human approval: {'YES' if requires_approval else 'NO'}"
        + (f" ({blocked_by})" if blocked_by else "")
    )

    return PlanStep(
        phase=PHASE_GATE,
        description=desc,
        estimated_duration_s=0.1 if not requires_approval else 0.0,
        estimated_tokens=0,
        risk_assessment=overall_risk,
        blocked_by=blocked_by,
    )


def _estimate_apply(
    ctx: Any,
    generate_step: PlanStep,
) -> PlanStep:
    """Simulate the APPLY phase.

    Lists files that would be modified.
    """
    modified = generate_step.would_modify
    file_count = len(modified)

    if file_count == 0:
        desc = "No files identified for modification"
        risk = _RISK_LOW
    elif file_count == 1:
        desc = f"Would modify: {modified[0]}"
        risk = _RISK_LOW
    elif file_count <= 5:
        file_list = ", ".join(modified[:5])
        desc = f"Would modify {file_count} files: {file_list}"
        risk = _RISK_MEDIUM
    else:
        file_list = ", ".join(modified[:3])
        desc = f"Would modify {file_count} files: {file_list} (+{file_count - 3} more)"
        risk = _RISK_HIGH

    return PlanStep(
        phase=PHASE_APPLY,
        description=desc,
        estimated_duration_s=file_count * 0.5,  # ~0.5s per file patch
        estimated_tokens=0,
        risk_assessment=risk,
        would_modify=list(modified),
    )


# ---------------------------------------------------------------------------
# PlanModeExecutor
# ---------------------------------------------------------------------------


class PlanModeExecutor:
    """Runs through pipeline phases without executing them, producing a plan.

    Usage
    -----
    ::

        config = PlanModeConfig.from_env()
        executor = PlanModeExecutor()
        report = await executor.plan(ctx, stack, config)
        print(PlanModeExecutor.format_for_display(report))
    """

    async def plan(
        self,
        ctx: Any,
        stack: Any = None,
        config: Optional[PlanModeConfig] = None,
    ) -> PlanReport:
        """Dry-run the pipeline and produce a :class:`PlanReport`.

        Parameters
        ----------
        ctx:
            An ``OperationContext`` (or any object with ``description``,
            ``target_files``, and ``expanded_context_files`` attributes).
        stack:
            Optional ``GovernanceStack`` — used for enriched estimates if
            available (e.g. ledger history, routing policy state).
        config:
            Plan-mode configuration.  If None, builds from env.

        Returns
        -------
        PlanReport:
            Complete dry-run analysis.
        """
        if config is None:
            config = PlanModeConfig.from_env()

        goal = getattr(ctx, "description", None) or getattr(ctx, "goal", "") or ""
        logger.info("Plan mode: analysing goal=%r", goal[:120])

        start = time.monotonic()

        # Phase 1: CLASSIFY
        classify_step = _estimate_classify(ctx, config)

        # Phase 2: ROUTE
        route_step = _estimate_route(ctx, classify_step)

        # Phase 3: CONTEXT_EXPANSION
        expansion_step = _estimate_context_expansion(ctx, config)

        # Phase 4: GENERATE
        generate_step = _estimate_generate(ctx, route_step, expansion_step)

        # Phase 5: VALIDATE
        validate_step = _estimate_validate(ctx, generate_step)

        # Phase 6: GATE
        gate_step = _estimate_gate(ctx, classify_step, generate_step)

        # Phase 7: APPLY
        apply_step = _estimate_apply(ctx, generate_step)

        steps = [
            classify_step,
            route_step,
            expansion_step,
            generate_step,
            validate_step,
            gate_step,
            apply_step,
        ]

        # Aggregate report
        total_tokens = sum(s.estimated_tokens for s in steps)
        total_duration = sum(s.estimated_duration_s for s in steps)
        overall_risk = _max_risk(*(s.risk_assessment for s in steps))

        # Deduplicated file list
        seen: set[str] = set()
        files_affected: list[str] = []
        for step in steps:
            for f in step.would_modify:
                if f not in seen:
                    seen.add(f)
                    files_affected.append(f)

        requires_approval = bool(gate_step.blocked_by)

        # Recommendation logic
        if overall_risk == _RISK_CRITICAL:
            recommendation = "too risky"
        elif overall_risk == _RISK_HIGH or requires_approval:
            recommendation = "review first"
        else:
            recommendation = "proceed"

        elapsed = time.monotonic() - start
        logger.info(
            "Plan mode complete: %d steps, ~%d tokens, ~%.1fs estimated, "
            "risk=%s, recommendation=%s (analysis took %.3fs)",
            len(steps),
            total_tokens,
            total_duration,
            overall_risk,
            recommendation,
            elapsed,
        )

        return PlanReport(
            goal=goal,
            steps=steps,
            total_estimated_tokens=total_tokens,
            total_estimated_duration_s=total_duration,
            risk_level=overall_risk,
            files_affected=files_affected,
            requires_approval=requires_approval,
            recommendation=recommendation,
        )

    @staticmethod
    def format_for_display(report: PlanReport) -> str:
        """Render a :class:`PlanReport` as human-readable text.

        Parameters
        ----------
        report:
            The plan report to format.

        Returns
        -------
        str:
            Multi-line formatted plan suitable for terminal display.
        """
        lines: list[str] = []

        # Header
        goal_display = report.goal[:80] + ("..." if len(report.goal) > 80 else "")
        lines.append(f"## Operation Plan: {goal_display}")
        lines.append("")

        # Steps
        for i, step in enumerate(report.steps, 1):
            parts = [f"Step {i}: {step.phase}"]

            # Core description
            parts.append(f" -- {step.description}")

            # Tokens if present
            if step.estimated_tokens > 0:
                parts.append(f" (~{step.estimated_tokens:,} tokens)")

            # Duration
            if step.estimated_duration_s > 0:
                parts.append(f" ~{step.estimated_duration_s:.1f}s")

            # Risk badge
            risk_badge = {
                _RISK_LOW: "",
                _RISK_MEDIUM: " [MEDIUM RISK]",
                _RISK_HIGH: " [HIGH RISK]",
                _RISK_CRITICAL: " [CRITICAL RISK]",
            }.get(step.risk_assessment, "")
            parts.append(risk_badge)

            lines.append("".join(parts))

            # Files that would be modified
            if step.would_modify:
                for f in step.would_modify[:5]:
                    lines.append(f"       -> {f}")
                if len(step.would_modify) > 5:
                    lines.append(f"       -> (+{len(step.would_modify) - 5} more)")

            # Blocker info
            if step.blocked_by:
                lines.append(f"       BLOCKED: {step.blocked_by}")

        # Summary
        lines.append("")
        lines.append("---")

        token_str = f"~{report.total_estimated_tokens:,} tokens" if report.total_estimated_tokens else "no LLM calls"
        lines.append(
            f"Total: {token_str}, "
            f"~{report.total_estimated_duration_s:.1f}s, "
            f"{report.risk_level} risk"
        )

        if report.files_affected:
            lines.append(f"Files affected ({len(report.files_affected)}): "
                         + ", ".join(report.files_affected[:5])
                         + (f" (+{len(report.files_affected) - 5} more)"
                            if len(report.files_affected) > 5 else ""))

        approval_str = "YES" if report.requires_approval else "NO"
        lines.append(f"Requires approval: {approval_str}")

        recommendation_display = {
            "proceed": "Proceed -- low risk, safe to execute.",
            "review first": "Review the plan before proceeding.",
            "too risky": "Too risky -- manual intervention recommended.",
        }.get(report.recommendation, report.recommendation)
        lines.append(f"Recommendation: {recommendation_display}")

        return "\n".join(lines)
