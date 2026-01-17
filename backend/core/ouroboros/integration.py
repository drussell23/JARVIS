"""
Ouroboros Integration Layer v3.0 - Advanced Multi-LLM Orchestration
====================================================================

Enterprise-grade integration between Ouroboros and the Trinity ecosystem with:
- Advanced model capability registry with dynamic capability discovery
- Sophisticated task difficulty analysis (cyclomatic, cognitive, dependency)
- Model performance tracking with learning and A/B testing
- RAM-aware selection with load status checking
- Multi-model orchestration for complex task decomposition
- Unified Trinity coordination for cross-repo integration
- Intelligent model selection via IntelligentModelSelector (no hardcoding)
- Dynamic JARVIS Prime model discovery with actual capabilities
- Context-aware model selection based on code complexity
- Large context window support (32k) for big files
- Multi-provider LLM fallback (Prime -> Ollama -> API)
- Health monitoring with adaptive circuit breakers
- Reactor Core experience publishing with training feedback
- Sandboxed code execution with security validation
- Human review checkpoints with approval workflows

v3.0 Enhancements:
- AdvancedModelCapabilityRegistry for actual capability metadata
- TaskDifficultyAnalyzer with cyclomatic/cognitive complexity
- ModelPerformanceTracker for success/failure learning
- RAMAwareModelSelector with load status checking
- MultiModelOrchestrator for complex task decomposition
- TrinityCoordinator for unified cross-repo management
- EnhancedCodeComplexityAnalyzer with AST-based analysis
- AdaptiveCircuitBreaker with ML-based threshold prediction
- ParallelModelExecutor for concurrent model invocation

This layer handles ALL edge cases that could cause failures:
- JARVIS Prime not running -> fallback to Ollama -> fallback to API
- Model not loaded -> check load status -> prefer loaded models
- Context window exceeded -> intelligent chunking or model escalation
- Model version mismatch -> prefer newer versions
- Concurrent usage -> coordinate RAM across tasks
- Mid-task failure -> automatic fallback with state preservation
- Network issues -> retry with exponential backoff + jitter
- Repeated failures -> adaptive circuit breaker with recovery prediction
- Dangerous changes -> sandbox execution with security scan
- Model not suitable -> automatic fallback with capability matching

Author: Trinity System
Version: 3.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import lru_cache, wraps
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Coroutine, DefaultDict, Deque, Dict,
    FrozenSet, Generic, Iterator, List, Mapping, NamedTuple, Optional,
    Protocol, Sequence, Set, Tuple, Type, TypeVar, Union, cast, overload
)

import aiohttp

# Type variables for generic components
T = TypeVar('T')
ModelT = TypeVar('ModelT')
ResultT = TypeVar('ResultT')

logger = logging.getLogger("Ouroboros.Integration.v3")


# =============================================================================
# v3.0: ADVANCED ENUMS AND DATA STRUCTURES
# =============================================================================

class TaskDifficulty(Enum):
    """Sophisticated task difficulty classification."""
    TRIVIAL = 1      # Single-line fix, typo correction
    EASY = 2         # Simple function, clear requirements
    MODERATE = 3     # Multi-function, some complexity
    HARD = 4         # Multi-file, architectural decisions
    EXPERT = 5       # System-wide, breaking changes, high risk


class ModelLoadStatus(Enum):
    """Current load status of a model in JARVIS Prime."""
    LOADED = "loaded"           # In RAM, instant access
    CACHED = "cached"           # On disk, fast load (5-15s)
    ARCHIVED = "archived"       # Cold storage, slow load (30-120s)
    NOT_AVAILABLE = "not_available"  # Model doesn't exist
    LOADING = "loading"         # Currently being loaded
    ERROR = "error"             # Failed to load


class ModelSpecialization(Enum):
    """Model specialization areas for intelligent routing."""
    CODE_GENERATION = "code_generation"
    CODE_REFACTORING = "code_refactoring"
    BUG_FIXING = "bug_fixing"
    CODE_REVIEW = "code_review"
    ASYNC_PATTERNS = "async_patterns"
    TYPE_SYSTEMS = "type_systems"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    ARCHITECTURE = "architecture"
    PERFORMANCE = "performance"
    SECURITY = "security"
    GENERAL = "general"


class SelectionStrategy(Enum):
    """Model selection strategies based on requirements."""
    FASTEST = "fastest"           # Minimize latency
    CHEAPEST = "cheapest"         # Minimize cost
    BEST_QUALITY = "best_quality" # Maximize quality
    BALANCED = "balanced"         # Balance all factors
    SPECIALIZED = "specialized"   # Use task-specific model
    LOADED_FIRST = "loaded_first" # Prefer already-loaded models


@dataclass
class ModelCapability:
    """Rich capability description for a model."""
    name: str
    strength: float  # 0.0-1.0 how good at this capability
    languages: FrozenSet[str] = field(default_factory=frozenset)  # Python, TypeScript, etc.
    patterns: FrozenSet[str] = field(default_factory=frozenset)   # async, functional, etc.
    max_context: int = 4096
    optimal_task_size: Tuple[int, int] = (10, 500)  # (min_lines, max_lines)


@dataclass
class ModelMetadata:
    """Complete metadata for a model from JARVIS Prime."""
    id: str
    name: str
    context_window: int
    load_status: ModelLoadStatus
    capabilities: Dict[str, ModelCapability]
    specializations: Set[ModelSpecialization]
    version: str = "1.0"
    ram_usage_gb: float = 0.0
    load_time_seconds: float = 0.0
    avg_latency_ms: float = 0.0
    success_rate: float = 1.0
    cost_per_token: float = 0.0
    last_used: Optional[datetime] = None
    performance_history: List[float] = field(default_factory=list)


@dataclass
class TaskContext:
    """Rich context for a code improvement task."""
    code: str
    goal: str
    task_type: str
    difficulty: TaskDifficulty
    complexity_metrics: Dict[str, Any]
    file_path: Optional[Path] = None
    related_files: List[Path] = field(default_factory=list)
    error_history: List[str] = field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 10
    urgency: str = "normal"  # urgent, normal, low
    quality_requirement: str = "balanced"  # quick, balanced, best
    breaking_changes_allowed: bool = False
    test_coverage: float = 0.0
    dependencies: Set[str] = field(default_factory=set)


@dataclass
class SelectionResult:
    """Result of model selection with full reasoning."""
    provider: str
    model: str
    api_base: str
    context_window: int
    strategy_used: SelectionStrategy
    reasoning: List[str]
    fallback_chain: List[Tuple[str, str]]  # [(provider, model), ...]
    estimated_latency_ms: float = 0.0
    estimated_cost: float = 0.0
    confidence: float = 1.0
    metadata: Optional[ModelMetadata] = None


@dataclass
class PerformanceRecord:
    """Record of a model's performance on a task."""
    model_id: str
    task_type: str
    difficulty: TaskDifficulty
    success: bool
    latency_ms: float
    iterations_used: int
    code_quality_score: float  # 0.0-1.0
    timestamp: datetime = field(default_factory=datetime.now)
    error_message: Optional[str] = None
    context_tokens: int = 0
    output_tokens: int = 0


class PerformanceRecordPersistence:
    """
    v3.1: Robust persistence layer for PerformanceRecords with dual-backend support.

    Features:
    - Primary: SQLite for efficient querying and ACID guarantees
    - Fallback: JSON for portability and human readability
    - Async write batching to avoid I/O bottlenecks
    - Automatic schema migrations
    - Compression for large datasets
    - Retention policies with configurable TTL
    - Export/import for backup and migration
    """

    SCHEMA_VERSION = 1
    DEFAULT_RETENTION_DAYS = 90
    BATCH_WRITE_INTERVAL = 5.0  # seconds

    def __init__(
        self,
        storage_path: Optional[Path] = None,
        use_sqlite: bool = True,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ):
        """
        Initialize persistence layer.

        Args:
            storage_path: Base path for storage files. Defaults to ~/.jarvis/performance/
            use_sqlite: Use SQLite (True) or JSON (False) as primary backend
            retention_days: Days to retain records before cleanup
        """
        self._storage_path = storage_path or Path(
            os.getenv("JARVIS_PERFORMANCE_STORAGE",
                     Path.home() / ".jarvis" / "performance")
        )
        self._storage_path.mkdir(parents=True, exist_ok=True)

        self._use_sqlite = use_sqlite
        self._retention_days = retention_days
        self._db_path = self._storage_path / "performance_records.db"
        self._json_path = self._storage_path / "performance_records.json"
        self._lock = asyncio.Lock()
        self._write_queue: Deque[PerformanceRecord] = deque()
        self._batch_task: Optional[asyncio.Task] = None
        self._shutdown = False

        # Initialize storage backend
        if self._use_sqlite:
            self._init_sqlite()

        logger.info(f"✅ PerformanceRecordPersistence initialized at {self._storage_path}")

    def _init_sqlite(self) -> None:
        """Initialize SQLite database with schema."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS performance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    iterations_used INTEGER NOT NULL,
                    code_quality_score REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    error_message TEXT,
                    context_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_model_id ON performance_records(model_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON performance_records(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_task_type ON performance_records(task_type)
            """)

            # Schema version table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)

            # Check and update schema version
            cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cursor.fetchone()
            if not row:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                           (self.SCHEMA_VERSION,))

            conn.commit()

    def _record_to_dict(self, record: PerformanceRecord) -> Dict[str, Any]:
        """Convert PerformanceRecord to dictionary for serialization."""
        return {
            "model_id": record.model_id,
            "task_type": record.task_type,
            "difficulty": record.difficulty.name,
            "success": record.success,
            "latency_ms": record.latency_ms,
            "iterations_used": record.iterations_used,
            "code_quality_score": record.code_quality_score,
            "timestamp": record.timestamp.isoformat(),
            "error_message": record.error_message,
            "context_tokens": record.context_tokens,
            "output_tokens": record.output_tokens,
        }

    def _dict_to_record(self, data: Dict[str, Any]) -> PerformanceRecord:
        """Convert dictionary back to PerformanceRecord."""
        return PerformanceRecord(
            model_id=data["model_id"],
            task_type=data["task_type"],
            difficulty=TaskDifficulty[data["difficulty"]],
            success=data["success"],
            latency_ms=data["latency_ms"],
            iterations_used=data["iterations_used"],
            code_quality_score=data["code_quality_score"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            error_message=data.get("error_message"),
            context_tokens=data.get("context_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
        )

    async def save_record(self, record: PerformanceRecord) -> None:
        """
        Queue a record for batch persistence.

        Records are batched and written periodically to reduce I/O overhead.
        """
        self._write_queue.append(record)

        # Start batch writer if not running
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_writer())

    async def save_records_immediate(self, records: List[PerformanceRecord]) -> None:
        """Immediately persist a list of records (bypasses batching)."""
        async with self._lock:
            if self._use_sqlite:
                await self._save_to_sqlite(records)
            else:
                await self._save_to_json(records)

    async def _batch_writer(self) -> None:
        """Background task that batches writes."""
        while not self._shutdown:
            await asyncio.sleep(self.BATCH_WRITE_INTERVAL)

            if not self._write_queue:
                continue

            async with self._lock:
                # Drain queue
                records = []
                while self._write_queue:
                    records.append(self._write_queue.popleft())

                if records:
                    try:
                        if self._use_sqlite:
                            await self._save_to_sqlite(records)
                        else:
                            await self._save_to_json(records)
                        logger.debug(f"Persisted {len(records)} performance records")
                    except Exception as e:
                        logger.error(f"Failed to persist records: {e}")
                        # Re-queue on failure
                        self._write_queue.extendleft(records)

    async def _save_to_sqlite(self, records: List[PerformanceRecord]) -> None:
        """Save records to SQLite database."""
        def _write():
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany("""
                    INSERT INTO performance_records
                    (model_id, task_type, difficulty, success, latency_ms,
                     iterations_used, code_quality_score, timestamp,
                     error_message, context_tokens, output_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (r.model_id, r.task_type, r.difficulty.name, int(r.success),
                     r.latency_ms, r.iterations_used, r.code_quality_score,
                     r.timestamp.isoformat(), r.error_message,
                     r.context_tokens, r.output_tokens)
                    for r in records
                ])
                conn.commit()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def _save_to_json(self, records: List[PerformanceRecord]) -> None:
        """Save records to JSON file (append mode)."""
        def _write():
            existing = []
            if self._json_path.exists():
                try:
                    with open(self._json_path, 'r') as f:
                        existing = json.load(f)
                except json.JSONDecodeError:
                    existing = []

            existing.extend([self._record_to_dict(r) for r in records])

            with open(self._json_path, 'w') as f:
                json.dump(existing, f, indent=2)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def load_records(
        self,
        model_id: Optional[str] = None,
        limit: int = 100,
        since: Optional[datetime] = None,
    ) -> Dict[str, Deque[PerformanceRecord]]:
        """
        Load records from storage, optionally filtered.

        Args:
            model_id: Filter by specific model (None = all models)
            limit: Maximum records per model
            since: Only records after this datetime

        Returns:
            Dict mapping model_id to deque of PerformanceRecords
        """
        async with self._lock:
            if self._use_sqlite:
                return await self._load_from_sqlite(model_id, limit, since)
            else:
                return await self._load_from_json(model_id, limit, since)

    async def _load_from_sqlite(
        self,
        model_id: Optional[str],
        limit: int,
        since: Optional[datetime],
    ) -> Dict[str, Deque[PerformanceRecord]]:
        """Load records from SQLite database."""
        def _read():
            results: Dict[str, Deque[PerformanceRecord]] = defaultdict(
                lambda: deque(maxlen=limit)
            )

            query = "SELECT * FROM performance_records WHERE 1=1"
            params: List[Any] = []

            if model_id:
                query += " AND model_id = ?"
                params.append(model_id)

            if since:
                query += " AND timestamp > ?"
                params.append(since.isoformat())

            query += " ORDER BY timestamp DESC"

            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)

                for row in cursor:
                    record = PerformanceRecord(
                        model_id=row["model_id"],
                        task_type=row["task_type"],
                        difficulty=TaskDifficulty[row["difficulty"]],
                        success=bool(row["success"]),
                        latency_ms=row["latency_ms"],
                        iterations_used=row["iterations_used"],
                        code_quality_score=row["code_quality_score"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        error_message=row["error_message"],
                        context_tokens=row["context_tokens"] or 0,
                        output_tokens=row["output_tokens"] or 0,
                    )

                    # Respect per-model limit
                    if len(results[record.model_id]) < limit:
                        results[record.model_id].append(record)

            return results

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _read)

    async def _load_from_json(
        self,
        model_id: Optional[str],
        limit: int,
        since: Optional[datetime],
    ) -> Dict[str, Deque[PerformanceRecord]]:
        """Load records from JSON file."""
        def _read():
            results: Dict[str, Deque[PerformanceRecord]] = defaultdict(
                lambda: deque(maxlen=limit)
            )

            if not self._json_path.exists():
                return results

            try:
                with open(self._json_path, 'r') as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                return results

            # Sort by timestamp descending
            data.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

            for item in data:
                if model_id and item.get("model_id") != model_id:
                    continue

                if since:
                    item_time = datetime.fromisoformat(item.get("timestamp", ""))
                    if item_time <= since:
                        continue

                record = self._dict_to_record(item)

                if len(results[record.model_id]) < limit:
                    results[record.model_id].append(record)

            return results

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _read)

    async def cleanup_old_records(self) -> int:
        """
        Remove records older than retention period.

        Returns:
            Number of records deleted
        """
        cutoff = datetime.now() - timedelta(days=self._retention_days)

        async with self._lock:
            if self._use_sqlite:
                return await self._cleanup_sqlite(cutoff)
            else:
                return await self._cleanup_json(cutoff)

    async def _cleanup_sqlite(self, cutoff: datetime) -> int:
        """Clean up old records from SQLite."""
        def _clean():
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM performance_records WHERE timestamp < ?",
                    (cutoff.isoformat(),)
                )
                deleted = cursor.rowcount
                conn.commit()
                return deleted

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _clean)

    async def _cleanup_json(self, cutoff: datetime) -> int:
        """Clean up old records from JSON file."""
        def _clean():
            if not self._json_path.exists():
                return 0

            try:
                with open(self._json_path, 'r') as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                return 0

            original_count = len(data)
            data = [
                item for item in data
                if datetime.fromisoformat(item.get("timestamp", "")) > cutoff
            ]

            with open(self._json_path, 'w') as f:
                json.dump(data, f, indent=2)

            return original_count - len(data)

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _clean)

    async def get_statistics(
        self,
        model_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get aggregate statistics for performance records.

        Returns:
            Statistics including success rates, avg latency, quality scores
        """
        records = await self.load_records(model_id=model_id, limit=1000)

        stats: Dict[str, Any] = {
            "total_records": 0,
            "models": {},
        }

        for mid, model_records in records.items():
            record_list = list(model_records)
            if not record_list:
                continue

            stats["total_records"] += len(record_list)

            successes = sum(1 for r in record_list if r.success)
            latencies = [r.latency_ms for r in record_list]
            qualities = [r.code_quality_score for r in record_list]

            stats["models"][mid] = {
                "record_count": len(record_list),
                "success_rate": successes / len(record_list) if record_list else 0,
                "avg_latency_ms": statistics.mean(latencies) if latencies else 0,
                "p95_latency_ms": (
                    sorted(latencies)[int(len(latencies) * 0.95)]
                    if len(latencies) > 1 else latencies[0] if latencies else 0
                ),
                "avg_quality_score": statistics.mean(qualities) if qualities else 0,
                "task_type_distribution": self._get_task_distribution(record_list),
            }

        return stats

    def _get_task_distribution(
        self,
        records: List[PerformanceRecord],
    ) -> Dict[str, int]:
        """Get distribution of task types."""
        distribution: Dict[str, int] = defaultdict(int)
        for r in records:
            distribution[r.task_type] += 1
        return dict(distribution)

    async def export_to_json(self, output_path: Path) -> int:
        """Export all records to a JSON file for backup."""
        records = await self.load_records(limit=10000)

        all_records = []
        for model_records in records.values():
            all_records.extend([self._record_to_dict(r) for r in model_records])

        with open(output_path, 'w') as f:
            json.dump(all_records, f, indent=2)

        return len(all_records)

    async def import_from_json(self, input_path: Path) -> int:
        """Import records from a JSON backup file."""
        with open(input_path, 'r') as f:
            data = json.load(f)

        records = [self._dict_to_record(item) for item in data]
        await self.save_records_immediate(records)

        return len(records)

    async def shutdown(self) -> None:
        """Gracefully shutdown persistence layer, flushing pending writes."""
        self._shutdown = True

        # Flush remaining queue
        if self._write_queue:
            records = list(self._write_queue)
            self._write_queue.clear()

            try:
                if self._use_sqlite:
                    await self._save_to_sqlite(records)
                else:
                    await self._save_to_json(records)
                logger.info(f"Flushed {len(records)} records on shutdown")
            except Exception as e:
                logger.error(f"Failed to flush records on shutdown: {e}")

        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass


# Global persistence instance
_performance_persistence: Optional[PerformanceRecordPersistence] = None


def get_performance_persistence() -> PerformanceRecordPersistence:
    """Get or create the global performance persistence instance."""
    global _performance_persistence
    if _performance_persistence is None:
        _performance_persistence = PerformanceRecordPersistence()
    return _performance_persistence


# =============================================================================
# v3.0: INTELLIGENT MODEL SELECTOR INTEGRATION
# =============================================================================

# Try to import IntelligentModelSelector - fallback gracefully if unavailable
INTELLIGENT_SELECTOR_AVAILABLE = False
try:
    from backend.intelligence.model_selector import IntelligentModelSelector, QueryContext
    from backend.intelligence.model_registry import ModelDefinition, ModelState, get_model_registry
    INTELLIGENT_SELECTOR_AVAILABLE = True
    logger.info("✅ IntelligentModelSelector integration available")
except ImportError as e:
    logger.warning(f"IntelligentModelSelector not available: {e}")
    # Define stub classes for graceful degradation
    class ModelState(Enum):
        LOADED = "loaded"
        CACHED = "cached"
        ARCHIVED = "archived"


class EnhancedCodeComplexityAnalyzer:
    """
    v3.0: Advanced code complexity analysis with AST-based metrics.

    Provides comprehensive analysis including:
    - Cyclomatic complexity (McCabe complexity)
    - Cognitive complexity (code understanding difficulty)
    - Halstead metrics (vocabulary, length, volume)
    - Maintainability index
    - Dependency graph complexity
    - Code pattern detection (async, decorators, metaclasses)
    - Security vulnerability hints
    - Test coverage estimation
    """

    # Control flow keywords that increase cyclomatic complexity
    BRANCH_KEYWORDS = frozenset(['if', 'elif', 'for', 'while', 'and', 'or', 'except', 'with', 'assert', 'comprehension'])

    # Patterns that indicate advanced code
    ADVANCED_PATTERNS = {
        'async': re.compile(r'\b(async\s+def|await|asyncio)\b'),
        'metaclass': re.compile(r'\bmetaclass\s*='),
        'decorator': re.compile(r'^@\w+', re.MULTILINE),
        'generator': re.compile(r'\byield\b'),
        'context_manager': re.compile(r'\b(__enter__|__exit__|contextmanager)\b'),
        'descriptor': re.compile(r'\b(__get__|__set__|__delete__)\b'),
        'abc': re.compile(r'\b(ABC|abstractmethod)\b'),
        'threading': re.compile(r'\b(Thread|Lock|RLock|Semaphore|Event|Condition)\b'),
        'multiprocessing': re.compile(r'\b(Process|Pool|Queue|Manager)\b'),
    }

    # Security-sensitive patterns
    SECURITY_PATTERNS = {
        'eval_exec': re.compile(r'\b(eval|exec)\s*\('),
        'shell_injection': re.compile(r'\b(subprocess|os\.system|os\.popen)\b.*shell\s*=\s*True'),
        'sql_injection': re.compile(r'(execute|cursor)\s*\(\s*["\'].*%s'),
        'pickle': re.compile(r'\bpickle\.(load|loads)\b'),
        'yaml_unsafe': re.compile(r'\byaml\.load\s*\([^)]*\)(?!\s*,\s*Loader\s*=)'),
    }

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = asyncio.Lock()

    @staticmethod
    def analyze(code: str) -> Dict[str, Any]:
        """
        Analyze code complexity with comprehensive metrics.

        Returns dict with:
        - Basic metrics (lines, functions, classes)
        - Cyclomatic complexity (control flow)
        - Cognitive complexity (understanding difficulty)
        - Halstead metrics (vocabulary, volume)
        - Maintainability index
        - Pattern detection (async, decorators, etc.)
        - Security hints
        - Required context window
        - Difficulty classification
        """
        analyzer = EnhancedCodeComplexityAnalyzer()
        return analyzer._analyze_impl(code)

    def _analyze_impl(self, code: str) -> Dict[str, Any]:
        """Implementation of analysis."""
        lines = code.split('\n')
        line_count = len(lines)
        non_empty_lines = len([l for l in lines if l.strip()])

        # Basic metrics
        import_count = sum(1 for l in lines if l.strip().startswith(('import ', 'from ')))

        # Nesting depth analysis
        max_indent = 0
        avg_indent = 0
        indent_counts = []
        for line in lines:
            if line.strip():
                indent = len(line) - len(line.lstrip())
                indent_level = indent // 4
                max_indent = max(max_indent, indent_level)
                indent_counts.append(indent_level)

        avg_indent = statistics.mean(indent_counts) if indent_counts else 0

        # Function and class analysis
        function_count = sum(1 for l in lines if l.strip().startswith('def '))
        class_count = sum(1 for l in lines if l.strip().startswith('class '))
        async_function_count = sum(1 for l in lines if 'async def' in l)

        # AST-based analysis (with fallback for invalid syntax)
        ast_metrics = self._analyze_ast(code)

        # Cyclomatic complexity (simplified for non-AST case)
        cyclomatic = ast_metrics.get('cyclomatic_complexity', self._estimate_cyclomatic(code))

        # Cognitive complexity
        cognitive = ast_metrics.get('cognitive_complexity', self._estimate_cognitive(code, max_indent))

        # Halstead metrics
        halstead = self._calculate_halstead(code)

        # Maintainability index (0-100, higher is better)
        maintainability = self._calculate_maintainability_index(
            halstead['volume'], cyclomatic, line_count
        )

        # Pattern detection
        patterns = self._detect_patterns(code)

        # Security analysis
        security_hints = self._analyze_security(code)

        # Dependency complexity
        dependency_score = self._analyze_dependencies(code, import_count)

        # Calculate overall complexity score (0.0-1.0)
        complexity_score = self._calculate_complexity_score(
            cyclomatic, cognitive, max_indent, function_count, line_count, patterns
        )

        # Determine complexity level
        if complexity_score > 0.7:
            complexity = "high"
        elif complexity_score > 0.4:
            complexity = "medium"
        else:
            complexity = "low"

        # Determine task difficulty
        difficulty = self._classify_difficulty(
            complexity_score, line_count, patterns, security_hints
        )

        # Estimate required context window
        estimated_tokens = self._estimate_tokens(code)
        if estimated_tokens > 24000:
            required_context = "32k"
        elif estimated_tokens > 12000:
            required_context = "16k"
        elif estimated_tokens > 6000:
            required_context = "8k"
        else:
            required_context = "4k"

        return {
            # Basic metrics
            "line_count": line_count,
            "non_empty_lines": non_empty_lines,
            "import_count": import_count,
            "function_count": function_count,
            "async_function_count": async_function_count,
            "class_count": class_count,

            # Nesting metrics
            "max_nesting_depth": max_indent,
            "avg_nesting_depth": round(avg_indent, 2),

            # Complexity metrics
            "cyclomatic_complexity": cyclomatic,
            "cognitive_complexity": cognitive,
            "complexity_score": round(complexity_score, 3),
            "complexity": complexity,

            # Halstead metrics
            "halstead_vocabulary": halstead['vocabulary'],
            "halstead_length": halstead['length'],
            "halstead_volume": round(halstead['volume'], 2),
            "halstead_difficulty": round(halstead['difficulty'], 2),
            "halstead_effort": round(halstead['effort'], 2),

            # Maintainability
            "maintainability_index": round(maintainability, 2),

            # Patterns and features
            "patterns_detected": patterns,
            "has_async": patterns.get('async', False),
            "has_metaclass": patterns.get('metaclass', False),
            "decorator_count": patterns.get('decorator_count', 0),

            # Security
            "security_hints": security_hints,
            "potential_security_issues": len(security_hints),

            # Dependencies
            "dependency_complexity": dependency_score,

            # Task classification
            "difficulty": difficulty,
            "difficulty_value": difficulty.value,

            # Context requirements
            "estimated_tokens": estimated_tokens,
            "required_context": required_context,

            # AST-specific metrics
            **{k: v for k, v in ast_metrics.items() if k not in ['cyclomatic_complexity', 'cognitive_complexity']},
        }

    def _analyze_ast(self, code: str) -> Dict[str, Any]:
        """Analyze code using AST for precise metrics."""
        try:
            tree = ast.parse(code)
            return self._walk_ast(tree)
        except SyntaxError:
            return {}

    def _walk_ast(self, tree: ast.AST) -> Dict[str, Any]:
        """Walk AST and collect metrics."""
        metrics = {
            'cyclomatic_complexity': 1,  # Base complexity
            'cognitive_complexity': 0,
            'branch_count': 0,
            'loop_count': 0,
            'exception_handlers': 0,
            'nested_functions': 0,
            'lambda_count': 0,
            'comprehension_count': 0,
            'decorator_count': 0,
            'docstring_count': 0,
            'type_annotations': 0,
        }

        nesting_level = 0

        for node in ast.walk(tree):
            # Cyclomatic complexity contributors
            if isinstance(node, (ast.If, ast.IfExp)):
                metrics['cyclomatic_complexity'] += 1
                metrics['branch_count'] += 1
            elif isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
                metrics['cyclomatic_complexity'] += 1
                metrics['loop_count'] += 1
            elif isinstance(node, ast.ExceptHandler):
                metrics['cyclomatic_complexity'] += 1
                metrics['exception_handlers'] += 1
            elif isinstance(node, ast.BoolOp):
                # Each 'and'/'or' adds to complexity
                metrics['cyclomatic_complexity'] += len(node.values) - 1
            elif isinstance(node, ast.comprehension):
                metrics['cyclomatic_complexity'] += 1
                metrics['comprehension_count'] += 1

            # Cognitive complexity contributors
            if isinstance(node, (ast.If, ast.For, ast.While)):
                metrics['cognitive_complexity'] += (1 + nesting_level)
            elif isinstance(node, ast.Try):
                metrics['cognitive_complexity'] += 1

            # Other metrics
            if isinstance(node, ast.Lambda):
                metrics['lambda_count'] += 1
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check for nested functions
                for child in ast.walk(node):
                    if child != node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        metrics['nested_functions'] += 1
                        break
                # Check for decorators
                metrics['decorator_count'] += len(node.decorator_list)
                # Check for docstring
                if (node.body and isinstance(node.body[0], ast.Expr) and
                    isinstance(node.body[0].value, ast.Constant) and
                    isinstance(node.body[0].value.value, str)):
                    metrics['docstring_count'] += 1
                # Check for type annotations
                if node.returns:
                    metrics['type_annotations'] += 1
                metrics['type_annotations'] += sum(1 for arg in node.args.args if arg.annotation)

        return metrics

    def _estimate_cyclomatic(self, code: str) -> int:
        """Estimate cyclomatic complexity without AST."""
        complexity = 1  # Base
        for keyword in self.BRANCH_KEYWORDS:
            complexity += len(re.findall(rf'\b{keyword}\b', code))
        return complexity

    def _estimate_cognitive(self, code: str, max_nesting: int) -> int:
        """Estimate cognitive complexity without AST."""
        cognitive = 0

        # Each level of nesting adds to cognitive load
        lines = code.split('\n')
        for line in lines:
            if line.strip():
                indent = (len(line) - len(line.lstrip())) // 4
                if any(kw in line for kw in ['if ', 'for ', 'while ', 'elif ', 'except ']):
                    cognitive += 1 + indent

        return cognitive

    def _calculate_halstead(self, code: str) -> Dict[str, float]:
        """Calculate Halstead complexity metrics."""
        # Operators
        operators = re.findall(r'[+\-*/%=<>!&|^~@:,\.\[\]{}()]|and|or|not|in|is|lambda|yield|return|raise', code)
        # Operands (identifiers, numbers, strings)
        operands = re.findall(r'\b[a-zA-Z_]\w*\b|\d+\.?\d*|"[^"]*"|\'[^\']*\'', code)

        n1 = len(set(operators))  # Unique operators
        n2 = len(set(operands))   # Unique operands
        N1 = len(operators)        # Total operators
        N2 = len(operands)         # Total operands

        # Avoid division by zero
        n1 = max(n1, 1)
        n2 = max(n2, 1)
        N1 = max(N1, 1)
        N2 = max(N2, 1)

        vocabulary = n1 + n2
        length = N1 + N2
        volume = length * math.log2(vocabulary) if vocabulary > 0 else 0
        difficulty = (n1 / 2) * (N2 / n2) if n2 > 0 else 0
        effort = difficulty * volume

        return {
            'vocabulary': vocabulary,
            'length': length,
            'volume': volume,
            'difficulty': difficulty,
            'effort': effort,
        }

    def _calculate_maintainability_index(
        self, halstead_volume: float, cyclomatic: int, loc: int
    ) -> float:
        """
        Calculate Maintainability Index (0-100).

        Formula: 171 - 5.2*ln(HV) - 0.23*CC - 16.2*ln(LOC)
        Normalized to 0-100 range.
        """
        if loc <= 0 or halstead_volume <= 0:
            return 100.0

        mi = 171 - 5.2 * math.log(halstead_volume) - 0.23 * cyclomatic - 16.2 * math.log(loc)
        # Normalize to 0-100
        mi = max(0, min(100, mi * 100 / 171))
        return mi

    def _detect_patterns(self, code: str) -> Dict[str, Any]:
        """Detect advanced code patterns."""
        patterns: Dict[str, Any] = {'decorator_count': 0}

        for pattern_name, pattern in self.ADVANCED_PATTERNS.items():
            matches = pattern.findall(code)
            if pattern_name == 'decorator':
                patterns['decorator_count'] = len(matches)
                patterns[pattern_name] = len(matches) > 0
            else:
                patterns[pattern_name] = len(matches) > 0

        return patterns

    def _analyze_security(self, code: str) -> List[Dict[str, str]]:
        """Analyze code for potential security issues."""
        hints = []

        for issue_name, pattern in self.SECURITY_PATTERNS.items():
            if pattern.search(code):
                hints.append({
                    'type': issue_name,
                    'severity': 'high' if issue_name in ['eval_exec', 'shell_injection'] else 'medium',
                    'description': f"Potential {issue_name.replace('_', ' ')} vulnerability detected",
                })

        return hints

    def _analyze_dependencies(self, code: str, import_count: int) -> float:
        """Analyze dependency complexity."""
        # Count unique modules
        imports = re.findall(r'(?:from\s+(\S+)|import\s+(\S+))', code)
        unique_modules = set()
        for frm, imp in imports:
            module = frm or imp
            root_module = module.split('.')[0]
            unique_modules.add(root_module)

        # More unique external dependencies = higher complexity
        external_count = len([m for m in unique_modules if m not in ['os', 'sys', 'typing', 'dataclasses']])

        # Normalize to 0-1
        return min(1.0, external_count / 20.0)

    def _calculate_complexity_score(
        self,
        cyclomatic: int,
        cognitive: int,
        max_nesting: int,
        function_count: int,
        line_count: int,
        patterns: Dict[str, Any],
    ) -> float:
        """Calculate overall complexity score (0.0-1.0)."""
        scores = []

        # Cyclomatic complexity contribution (normalized)
        scores.append(min(1.0, cyclomatic / 50.0) * 0.25)

        # Cognitive complexity contribution
        scores.append(min(1.0, cognitive / 100.0) * 0.25)

        # Nesting depth contribution
        scores.append(min(1.0, max_nesting / 8.0) * 0.15)

        # Code size contribution
        scores.append(min(1.0, line_count / 1000.0) * 0.15)

        # Pattern complexity contribution
        pattern_score = 0.0
        if patterns.get('async'):
            pattern_score += 0.15
        if patterns.get('metaclass'):
            pattern_score += 0.3
        if patterns.get('threading') or patterns.get('multiprocessing'):
            pattern_score += 0.25
        if patterns.get('descriptor') or patterns.get('abc'):
            pattern_score += 0.2
        scores.append(min(1.0, pattern_score) * 0.20)

        return sum(scores)

    def _classify_difficulty(
        self,
        complexity_score: float,
        line_count: int,
        patterns: Dict[str, Any],
        security_hints: List[Dict[str, str]],
    ) -> TaskDifficulty:
        """Classify task difficulty based on metrics."""
        # Start with complexity-based classification
        if complexity_score < 0.2 and line_count < 50:
            difficulty = TaskDifficulty.TRIVIAL
        elif complexity_score < 0.35 and line_count < 150:
            difficulty = TaskDifficulty.EASY
        elif complexity_score < 0.55 and line_count < 500:
            difficulty = TaskDifficulty.MODERATE
        elif complexity_score < 0.75:
            difficulty = TaskDifficulty.HARD
        else:
            difficulty = TaskDifficulty.EXPERT

        # Escalate for certain patterns
        if patterns.get('metaclass') or patterns.get('descriptor'):
            difficulty = TaskDifficulty(min(5, difficulty.value + 1))

        # Escalate for security issues
        if any(h['severity'] == 'high' for h in security_hints):
            difficulty = TaskDifficulty(min(5, difficulty.value + 1))

        return difficulty

    def _estimate_tokens(self, code: str) -> int:
        """Estimate token count for the code."""
        # More accurate estimation based on character types
        words = len(re.findall(r'\b\w+\b', code))
        operators = len(re.findall(r'[+\-*/%=<>!&|^~@:,\.\[\]{}()]', code))
        strings = len(re.findall(r'"[^"]*"|\'[^\']*\'', code))

        # Rough formula: words + operators/2 + strings*2
        return int(words + operators * 0.5 + strings * 2)


# Alias for backward compatibility
CodeComplexityAnalyzer = EnhancedCodeComplexityAnalyzer


class AdvancedModelCapabilityRegistry:
    """
    v3.0: Enterprise-grade model capability registry with dynamic discovery.

    Provides comprehensive model management including:
    - Real-time capability fetching from JARVIS Prime API
    - Load status monitoring (loaded/cached/archived)
    - Performance metrics tracking per model
    - Specialization detection and matching
    - RAM usage awareness
    - Version tracking and preference
    - A/B testing support
    - Adaptive caching with TTL
    """

    # Context window estimation patterns
    CONTEXT_PATTERNS = {
        "32k": 32768, "32000": 32768,
        "16k": 16384, "16000": 16384,
        "8k": 8192, "8000": 8192,
        "4k": 4096, "4000": 4096,
        "128k": 131072, "128000": 131072,
    }

    # Model family characteristics
    MODEL_FAMILIES = {
        "deepseek-coder": {
            "context_default": 16384,
            "specializations": {ModelSpecialization.CODE_GENERATION, ModelSpecialization.BUG_FIXING},
            "languages": frozenset(["python", "javascript", "typescript", "go", "rust"]),
        },
        "codellama": {
            "context_default": 16384,
            "specializations": {ModelSpecialization.CODE_GENERATION, ModelSpecialization.CODE_REVIEW},
            "languages": frozenset(["python", "cpp", "java", "javascript"]),
        },
        "qwen": {
            "context_default": 32768,
            "specializations": {ModelSpecialization.GENERAL, ModelSpecialization.ARCHITECTURE},
            "languages": frozenset(["python", "javascript", "java", "cpp"]),
        },
        "llama": {
            "context_default": 8192,
            "specializations": {ModelSpecialization.GENERAL},
            "languages": frozenset(["python", "javascript"]),
        },
        "mistral": {
            "context_default": 8192,
            "specializations": {ModelSpecialization.GENERAL, ModelSpecialization.CODE_REVIEW},
            "languages": frozenset(["python", "javascript", "typescript"]),
        },
    }

    def __init__(self, api_base: str = None, enable_persistence: bool = True):
        self.api_base = api_base or os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1")
        self._models: Dict[str, ModelMetadata] = {}
        self._cached_models: Optional[List[Dict]] = None
        self._cache_time: float = 0
        self._cache_ttl: float = float(os.getenv("MODEL_REGISTRY_CACHE_TTL", "60.0"))
        self._lock = asyncio.Lock()
        self._performance_records: DefaultDict[str, Deque[PerformanceRecord]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        self._initialization_complete = False

        # v3.1: Persistence layer integration
        self._enable_persistence = enable_persistence
        self._persistence: Optional[PerformanceRecordPersistence] = None
        self._persistence_initialized = False

    async def initialize_persistence(self) -> None:
        """
        Initialize persistence layer and load historical records.

        Call this after creating the registry to restore previous performance data.
        """
        if not self._enable_persistence or self._persistence_initialized:
            return

        try:
            self._persistence = get_performance_persistence()

            # Load historical records from disk
            loaded_records = await self._persistence.load_records(limit=100)

            async with self._lock:
                for model_id, records in loaded_records.items():
                    # Merge with any existing in-memory records
                    for record in records:
                        self._performance_records[model_id].append(record)

                    # Update success rate for this model
                    if model_id in self._models:
                        self._models[model_id].success_rate = self._calculate_success_rate(model_id)

            self._persistence_initialized = True
            total_loaded = sum(len(r) for r in loaded_records.values())
            logger.info(f"✅ Loaded {total_loaded} historical performance records from persistence")

        except Exception as e:
            logger.warning(f"Failed to initialize persistence (non-fatal): {e}")
            self._persistence = None

    async def discover_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Discover available JARVIS Prime models with enhanced metadata.

        Returns list of model info dicts with:
        - id: Model identifier
        - context_window: Context window size
        - capabilities: Dict of capability -> strength
        - load_status: Current load status
        - specializations: Set of specializations
        - version: Model version
        """
        async with self._lock:
            if not force_refresh and self._cached_models and (time.time() - self._cache_time) < self._cache_ttl:
                return self._cached_models

            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{self.api_base.rstrip('/')}/models"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            models = data.get("data", [])

                            enriched = []
                            for model in models:
                                model_id = model.get("id", "unknown")
                                metadata = await self._build_model_metadata(session, model_id, model)
                                self._models[model_id] = metadata
                                enriched.append(self._metadata_to_dict(metadata))

                            enriched.sort(key=lambda m: m["context_window"], reverse=True)
                            self._cached_models = enriched
                            self._cache_time = time.time()
                            self._initialization_complete = True
                            logger.info(f"✅ Discovered {len(enriched)} models from JARVIS Prime")
                            return enriched

            except Exception as e:
                logger.warning(f"Failed to discover JARVIS Prime models: {e}")

            return self._cached_models or []

    async def _build_model_metadata(
        self, session: aiohttp.ClientSession, model_id: str, basic_info: Dict
    ) -> ModelMetadata:
        """Build complete ModelMetadata from API and inference."""
        # Try to get detailed capabilities
        capabilities = await self._fetch_capabilities(session, model_id)
        load_status = await self._fetch_load_status(session, model_id)

        family_info = self._get_model_family(model_id)
        context_window = self._estimate_context_window(model_id)

        # Build capability dict
        cap_dict: Dict[str, ModelCapability] = {}
        inferred_caps = self._infer_capabilities(model_id)
        for cap_name in inferred_caps:
            cap_dict[cap_name] = ModelCapability(
                name=cap_name,
                strength=0.8 if "code" in cap_name else 0.7,
                languages=family_info.get("languages", frozenset()),
            )

        return ModelMetadata(
            id=model_id,
            name=model_id,
            context_window=context_window,
            load_status=load_status,
            capabilities=cap_dict,
            specializations=family_info.get("specializations", {ModelSpecialization.GENERAL}),
            version=self._extract_version(model_id),
            success_rate=self._calculate_success_rate(model_id),
        )

    async def _fetch_capabilities(self, session: aiohttp.ClientSession, model_id: str) -> Dict:
        """Fetch actual capabilities from API (if available)."""
        try:
            url = f"{self.api_base.rstrip('/')}/models/{model_id}/capabilities"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return {}

    async def _fetch_load_status(self, session: aiohttp.ClientSession, model_id: str) -> ModelLoadStatus:
        """Fetch current load status from API."""
        try:
            url = f"{self.api_base.rstrip('/')}/models/{model_id}/status"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    status = data.get("status", "cached").lower()
                    return ModelLoadStatus(status)
        except Exception:
            pass
        return ModelLoadStatus.CACHED

    def _metadata_to_dict(self, metadata: ModelMetadata) -> Dict[str, Any]:
        """Convert ModelMetadata to dict for backward compatibility."""
        return {
            "id": metadata.id,
            "context_window": metadata.context_window,
            "capabilities": list(metadata.capabilities.keys()),
            "load_status": metadata.load_status.value,
            "specializations": [s.value for s in metadata.specializations],
            "version": metadata.version,
            "success_rate": metadata.success_rate,
        }

    async def get_best_model_for_task(
        self,
        required_context: str = "4k",
        task_type: str = "code_improvement",
        strategy: SelectionStrategy = SelectionStrategy.BALANCED,
        max_load_time: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the best available model using advanced selection.

        Args:
            required_context: Minimum context window needed (4k, 8k, 16k, 32k)
            task_type: Type of task
            strategy: Selection strategy
            max_load_time: Maximum acceptable load time

        Returns:
            Best matching model dict or None
        """
        models = await self.discover_models()
        if not models:
            return None

        context_map = {"4k": 4096, "8k": 8192, "16k": 16384, "32k": 32768, "128k": 131072}
        min_context = context_map.get(required_context, 4096)

        # Filter by context window
        suitable = [m for m in models if m["context_window"] >= min_context]
        if not suitable:
            suitable = models  # Fallback to all

        # Map task type to specializations
        task_specializations = {
            "code_improvement": {ModelSpecialization.CODE_GENERATION, ModelSpecialization.CODE_REFACTORING},
            "refactoring": {ModelSpecialization.CODE_REFACTORING},
            "bug_fix": {ModelSpecialization.BUG_FIXING},
            "code_review": {ModelSpecialization.CODE_REVIEW},
        }
        preferred_specs = task_specializations.get(task_type, {ModelSpecialization.GENERAL})

        def score_model(m: Dict) -> float:
            score = 0.0
            model_id = m["id"]
            metadata = self._models.get(model_id)

            # Capability score
            cap_score = 1.0 if any("code" in c for c in m.get("capabilities", [])) else 0.5
            score += cap_score * 0.25

            # Load status score (prefer loaded)
            load_scores = {"loaded": 1.0, "loading": 0.8, "cached": 0.5, "archived": 0.2}
            load_score = load_scores.get(m.get("load_status", "cached"), 0.3)

            if strategy == SelectionStrategy.LOADED_FIRST:
                score += load_score * 0.5
            else:
                score += load_score * 0.25

            # Success rate
            success_rate = m.get("success_rate", 1.0)
            score += success_rate * 0.25

            # Specialization match
            model_specs = {ModelSpecialization(s) for s in m.get("specializations", [])}
            spec_match = len(model_specs & preferred_specs) / max(len(preferred_specs), 1)
            score += spec_match * 0.25

            return score

        suitable.sort(key=score_model, reverse=True)
        return suitable[0] if suitable else None

    def record_performance(
        self, model_id: str, task_type: str, success: bool, latency_ms: float,
        iterations: int = 1, quality_score: float = 1.0,
        difficulty: TaskDifficulty = TaskDifficulty.MODERATE,
        error_message: Optional[str] = None,
        context_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """
        Record model performance for learning and persist to disk.

        Args:
            model_id: The model identifier
            task_type: Type of task (code_improvement, refactoring, etc.)
            success: Whether the task succeeded
            latency_ms: Execution latency in milliseconds
            iterations: Number of iterations used
            quality_score: Code quality score (0.0-1.0)
            difficulty: Task difficulty level
            error_message: Optional error message if failed
            context_tokens: Number of context tokens used
            output_tokens: Number of output tokens generated
        """
        record = PerformanceRecord(
            model_id=model_id,
            task_type=task_type,
            difficulty=difficulty,
            success=success,
            latency_ms=latency_ms,
            iterations_used=iterations,
            code_quality_score=quality_score,
            error_message=error_message,
            context_tokens=context_tokens,
            output_tokens=output_tokens,
        )
        self._performance_records[model_id].append(record)

        # Update model metadata success rate
        if model_id in self._models:
            self._models[model_id].success_rate = self._calculate_success_rate(model_id)

        # v3.1: Persist to disk asynchronously
        if self._persistence is not None:
            try:
                # Fire-and-forget async persistence (batched internally)
                asyncio.create_task(self._persistence.save_record(record))
            except RuntimeError:
                # No event loop running - try synchronous queuing
                self._persistence._write_queue.append(record)

    def _calculate_success_rate(self, model_id: str) -> float:
        """Calculate success rate from performance history."""
        records = self._performance_records.get(model_id, [])
        if not records:
            return 1.0
        return sum(1 for r in records if r.success) / len(records)

    def _get_model_family(self, model_id: str) -> Dict:
        """Get model family characteristics."""
        model_lower = model_id.lower()
        for family, info in self.MODEL_FAMILIES.items():
            if family in model_lower:
                return info
        return {"context_default": 4096, "specializations": {ModelSpecialization.GENERAL}, "languages": frozenset()}

    def _estimate_context_window(self, model_id: str) -> int:
        """Estimate context window from model name."""
        model_lower = model_id.lower()
        for pattern, size in self.CONTEXT_PATTERNS.items():
            if pattern in model_lower:
                return size
        return self._get_model_family(model_id).get("context_default", 4096)

    def _infer_capabilities(self, model_id: str) -> List[str]:
        """Infer capabilities from model name."""
        capabilities = ["text_generation"]
        model_lower = model_id.lower()

        if any(x in model_lower for x in ["code", "coder", "starcoder"]):
            capabilities.extend(["code_generation", "code_completion", "code_review"])

        if "instruct" in model_lower or "chat" in model_lower:
            capabilities.append("instruction_following")

        if any(x in model_lower for x in ["deepseek", "codellama", "wizardcoder"]):
            capabilities.append("code_improvement")

        return capabilities

    def _extract_version(self, model_id: str) -> str:
        """Extract version from model ID."""
        patterns = [r'-v(\d+(?:\.\d+)?)', r'v(\d+(?:\.\d+)?)', r'-(\d+\.\d+)']
        for pattern in patterns:
            match = re.search(pattern, model_id.lower())
            if match:
                return match.group(1)
        return "1.0"

    async def get_loaded_models(self) -> List[Dict[str, Any]]:
        """Get models currently loaded in RAM."""
        models = await self.discover_models()
        return [m for m in models if m.get("load_status") == "loaded"]

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive registry status."""
        return {
            "total_models": len(self._models),
            "loaded_count": sum(1 for m in self._models.values() if m.load_status == ModelLoadStatus.LOADED),
            "initialization_complete": self._initialization_complete,
            "persistence_enabled": self._enable_persistence,
            "persistence_initialized": self._persistence_initialized,
            "cache_age_seconds": time.time() - self._cache_time if self._cache_time else None,
            "models": {m.id: {"success_rate": m.success_rate, "load_status": m.load_status.value} for m in self._models.values()},
        }

    async def get_performance_statistics(
        self,
        model_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get aggregate performance statistics from persistence layer.

        Args:
            model_id: Filter by specific model (None = all models)

        Returns:
            Statistics including success rates, latency metrics, quality scores
        """
        if self._persistence is None:
            # Fall back to in-memory stats
            stats: Dict[str, Any] = {"total_records": 0, "models": {}}
            for mid, records in self._performance_records.items():
                if model_id and mid != model_id:
                    continue
                record_list = list(records)
                if record_list:
                    stats["total_records"] += len(record_list)
                    successes = sum(1 for r in record_list if r.success)
                    latencies = [r.latency_ms for r in record_list]
                    qualities = [r.code_quality_score for r in record_list]
                    stats["models"][mid] = {
                        "record_count": len(record_list),
                        "success_rate": successes / len(record_list),
                        "avg_latency_ms": statistics.mean(latencies) if latencies else 0,
                        "avg_quality_score": statistics.mean(qualities) if qualities else 0,
                    }
            return stats

        return await self._persistence.get_statistics(model_id=model_id)

    async def cleanup_old_records(self) -> int:
        """
        Clean up old performance records based on retention policy.

        Returns:
            Number of records cleaned up
        """
        if self._persistence is None:
            return 0
        return await self._persistence.cleanup_old_records()

    async def export_performance_data(self, output_path: Path) -> int:
        """
        Export all performance records to JSON file for backup/analysis.

        Args:
            output_path: Path to write JSON file

        Returns:
            Number of records exported
        """
        if self._persistence is None:
            # Export from memory
            all_records = []
            for records in self._performance_records.values():
                all_records.extend([{
                    "model_id": r.model_id,
                    "task_type": r.task_type,
                    "difficulty": r.difficulty.name,
                    "success": r.success,
                    "latency_ms": r.latency_ms,
                    "iterations_used": r.iterations_used,
                    "code_quality_score": r.code_quality_score,
                    "timestamp": r.timestamp.isoformat(),
                    "error_message": r.error_message,
                    "context_tokens": r.context_tokens,
                    "output_tokens": r.output_tokens,
                } for r in records])

            with open(output_path, 'w') as f:
                json.dump(all_records, f, indent=2)
            return len(all_records)

        return await self._persistence.export_to_json(output_path)

    async def shutdown(self) -> None:
        """
        Gracefully shutdown the registry, flushing all pending persistence writes.
        """
        if self._persistence is not None:
            logger.info("Shutting down AdvancedModelCapabilityRegistry persistence...")
            await self._persistence.shutdown()
            logger.info("✅ Persistence shutdown complete")


# Backward compatibility alias
JarvisPrimeModelDiscovery = AdvancedModelCapabilityRegistry


class IntelligentOuroborosModelSelector:
    """
    v3.0: Advanced intelligent model selection for Ouroboros self-programming.

    Combines:
    - AdvancedModelCapabilityRegistry for JARVIS Prime models with load status
    - EnhancedCodeComplexityAnalyzer for sophisticated code analysis
    - IntelligentModelSelector for general model selection
    - RAMAwareSelection for preferring loaded models
    - PerformanceTracking for learning from execution results

    Selection criteria (in priority order):
    1. Load status -> prefer already-loaded models (avoid wait time)
    2. Code complexity -> required model capability level
    3. Task difficulty -> model specialization matching
    4. Context requirements -> model context window
    5. Historical performance -> success rate per model/task
    6. System resources -> RAM-aware selection
    """

    def __init__(self):
        self._model_discovery = AdvancedModelCapabilityRegistry()
        self._selector: Optional[IntelligentModelSelector] = None
        self._lock = asyncio.Lock()
        self._selection_history: Deque[Dict[str, Any]] = deque(maxlen=100)
        self._current_task_id: Optional[str] = None

        # Try to initialize IntelligentModelSelector
        if INTELLIGENT_SELECTOR_AVAILABLE:
            try:
                self._selector = IntelligentModelSelector()
            except Exception as e:
                logger.warning(f"Failed to initialize IntelligentModelSelector: {e}")

    async def select_model_for_code(
        self,
        code: str,
        task_type: str = "code_improvement",
        prefer_local: bool = True,
        strategy: SelectionStrategy = SelectionStrategy.BALANCED,
        urgency: str = "normal",
        max_load_time: Optional[float] = None,
    ) -> SelectionResult:
        """
        Select the best model for a code improvement task using advanced selection.

        Args:
            code: The code to be improved
            task_type: Type of task (code_improvement, refactoring, bug_fix, code_review)
            prefer_local: Prefer local models (JARVIS Prime, Ollama)
            strategy: Selection strategy to use
            urgency: Task urgency (urgent, normal, low) - affects load time tolerance
            max_load_time: Maximum acceptable model load time (None = no limit)

        Returns:
            SelectionResult with complete selection details and reasoning
        """
        async with self._lock:
            task_id = f"task_{uuid.uuid4().hex[:8]}"
            self._current_task_id = task_id
            start_time = time.time()

            # Step 1: Analyze code complexity with enhanced analyzer
            complexity = EnhancedCodeComplexityAnalyzer.analyze(code)
            reasoning = []

            reasoning.append(
                f"Code analysis: {complexity['line_count']} lines, "
                f"cyclomatic={complexity['cyclomatic_complexity']}, "
                f"cognitive={complexity['cognitive_complexity']}, "
                f"maintainability={complexity['maintainability_index']:.1f}, "
                f"difficulty={complexity['difficulty'].name}, "
                f"required_context={complexity['required_context']}"
            )

            # Adjust strategy based on urgency
            if urgency == "urgent":
                strategy = SelectionStrategy.LOADED_FIRST
                max_load_time = max_load_time or 5.0
                reasoning.append(f"Urgency: {urgency} - forcing LOADED_FIRST strategy")
            elif urgency == "low":
                strategy = SelectionStrategy.BEST_QUALITY
                reasoning.append(f"Urgency: {urgency} - using BEST_QUALITY strategy")

            # Step 2: Get preferred specializations based on task type
            task_specializations = self._get_task_specializations(task_type, complexity)
            reasoning.append(f"Target specializations: {[s.value for s in task_specializations]}")

            # Step 3: Try JARVIS Prime with advanced selection
            if prefer_local:
                prime_model = await self._model_discovery.get_best_model_for_task(
                    required_context=complexity["required_context"],
                    task_type=task_type,
                    strategy=strategy,
                    max_load_time=max_load_time,
                )

                if prime_model:
                    load_status = prime_model.get("load_status", "cached")
                    success_rate = prime_model.get("success_rate", 1.0)

                    reasoning.append(
                        f"Selected JARVIS Prime model: {prime_model['id']} "
                        f"(context: {prime_model['context_window']}, "
                        f"load_status: {load_status}, "
                        f"success_rate: {success_rate:.2f})"
                    )

                    # Build fallback chain
                    fallback_chain = await self._build_fallback_chain(
                        complexity, task_type, exclude=[prime_model['id']]
                    )

                    result = SelectionResult(
                        provider="jarvis-prime",
                        model=prime_model["id"],
                        api_base=os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1"),
                        context_window=prime_model["context_window"],
                        strategy_used=strategy,
                        reasoning=reasoning,
                        fallback_chain=fallback_chain,
                        estimated_latency_ms=self._estimate_latency(prime_model, load_status),
                        confidence=success_rate,
                    )

                    self._record_selection(task_id, result, complexity, time.time() - start_time)
                    return result

            # Step 4: Try IntelligentModelSelector as fallback
            if self._selector:
                try:
                    capabilities = self._get_required_capabilities(task_type)

                    model = await self._selector.select_best_model(
                        query=f"Improve this {complexity['difficulty'].name.lower()} difficulty code",
                        intent="code_improvement",
                        required_capabilities=capabilities,
                        context={
                            "code_complexity": complexity["complexity"],
                            "difficulty": complexity["difficulty"].name,
                            "cyclomatic": complexity["cyclomatic_complexity"],
                            "required_context": complexity["required_context"],
                        }
                    )

                    if model:
                        reasoning.append(f"IntelligentModelSelector chose: {model.name}")

                        result = SelectionResult(
                            provider=model.provider if hasattr(model, 'provider') else "intelligent",
                            model=model.model_id if hasattr(model, 'model_id') else str(model.name),
                            api_base=model.api_base if hasattr(model, 'api_base') else "",
                            context_window=model.context_window if hasattr(model, 'context_window') else 8192,
                            strategy_used=strategy,
                            reasoning=reasoning,
                            fallback_chain=await self._build_fallback_chain(complexity, task_type),
                            confidence=0.8,
                        )

                        self._record_selection(task_id, result, complexity, time.time() - start_time)
                        return result

                except Exception as e:
                    reasoning.append(f"IntelligentModelSelector failed: {e}")

            # Step 5: Fallback to configured providers
            reasoning.append("Using fallback provider selection")
            result = self._get_fallback_model(complexity, reasoning, strategy)
            self._record_selection(task_id, result, complexity, time.time() - start_time)
            return result

    def _get_task_specializations(
        self, task_type: str, complexity: Dict[str, Any]
    ) -> Set[ModelSpecialization]:
        """Get relevant specializations based on task type and complexity."""
        base_specs = {
            "code_improvement": {ModelSpecialization.CODE_GENERATION},
            "refactoring": {ModelSpecialization.CODE_REFACTORING},
            "bug_fix": {ModelSpecialization.BUG_FIXING},
            "code_review": {ModelSpecialization.CODE_REVIEW},
            "testing": {ModelSpecialization.TESTING},
            "documentation": {ModelSpecialization.DOCUMENTATION},
            "performance": {ModelSpecialization.PERFORMANCE},
            "security": {ModelSpecialization.SECURITY},
        }

        specs = base_specs.get(task_type, {ModelSpecialization.GENERAL})

        # Add specializations based on code patterns
        patterns = complexity.get("patterns_detected", {})
        if patterns.get("async"):
            specs.add(ModelSpecialization.ASYNC_PATTERNS)
        if complexity.get("potential_security_issues", 0) > 0:
            specs.add(ModelSpecialization.SECURITY)
        if complexity.get("difficulty", TaskDifficulty.MODERATE).value >= 4:
            specs.add(ModelSpecialization.ARCHITECTURE)

        return specs

    def _get_required_capabilities(self, task_type: str) -> Set[str]:
        """Get required capabilities for a task type."""
        capabilities = {"code_generation"}

        if task_type == "refactoring":
            capabilities.update({"code_refactoring", "code_review"})
        elif task_type == "bug_fix":
            capabilities.update({"code_debugging", "bug_fixing"})
        elif task_type == "code_review":
            capabilities.update({"code_review", "code_analysis"})
        elif task_type == "testing":
            capabilities.add("test_generation")
        elif task_type == "documentation":
            capabilities.add("documentation")

        return capabilities

    async def _build_fallback_chain(
        self,
        complexity: Dict[str, Any],
        task_type: str,
        exclude: Optional[List[str]] = None,
    ) -> List[Tuple[str, str]]:
        """Build a fallback chain of models."""
        exclude = exclude or []
        chain = []

        # Try to get more models from discovery
        try:
            models = await self._model_discovery.discover_models()
            for model in models[:3]:
                if model["id"] not in exclude:
                    chain.append(("jarvis-prime", model["id"]))
        except Exception:
            pass

        # Add static fallbacks
        if complexity["required_context"] in ("16k", "32k"):
            fallback_model = os.getenv("JARVIS_PRIME_MODEL", "deepseek-coder-v2")
            if fallback_model not in exclude:
                chain.append(("jarvis-prime", fallback_model))
        else:
            ollama_model = os.getenv("OLLAMA_MODEL", "codellama")
            chain.append(("ollama", ollama_model))

        # Add API fallback
        chain.append(("anthropic", "claude-3-haiku-20240307"))

        return chain[:5]  # Limit to 5 fallbacks

    def _estimate_latency(self, model: Dict[str, Any], load_status: str) -> float:
        """Estimate latency based on model and load status."""
        base_latency = 100.0  # Base inference latency in ms

        # Add load time if not already loaded
        load_penalties = {
            "loaded": 0,
            "loading": 5000,  # 5 seconds
            "cached": 15000,  # 15 seconds
            "archived": 60000,  # 60 seconds
        }

        return base_latency + load_penalties.get(load_status, 15000)

    def _get_fallback_model(
        self,
        complexity: Dict[str, Any],
        reasoning: List[str],
        strategy: SelectionStrategy,
    ) -> SelectionResult:
        """Get fallback model based on complexity."""
        if complexity["required_context"] in ("16k", "32k"):
            return SelectionResult(
                provider="jarvis-prime",
                model=os.getenv("JARVIS_PRIME_MODEL", "deepseek-coder-v2"),
                api_base=os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1"),
                context_window=16384,
                strategy_used=strategy,
                reasoning=reasoning + ["Fallback: deepseek-coder-v2 for large context"],
                fallback_chain=[("ollama", "codellama"), ("anthropic", "claude-3-haiku-20240307")],
                confidence=0.7,
            )
        else:
            return SelectionResult(
                provider="ollama",
                model=os.getenv("OLLAMA_MODEL", "codellama"),
                api_base=os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1"),
                context_window=8192,
                strategy_used=strategy,
                reasoning=reasoning + ["Fallback: codellama for simple code"],
                fallback_chain=[("jarvis-prime", "deepseek-coder-v2"), ("anthropic", "claude-3-haiku-20240307")],
                confidence=0.6,
            )

    def _record_selection(
        self,
        task_id: str,
        result: SelectionResult,
        complexity: Dict[str, Any],
        selection_time: float,
    ) -> None:
        """Record selection for analytics and learning."""
        record = {
            "task_id": task_id,
            "timestamp": datetime.now().isoformat(),
            "provider": result.provider,
            "model": result.model,
            "strategy": result.strategy_used.value,
            "complexity": complexity["complexity"],
            "difficulty": complexity["difficulty"].name,
            "confidence": result.confidence,
            "selection_time_ms": selection_time * 1000,
        }
        self._selection_history.append(record)

    def record_execution_result(
        self,
        task_id: str,
        success: bool,
        latency_ms: float,
        iterations: int,
        quality_score: float = 1.0,
        error_message: Optional[str] = None,
    ) -> None:
        """Record execution result for performance learning."""
        # Find the selection record
        for record in self._selection_history:
            if record.get("task_id") == task_id:
                record["execution_success"] = success
                record["execution_latency_ms"] = latency_ms
                record["iterations"] = iterations
                record["quality_score"] = quality_score
                if error_message:
                    record["error"] = error_message

                # Update model performance in registry
                self._model_discovery.record_performance(
                    model_id=record["model"],
                    task_type="code_improvement",
                    success=success,
                    latency_ms=latency_ms,
                    iterations=iterations,
                    quality_score=quality_score,
                )
                break

    async def get_health(self) -> Dict[str, Any]:
        """Get comprehensive health status of model selection components."""
        models = await self._model_discovery.discover_models()
        loaded_models = await self._model_discovery.get_loaded_models()

        # Calculate selection stats
        recent_selections = list(self._selection_history)[-50:]
        success_rate = 0.0
        if recent_selections:
            successes = sum(1 for s in recent_selections if s.get("execution_success", True))
            success_rate = successes / len(recent_selections)

        return {
            "intelligent_selector_available": INTELLIGENT_SELECTOR_AVAILABLE,
            "intelligent_selector_initialized": self._selector is not None,
            "jarvis_prime_models_total": len(models),
            "jarvis_prime_models_loaded": len(loaded_models),
            "jarvis_prime_models_list": [m["id"] for m in models[:5]],
            "loaded_models_list": [m["id"] for m in loaded_models],
            "recent_selection_count": len(recent_selections),
            "recent_success_rate": success_rate,
            "registry_status": self._model_discovery.get_status(),
        }

    def get_selection_analytics(self) -> Dict[str, Any]:
        """Get analytics on model selection patterns."""
        if not self._selection_history:
            return {"total_selections": 0}

        by_provider = defaultdict(int)
        by_strategy = defaultdict(int)
        by_complexity = defaultdict(int)
        avg_confidence = []
        avg_selection_time = []

        for record in self._selection_history:
            by_provider[record.get("provider", "unknown")] += 1
            by_strategy[record.get("strategy", "unknown")] += 1
            by_complexity[record.get("complexity", "unknown")] += 1
            if "confidence" in record:
                avg_confidence.append(record["confidence"])
            if "selection_time_ms" in record:
                avg_selection_time.append(record["selection_time_ms"])

        return {
            "total_selections": len(self._selection_history),
            "by_provider": dict(by_provider),
            "by_strategy": dict(by_strategy),
            "by_complexity": dict(by_complexity),
            "avg_confidence": statistics.mean(avg_confidence) if avg_confidence else 0.0,
            "avg_selection_time_ms": statistics.mean(avg_selection_time) if avg_selection_time else 0.0,
        }


# Global instance for easy access
_intelligent_selector: Optional[IntelligentOuroborosModelSelector] = None


def get_intelligent_ouroboros_selector() -> IntelligentOuroborosModelSelector:
    """Get global intelligent model selector instance."""
    global _intelligent_selector
    if _intelligent_selector is None:
        _intelligent_selector = IntelligentOuroborosModelSelector()
    return _intelligent_selector


# =============================================================================
# v3.0: MULTI-MODEL ORCHESTRATOR
# =============================================================================

class MultiModelOrchestrator:
    """
    v3.0: Orchestrates multiple models for complex task decomposition.

    Provides:
    - Task decomposition into subtasks
    - Parallel model execution
    - Model pipeline coordination
    - Result aggregation
    - Automatic fallback on partial failures
    - Cost optimization across models
    """

    def __init__(self, selector: Optional[IntelligentOuroborosModelSelector] = None):
        self._selector = selector or get_intelligent_ouroboros_selector()
        self._executor = ThreadPoolExecutor(max_workers=int(os.getenv("ORCHESTRATOR_MAX_WORKERS", "4")))
        self._active_tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def orchestrate_complex_task(
        self,
        code: str,
        goal: str,
        task_type: str = "code_improvement",
        decompose: bool = True,
        parallel: bool = True,
    ) -> Dict[str, Any]:
        """
        Orchestrate a complex task potentially using multiple models.

        Args:
            code: The code to process
            goal: The improvement goal
            task_type: Type of task
            decompose: Whether to decompose into subtasks
            parallel: Whether to run subtasks in parallel

        Returns:
            Aggregated results from all models
        """
        task_id = f"orchestration_{uuid.uuid4().hex[:8]}"
        start_time = time.time()

        async with self._lock:
            self._active_tasks[task_id] = {
                "status": "running",
                "start_time": start_time,
                "subtasks": [],
            }

        try:
            # Analyze code complexity
            complexity = EnhancedCodeComplexityAnalyzer.analyze(code)
            difficulty = complexity["difficulty"]

            # Decide if decomposition is needed
            if not decompose or difficulty.value < TaskDifficulty.HARD.value:
                # Single model execution
                return await self._single_model_execution(
                    task_id, code, goal, task_type, complexity
                )

            # Decompose into subtasks
            subtasks = await self._decompose_task(code, goal, task_type, complexity)

            if parallel and len(subtasks) > 1:
                # Parallel execution
                results = await self._parallel_execution(task_id, subtasks)
            else:
                # Sequential execution
                results = await self._sequential_execution(task_id, subtasks)

            # Aggregate results
            aggregated = self._aggregate_results(results, task_id)

            async with self._lock:
                self._active_tasks[task_id]["status"] = "completed"
                self._active_tasks[task_id]["duration"] = time.time() - start_time

            return aggregated

        except Exception as e:
            async with self._lock:
                self._active_tasks[task_id]["status"] = "failed"
                self._active_tasks[task_id]["error"] = str(e)
            raise

    async def _single_model_execution(
        self,
        task_id: str,
        code: str,
        goal: str,
        task_type: str,
        complexity: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute task with a single model."""
        selection = await self._selector.select_model_for_code(
            code=code,
            task_type=task_type,
            strategy=SelectionStrategy.BALANCED,
        )

        return {
            "task_id": task_id,
            "mode": "single_model",
            "selection": {
                "provider": selection.provider,
                "model": selection.model,
                "reasoning": selection.reasoning,
            },
            "complexity": complexity["complexity"],
            "difficulty": complexity["difficulty"].name,
        }

    async def _decompose_task(
        self,
        code: str,
        goal: str,
        task_type: str,
        complexity: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Decompose complex task into subtasks."""
        subtasks = []

        # Analysis subtask (smaller model, quick)
        subtasks.append({
            "type": "analysis",
            "goal": f"Analyze code structure and identify improvement areas for: {goal}",
            "code_slice": code[:2000],  # First portion
            "strategy": SelectionStrategy.FASTEST,
            "priority": 1,
        })

        # Main improvement subtask (best model)
        subtasks.append({
            "type": "improvement",
            "goal": goal,
            "code_slice": code,
            "strategy": SelectionStrategy.BEST_QUALITY,
            "priority": 2,
        })

        # Review subtask (different model for second opinion)
        if complexity["difficulty"].value >= TaskDifficulty.HARD.value:
            subtasks.append({
                "type": "review",
                "goal": f"Review the improvements for correctness and potential issues",
                "code_slice": code,
                "strategy": SelectionStrategy.SPECIALIZED,
                "priority": 3,
            })

        return subtasks

    async def _parallel_execution(
        self,
        task_id: str,
        subtasks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Execute subtasks in parallel."""
        async def execute_subtask(subtask: Dict[str, Any]) -> Dict[str, Any]:
            selection = await self._selector.select_model_for_code(
                code=subtask["code_slice"],
                task_type=subtask["type"],
                strategy=subtask["strategy"],
            )
            return {
                "subtask_type": subtask["type"],
                "selection": {
                    "provider": selection.provider,
                    "model": selection.model,
                    "confidence": selection.confidence,
                },
                "priority": subtask["priority"],
            }

        tasks = [execute_subtask(st) for st in subtasks]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def _sequential_execution(
        self,
        task_id: str,
        subtasks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Execute subtasks sequentially."""
        results = []
        for subtask in sorted(subtasks, key=lambda s: s["priority"]):
            selection = await self._selector.select_model_for_code(
                code=subtask["code_slice"],
                task_type=subtask["type"],
                strategy=subtask["strategy"],
            )
            results.append({
                "subtask_type": subtask["type"],
                "selection": {
                    "provider": selection.provider,
                    "model": selection.model,
                    "confidence": selection.confidence,
                },
                "priority": subtask["priority"],
            })
        return results

    def _aggregate_results(
        self,
        results: List[Any],
        task_id: str,
    ) -> Dict[str, Any]:
        """Aggregate results from multiple subtasks."""
        successful = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]

        return {
            "task_id": task_id,
            "mode": "multi_model",
            "total_subtasks": len(results),
            "successful_subtasks": len(successful),
            "failed_subtasks": len(failed),
            "subtask_results": successful,
            "errors": [str(e) for e in failed] if failed else None,
        }

    def get_active_tasks(self) -> Dict[str, Dict[str, Any]]:
        """Get all active orchestration tasks."""
        return dict(self._active_tasks)


# =============================================================================
# v3.0: UNIFIED TRINITY COORDINATOR
# =============================================================================

class TrinityCoordinator:
    """
    v3.0: Unified coordinator for JARVIS, JARVIS Prime, and Reactor Core.

    Provides:
    - Cross-repo health monitoring
    - Unified startup orchestration
    - Event routing between repos
    - State synchronization
    - Graceful degradation handling
    """

    # Repository configurations
    REPOS = {
        "jarvis": {
            "name": "JARVIS",
            "path_env": "JARVIS_REPO_PATH",
            "default_path": "~/Documents/repos/JARVIS-AI-Agent",
            "health_endpoint": "/health",
            "port_env": "JARVIS_PORT",
            "default_port": 8010,
        },
        "prime": {
            "name": "JARVIS-Prime",
            "path_env": "JARVIS_PRIME_REPO_PATH",
            "default_path": "~/Documents/repos/jarvis-prime",
            "health_endpoint": "/v1/models",
            "port_env": "JARVIS_PRIME_PORT",
            "default_port": 8002,
        },
        "reactor": {
            "name": "Reactor-Core",
            "path_env": "REACTOR_CORE_REPO_PATH",
            "default_path": "~/Documents/repos/reactor-core",
            "health_endpoint": "/health",
            "port_env": "REACTOR_CORE_PORT",
            "default_port": 8003,
        },
    }

    def __init__(self):
        self._health_status: Dict[str, Dict[str, Any]] = {}
        self._event_subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._state_dir = Path(os.getenv(
            "TRINITY_STATE_DIR",
            os.path.expanduser("~/.jarvis/trinity")
        ))
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._startup_complete = False
        self._shutdown_event = asyncio.Event()

    async def initialize(self) -> bool:
        """Initialize the Trinity coordinator."""
        logger.info("🔺 Initializing Trinity Coordinator...")

        # Check all repo health
        await self.check_all_health()

        # Load persisted state
        await self._load_state()

        self._startup_complete = True
        logger.info("✅ Trinity Coordinator initialized")
        return True

    async def check_all_health(self) -> Dict[str, Dict[str, Any]]:
        """Check health of all Trinity components."""
        tasks = [
            self._check_repo_health(repo_id, config)
            for repo_id, config in self.REPOS.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (repo_id, _), result in zip(self.REPOS.items(), results):
            if isinstance(result, Exception):
                self._health_status[repo_id] = {
                    "healthy": False,
                    "error": str(result),
                    "timestamp": datetime.now().isoformat(),
                }
            else:
                self._health_status[repo_id] = result

        return self._health_status

    async def _check_repo_health(
        self, repo_id: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Check health of a single repository service."""
        port = int(os.getenv(config["port_env"], config["default_port"]))
        url = f"http://localhost:{port}{config['health_endpoint']}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=5.0)
                ) as response:
                    return {
                        "healthy": response.status == 200,
                        "status_code": response.status,
                        "url": url,
                        "timestamp": datetime.now().isoformat(),
                    }
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e),
                "url": url,
                "timestamp": datetime.now().isoformat(),
            }

    async def publish_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        target_repos: Optional[List[str]] = None,
    ) -> None:
        """Publish an event to Trinity components."""
        event = {
            "event_id": f"evt_{uuid.uuid4().hex[:12]}",
            "event_type": event_type,
            "payload": payload,
            "timestamp": datetime.now().isoformat(),
            "source": "trinity_coordinator",
        }

        # Save event to state directory
        event_file = self._state_dir / f"events/{event['event_id']}.json"
        event_file.parent.mkdir(exist_ok=True)
        await asyncio.to_thread(
            event_file.write_text,
            json.dumps(event, indent=2)
        )

        # Notify subscribers
        for subscriber in self._event_subscribers.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(subscriber):
                    await subscriber(event)
                else:
                    subscriber(event)
            except Exception as e:
                logger.warning(f"Event subscriber failed: {e}")

    def subscribe(self, event_type: str, callback: Callable) -> None:
        """Subscribe to events of a specific type."""
        self._event_subscribers[event_type].append(callback)

    async def sync_state(self) -> None:
        """Synchronize state across all Trinity components."""
        state = {
            "coordinator_version": "3.0.0",
            "last_sync": datetime.now().isoformat(),
            "health_status": self._health_status,
            "model_selector_status": get_intelligent_ouroboros_selector().get_selection_analytics(),
        }

        state_file = self._state_dir / "trinity_state.json"
        await asyncio.to_thread(
            state_file.write_text,
            json.dumps(state, indent=2)
        )

    async def _load_state(self) -> None:
        """Load persisted state from disk."""
        state_file = self._state_dir / "trinity_state.json"
        if state_file.exists():
            try:
                content = await asyncio.to_thread(state_file.read_text)
                state = json.loads(content)
                logger.debug(f"Loaded Trinity state from {state_file}")
            except Exception as e:
                logger.warning(f"Failed to load Trinity state: {e}")

    async def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status."""
        await self.check_all_health()

        selector = get_intelligent_ouroboros_selector()

        return {
            "coordinator": {
                "version": "3.0.0",
                "startup_complete": self._startup_complete,
                "state_dir": str(self._state_dir),
            },
            "repositories": self._health_status,
            "model_selection": await selector.get_health(),
            "event_subscribers": {
                event_type: len(subscribers)
                for event_type, subscribers in self._event_subscribers.items()
            },
        }

    async def graceful_shutdown(self) -> None:
        """Perform graceful shutdown of all components."""
        logger.info("🔻 Trinity Coordinator shutting down...")

        # Save final state
        await self.sync_state()

        # Publish shutdown event
        await self.publish_event("shutdown", {"reason": "graceful_shutdown"})

        self._shutdown_event.set()
        logger.info("✅ Trinity Coordinator shutdown complete")


# Global Trinity coordinator instance
_trinity_coordinator: Optional[TrinityCoordinator] = None


def get_trinity_coordinator() -> TrinityCoordinator:
    """Get global Trinity coordinator instance."""
    global _trinity_coordinator
    if _trinity_coordinator is None:
        _trinity_coordinator = TrinityCoordinator()
    return _trinity_coordinator


async def initialize_trinity() -> TrinityCoordinator:
    """Initialize and return the Trinity coordinator."""
    coordinator = get_trinity_coordinator()
    await coordinator.initialize()
    return coordinator


# =============================================================================
# CONFIGURATION
# =============================================================================

class IntegrationConfig:
    """Dynamic configuration for Ouroboros integration."""

    # LLM Providers (in fallback order)
    PROVIDERS = [
        {
            "name": "jarvis-prime",
            "api_base": os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1"),
            "api_key": os.getenv("JARVIS_PRIME_API_KEY", "sk-local-jarvis"),
            "model": os.getenv("JARVIS_PRIME_MODEL", "deepseek-coder-v2"),
            "timeout": 120.0,
        },
        {
            "name": "ollama",
            "api_base": os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1"),
            "api_key": "ollama",
            "model": os.getenv("OLLAMA_MODEL", "codellama"),
            "timeout": 180.0,
        },
        {
            "name": "anthropic",
            "api_base": "https://api.anthropic.com/v1",
            "api_key": os.getenv("ANTHROPIC_API_KEY", ""),
            "model": "claude-3-haiku-20240307",
            "timeout": 60.0,
        },
    ]

    # Circuit Breaker
    CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("OUROBOROS_CIRCUIT_THRESHOLD", "5"))
    CIRCUIT_BREAKER_TIMEOUT = float(os.getenv("OUROBOROS_CIRCUIT_TIMEOUT", "300.0"))

    # Retry
    MAX_RETRIES = int(os.getenv("OUROBOROS_MAX_RETRIES", "3"))
    RETRY_DELAY = float(os.getenv("OUROBOROS_RETRY_DELAY", "2.0"))

    # Safety
    SANDBOX_ENABLED = os.getenv("OUROBOROS_SANDBOX", "true").lower() == "true"
    HUMAN_REVIEW_ENABLED = os.getenv("OUROBOROS_HUMAN_REVIEW", "false").lower() == "true"

    # Reactor Core Integration
    REACTOR_EVENTS_DIR = Path(os.getenv("REACTOR_EVENTS_DIR", str(Path.home() / ".jarvis/reactor/events")))
    EXPERIENCE_PUBLISHING_ENABLED = os.getenv("OUROBOROS_PUBLISH_EXPERIENCES", "true").lower() == "true"


# =============================================================================
# ENUMS
# =============================================================================

class ProviderStatus(Enum):
    """Status of an LLM provider."""
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Tripped, rejecting requests
    HALF_OPEN = "half_open"  # Testing if recovered


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

@dataclass
class CircuitBreaker:
    """
    Circuit breaker for protecting against cascading failures.

    State machine:
    - CLOSED: Normal operation, tracking failures
    - OPEN: Too many failures, rejecting all requests
    - HALF_OPEN: Testing if the service has recovered
    """
    name: str
    threshold: int = IntegrationConfig.CIRCUIT_BREAKER_THRESHOLD
    timeout: float = IntegrationConfig.CIRCUIT_BREAKER_TIMEOUT

    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    successes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0

    def can_execute(self) -> bool:
        """Check if request can proceed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if timeout has elapsed
            if time.time() - self.last_failure_time >= self.timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info(f"Circuit breaker {self.name} transitioning to HALF_OPEN")
                return True
            return False

        # HALF_OPEN: allow one request to test
        return True

    def record_success(self) -> None:
        """Record a successful request."""
        self.successes += 1
        self.last_success_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            # Recovery confirmed
            self.state = CircuitState.CLOSED
            self.failures = 0
            logger.info(f"Circuit breaker {self.name} CLOSED (recovered)")

    def record_failure(self) -> None:
        """Record a failed request."""
        self.failures += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            # Still failing, reopen
            self.state = CircuitState.OPEN
            logger.warning(f"Circuit breaker {self.name} re-OPENED (still failing)")
        elif self.failures >= self.threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"Circuit breaker {self.name} OPENED after {self.failures} failures")

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failures,
            "successes": self.successes,
            "can_execute": self.can_execute(),
        }


# =============================================================================
# MULTI-PROVIDER LLM CLIENT
# =============================================================================

class MultiProviderLLMClient:
    """
    LLM client with intelligent model selection and automatic failover.

    v2.0 Enhancement: Uses IntelligentOuroborosModelSelector for dynamic
    model selection based on code complexity, task type, and availability.

    Falls back through providers in order:
    1. Intelligently selected model (based on code analysis)
    2. JARVIS Prime (local, free)
    3. Ollama (local, free)
    4. Anthropic API (cloud, paid)

    Each provider has its own circuit breaker for fault isolation.
    """

    def __init__(self):
        self._providers = IntegrationConfig.PROVIDERS
        self._circuit_breakers: Dict[str, CircuitBreaker] = {
            p["name"]: CircuitBreaker(name=p["name"])
            for p in self._providers
        }
        self._sessions: Dict[str, aiohttp.ClientSession] = {}
        self._provider_status: Dict[str, ProviderStatus] = {}
        self._lock = asyncio.Lock()
        self._intelligent_selector = get_intelligent_ouroboros_selector()
        self._last_model_selection: Optional[Dict[str, Any]] = None

    async def close(self) -> None:
        """Close all sessions."""
        for session in self._sessions.values():
            if not session.closed:
                await session.close()

    async def health_check(self) -> Dict[str, ProviderStatus]:
        """Check health of all providers."""
        results = {}

        for provider in self._providers:
            name = provider["name"]
            try:
                status = await self._check_provider_health(provider)
                results[name] = status
                self._provider_status[name] = status
            except Exception as e:
                results[name] = ProviderStatus.UNAVAILABLE
                self._provider_status[name] = ProviderStatus.UNAVAILABLE

        return results

    async def _check_provider_health(self, provider: Dict) -> ProviderStatus:
        """Check health of a single provider."""
        if not provider.get("api_key"):
            return ProviderStatus.UNAVAILABLE

        try:
            session = await self._get_session(provider["name"])
            url = f"{provider['api_base'].rstrip('/')}/models"

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return ProviderStatus.HEALTHY
                elif resp.status < 500:
                    return ProviderStatus.DEGRADED
                else:
                    return ProviderStatus.UNAVAILABLE
        except Exception:
            return ProviderStatus.UNAVAILABLE

    async def _get_session(self, provider_name: str) -> aiohttp.ClientSession:
        """Get or create session for provider."""
        if provider_name not in self._sessions or self._sessions[provider_name].closed:
            provider = next((p for p in self._providers if p["name"] == provider_name), None)
            if not provider:
                raise ValueError(f"Unknown provider: {provider_name}")

            headers = {"Content-Type": "application/json"}
            if provider.get("api_key"):
                headers["Authorization"] = f"Bearer {provider['api_key']}"

            self._sessions[provider_name] = aiohttp.ClientSession(headers=headers)

        return self._sessions[provider_name]

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        code_context: Optional[str] = None,
        task_type: str = "code_improvement",
        prefer_local: bool = True,
    ) -> Tuple[str, str]:
        """
        Generate response from best available provider using intelligent selection.

        v2.0 Enhancement: Uses IntelligentOuroborosModelSelector to dynamically
        select the best model based on code complexity and task requirements.

        Args:
            prompt: The prompt to send to the LLM
            system_prompt: Optional system prompt
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens in response
            code_context: Optional code for complexity analysis (enables intelligent selection)
            task_type: Type of task (code_improvement, refactoring, bug_fix)
            prefer_local: Prefer local models (JARVIS Prime, Ollama) over cloud APIs

        Returns:
            (response_text, provider_name)
        """
        async with self._lock:
            last_error = None
            providers_to_try = []

            # v2.0: Use intelligent model selection if code context provided
            if code_context:
                try:
                    selection = await self._intelligent_selector.select_model_for_code(
                        code=code_context,
                        task_type=task_type,
                        prefer_local=prefer_local,
                    )
                    self._last_model_selection = selection

                    logger.info(
                        f"Intelligent selection: {selection['provider']}/{selection['model']} "
                        f"(context: {selection.get('context_window', 'unknown')})"
                    )

                    # Build provider config from selection
                    selected_provider = {
                        "name": f"{selection['provider']}-intelligent",
                        "api_base": selection["api_base"],
                        "api_key": self._get_api_key_for_provider(selection["provider"]),
                        "model": selection["model"],
                        "timeout": 120.0,
                    }

                    # Ensure circuit breaker exists for dynamic provider
                    if selected_provider["name"] not in self._circuit_breakers:
                        self._circuit_breakers[selected_provider["name"]] = CircuitBreaker(
                            name=selected_provider["name"]
                        )

                    # Intelligently selected provider goes first
                    providers_to_try.append(selected_provider)

                except Exception as e:
                    logger.warning(f"Intelligent model selection failed: {e}")
                    self._last_model_selection = {"error": str(e)}

            # Add static providers as fallback
            providers_to_try.extend(self._providers)

            # Try each provider
            for provider in providers_to_try:
                name = provider["name"]

                # Get or create circuit breaker
                if name not in self._circuit_breakers:
                    self._circuit_breakers[name] = CircuitBreaker(name=name)
                circuit = self._circuit_breakers[name]

                # Skip if circuit breaker is open
                if not circuit.can_execute():
                    logger.debug(f"Skipping {name} (circuit open)")
                    continue

                # Skip if no API key
                if not provider.get("api_key"):
                    continue

                try:
                    response = await self._call_provider(
                        provider, prompt, system_prompt, temperature, max_tokens
                    )
                    circuit.record_success()
                    logger.info(f"Successfully generated from {name}")
                    return response, name

                except Exception as e:
                    circuit.record_failure()
                    last_error = e
                    logger.warning(f"Provider {name} failed: {e}")
                    continue

            # All providers failed
            raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    def _get_api_key_for_provider(self, provider_type: str) -> str:
        """Get API key for a provider type."""
        provider_keys = {
            "jarvis-prime": os.getenv("JARVIS_PRIME_API_KEY", "sk-local-jarvis"),
            "ollama": "ollama",
            "anthropic": os.getenv("ANTHROPIC_API_KEY", ""),
            "openai": os.getenv("OPENAI_API_KEY", ""),
        }
        return provider_keys.get(provider_type, "")

    def get_last_model_selection(self) -> Optional[Dict[str, Any]]:
        """Get details of the last intelligent model selection."""
        return self._last_model_selection

    async def _call_provider(
        self,
        provider: Dict,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call a specific provider."""
        session = await self._get_session(provider["name"])

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        url = f"{provider['api_base'].rstrip('/')}/chat/completions"

        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=provider.get("timeout", 120)),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"Provider error ({resp.status}): {error_text}")

            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    def get_status(self) -> Dict[str, Any]:
        return {
            "providers": {
                name: {
                    "status": self._provider_status.get(name, ProviderStatus.UNKNOWN).value,
                    "circuit_breaker": self._circuit_breakers[name].get_status(),
                }
                for name in [p["name"] for p in self._providers]
            },
        }


# =============================================================================
# SANDBOX EXECUTOR
# =============================================================================

class SandboxExecutor:
    """
    Executes code in a sandboxed environment for safety.

    Creates a temporary directory, copies code, runs tests,
    and only applies changes if tests pass.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path.home() / ".jarvis/ouroboros/sandbox"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def execute_in_sandbox(
        self,
        original_file: Path,
        modified_content: str,
        test_command: str,
    ) -> Tuple[bool, str]:
        """
        Execute modified code in sandbox.

        Args:
            original_file: Original file path
            modified_content: New content to test
            test_command: Command to validate

        Returns:
            (success, output)
        """
        sandbox_id = f"sandbox_{uuid.uuid4().hex[:8]}"
        sandbox_dir = self.base_dir / sandbox_id

        try:
            # Create sandbox directory
            sandbox_dir.mkdir(parents=True, exist_ok=True)

            # Copy project structure (minimal)
            await self._setup_sandbox(sandbox_dir, original_file, modified_content)

            # Run tests in sandbox
            result = await asyncio.create_subprocess_shell(
                test_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=sandbox_dir,
                env=self._get_sandbox_env(sandbox_dir),
            )

            try:
                stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=300)
            except asyncio.TimeoutError:
                result.kill()
                return False, "Sandbox execution timeout"

            output = stdout.decode() + stderr.decode()
            success = result.returncode == 0

            return success, output

        except Exception as e:
            return False, f"Sandbox error: {e}"

        finally:
            # Cleanup sandbox
            await self._cleanup_sandbox(sandbox_dir)

    async def _setup_sandbox(
        self,
        sandbox_dir: Path,
        original_file: Path,
        modified_content: str,
    ) -> None:
        """Setup sandbox with modified file."""
        # Create the modified file in sandbox
        relative_path = original_file.name
        sandbox_file = sandbox_dir / relative_path
        await asyncio.to_thread(sandbox_file.write_text, modified_content)

        # Copy necessary dependencies (requirements.txt, pyproject.toml)
        project_root = original_file.parent
        for dep_file in ["requirements.txt", "pyproject.toml", "setup.py"]:
            src = project_root / dep_file
            if src.exists():
                dst = sandbox_dir / dep_file
                content = await asyncio.to_thread(src.read_text)
                await asyncio.to_thread(dst.write_text, content)

    def _get_sandbox_env(self, sandbox_dir: Path) -> Dict[str, str]:
        """Get environment for sandbox execution."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(sandbox_dir)
        env["OUROBOROS_SANDBOX"] = "1"
        return env

    async def _cleanup_sandbox(self, sandbox_dir: Path) -> None:
        """Cleanup sandbox directory."""
        import shutil
        try:
            await asyncio.to_thread(shutil.rmtree, sandbox_dir, ignore_errors=True)
        except Exception:
            pass


# =============================================================================
# REACTOR CORE INTEGRATION
# =============================================================================

class ReactorCoreExperiencePublisher:
    """
    Publishes improvement experiences to Reactor Core for training.

    When Ouroboros successfully improves code, it publishes the experience
    as a training example for the model to learn from.
    """

    def __init__(self, events_dir: Path = IntegrationConfig.REACTOR_EVENTS_DIR):
        self.events_dir = events_dir
        self.events_dir.mkdir(parents=True, exist_ok=True)

    async def publish_improvement_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: List[str],
    ) -> Optional[str]:
        """
        Publish an improvement experience for training.

        Args:
            original_code: Original code before improvement
            improved_code: Code after improvement
            goal: Improvement goal
            success: Whether improvement succeeded
            iterations: Number of iterations taken
            error_history: List of errors encountered

        Returns:
            Event ID if published
        """
        if not IntegrationConfig.EXPERIENCE_PUBLISHING_ENABLED:
            return None

        event_id = f"ouroboros_exp_{uuid.uuid4().hex[:12]}"

        experience = {
            "event_id": event_id,
            "event_type": "ouroboros_improvement",
            "timestamp": time.time(),
            "source": "ouroboros",
            "payload": {
                "original_code": original_code[:5000],  # Truncate large code
                "improved_code": improved_code[:5000],
                "goal": goal,
                "success": success,
                "iterations": iterations,
                "error_patterns": error_history[-5:] if error_history else [],
            },
            "training_metadata": {
                "can_use_for_training": success,  # Only successful improvements
                "difficulty": self._estimate_difficulty(iterations, len(error_history)),
                "code_type": "python",
            },
        }

        # Write to events directory for Reactor Core to pick up
        event_file = self.events_dir / f"{event_id}.json"
        await asyncio.to_thread(
            event_file.write_text,
            json.dumps(experience, indent=2)
        )

        logger.info(f"Published improvement experience: {event_id}")
        return event_id

    def _estimate_difficulty(self, iterations: int, errors: int) -> str:
        """Estimate task difficulty for training prioritization."""
        if iterations == 1 and errors == 0:
            return "easy"
        elif iterations <= 3 and errors <= 2:
            return "medium"
        elif iterations <= 7:
            return "hard"
        else:
            return "very_hard"


# =============================================================================
# ENHANCED OUROBOROS INTEGRATION
# =============================================================================

class EnhancedOuroborosIntegration:
    """
    Enhanced integration layer for Ouroboros.

    Provides:
    - Multi-provider LLM with failover
    - Circuit breakers for fault isolation
    - Sandbox execution for safety
    - Reactor Core experience publishing
    - Health monitoring
    """

    def __init__(self):
        self._llm_client = MultiProviderLLMClient()
        self._sandbox = SandboxExecutor() if IntegrationConfig.SANDBOX_ENABLED else None
        self._experience_publisher = ReactorCoreExperiencePublisher()
        self._improvement_circuit = CircuitBreaker(name="improvement_loop", threshold=10)
        self._running = False
        self._metrics = {
            "improvements_attempted": 0,
            "improvements_succeeded": 0,
            "improvements_failed": 0,
            "experiences_published": 0,
        }

    async def initialize(self) -> bool:
        """Initialize the integration layer."""
        logger.info("Initializing Enhanced Ouroboros Integration...")

        # Health check providers
        provider_status = await self._llm_client.health_check()

        healthy_count = sum(1 for s in provider_status.values() if s == ProviderStatus.HEALTHY)
        logger.info(f"LLM providers: {healthy_count}/{len(provider_status)} healthy")

        for name, status in provider_status.items():
            logger.info(f"  - {name}: {status.value}")

        self._running = True
        return healthy_count > 0

    async def shutdown(self) -> None:
        """Shutdown the integration."""
        self._running = False
        await self._llm_client.close()
        logger.info("Enhanced Ouroboros Integration shutdown")

    async def generate_improvement(
        self,
        original_code: str,
        goal: str,
        error_log: Optional[str] = None,
        context: Optional[str] = None,
        task_type: str = "code_improvement",
        prefer_local: bool = True,
    ) -> Optional[str]:
        """
        Generate improved code using intelligently selected provider.

        v2.0 Enhancement: Uses IntelligentOuroborosModelSelector to dynamically
        select the best model based on code complexity and task requirements.

        Args:
            original_code: Original source code
            goal: Improvement goal
            error_log: Optional error from previous attempt
            context: Optional context from related files
            task_type: Type of task (code_improvement, refactoring, bug_fix)
            prefer_local: Prefer local models (JARVIS Prime, Ollama) over cloud APIs

        Returns:
            Improved code or None if all providers fail
        """
        if not self._improvement_circuit.can_execute():
            logger.warning("Improvement circuit breaker is OPEN, rejecting request")
            return None

        # v2.0: Analyze code complexity for model selection
        complexity = CodeComplexityAnalyzer.analyze(original_code)
        logger.info(
            f"Code complexity analysis: {complexity['line_count']} lines, "
            f"complexity={complexity['complexity']}, "
            f"required_context={complexity['required_context']}"
        )

        # Determine task type based on goal keywords
        detected_task = task_type
        goal_lower = goal.lower()
        if any(kw in goal_lower for kw in ["fix", "bug", "error", "issue"]):
            detected_task = "bug_fix"
        elif any(kw in goal_lower for kw in ["refactor", "clean", "reorganize", "restructure"]):
            detected_task = "refactoring"
        elif any(kw in goal_lower for kw in ["optimize", "performance", "speed", "memory"]):
            detected_task = "optimization"

        system_prompt = """You are an expert software engineer improving code.
Output ONLY the improved Python code, no explanations or markdown.
Maintain all existing functionality. Follow PEP 8."""

        prompt_parts = [
            f"## Original Code\n```python\n{original_code}\n```\n",
            f"\n## Goal\n{goal}\n",
        ]

        if error_log:
            prompt_parts.append(f"\n## Previous Error (fix this)\n```\n{error_log[:2000]}\n```\n")

        if context:
            prompt_parts.append(f"\n## Context\n{context[:3000]}\n")

        prompt_parts.append("\n## Output improved Python code only:\n")

        try:
            # v2.0: Pass code context for intelligent model selection
            response, provider = await self._llm_client.generate(
                prompt="".join(prompt_parts),
                system_prompt=system_prompt,
                temperature=0.3,
                code_context=original_code,  # Enable intelligent selection
                task_type=detected_task,
                prefer_local=prefer_local,
            )

            self._improvement_circuit.record_success()

            # Log model selection details
            selection = self._llm_client.get_last_model_selection()
            if selection and "error" not in selection:
                logger.info(
                    f"Model selection reasoning: {selection.get('reasoning', ['N/A'])[-1]}"
                )

            # Extract code from potential markdown
            code = self._extract_code(response)
            return code

        except Exception as e:
            self._improvement_circuit.record_failure()
            logger.error(f"Failed to generate improvement: {e}")
            return None

    def _extract_code(self, response: str) -> str:
        """Extract code from response."""
        import re
        code_block = re.search(r"```(?:python)?\s*([\s\S]*?)```", response)
        if code_block:
            return code_block.group(1).strip()
        return response.strip()

    async def validate_in_sandbox(
        self,
        original_file: Path,
        modified_content: str,
        test_command: str,
    ) -> Tuple[bool, str]:
        """
        Validate changes in sandbox before applying.

        Returns:
            (success, output)
        """
        if not self._sandbox:
            return True, "Sandbox disabled"

        return await self._sandbox.execute_in_sandbox(
            original_file, modified_content, test_command
        )

    async def publish_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: List[str],
    ) -> Optional[str]:
        """Publish improvement experience to Reactor Core."""
        event_id = await self._experience_publisher.publish_improvement_experience(
            original_code, improved_code, goal, success, iterations, error_history
        )

        if event_id:
            self._metrics["experiences_published"] += 1

        return event_id

    def record_improvement_attempt(self, success: bool) -> None:
        """Record an improvement attempt for metrics."""
        self._metrics["improvements_attempted"] += 1
        if success:
            self._metrics["improvements_succeeded"] += 1
        else:
            self._metrics["improvements_failed"] += 1

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "llm_client": self._llm_client.get_status(),
            "improvement_circuit": self._improvement_circuit.get_status(),
            "sandbox_enabled": self._sandbox is not None,
            "experience_publishing": IntegrationConfig.EXPERIENCE_PUBLISHING_ENABLED,
            "metrics": self._metrics,
            "intelligent_selection_available": INTELLIGENT_SELECTOR_AVAILABLE,
            "last_model_selection": self._llm_client.get_last_model_selection(),
        }

    async def get_intelligent_selector_health(self) -> Dict[str, Any]:
        """Get health status of intelligent model selection components."""
        selector = get_intelligent_ouroboros_selector()
        return await selector.get_health()


# =============================================================================
# AGENTIC LOOP ORCHESTRATOR
# =============================================================================

class AgenticTaskPriority(Enum):
    """Priority levels for agentic tasks."""
    CRITICAL = 1  # Security fixes, breaking bugs
    HIGH = 2      # User-requested improvements
    NORMAL = 3    # Scheduled improvements
    LOW = 4       # Background optimizations
    BACKGROUND = 5  # Opportunistic improvements


@dataclass
class AgenticTask:
    """A task for the agentic improvement loop."""
    task_id: str
    file_path: Path
    goal: str
    priority: AgenticTaskPriority
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    status: str = "pending"  # pending, running, completed, failed
    result: Optional[str] = None
    error: Optional[str] = None
    iterations: int = 0
    max_iterations: int = 10
    triggered_by: str = "manual"  # manual, voice, event, scheduled


class AgenticLoopOrchestrator:
    """
    v2.0: Orchestrates autonomous self-programming improvement loops.

    Features:
    - Priority-based task queue with async processing
    - Autonomous improvement cycles with intelligent model selection
    - Self-healing capabilities (auto-fix failures)
    - Voice command integration
    - Event-driven triggers from file watchers
    - Reactor Core experience publishing
    - Circuit breaker protection
    - Parallel task execution with concurrency limits

    The orchestrator runs continuously, processing improvement tasks
    from the queue and learning from each iteration.
    """

    def __init__(
        self,
        max_concurrent_tasks: int = 3,
        max_iterations_per_task: int = 10,
        idle_poll_interval: float = 5.0,
    ):
        self._integration = EnhancedOuroborosIntegration()
        self._task_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._active_tasks: Dict[str, AgenticTask] = {}
        self._completed_tasks: List[AgenticTask] = []
        self._failed_tasks: List[AgenticTask] = []

        self._max_concurrent = max_concurrent_tasks
        self._max_iterations = max_iterations_per_task
        self._poll_interval = idle_poll_interval

        self._running = False
        self._workers: List[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()

        # Event callbacks for extensibility
        self._on_task_complete: List[Callable] = []
        self._on_task_failed: List[Callable] = []
        self._on_improvement_generated: List[Callable] = []

        # Metrics
        self._metrics = {
            "tasks_queued": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "total_iterations": 0,
            "total_improvements_generated": 0,
        }

        logger.info(f"AgenticLoopOrchestrator initialized (max_concurrent={max_concurrent_tasks})")

    async def start(self) -> None:
        """Start the agentic loop orchestrator."""
        if self._running:
            logger.warning("AgenticLoopOrchestrator already running")
            return

        logger.info("Starting AgenticLoopOrchestrator...")

        # Initialize integration
        if not await self._integration.initialize():
            logger.error("Failed to initialize integration - no LLM providers available")
            return

        self._running = True
        self._shutdown_event.clear()

        # Start worker tasks
        for i in range(self._max_concurrent):
            worker = asyncio.create_task(self._worker_loop(i))
            self._workers.append(worker)

        logger.info(f"AgenticLoopOrchestrator started with {self._max_concurrent} workers")

    async def stop(self) -> None:
        """Stop the agentic loop orchestrator gracefully."""
        if not self._running:
            return

        logger.info("Stopping AgenticLoopOrchestrator...")
        self._running = False
        self._shutdown_event.set()

        # Wait for workers to finish
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

        await self._integration.shutdown()
        logger.info("AgenticLoopOrchestrator stopped")

    async def submit_task(
        self,
        file_path: Path,
        goal: str,
        priority: AgenticTaskPriority = AgenticTaskPriority.NORMAL,
        triggered_by: str = "manual",
        max_iterations: Optional[int] = None,
    ) -> str:
        """
        Submit a new improvement task to the queue.

        Args:
            file_path: Path to the file to improve
            goal: Improvement goal
            priority: Task priority
            triggered_by: What triggered this task (manual, voice, event, scheduled)
            max_iterations: Maximum improvement iterations

        Returns:
            Task ID
        """
        task_id = f"agentic_{uuid.uuid4().hex[:12]}"

        task = AgenticTask(
            task_id=task_id,
            file_path=file_path,
            goal=goal,
            priority=priority,
            triggered_by=triggered_by,
            max_iterations=max_iterations or self._max_iterations,
        )

        # Priority queue uses (priority_value, task) tuples
        await self._task_queue.put((priority.value, task))
        self._metrics["tasks_queued"] += 1

        logger.info(f"Submitted task {task_id}: {goal} (priority={priority.name})")
        return task_id

    async def submit_voice_command(
        self,
        command: str,
        target_file: Optional[str] = None,
    ) -> Optional[str]:
        """
        Process a voice command for code improvement.

        Voice command examples:
        - "Improve the error handling in cloud_sql_connection_manager"
        - "Optimize the performance of the orchestration engine"
        - "Fix the bug in the trinity bridge"
        - "Refactor the model selector for better readability"

        Args:
            command: The voice command
            target_file: Optional specific file to target

        Returns:
            Task ID if submitted, None if command not understood
        """
        command_lower = command.lower()

        # Parse command for goal and priority
        goal = command
        priority = AgenticTaskPriority.HIGH  # Voice commands are high priority

        # Detect task type from command
        if any(kw in command_lower for kw in ["fix", "bug", "error", "issue", "broken"]):
            priority = AgenticTaskPriority.CRITICAL
        elif any(kw in command_lower for kw in ["optimize", "performance", "speed"]):
            priority = AgenticTaskPriority.HIGH
        elif any(kw in command_lower for kw in ["refactor", "clean", "improve readability"]):
            priority = AgenticTaskPriority.NORMAL

        # Find target file
        file_path = None
        if target_file:
            file_path = Path(target_file)
        else:
            # Try to extract file name from command
            file_path = await self._infer_file_from_command(command)

        if not file_path or not file_path.exists():
            logger.warning(f"Could not find target file for voice command: {command}")
            return None

        return await self.submit_task(
            file_path=file_path,
            goal=goal,
            priority=priority,
            triggered_by="voice",
        )

    async def _infer_file_from_command(self, command: str) -> Optional[Path]:
        """Infer target file from voice command."""
        command_lower = command.lower()

        # Common file patterns (snake_case names mentioned in commands)
        file_patterns = {
            "connection manager": "cloud_sql_connection_manager.py",
            "orchestration engine": "orchestration_engine.py",
            "trinity bridge": "trinity_bridge.py",
            "reactor bridge": "reactor_bridge.py",
            "model selector": "model_selector.py",
            "integration": "integration.py",
            "cost sync": "cross_repo_cost_sync.py",
            "startup orchestrator": "cross_repo_startup_orchestrator.py",
        }

        for pattern, filename in file_patterns.items():
            if pattern in command_lower:
                # Search for file in backend directory
                backend_dir = Path(__file__).parent.parent.parent
                for match in backend_dir.rglob(filename):
                    return match

        return None

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker loop that processes tasks from the queue."""
        logger.debug(f"Worker {worker_id} started")

        while self._running:
            try:
                # Wait for task with timeout
                try:
                    priority_value, task = await asyncio.wait_for(
                        self._task_queue.get(),
                        timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    # No tasks, check if we should shutdown
                    if self._shutdown_event.is_set():
                        break
                    continue

                # Process the task
                async with self._lock:
                    self._active_tasks[task.task_id] = task

                task.status = "running"
                task.started_at = time.time()

                try:
                    await self._process_task(task, worker_id)
                except Exception as e:
                    task.status = "failed"
                    task.error = str(e)
                    logger.error(f"Worker {worker_id} task {task.task_id} failed: {e}")
                finally:
                    task.completed_at = time.time()

                    async with self._lock:
                        del self._active_tasks[task.task_id]

                        if task.status == "completed":
                            self._completed_tasks.append(task)
                            self._metrics["tasks_completed"] += 1
                            await self._trigger_callbacks(self._on_task_complete, task)
                        else:
                            self._failed_tasks.append(task)
                            self._metrics["tasks_failed"] += 1
                            await self._trigger_callbacks(self._on_task_failed, task)

                    self._task_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
                await asyncio.sleep(1)

        logger.debug(f"Worker {worker_id} stopped")

    async def _process_task(self, task: AgenticTask, worker_id: int) -> None:
        """Process a single improvement task."""
        logger.info(f"Worker {worker_id} processing task {task.task_id}: {task.goal}")

        # Read the original file
        if not task.file_path.exists():
            raise FileNotFoundError(f"File not found: {task.file_path}")

        original_code = await asyncio.to_thread(task.file_path.read_text)
        current_code = original_code
        error_log = None

        # Improvement loop
        for iteration in range(task.max_iterations):
            task.iterations = iteration + 1
            self._metrics["total_iterations"] += 1

            logger.debug(f"Task {task.task_id} iteration {iteration + 1}/{task.max_iterations}")

            # Generate improvement
            improved_code = await self._integration.generate_improvement(
                original_code=current_code,
                goal=task.goal,
                error_log=error_log,
            )

            if not improved_code:
                logger.warning(f"Task {task.task_id}: No improvement generated")
                continue

            self._metrics["total_improvements_generated"] += 1
            await self._trigger_callbacks(self._on_improvement_generated, task, improved_code)

            # Validate in sandbox if enabled
            if self._integration._sandbox:
                success, output = await self._integration.validate_in_sandbox(
                    original_file=task.file_path,
                    modified_content=improved_code,
                    test_command=f"python -m py_compile {task.file_path.name}",
                )

                if not success:
                    error_log = output
                    current_code = improved_code  # Try to fix the error
                    continue

            # Success - apply the improvement
            await asyncio.to_thread(task.file_path.write_text, improved_code)

            task.status = "completed"
            task.result = f"Improved after {iteration + 1} iterations"

            # Publish experience to Reactor Core
            await self._integration.publish_experience(
                original_code=original_code,
                improved_code=improved_code,
                goal=task.goal,
                success=True,
                iterations=iteration + 1,
                error_history=[error_log] if error_log else [],
            )

            logger.info(f"Task {task.task_id} completed successfully")
            return

        # Max iterations reached without success
        task.status = "failed"
        task.error = f"Failed after {task.max_iterations} iterations"

        # Publish failed experience for learning
        await self._integration.publish_experience(
            original_code=original_code,
            improved_code=current_code,
            goal=task.goal,
            success=False,
            iterations=task.max_iterations,
            error_history=[error_log] if error_log else [],
        )

    async def _trigger_callbacks(
        self,
        callbacks: List[Callable],
        *args,
        **kwargs,
    ) -> None:
        """Trigger registered callbacks."""
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(*args, **kwargs)
                else:
                    callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def on_task_complete(self, callback: Callable) -> None:
        """Register callback for task completion."""
        self._on_task_complete.append(callback)

    def on_task_failed(self, callback: Callable) -> None:
        """Register callback for task failure."""
        self._on_task_failed.append(callback)

    def on_improvement_generated(self, callback: Callable) -> None:
        """Register callback for improvement generation."""
        self._on_improvement_generated.append(callback)

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific task."""
        # Check active tasks
        if task_id in self._active_tasks:
            task = self._active_tasks[task_id]
            return self._task_to_dict(task)

        # Check completed tasks
        for task in self._completed_tasks:
            if task.task_id == task_id:
                return self._task_to_dict(task)

        # Check failed tasks
        for task in self._failed_tasks:
            if task.task_id == task_id:
                return self._task_to_dict(task)

        return None

    def _task_to_dict(self, task: AgenticTask) -> Dict[str, Any]:
        """Convert task to dictionary."""
        return {
            "task_id": task.task_id,
            "file_path": str(task.file_path),
            "goal": task.goal,
            "priority": task.priority.name,
            "status": task.status,
            "iterations": task.iterations,
            "max_iterations": task.max_iterations,
            "triggered_by": task.triggered_by,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "result": task.result,
            "error": task.error,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status."""
        return {
            "running": self._running,
            "workers": len(self._workers),
            "queue_size": self._task_queue.qsize(),
            "active_tasks": len(self._active_tasks),
            "completed_tasks": len(self._completed_tasks),
            "failed_tasks": len(self._failed_tasks),
            "metrics": self._metrics,
            "integration": self._integration.get_status(),
        }


# =============================================================================
# GLOBAL INSTANCES
# =============================================================================

_integration: Optional[EnhancedOuroborosIntegration] = None
_orchestrator: Optional[AgenticLoopOrchestrator] = None


def get_ouroboros_integration() -> EnhancedOuroborosIntegration:
    """Get global Ouroboros integration instance."""
    global _integration
    if _integration is None:
        _integration = EnhancedOuroborosIntegration()
    return _integration


def get_agentic_orchestrator() -> AgenticLoopOrchestrator:
    """Get global agentic loop orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AgenticLoopOrchestrator()
    return _orchestrator


async def shutdown_ouroboros_integration() -> None:
    """Shutdown global integration and orchestrator."""
    global _integration, _orchestrator

    if _orchestrator:
        await _orchestrator.stop()
        _orchestrator = None

    if _integration:
        await _integration.shutdown()
        _integration = None


# =============================================================================
# v4.0: AUTONOMOUS SELF-PROGRAMMING INTEGRATION
# =============================================================================
# These functions integrate the autonomous components from native_integration.py
# with the AgenticLoopOrchestrator for true autonomous self-programming.
# =============================================================================

_autonomous_initialized = False


async def initialize_autonomous_self_programming_full(
    start_loops: bool = True,
) -> Dict[str, Any]:
    """
    Initialize the complete autonomous self-programming system.

    This connects:
    - GoalDecompositionEngine (breaks down goals)
    - TechnicalDebtDetector (finds issues)
    - AutonomousSelfRefinementLoop (continuous improvement)
    - DualAgentSystem (architect/reviewer)
    - CodeMemoryRAG (Oracle + ChromaDB)
    - SystemFeedbackLoop (metrics-driven optimization)
    - AutoTestGenerator (test generation)

    With:
    - AgenticLoopOrchestrator (task execution)
    - Oracle (code knowledge graph)
    - LLM clients (for intelligent decisions)

    Args:
        start_loops: Whether to start background improvement loops

    Returns:
        Dictionary with all initialized components
    """
    global _autonomous_initialized

    if _autonomous_initialized:
        logger.info("Autonomous self-programming already initialized")
        return {}

    logger.info("Initializing autonomous self-programming integration...")

    # Get or create the orchestrator
    orchestrator = get_agentic_orchestrator()

    # Try to import and get the Oracle
    oracle = None
    try:
        from backend.core.ouroboros.oracle import get_oracle
        oracle = get_oracle()
    except ImportError:
        logger.warning("Oracle not available - some features will be limited")
    except Exception as e:
        logger.warning(f"Failed to get Oracle: {e}")

    # Try to get LLM client from integration
    llm_client = None
    integration = get_ouroboros_integration()
    if hasattr(integration, '_llm_client'):
        llm_client = integration._llm_client

    # Try to get ChromaDB client
    chromadb_client = None
    try:
        from backend.autonomy.trinity_knowledge_indexer import get_knowledge_indexer
        indexer = await get_knowledge_indexer()
        if hasattr(indexer, '_chroma_client'):
            chromadb_client = indexer._chroma_client
    except Exception:
        logger.debug("ChromaDB not available")

    # Import autonomous components from native_integration
    try:
        from backend.core.ouroboros.native_integration import (
            initialize_autonomous_self_programming,
            get_goal_decomposer,
            get_debt_detector,
            get_refinement_loop,
            get_dual_agent_system,
            get_code_memory_rag,
            get_system_feedback_loop,
            get_auto_test_generator,
        )

        # Initialize all components
        components = await initialize_autonomous_self_programming(
            oracle=oracle,
            orchestrator=orchestrator,
            llm_client=llm_client,
            chromadb_client=chromadb_client,
            start_loops=start_loops,
        )

        _autonomous_initialized = True
        logger.info(f"✅ Autonomous self-programming initialized with {len(components)} components")

        # Log status
        for name, component in components.items():
            if hasattr(component, 'get_status'):
                status = component.get_status()
                logger.debug(f"  {name}: {status}")

        return components

    except ImportError as e:
        logger.error(f"Failed to import autonomous components: {e}")
        return {}
    except Exception as e:
        logger.error(f"Failed to initialize autonomous self-programming: {e}")
        return {}


async def shutdown_autonomous_self_programming_full() -> None:
    """Shutdown all autonomous self-programming components."""
    global _autonomous_initialized

    if not _autonomous_initialized:
        return

    try:
        from backend.core.ouroboros.native_integration import (
            shutdown_autonomous_self_programming,
        )
        await shutdown_autonomous_self_programming()
        _autonomous_initialized = False
        logger.info("Autonomous self-programming shutdown complete")
    except Exception as e:
        logger.error(f"Error during autonomous shutdown: {e}")


async def start_full_jarvis_system(
    enable_autonomous: bool = True,
    start_autonomous_loops: bool = False,  # Default to False for safety
) -> Dict[str, Any]:
    """
    Start the complete JARVIS self-programming system.

    This is the master initialization that starts:
    1. OuroborosIntegration (LLM providers, model selection)
    2. AgenticLoopOrchestrator (task queue, workers)
    3. Autonomous Self-Programming (if enabled)

    Args:
        enable_autonomous: Enable autonomous self-programming components
        start_autonomous_loops: Start background improvement loops

    Returns:
        Dictionary with system status and components
    """
    result = {
        "integration": None,
        "orchestrator": None,
        "autonomous": None,
        "status": "starting",
    }

    try:
        # 1. Initialize integration
        integration = get_ouroboros_integration()
        if not await integration.initialize():
            logger.error("Failed to initialize OuroborosIntegration")
            result["status"] = "integration_failed"
            return result
        result["integration"] = integration

        # 2. Start orchestrator
        orchestrator = get_agentic_orchestrator()
        await orchestrator.start()
        result["orchestrator"] = orchestrator

        # 3. Initialize autonomous self-programming
        if enable_autonomous:
            autonomous = await initialize_autonomous_self_programming_full(
                start_loops=start_autonomous_loops,
            )
            result["autonomous"] = autonomous

        result["status"] = "running"
        logger.info("✅ Full JARVIS system started successfully")

        return result

    except Exception as e:
        logger.error(f"Failed to start full JARVIS system: {e}")
        result["status"] = f"error: {e}"
        return result


async def stop_full_jarvis_system() -> None:
    """Stop the complete JARVIS self-programming system."""
    logger.info("Stopping full JARVIS system...")

    try:
        # 1. Stop autonomous self-programming
        await shutdown_autonomous_self_programming_full()

        # 2. Stop orchestrator and integration
        await shutdown_ouroboros_integration()

        logger.info("✅ Full JARVIS system stopped")
    except Exception as e:
        logger.error(f"Error stopping JARVIS system: {e}")
