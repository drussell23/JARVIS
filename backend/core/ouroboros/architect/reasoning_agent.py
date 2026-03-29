"""
ArchitectureReasoningAgent
===========================

Entry point for the Ouroboros architect subsystem.  Determines which
hypotheses warrant architectural design and (v1) logs qualifying hypotheses
while deferring plan generation until the model bridge is complete.

Design principles:
- Threshold filtering is the sole gate in v1.  The agent accepts any object
  exposing ``.gap_type`` and ``.confidence`` attributes (duck typing) so tests
  and downstream callers can pass real ``FeatureHypothesis`` instances or
  lightweight mocks interchangeably.
- No hardcoded model URLs, provider details, or pipeline steps.  Configuration
  flows through ``AgentConfig``; model-specific behaviour lives in the bridge
  layer (not yet built).
- ``design()`` is already ``async`` so the signature is stable.  When the
  model bridge is wired, the body is the only thing that changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.core.ouroboros.architect.design_prompt import (
    ARCHITECTURAL_PLAN_JSON_SCHEMA,
    build_design_prompt,
)
from backend.core.ouroboros.architect.plan import (
    AcceptanceCheck,
    ArchitecturalPlan,
    CheckKind,
    PlanStep,
    StepIntentKind,
)
from backend.core.ouroboros.architect.plan_validator import PlanValidator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ARCHITECTURAL_GAP_TYPES: frozenset = frozenset({
    "missing_capability",
    "manifesto_violation",
})

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """Tuneable knobs for :class:`ArchitectureReasoningAgent`.

    Parameters
    ----------
    min_confidence:
        Minimum hypothesis confidence required to proceed to design.
        Hypotheses below this threshold are filtered out immediately.
    model:
        Primary model identifier used when the model bridge is active.
    fallback_model:
        Model identifier used when the primary model is unavailable.
    max_steps:
        Upper bound on the number of ``PlanStep`` objects the agent may
        produce per plan (enforced by the model bridge, advisory in v1).
    """

    min_confidence: float = 0.7
    model: str = "doubleword-397b"
    fallback_model: str = "claude-api"
    max_steps: int = 10


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ArchitectureReasoningAgent:
    """Decides which hypotheses qualify for architectural design and (v1)
    logs them while deferring actual plan generation.

    Parameters
    ----------
    oracle:
        The Oracle instance used for codebase context lookup.  Injected so the
        agent can request file neighbourhood data when generating plans.
    doubleword:
        The Doubleword provider client.  Injected to support model-driven plan
        generation when the bridge is complete.
    config:
        Agent configuration.  Defaults to :class:`AgentConfig` defaults.
    """

    def __init__(
        self,
        oracle: Any,
        doubleword: Any,
        config: AgentConfig = AgentConfig(),  # noqa: B008
    ) -> None:
        self._oracle = oracle
        self._doubleword = doubleword
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_design(self, hypothesis: Any) -> bool:
        """Return ``True`` if *hypothesis* clears the architectural threshold.

        Two conditions must both hold:

        1. ``hypothesis.gap_type`` is one of the recognised architectural gap
           types (``missing_capability`` or ``manifesto_violation``).
        2. ``hypothesis.confidence`` is at or above ``config.min_confidence``.

        Deliberately duck-typed: accepts any object with ``.gap_type`` and
        ``.confidence`` attributes.
        """
        if hypothesis.gap_type not in _ARCHITECTURAL_GAP_TYPES:
            logger.debug(
                "should_design: filtering hypothesis — gap_type %r not architectural",
                hypothesis.gap_type,
            )
            return False

        if hypothesis.confidence < self._config.min_confidence:
            logger.debug(
                "should_design: filtering hypothesis — confidence %.3f < min %.3f",
                hypothesis.confidence,
                self._config.min_confidence,
            )
            return False

        return True

    async def design(
        self,
        hypothesis: Any,
        snapshot: Any,
        oracle: Any,
    ) -> Optional[ArchitecturalPlan]:
        """Attempt to produce an :class:`ArchitecturalPlan` for *hypothesis*.

        v1 behaviour
        ------------
        - If the hypothesis does not pass :meth:`should_design`, return ``None``
          immediately.
        - If the hypothesis qualifies, log that it is ready for model-based
          design and return ``None``.  Plan generation is deferred until the
          OperationContext / model bridge is wired (Task 9+).

        The ``snapshot`` and ``oracle`` parameters are accepted now so that the
        method signature is stable and callers do not need updating when the
        bridge lands.
        """
        if not self.should_design(hypothesis):
            return None

        logger.info(
            "design: hypothesis %r qualifies (gap_type=%r, confidence=%.3f) — "
            "attempting model-based plan generation",
            getattr(hypothesis, "hypothesis_id", repr(hypothesis)),
            hypothesis.gap_type,
            hypothesis.confidence,
        )
        # v2: attempt model-based plan generation
        if self._doubleword is not None and hasattr(self._doubleword, "prompt_only"):
            try:
                return await self._generate_plan(hypothesis, snapshot, oracle)
            except Exception as exc:
                logger.warning("ArchitectureReasoningAgent: plan generation failed: %s", exc)
                return None
        return None  # no doubleword available

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _generate_plan(
        self,
        hypothesis: Any,
        snapshot: Any,
        oracle: Any,
    ) -> Optional[ArchitecturalPlan]:
        """Invoke Doubleword 397B to generate and validate an ArchitecturalPlan.

        Steps
        -----
        1. Build oracle context string (best-effort; empty string on failure).
        2. Build the design prompt via :func:`build_design_prompt`.
        3. Call ``self._doubleword.prompt_only()`` with the JSON schema as the
           ``response_format`` hint.
        4. Parse the returned JSON into typed plan objects.
        5. Validate the plan via :class:`PlanValidator`.
        6. Return the validated plan, or ``None`` if any step fails.

        All exceptions propagate to the caller (``design``) which logs and
        returns ``None``.
        """
        # --- 1. Gather oracle context ---
        oracle_context = ""
        try:
            if oracle is not None and hasattr(oracle, "get_file_neighbourhood"):
                neighbourhood = oracle.get_file_neighbourhood(
                    getattr(hypothesis, "suggested_repos", ()) or ()
                )
                if neighbourhood is not None:
                    oracle_context = str(neighbourhood)
        except Exception as oracle_exc:  # noqa: BLE001
            logger.debug(
                "ArchitectureReasoningAgent: oracle context retrieval failed: %s",
                oracle_exc,
            )

        # --- 2. Build prompt ---
        prompt = build_design_prompt(
            hypothesis=hypothesis,
            oracle_context=oracle_context,
            max_steps=self._config.max_steps,
        )

        # --- 3. Call Doubleword ---
        raw = await self._doubleword.prompt_only(
            prompt=prompt,
            caller_id="architecture_agent",
            response_format=ARCHITECTURAL_PLAN_JSON_SCHEMA,
        )

        if not raw:
            logger.warning(
                "ArchitectureReasoningAgent: empty response from Doubleword "
                "(hypothesis=%r)",
                getattr(hypothesis, "hypothesis_id", repr(hypothesis)),
            )
            return None

        # --- 4. Parse JSON ---
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "ArchitectureReasoningAgent: JSON parse error: %s (raw[:200]=%r)",
                exc,
                raw[:200],
            )
            return None

        # Require at minimum a "steps" key to avoid constructing a broken plan
        if "steps" not in data:
            logger.warning(
                "ArchitectureReasoningAgent: model response missing 'steps' key",
            )
            return None

        # --- 4a. Parse PlanStep objects ---
        raw_steps: list[dict] = data.get("steps", [])
        steps: list[PlanStep] = []
        for raw_step in raw_steps:
            try:
                intent_str = raw_step.get("intent_kind", "create_file")
                intent_kind = StepIntentKind(intent_str)
                step = PlanStep(
                    step_index=int(raw_step["step_index"]),
                    description=str(raw_step.get("description", "")),
                    intent_kind=intent_kind,
                    target_paths=tuple(raw_step.get("target_paths", [])),
                    repo=str(raw_step.get("repo", "")),
                    ancillary_paths=tuple(raw_step.get("ancillary_paths", [])),
                    interface_contracts=tuple(raw_step.get("interface_contracts", [])),
                    tests_required=tuple(raw_step.get("tests_required", [])),
                    risk_tier_hint=str(raw_step.get("risk_tier_hint", "safe_auto")),
                    depends_on=tuple(int(d) for d in raw_step.get("depends_on", [])),
                )
                steps.append(step)
            except (KeyError, ValueError, TypeError) as step_exc:
                logger.warning(
                    "ArchitectureReasoningAgent: failed to parse step %r: %s",
                    raw_step,
                    step_exc,
                )
                return None

        # --- 4b. Parse AcceptanceCheck objects ---
        raw_checks: list[dict] = data.get("acceptance_checks", [])
        checks: list[AcceptanceCheck] = []
        for raw_chk in raw_checks:
            try:
                check_kind_str = raw_chk.get("check_kind", "exit_code")
                check_kind = CheckKind(check_kind_str)
                check = AcceptanceCheck(
                    check_id=str(raw_chk["check_id"]),
                    check_kind=check_kind,
                    command=str(raw_chk.get("command", "")),
                    expected=str(raw_chk.get("expected", "")),
                    cwd=str(raw_chk.get("cwd", ".")),
                    timeout_s=float(raw_chk.get("timeout_s", 120.0)),
                    run_after_step=raw_chk.get("run_after_step"),
                    sandbox_required=bool(raw_chk.get("sandbox_required", True)),
                )
                checks.append(check)
            except (KeyError, ValueError, TypeError) as chk_exc:
                logger.warning(
                    "ArchitectureReasoningAgent: failed to parse acceptance check %r: %s",
                    raw_chk,
                    chk_exc,
                )
                return None

        # --- 4c. Derive hypothesis provenance fields ---
        parent_id = str(getattr(hypothesis, "hypothesis_id", ""))
        parent_fingerprint = str(getattr(hypothesis, "hypothesis_fingerprint", ""))
        snapshot_hash = str(
            getattr(hypothesis, "synthesized_for_snapshot_hash", "")
        )

        # --- 4d. Construct ArchitecturalPlan ---
        plan = ArchitecturalPlan.create(
            parent_hypothesis_id=parent_id,
            parent_hypothesis_fingerprint=parent_fingerprint,
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            repos_affected=tuple(data.get("repos_affected", [])),
            non_goals=tuple(data.get("non_goals", [])),
            steps=tuple(steps),
            acceptance_checks=tuple(checks),
            model_used=self._config.model,
            snapshot_hash=snapshot_hash,
        )

        # --- 5. Validate ---
        result = PlanValidator(max_steps=self._config.max_steps).validate(plan)
        if not result.passed:
            logger.warning(
                "ArchitectureReasoningAgent: plan validation failed "
                "(hypothesis=%r, reasons=%r)",
                parent_id,
                result.reasons,
            )
            return None

        logger.info(
            "ArchitectureReasoningAgent: plan %r generated and validated "
            "(steps=%d, hypothesis=%r)",
            plan.plan_id,
            len(plan.steps),
            parent_id,
        )
        return plan

    def health(self) -> dict:
        """Return a dict describing the agent's configuration and readiness.

        ``"model_integration"`` is ``"active"`` when a Doubleword client with
        ``prompt_only`` is injected, otherwise ``"pending"``.
        """
        integration_status = (
            "active"
            if self._doubleword is not None and hasattr(self._doubleword, "prompt_only")
            else "pending"
        )
        return {
            "min_confidence": self._config.min_confidence,
            "model": self._config.model,
            "fallback_model": self._config.fallback_model,
            "max_steps": self._config.max_steps,
            "architectural_gap_types": sorted(_ARCHITECTURAL_GAP_TYPES),
            "model_integration": integration_status,
        }
