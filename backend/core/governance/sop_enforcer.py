"""
SOP Enforcer - Clinical-Grade Architectural Discipline
=======================================================

Implements the "Measure Twice, Cut Once" philosophy by forcing JARVIS to
create a structured Design Plan before writing any code.

Inspired by MetaGPT's SOP patterns, this module provides:
- ThinkingProtocol: Enforces structured planning (Goal -> Context -> Changes -> Risks)
- SOPEnforcer: Middleware that intercepts coding tasks and validates design plans
- ComplexityAnalyzer: Determines if a task requires planning
- DesignPlanValidator: Validates plan quality before allowing execution

Integration:
    The SOPEnforcer is integrated into the AgenticTaskRunner's execution pipeline.
    When a coding task is detected, execution is blocked until a valid design plan
    is provided and approved.

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any, Callable, Dict, List, Literal, Optional, Protocol, Set, Tuple, Type, TypeVar, Union
)
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# =============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class SOPEnforcerConfig:
    """Configuration for the SOP Enforcer."""

    # Enforcement settings
    enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_ENFORCER_ENABLED", True)
    )
    strict_mode: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_STRICT_MODE", True)
    )

    # Complexity thresholds
    complexity_threshold: float = field(
        default_factory=lambda: _get_env_float("JARVIS_SOP_COMPLEXITY_THRESHOLD", 0.5)
    )
    min_files_for_plan: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_MIN_FILES_FOR_PLAN", 2)
    )

    # Plan validation
    require_goal: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_REQUIRE_GOAL", True)
    )
    require_context: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_REQUIRE_CONTEXT", True)
    )
    require_proposed_changes: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_REQUIRE_CHANGES", True)
    )
    require_risk_assessment: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_REQUIRE_RISKS", True)
    )
    require_test_plan: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_REQUIRE_TESTS", False)
    )

    # Quality thresholds
    min_goal_length: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_MIN_GOAL_LENGTH", 20)
    )
    min_context_length: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_MIN_CONTEXT_LENGTH", 50)
    )
    min_changes_count: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_MIN_CHANGES_COUNT", 1)
    )
    min_risks_count: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_MIN_RISKS_COUNT", 1)
    )

    # Plan caching
    cache_plans: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_CACHE_PLANS", True)
    )
    plan_cache_ttl_seconds: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_PLAN_CACHE_TTL", 3600)
    )

    # Cross-repo integration
    cross_repo_enabled: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_CROSS_REPO", True)
    )

    # Bypass settings
    bypass_keywords: List[str] = field(
        default_factory=lambda: _get_env("JARVIS_SOP_BYPASS_KEYWORDS", "hotfix,urgent,emergency").split(",")
    )


# =============================================================================
# Enums
# =============================================================================

class TaskComplexity(str, Enum):
    """Task complexity levels."""
    TRIVIAL = "trivial"       # Single file, simple change
    SIMPLE = "simple"         # 1-2 files, straightforward
    MODERATE = "moderate"     # 3-5 files, some dependencies
    COMPLEX = "complex"       # 6+ files, architectural impact
    CRITICAL = "critical"     # System-wide, breaking changes


class PlanStatus(str, Enum):
    """Status of a design plan."""
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EnforcementAction(str, Enum):
    """Actions taken by the enforcer."""
    ALLOW = "allow"           # Task can proceed
    REQUIRE_PLAN = "require_plan"  # Plan required
    BLOCK = "block"           # Blocked, plan insufficient
    BYPASS = "bypass"         # Bypassed (emergency)


# =============================================================================
# Pydantic Models for Design Plan
# =============================================================================

class ProposedChange(BaseModel):
    """A proposed code change."""
    file_path: str = Field(..., description="Path to the file being modified")
    change_type: Literal["create", "modify", "delete", "refactor"] = Field(
        ..., description="Type of change"
    )
    description: str = Field(..., description="What will be changed and why")
    estimated_lines: Optional[int] = Field(None, description="Estimated lines of code")
    dependencies: List[str] = Field(default_factory=list, description="Files this change depends on")


class RiskAssessment(BaseModel):
    """Assessment of a potential risk."""
    risk_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    category: Literal["security", "performance", "breaking_change", "data_loss", "regression", "other"] = Field(
        ..., description="Risk category"
    )
    description: str = Field(..., description="Description of the risk")
    severity: Literal["low", "medium", "high", "critical"] = Field(..., description="Risk severity")
    mitigation: str = Field(..., description="How this risk will be mitigated")
    probability: Literal["unlikely", "possible", "likely", "certain"] = Field(
        "possible", description="Likelihood of occurrence"
    )


class TestPlan(BaseModel):
    """Test plan for validating changes."""
    unit_tests: List[str] = Field(default_factory=list, description="Unit tests to add/modify")
    integration_tests: List[str] = Field(default_factory=list, description="Integration tests")
    manual_tests: List[str] = Field(default_factory=list, description="Manual testing steps")
    coverage_target: float = Field(0.8, description="Target code coverage (0-1)")


class DesignPlan(BaseModel):
    """
    Complete design plan for a coding task.

    This is the "Clinical-Grade" plan that JARVIS must create before writing code.
    It enforces the "Measure Twice, Cut Once" philosophy.
    """
    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=datetime.now)
    status: PlanStatus = Field(default=PlanStatus.DRAFT)

    # Core components
    goal: str = Field(
        ...,
        min_length=20,
        description="Clear statement of what this change accomplishes"
    )
    context: str = Field(
        ...,
        min_length=50,
        description="Background, requirements, and current state analysis"
    )
    proposed_changes: List[ProposedChange] = Field(
        ...,
        min_length=1,
        description="List of specific code changes to be made"
    )
    risk_assessment: List[RiskAssessment] = Field(
        ...,
        min_length=1,
        description="Potential risks and their mitigations"
    )

    # Optional components
    test_plan: Optional[TestPlan] = Field(None, description="Testing strategy")
    rollback_plan: Optional[str] = Field(None, description="How to rollback if needed")
    dependencies: List[str] = Field(default_factory=list, description="External dependencies")
    affected_repos: List[str] = Field(default_factory=list, description="Repos affected by changes")

    # Metadata
    author: str = Field(default="jarvis", description="Plan author")
    reviewer: Optional[str] = Field(None, description="Human reviewer if applicable")
    approval_timestamp: Optional[datetime] = Field(None)
    execution_notes: Optional[str] = Field(None)

    @model_validator(mode="after")
    def validate_plan_coherence(self) -> "DesignPlan":
        """Validate that the plan is coherent and complete."""
        # Check that proposed changes have at least one file
        if not self.proposed_changes:
            raise ValueError("At least one proposed change is required")

        # Check that risks are assessed
        if not self.risk_assessment:
            raise ValueError("At least one risk must be assessed")

        # Check for high/critical risks without mitigation
        for risk in self.risk_assessment:
            if risk.severity in ("high", "critical") and not risk.mitigation.strip():
                raise ValueError(f"High/Critical risk '{risk.description}' requires mitigation")

        return self


# =============================================================================
# Thinking Protocol
# =============================================================================

class ThinkingProtocol(ABC):
    """
    Abstract protocol for structured thinking before code execution.

    This enforces that JARVIS "thinks before acting" by requiring a structured
    thought process documented in a DesignPlan.
    """

    @abstractmethod
    async def generate_plan(
        self,
        task: str,
        context: Dict[str, Any],
        llm: Any,
    ) -> DesignPlan:
        """Generate a design plan for a task."""
        pass

    @abstractmethod
    async def validate_plan(
        self,
        plan: DesignPlan,
        config: SOPEnforcerConfig,
    ) -> Tuple[bool, List[str]]:
        """
        Validate a design plan.

        Returns:
            Tuple of (is_valid, validation_errors)
        """
        pass

    @abstractmethod
    async def approve_plan(
        self,
        plan: DesignPlan,
        approver: str = "auto",
    ) -> DesignPlan:
        """Approve a plan for execution."""
        pass


class JARVISThinkingProtocol(ThinkingProtocol):
    """
    JARVIS implementation of the ThinkingProtocol.

    Uses LLM to generate structured design plans and validates them
    against quality thresholds.
    """

    # Prompt template for plan generation
    PLAN_GENERATION_PROMPT = """You are JARVIS, a clinical-grade AI assistant. Before writing any code,
you MUST create a comprehensive Design Plan.

## Task
{task}

## Context
{context}

## Repository Structure
{repo_map}

## Instructions
Generate a complete Design Plan in JSON format with the following structure:

```json
{{
  "goal": "Clear statement of what this change accomplishes (20+ chars)",
  "context": "Background, requirements, and current state analysis (50+ chars)",
  "proposed_changes": [
    {{
      "file_path": "path/to/file.py",
      "change_type": "modify|create|delete|refactor",
      "description": "What will be changed and why",
      "estimated_lines": 50,
      "dependencies": ["other/file.py"]
    }}
  ],
  "risk_assessment": [
    {{
      "category": "security|performance|breaking_change|data_loss|regression|other",
      "description": "Description of the risk",
      "severity": "low|medium|high|critical",
      "mitigation": "How this risk will be mitigated",
      "probability": "unlikely|possible|likely|certain"
    }}
  ],
  "test_plan": {{
    "unit_tests": ["test_function.py"],
    "integration_tests": ["test_integration.py"],
    "manual_tests": ["Verify X works"],
    "coverage_target": 0.8
  }},
  "rollback_plan": "Steps to rollback if needed",
  "affected_repos": ["jarvis", "jarvis_prime", "reactor_core"]
}}
```

Think carefully about:
1. What files need to change
2. What could go wrong (security, performance, breaking changes)
3. How to test the changes
4. How to rollback if needed

Output ONLY the JSON, no other text.
"""

    def __init__(self, config: Optional[SOPEnforcerConfig] = None):
        self.config = config or SOPEnforcerConfig()
        self._plan_cache: Dict[str, Tuple[DesignPlan, float]] = {}

    def _get_cache_key(self, task: str, context: Dict[str, Any]) -> str:
        """Generate a cache key for a task."""
        data = f"{task}:{json.dumps(context, sort_keys=True, default=str)}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    async def generate_plan(
        self,
        task: str,
        context: Dict[str, Any],
        llm: Any,
    ) -> DesignPlan:
        """Generate a design plan using the LLM."""
        # Check cache
        cache_key = self._get_cache_key(task, context)
        if self.config.cache_plans and cache_key in self._plan_cache:
            plan, timestamp = self._plan_cache[cache_key]
            if time.time() - timestamp < self.config.plan_cache_ttl_seconds:
                logger.info(f"[ThinkingProtocol] Using cached plan: {plan.plan_id}")
                return plan

        # Build prompt
        repo_map = context.get("repo_map", "Not available")
        prompt = self.PLAN_GENERATION_PROMPT.format(
            task=task,
            context=json.dumps(context, indent=2, default=str),
            repo_map=repo_map,
        )

        # Query LLM
        try:
            response = await llm.aask(prompt, timeout=120)

            # Extract JSON from response
            plan_data = self._extract_json(response)

            # Create DesignPlan
            plan = DesignPlan(**plan_data)
            logger.info(f"[ThinkingProtocol] Generated plan: {plan.plan_id}")

            # Cache the plan
            if self.config.cache_plans:
                self._plan_cache[cache_key] = (plan, time.time())

            return plan

        except ValidationError as e:
            logger.error(f"[ThinkingProtocol] Plan validation failed: {e}")
            raise ValueError(f"Generated plan failed validation: {e}")
        except Exception as e:
            logger.error(f"[ThinkingProtocol] Plan generation failed: {e}")
            raise

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """Extract JSON from LLM response."""
        # Try to find JSON in code blocks
        json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))

        # Try direct JSON parse
        text = text.strip()
        if text.startswith("{"):
            return json.loads(text)

        raise ValueError("Could not extract JSON from response")

    async def validate_plan(
        self,
        plan: DesignPlan,
        config: Optional[SOPEnforcerConfig] = None,
    ) -> Tuple[bool, List[str]]:
        """Validate a design plan against quality thresholds."""
        config = config or self.config
        errors: List[str] = []

        # Goal validation
        if config.require_goal:
            if len(plan.goal) < config.min_goal_length:
                errors.append(f"Goal too short: {len(plan.goal)} < {config.min_goal_length} chars")

        # Context validation
        if config.require_context:
            if len(plan.context) < config.min_context_length:
                errors.append(f"Context too short: {len(plan.context)} < {config.min_context_length} chars")

        # Proposed changes validation
        if config.require_proposed_changes:
            if len(plan.proposed_changes) < config.min_changes_count:
                errors.append(f"Too few changes: {len(plan.proposed_changes)} < {config.min_changes_count}")

            # Validate each change has a description
            for i, change in enumerate(plan.proposed_changes):
                if not change.description.strip():
                    errors.append(f"Change {i+1} missing description")
                if not change.file_path.strip():
                    errors.append(f"Change {i+1} missing file_path")

        # Risk assessment validation
        if config.require_risk_assessment:
            if len(plan.risk_assessment) < config.min_risks_count:
                errors.append(f"Too few risks: {len(plan.risk_assessment)} < {config.min_risks_count}")

            # Check for unmitigated high risks
            for risk in plan.risk_assessment:
                if risk.severity in ("high", "critical") and not risk.mitigation.strip():
                    errors.append(f"Unmitigated {risk.severity} risk: {risk.description[:50]}")

        # Test plan validation
        if config.require_test_plan:
            if not plan.test_plan:
                errors.append("Test plan required but not provided")
            elif not any([
                plan.test_plan.unit_tests,
                plan.test_plan.integration_tests,
                plan.test_plan.manual_tests,
            ]):
                errors.append("Test plan has no tests defined")

        is_valid = len(errors) == 0
        return is_valid, errors

    async def approve_plan(
        self,
        plan: DesignPlan,
        approver: str = "auto",
    ) -> DesignPlan:
        """Approve a plan for execution."""
        plan.status = PlanStatus.APPROVED
        plan.reviewer = approver
        plan.approval_timestamp = datetime.now()
        logger.info(f"[ThinkingProtocol] Plan approved: {plan.plan_id} by {approver}")
        return plan


# =============================================================================
# Complexity Analyzer
# =============================================================================

class ComplexityAnalyzer:
    """
    Analyzes task complexity to determine if planning is required.

    Uses multiple signals:
    - Number of files mentioned
    - Keywords indicating complexity
    - Architectural scope
    - Risk indicators
    """

    # Complexity indicators (keyword -> weight)
    COMPLEXITY_KEYWORDS: Dict[str, float] = {
        # High complexity
        "refactor": 0.8, "architecture": 0.9, "redesign": 0.9, "migrate": 0.85,
        "breaking": 0.9, "database": 0.7, "schema": 0.8, "api": 0.6,
        "security": 0.8, "authentication": 0.7, "authorization": 0.7,
        "performance": 0.6, "optimize": 0.5, "scale": 0.7,

        # Medium complexity
        "integrate": 0.5, "connect": 0.4, "implement": 0.4, "feature": 0.4,
        "endpoint": 0.4, "handler": 0.3, "service": 0.4, "module": 0.4,

        # Lower complexity
        "fix": 0.3, "bug": 0.3, "update": 0.2, "change": 0.2,
        "add": 0.2, "remove": 0.2, "rename": 0.1, "typo": 0.1,
    }

    # Risk indicators
    RISK_KEYWORDS: Dict[str, float] = {
        "delete": 0.7, "drop": 0.8, "remove": 0.4, "breaking": 0.9,
        "production": 0.6, "deploy": 0.5, "migration": 0.7, "rollback": 0.5,
        "security": 0.8, "password": 0.9, "credential": 0.9, "secret": 0.9,
        "api_key": 0.9, "token": 0.7, "auth": 0.7, "permission": 0.6,
    }

    # File extension complexity (multi-file = more complex)
    FILE_PATTERNS = re.compile(
        r'[\w/-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|sql|yaml|json)',
        re.IGNORECASE
    )

    def __init__(self, config: Optional[SOPEnforcerConfig] = None):
        self.config = config or SOPEnforcerConfig()

    def analyze(self, task: str, context: Optional[Dict[str, Any]] = None) -> Tuple[TaskComplexity, float, Dict[str, Any]]:
        """
        Analyze task complexity.

        Returns:
            Tuple of (complexity_level, score, metadata)
        """
        task_lower = task.lower()
        context = context or {}
        metadata: Dict[str, Any] = {
            "keywords_found": [],
            "risk_keywords_found": [],
            "files_mentioned": [],
            "signals": [],
        }

        scores: List[float] = []

        # 1. Keyword complexity
        keyword_score = 0.0
        for keyword, weight in self.COMPLEXITY_KEYWORDS.items():
            if keyword in task_lower:
                keyword_score += weight
                metadata["keywords_found"].append(keyword)

        if keyword_score > 0:
            scores.append(min(keyword_score / 2.0, 1.0))
            metadata["signals"].append(f"Keywords: {keyword_score:.2f}")

        # 2. Risk indicators
        risk_score = 0.0
        for keyword, weight in self.RISK_KEYWORDS.items():
            if keyword in task_lower:
                risk_score += weight
                metadata["risk_keywords_found"].append(keyword)

        if risk_score > 0:
            scores.append(min(risk_score / 1.5, 1.0))
            metadata["signals"].append(f"Risk keywords: {risk_score:.2f}")

        # 3. File mentions
        files = self.FILE_PATTERNS.findall(task)
        metadata["files_mentioned"] = files
        if len(files) >= self.config.min_files_for_plan:
            file_score = min(len(files) / 5.0, 1.0)
            scores.append(file_score)
            metadata["signals"].append(f"Files: {len(files)}")

        # 4. Context complexity (if repo map shows many related files)
        if "mentioned_files" in context:
            related_files = context["mentioned_files"]
            if len(related_files) >= 3:
                scores.append(0.6)
                metadata["signals"].append(f"Related files: {len(related_files)}")

        # 5. Cross-repo indicator
        if any(repo in task_lower for repo in ["jarvis_prime", "reactor_core", "cross-repo"]):
            scores.append(0.8)
            metadata["signals"].append("Cross-repo scope")

        # Calculate final score
        if not scores:
            final_score = 0.1  # Default low complexity
        else:
            final_score = sum(scores) / len(scores)

        # Determine complexity level
        if final_score >= 0.8:
            complexity = TaskComplexity.CRITICAL
        elif final_score >= 0.6:
            complexity = TaskComplexity.COMPLEX
        elif final_score >= 0.4:
            complexity = TaskComplexity.MODERATE
        elif final_score >= 0.2:
            complexity = TaskComplexity.SIMPLE
        else:
            complexity = TaskComplexity.TRIVIAL

        return complexity, final_score, metadata


# =============================================================================
# SOP Enforcer
# =============================================================================

class SOPEnforcer:
    """
    The SOP Enforcer - Clinical-Grade Middleware for Coding Tasks.

    This middleware intercepts coding tasks and enforces the "Measure Twice, Cut Once"
    philosophy by requiring a validated Design Plan before allowing execution.

    Integration points:
    - AgenticTaskRunner.run() - Pre-execution check
    - JarvisPrimeClient.complete() - Can require planning for complex prompts
    - CrossRepoIntelligenceHub - Cross-system task validation
    """

    def __init__(
        self,
        config: Optional[SOPEnforcerConfig] = None,
        thinking_protocol: Optional[ThinkingProtocol] = None,
    ):
        self.config = config or SOPEnforcerConfig()
        self.thinking = thinking_protocol or JARVISThinkingProtocol(self.config)
        self.analyzer = ComplexityAnalyzer(self.config)

        # State
        self._pending_plans: Dict[str, DesignPlan] = {}
        self._approved_plans: Dict[str, DesignPlan] = {}
        self._blocked_tasks: Dict[str, str] = {}  # task_id -> reason
        self._enforcement_stats = {
            "tasks_checked": 0,
            "plans_generated": 0,
            "plans_approved": 0,
            "plans_rejected": 0,
            "bypasses": 0,
            "blocks": 0,
        }

        self._lock = asyncio.Lock()
        logger.info(f"[SOPEnforcer] Initialized (enabled={self.config.enabled}, strict={self.config.strict_mode})")

    async def check_task(
        self,
        task_id: str,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        existing_plan: Optional[DesignPlan] = None,
    ) -> Tuple[EnforcementAction, Optional[str], Optional[DesignPlan]]:
        """
        Check if a task can proceed or requires planning.

        Args:
            task_id: Unique identifier for the task
            goal: The task goal/description
            context: Additional context (repo map, mentioned files, etc.)
            existing_plan: Previously generated plan (if any)

        Returns:
            Tuple of (action, reason, plan)
            - action: What to do (ALLOW, REQUIRE_PLAN, BLOCK, BYPASS)
            - reason: Human-readable reason for the decision
            - plan: The design plan (if applicable)
        """
        async with self._lock:
            self._enforcement_stats["tasks_checked"] += 1

        if not self.config.enabled:
            return EnforcementAction.ALLOW, "SOP enforcement disabled", None

        context = context or {}

        # Check for bypass keywords
        goal_lower = goal.lower()
        for keyword in self.config.bypass_keywords:
            if keyword.strip().lower() in goal_lower:
                async with self._lock:
                    self._enforcement_stats["bypasses"] += 1
                return EnforcementAction.BYPASS, f"Bypass keyword detected: {keyword}", None

        # Check if this is a coding task
        from backend.core.jarvis_prime_client import CodingQuestionDetector
        detector = CodingQuestionDetector()
        is_coding, confidence, metadata = detector.detect(goal)

        if not is_coding:
            return EnforcementAction.ALLOW, "Not a coding task", None

        # Analyze complexity
        complexity, score, complexity_meta = self.analyzer.analyze(goal, context)
        context.update(complexity_meta)

        # Check if complexity requires planning
        if score < self.config.complexity_threshold and complexity in (TaskComplexity.TRIVIAL, TaskComplexity.SIMPLE):
            return EnforcementAction.ALLOW, f"Task complexity below threshold: {score:.2f}", None

        # Check for existing approved plan
        if existing_plan and existing_plan.status == PlanStatus.APPROVED:
            return EnforcementAction.ALLOW, "Using approved plan", existing_plan

        if task_id in self._approved_plans:
            plan = self._approved_plans[task_id]
            return EnforcementAction.ALLOW, "Using previously approved plan", plan

        # Plan is required
        async with self._lock:
            self._enforcement_stats["plans_generated"] += 1

        reason = (
            f"Design Plan required: complexity={complexity.value} ({score:.2f}), "
            f"signals={complexity_meta.get('signals', [])}"
        )

        return EnforcementAction.REQUIRE_PLAN, reason, None

    async def generate_and_validate_plan(
        self,
        task_id: str,
        goal: str,
        context: Dict[str, Any],
        llm: Any,
    ) -> Tuple[bool, DesignPlan, List[str]]:
        """
        Generate and validate a design plan.

        Args:
            task_id: Task identifier
            goal: The task goal
            context: Task context (including repo map)
            llm: LLM client for plan generation

        Returns:
            Tuple of (is_valid, plan, errors)
        """
        try:
            # Generate plan
            plan = await self.thinking.generate_plan(goal, context, llm)

            # Validate plan
            is_valid, errors = await self.thinking.validate_plan(plan, self.config)

            if is_valid:
                # Auto-approve if valid
                plan = await self.thinking.approve_plan(plan, "auto")
                async with self._lock:
                    self._approved_plans[task_id] = plan
                    self._enforcement_stats["plans_approved"] += 1
            else:
                async with self._lock:
                    self._pending_plans[task_id] = plan
                    self._enforcement_stats["plans_rejected"] += 1

            return is_valid, plan, errors

        except Exception as e:
            logger.error(f"[SOPEnforcer] Plan generation failed: {e}")
            # Create a minimal failure plan
            error_plan = DesignPlan(
                goal=goal[:100],
                context=f"Plan generation failed: {e}",
                proposed_changes=[ProposedChange(
                    file_path="unknown",
                    change_type="modify",
                    description="Plan generation failed",
                )],
                risk_assessment=[RiskAssessment(
                    category="other",
                    description="Plan generation failed",
                    severity="high",
                    mitigation="Manual review required",
                )],
                status=PlanStatus.REJECTED,
            )
            return False, error_plan, [str(e)]

    async def block_execution(
        self,
        task_id: str,
        reason: str,
    ) -> str:
        """Block task execution with a reason."""
        async with self._lock:
            self._blocked_tasks[task_id] = reason
            self._enforcement_stats["blocks"] += 1

        block_message = f"Safety Block: Design Plan required. {reason}"
        logger.warning(f"[SOPEnforcer] {block_message}")
        return block_message

    def get_stats(self) -> Dict[str, Any]:
        """Get enforcement statistics."""
        return {
            **self._enforcement_stats,
            "pending_plans": len(self._pending_plans),
            "approved_plans": len(self._approved_plans),
            "blocked_tasks": len(self._blocked_tasks),
        }

    def get_plan(self, task_id: str) -> Optional[DesignPlan]:
        """Get a plan by task ID."""
        return self._approved_plans.get(task_id) or self._pending_plans.get(task_id)

    def clear_expired_plans(self) -> int:
        """Clear expired plans from cache."""
        now = datetime.now()
        cleared = 0

        for task_id, plan in list(self._pending_plans.items()):
            age = (now - plan.created_at).total_seconds()
            if age > self.config.plan_cache_ttl_seconds:
                del self._pending_plans[task_id]
                cleared += 1

        return cleared


# =============================================================================
# Integration with AgenticTaskRunner
# =============================================================================

async def enforce_sop_before_execution(
    enforcer: SOPEnforcer,
    task_id: str,
    goal: str,
    context: Optional[Dict[str, Any]] = None,
    llm: Optional[Any] = None,
) -> Tuple[bool, Optional[DesignPlan], Optional[str]]:
    """
    Convenience function for AgenticTaskRunner integration.

    Returns:
        Tuple of (can_proceed, plan, block_reason)
    """
    action, reason, plan = await enforcer.check_task(task_id, goal, context)

    if action == EnforcementAction.ALLOW:
        return True, plan, None

    if action == EnforcementAction.BYPASS:
        logger.warning(f"[SOPEnforcer] Bypassing for: {reason}")
        return True, None, None

    if action == EnforcementAction.REQUIRE_PLAN:
        if llm:
            # Generate and validate plan
            is_valid, plan, errors = await enforcer.generate_and_validate_plan(
                task_id, goal, context or {}, llm
            )
            if is_valid:
                return True, plan, None
            else:
                block_reason = await enforcer.block_execution(
                    task_id,
                    f"Plan validation failed: {', '.join(errors)}"
                )
                return False, plan, block_reason
        else:
            block_reason = await enforcer.block_execution(task_id, reason)
            return False, None, block_reason

    # BLOCK action
    block_reason = await enforcer.block_execution(task_id, reason)
    return False, None, block_reason


# =============================================================================
# Singleton and Convenience Functions
# =============================================================================

_enforcer_instance: Optional[SOPEnforcer] = None


def get_sop_enforcer(config: Optional[SOPEnforcerConfig] = None) -> SOPEnforcer:
    """Get the singleton SOP enforcer."""
    global _enforcer_instance
    if _enforcer_instance is None:
        _enforcer_instance = SOPEnforcer(config)
    return _enforcer_instance


async def require_design_plan(
    goal: str,
    context: Optional[Dict[str, Any]] = None,
    llm: Optional[Any] = None,
) -> Tuple[bool, Optional[DesignPlan], Optional[str]]:
    """
    Convenience function to require a design plan for a task.

    Usage:
        can_proceed, plan, block_reason = await require_design_plan(
            goal="Refactor the authentication module",
            context={"repo_map": "..."},
            llm=my_llm_client,
        )
        if not can_proceed:
            return f"Blocked: {block_reason}"
    """
    enforcer = get_sop_enforcer()
    task_id = uuid4().hex
    return await enforce_sop_before_execution(enforcer, task_id, goal, context, llm)


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Configuration
    "SOPEnforcerConfig",

    # Enums
    "TaskComplexity",
    "PlanStatus",
    "EnforcementAction",

    # Pydantic Models
    "ProposedChange",
    "RiskAssessment",
    "TestPlan",
    "DesignPlan",

    # Core Classes
    "ThinkingProtocol",
    "JARVISThinkingProtocol",
    "ComplexityAnalyzer",
    "SOPEnforcer",

    # Integration
    "enforce_sop_before_execution",

    # Convenience Functions
    "get_sop_enforcer",
    "require_design_plan",
]
