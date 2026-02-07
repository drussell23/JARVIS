"""
v77.0: Coding Council Type Definitions
======================================

Core types, enums, and dataclasses for the Unified Coding Council.

Features:
- Immutable dataclasses with validation
- Comprehensive enums for task/framework types
- Serialization/deserialization support
- Type-safe configuration

Author: JARVIS v77.0
Version: 1.0.0
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes", "on")


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


def _get_env_path(key: str, default: str) -> Path:
    return Path(os.path.expanduser(_get_env(key, default)))


# =============================================================================
# Enums
# =============================================================================

class FrameworkType(str, Enum):
    """Available coding frameworks in the Council."""
    AIDER = "aider"              # Fast code editing
    OPENHANDS = "openhands"      # Sandboxed execution
    METAGPT = "metagpt"          # Multi-agent planning
    REPOMASTER = "repomaster"    # Codebase analysis
    CONTINUE = "continue"        # IDE integration
    CLAUDE_CODE = "claude_code"  # Direct Claude usage (fallback)
    # v84.0: Local JARVIS Prime inference (CodeLlama, Qwen, Llama etc.)
    JPRIME_LOCAL = "jprime_local"  # Local LLM via JARVIS Prime (cost-free, private)
    JPRIME_CODING = "jprime_coding"  # CodeLlama/DeepSeek Coder via J-Prime
    JPRIME_REASONING = "jprime_reasoning"  # Qwen/Llama via J-Prime for reasoning


class TaskComplexity(str, Enum):
    """Task complexity levels for routing decisions."""
    TRIVIAL = "trivial"      # Single line fix → Aider
    SIMPLE = "simple"        # Single file, small change → Aider
    MEDIUM = "medium"        # Multiple files, clear scope → Aider + RepoMaster
    COMPLEX = "complex"      # Architecture change → MetaGPT + OpenHands
    CRITICAL = "critical"    # Core system change → Full Council + Human approval


class TaskStatus(str, Enum):
    """Status of an evolution task."""
    PENDING = "pending"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    EXECUTING = "executing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class ValidationResult(str, Enum):
    """Result of code validation."""
    PASSED = "passed"
    SYNTAX_ERROR = "syntax_error"
    TYPE_ERROR = "type_error"
    SECURITY_ISSUE = "security_issue"
    TEST_FAILURE = "test_failure"
    CONTRACT_VIOLATION = "contract_violation"


class RollbackReason(str, Enum):
    """Reasons for rolling back changes."""
    VALIDATION_FAILED = "validation_failed"
    EXECUTION_ERROR = "execution_error"
    TIMEOUT = "timeout"
    USER_REQUESTED = "user_requested"
    CIRCUIT_BREAKER = "circuit_breaker"
    RESOURCE_EXHAUSTION = "resource_exhaustion"


# =============================================================================
# Configuration Dataclass
# =============================================================================

@dataclass
class CodingCouncilConfig:
    """Configuration for the Coding Council."""

    # Paths (environment-driven)
    repo_root: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_REPO_PATH",
            str(Path(__file__).parent.parent.parent.parent)
        )
    )
    frameworks_dir: Path = field(
        default_factory=lambda: _get_env_path(
            "CODING_COUNCIL_FRAMEWORKS_DIR",
            "~/.jarvis/coding_council/frameworks"
        )
    )
    cache_dir: Path = field(
        default_factory=lambda: _get_env_path(
            "CODING_COUNCIL_CACHE_DIR",
            "~/.jarvis/coding_council/cache"
        )
    )

    # Framework enablement
    aider_enabled: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_AIDER_ENABLED", True)
    )
    openhands_enabled: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_OPENHANDS_ENABLED", True)
    )
    metagpt_enabled: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_METAGPT_ENABLED", True)
    )
    repomaster_enabled: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_REPOMASTER_ENABLED", True)
    )
    continue_enabled: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_CONTINUE_ENABLED", False)
    )

    # v84.0: JARVIS Prime Local LLM Settings
    jprime_enabled: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_JPRIME_ENABLED", True)
    )
    jprime_url: str = field(
        default_factory=lambda: _get_env("JARVIS_PRIME_URL", "http://localhost:8000")
    )
    jprime_prefer_for_coding: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_JPRIME_PREFER_CODING", True)
    )
    jprime_fallback_to_claude: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_JPRIME_FALLBACK_CLAUDE", True)
    )
    jprime_coding_model: str = field(
        default_factory=lambda: _get_env("JPRIME_CODING_MODEL", "codellama-7b-instruct")
    )
    jprime_reasoning_model: str = field(
        default_factory=lambda: _get_env("JPRIME_REASONING_MODEL", "qwen2.5-7b-instruct")
    )
    jprime_general_model: str = field(
        default_factory=lambda: _get_env("JPRIME_GENERAL_MODEL", "llama-3-8b-instruct")
    )
    jprime_timeout: float = field(
        default_factory=lambda: _get_env_float("JPRIME_TIMEOUT", 120.0)
    )

    # Timeouts (seconds)
    default_timeout: float = field(
        default_factory=lambda: _get_env_float("CODING_COUNCIL_TIMEOUT", 300.0)
    )
    analysis_timeout: float = field(
        default_factory=lambda: _get_env_float("CODING_COUNCIL_ANALYSIS_TIMEOUT", 60.0)
    )
    planning_timeout: float = field(
        default_factory=lambda: _get_env_float("CODING_COUNCIL_PLANNING_TIMEOUT", 120.0)
    )
    execution_timeout: float = field(
        default_factory=lambda: _get_env_float("CODING_COUNCIL_EXECUTION_TIMEOUT", 300.0)
    )

    # Safety settings
    require_human_approval_for_critical: bool = field(
        default_factory=lambda: _get_env_bool("CODING_COUNCIL_REQUIRE_APPROVAL", True)
    )
    max_files_per_task: int = field(
        default_factory=lambda: _get_env_int("CODING_COUNCIL_MAX_FILES", 20)
    )
    max_lines_per_file: int = field(
        default_factory=lambda: _get_env_int("CODING_COUNCIL_MAX_LINES", 5000)
    )

    # Circuit breaker settings
    circuit_breaker_threshold: int = field(
        default_factory=lambda: _get_env_int("CODING_COUNCIL_CB_THRESHOLD", 3)
    )
    circuit_breaker_reset_seconds: float = field(
        default_factory=lambda: _get_env_float("CODING_COUNCIL_CB_RESET", 300.0)
    )

    # Resource limits
    max_memory_mb: int = field(
        default_factory=lambda: _get_env_int("CODING_COUNCIL_MAX_MEMORY_MB", 1536)
    )
    max_concurrent_tasks: int = field(
        default_factory=lambda: _get_env_int("CODING_COUNCIL_MAX_CONCURRENT", 2)
    )

    # Protected paths (comma-separated in env, never auto-modified)
    protected_paths: Set[str] = field(
        default_factory=lambda: set(_get_env(
            "CODING_COUNCIL_PROTECTED_PATHS",
            ".env,.git,secrets,credentials,*.key,*.pem"
        ).split(","))
    )


# =============================================================================
# Task Dataclasses
# =============================================================================

@dataclass
class EvolutionTask:
    """
    A task for the Coding Council to evolve code.

    Attributes:
        task_id: Unique identifier for the task
        description: Human-readable description of what to do
        target_files: Optional list of specific files to modify
        complexity: Estimated task complexity
        priority: Task priority (1-10, lower = higher priority)
        requires_sandbox: Force sandboxed execution
        requires_planning: Force MetaGPT planning phase
        requires_approval: Require human approval before applying
        correlation_id: ID for tracing across systems
        metadata: Additional context for the task
    """
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    target_files: List[str] = field(default_factory=list)
    complexity: TaskComplexity = TaskComplexity.SIMPLE
    priority: int = 5
    requires_sandbox: bool = False
    requires_planning: bool = False
    requires_approval: bool = False
    correlation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Timestamps
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Status tracking
    status: TaskStatus = TaskStatus.PENDING
    current_phase: str = ""
    progress_percent: int = 0

    def __post_init__(self):
        # Auto-set complexity based on target files
        if self.complexity == TaskComplexity.SIMPLE and len(self.target_files) > 3:
            self.complexity = TaskComplexity.MEDIUM
        if self.complexity == TaskComplexity.MEDIUM and len(self.target_files) > 10:
            self.complexity = TaskComplexity.COMPLEX

        # Auto-require approval for critical tasks
        if self.complexity == TaskComplexity.CRITICAL:
            self.requires_approval = True

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "target_files": self.target_files,
            "complexity": self.complexity.value,
            "priority": self.priority,
            "requires_sandbox": self.requires_sandbox,
            "requires_planning": self.requires_planning,
            "requires_approval": self.requires_approval,
            "correlation_id": self.correlation_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status.value,
            "current_phase": self.current_phase,
            "progress_percent": self.progress_percent,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvolutionTask":
        """Deserialize from dictionary."""
        return cls(
            task_id=data.get("task_id", str(uuid.uuid4())),
            description=data.get("description", ""),
            target_files=data.get("target_files", []),
            complexity=TaskComplexity(data.get("complexity", "simple")),
            priority=data.get("priority", 5),
            requires_sandbox=data.get("requires_sandbox", False),
            requires_planning=data.get("requires_planning", False),
            requires_approval=data.get("requires_approval", False),
            correlation_id=data.get("correlation_id"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            status=TaskStatus(data.get("status", "pending")),
            current_phase=data.get("current_phase", ""),
            progress_percent=data.get("progress_percent", 0),
        )


@dataclass
class FrameworkResult:
    """Result from a single framework execution."""
    framework: FrameworkType
    success: bool
    changes_made: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    output: str = ""
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    """Report from code validation."""
    result: ValidationResult
    passed: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    security_issues: List[Dict[str, Any]] = field(default_factory=list)
    type_errors: List[Dict[str, Any]] = field(default_factory=list)
    test_results: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvolutionResult:
    """
    Result of a Coding Council evolution task.

    Attributes:
        task_id: ID of the completed task
        success: Whether the evolution succeeded
        framework_used: Primary framework that executed the changes
        all_frameworks_used: All frameworks involved in the task
        changes_made: List of change descriptions
        files_modified: List of modified file paths
        files_created: List of newly created file paths
        files_deleted: List of deleted file paths
        validation_report: Code validation results
        rollback_id: ID for rolling back changes if needed
        error: Error message if failed
        execution_time_ms: Total execution time
        cost_estimate_usd: Estimated API cost
        insights: Analysis insights from RepoMaster/MetaGPT
    """
    task_id: str
    success: bool
    framework_used: FrameworkType
    all_frameworks_used: List[FrameworkType] = field(default_factory=list)
    changes_made: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    validation_report: Optional[ValidationReport] = None
    rollback_id: Optional[str] = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    cost_estimate_usd: float = 0.0
    insights: Dict[str, Any] = field(default_factory=dict)

    # Framework-specific results
    framework_results: Dict[str, FrameworkResult] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "task_id": self.task_id,
            "success": self.success,
            "framework_used": self.framework_used.value,
            "all_frameworks_used": [f.value for f in self.all_frameworks_used],
            "changes_made": self.changes_made,
            "files_modified": self.files_modified,
            "files_created": self.files_created,
            "files_deleted": self.files_deleted,
            "rollback_id": self.rollback_id,
            "error": self.error,
            "execution_time_ms": self.execution_time_ms,
            "cost_estimate_usd": self.cost_estimate_usd,
            "insights": self.insights,
        }


@dataclass
class RollbackInfo:
    """Information about a rollback savepoint."""
    rollback_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    created_at: float = field(default_factory=time.time)
    git_commit_before: str = ""
    git_stash_ref: Optional[str] = None
    files_backed_up: List[str] = field(default_factory=list)
    backup_dir: Optional[str] = None
    reason: Optional[RollbackReason] = None
    restored: bool = False
    restored_at: Optional[float] = None


@dataclass
class AnalysisResult:
    """Result from codebase analysis (RepoMaster)."""
    target_files: List[str]
    dependencies: Dict[str, List[str]]  # file -> list of files it depends on
    dependents: Dict[str, List[str]]    # file -> list of files that depend on it
    structure: Dict[str, Any]           # Codebase structure info
    insights: List[str]                 # Analysis insights
    suggestions: List[str]              # Improvement suggestions
    complexity_score: float             # 0-1, higher = more complex
    risk_score: float                   # 0-1, higher = more risky to modify


@dataclass
class PlanResult:
    """Result from planning phase (MetaGPT)."""
    prd: str                            # Product Requirements Document
    architecture: str                   # Architecture design
    steps: List[Dict[str, Any]]         # Execution steps
    estimated_complexity: TaskComplexity
    estimated_time_minutes: float
    risks: List[str]
    dependencies: List[str]
