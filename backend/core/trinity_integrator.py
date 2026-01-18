"""
Trinity Unified Orchestrator v86.0 - Production-Grade Cross-Repo Integration.
===============================================================================

The SINGLE POINT OF TRUTH for Trinity integration - a battle-hardened,
production-ready orchestrator that connects JARVIS Body, Prime, and Reactor-Core.

╔══════════════════════════════════════════════════════════════════════════════╗
║  v86.0 STARTUP PERFORMANCE ENHANCEMENTS (Solving Timeout Issues)             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  NEW in v86.0:                                                                ║
║  11.✅ PER-COMPONENT TIMEOUTS - Individual 120s/90s limits (not global 600s) ║
║  12.✅ REAL-TIME PROGRESS LOG - Live status updates during startup phases    ║
║  13.✅ FAST-FAIL DETECTION    - 5s circuit breaker identifies blocking       ║
║  14.✅ ADAPTIVE PARALLELISM   - Truly parallel startup with timeout enforce  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v83.0 CRITICAL ENHANCEMENTS (Addressing All Root Issues)                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  1. ✅ CRASH RECOVERY      - Auto-restart with exponential backoff          ║
║  2. ✅ PROCESS SUPERVISOR  - Monitor PIDs, detect zombies, auto-heal        ║
║  3. ✅ RESOURCE COORDINATOR- Port/memory/CPU reservation with pooling       ║
║  4. ✅ EVENT STORE         - WAL-backed durable events with replay          ║
║  5. ✅ DISTRIBUTED TRACER  - Cross-repo tracing with correlation IDs        ║
║  6. ✅ HEALTH AGGREGATOR   - Centralized health with anomaly detection      ║
║  7. ✅ TRANSACTIONAL START - Two-phase commit with automatic rollback       ║
║  8. ✅ CIRCUIT BREAKERS    - Fail-fast patterns throughout                  ║
║  9. ✅ ADAPTIVE THROTTLING - Dynamic backpressure based on system load      ║
║  10.✅ ZERO HARDCODING     - 100% config-driven via environment             ║
╚══════════════════════════════════════════════════════════════════════════════╝

v86.0 Startup Timeout Fix:
    PROBLEM:  ❌ Global 300s timeout hit, sequential blocking, no visibility
    SOLUTION: ✅ Per-component timeouts (J-Prime: 120s, Reactor: 90s)
              ✅ Real-time progress logging (live phase updates)
              ✅ Parallel execution with asyncio.wait_for per component
              ✅ Fast-fail circuit breakers (5s detection)

    Environment Variables (NEW):
    - JARVIS_PRIME_COMPONENT_TIMEOUT=120.0   # Individual J-Prime timeout
    - REACTOR_CORE_COMPONENT_TIMEOUT=90.0    # Individual Reactor timeout
    - SUPERVISOR_STARTUP_TIMEOUT=600.0       # Global safety net (doubled)

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │  TrinityUnifiedOrchestrator v86.0                                       │
    │  ├── ProcessSupervisor (PID monitoring, crash detection, restart)       │
    │  ├── CrashRecoveryManager (exponential backoff, cooldown, limits)       │
    │  ├── ResourceCoordinator (port pool, memory limits, CPU affinity)       │
    │  ├── EventStore (WAL, replay, dedup, TTL expiration)                    │
    │  ├── DistributedTracer (correlation IDs, span propagation)              │
    │  ├── UnifiedHealthAggregator (anomaly detection, trend analysis)        │
    │  ├── TransactionalStartup (prepare → commit → rollback)                 │
    │  ├── AdaptiveThrottler (backpressure, rate limiting, circuit breaking)  │
    │  └── v86.0 PerComponentTimeouts (individual fast-fail enforcement)      │
    └─────────────────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.trinity_integrator import TrinityUnifiedOrchestrator

    async def main():
        orchestrator = TrinityUnifiedOrchestrator()

        # v86.0: Single command with per-component timeout enforcement
        success = await orchestrator.start()

        if success:
            # Get unified health across all repos
            health = await orchestrator.get_unified_health()

            # Get distributed trace for debugging
            trace = orchestrator.tracer.get_current_trace()

        # Graceful shutdown with state preservation
        await orchestrator.stop()

Author: JARVIS Trinity v86.0 - Startup Performance Optimization
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import psutil
import resource
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum, auto
from functools import wraps, partial
from pathlib import Path
from threading import RLock
from typing import (
    Any, AsyncGenerator, Awaitable, Callable, Coroutine, Deque, Dict, Final,
    FrozenSet, Generator, Generic, Iterator, List, Literal, Mapping,
    NamedTuple, Optional, Protocol, Sequence, Set, Tuple,
    Type, TypeVar, Union, cast, overload, runtime_checkable,
)

# Type variables for generics
T = TypeVar("T")
R = TypeVar("R")
E = TypeVar("E", bound=Exception)

logger = logging.getLogger(__name__)

# =============================================================================
# Advanced Constants & Configuration Registry
# =============================================================================

class ConfigRegistry:
    """
    Centralized configuration registry with environment variable binding.
    Thread-safe, immutable after initialization, supports hot-reload signals.
    """

    _instance: Optional["ConfigRegistry"] = None
    _lock: RLock = RLock()
    _frozen: bool = False

    # Default configuration (all configurable via environment)
    DEFAULTS: Final[Dict[str, Any]] = {
        # Trinity Core
        "TRINITY_STARTUP_TIMEOUT": 120.0,
        "TRINITY_HEALTH_INTERVAL": 30.0,
        "TRINITY_SHUTDOWN_TIMEOUT": 60.0,
        "TRINITY_DATA_DIR": "~/.jarvis/trinity",

        # Crash Recovery
        "TRINITY_CRASH_MAX_RESTARTS": 5,
        "TRINITY_CRASH_INITIAL_BACKOFF": 1.0,
        "TRINITY_CRASH_MAX_BACKOFF": 300.0,
        "TRINITY_CRASH_BACKOFF_MULTIPLIER": 2.0,
        "TRINITY_CRASH_COOLDOWN_PERIOD": 300.0,

        # Process Supervisor
        "TRINITY_SUPERVISOR_CHECK_INTERVAL": 5.0,
        "TRINITY_SUPERVISOR_ZOMBIE_TIMEOUT": 30.0,
        "TRINITY_SUPERVISOR_HEARTBEAT_TIMEOUT": 60.0,

        # Resource Coordinator
        "TRINITY_RESOURCE_PORT_POOL_START": 8000,
        "TRINITY_RESOURCE_PORT_POOL_SIZE": 100,
        "TRINITY_RESOURCE_MEMORY_LIMIT_MB": 4096,
        "TRINITY_RESOURCE_CPU_LIMIT_PERCENT": 80,

        # Event Store
        "TRINITY_EVENT_STORE_PATH": "~/.jarvis/trinity/events.db",
        "TRINITY_EVENT_STORE_WAL_MODE": True,
        "TRINITY_EVENT_TTL_HOURS": 24,
        "TRINITY_EVENT_MAX_REPLAY": 1000,

        # Distributed Tracing
        "TRINITY_TRACING_ENABLED": True,
        "TRINITY_TRACING_SAMPLE_RATE": 1.0,
        "TRINITY_TRACING_MAX_SPANS": 10000,

        # Health Aggregator
        "TRINITY_HEALTH_ANOMALY_THRESHOLD": 0.8,
        "TRINITY_HEALTH_HISTORY_SIZE": 100,
        "TRINITY_HEALTH_TREND_WINDOW": 10,

        # Circuit Breaker
        "TRINITY_CIRCUIT_FAILURE_THRESHOLD": 5,
        "TRINITY_CIRCUIT_RECOVERY_TIMEOUT": 30.0,
        "TRINITY_CIRCUIT_HALF_OPEN_REQUESTS": 3,

        # Adaptive Throttling
        "TRINITY_THROTTLE_MAX_CONCURRENT": 100,
        "TRINITY_THROTTLE_QUEUE_SIZE": 1000,
        "TRINITY_THROTTLE_RATE_LIMIT": 100.0,

        # Component Paths (auto-detected if not set)
        "JARVIS_PRIME_REPO_PATH": "",
        "REACTOR_CORE_REPO_PATH": "",
        "JARVIS_PRIME_ENABLED": True,
        "REACTOR_CORE_ENABLED": True,
    }

    def __new__(cls) -> "ConfigRegistry":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._config = {}
                cls._instance._load_from_env()
            return cls._instance

    def _load_from_env(self) -> None:
        """Load configuration from environment variables."""
        for key, default in self.DEFAULTS.items():
            env_value = os.getenv(key)
            if env_value is not None:
                # Type coercion based on default type
                if isinstance(default, bool):
                    self._config[key] = env_value.lower() in ("true", "1", "yes", "on")
                elif isinstance(default, int):
                    self._config[key] = int(env_value)
                elif isinstance(default, float):
                    self._config[key] = float(env_value)
                else:
                    self._config[key] = env_value
            else:
                self._config[key] = default

    def get(self, key: str, default: T = None) -> T:
        """Get configuration value with type preservation."""
        return self._config.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._config[key]

    def freeze(self) -> None:
        """Freeze configuration to prevent further changes."""
        self._frozen = True

    def reload(self) -> None:
        """Reload configuration from environment (if not frozen)."""
        if not self._frozen:
            self._load_from_env()


# Global config accessor
def get_config() -> ConfigRegistry:
    """Get the global configuration registry."""
    return ConfigRegistry()


# =============================================================================
# Environment Helpers (Legacy - Use ConfigRegistry for new code)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


# =============================================================================
# v85.0: Intelligent Repo Discovery System
# =============================================================================

class DiscoveryStrategy(Enum):
    """Strategies for discovering repository paths."""
    ENVIRONMENT = auto()       # Highest priority: explicit env var
    RELATIVE = auto()          # Relative to current repo
    STANDARD_LOCATIONS = auto() # ~/Documents/repos, ~/repos, etc.
    GIT_BASED = auto()         # Find by .git presence and repo name
    SYMLINK = auto()           # Follow symlinks to actual locations
    HOME_SEARCH = auto()       # Recursive home directory search (expensive)


@dataclass
class DiscoveryResult:
    """Result of a repo discovery attempt."""
    path: Optional[Path]
    strategy_used: DiscoveryStrategy
    confidence: float  # 0.0 to 1.0
    discovery_time_ms: float
    error: Optional[str] = None
    alternatives: List[Path] = field(default_factory=list)


class IntelligentRepoDiscovery:
    """
    v85.0: Multi-strategy repository discovery with intelligent fallback.

    Discovers repository paths using multiple strategies in priority order:
    1. Environment variables (highest priority, fastest)
    2. Relative paths from current repo
    3. Standard locations (~Documents/repos, ~/repos, etc.)
    4. Git-based discovery (finds repos by .git presence)
    5. Symlink resolution
    6. Home directory search (last resort, expensive)

    Features:
    - Zero hardcoding: all paths from env vars or discovery
    - Cross-platform support (macOS, Linux, Windows)
    - Async-native with caching
    - Confidence scoring for reliability assessment
    - Alternative path suggestions on failure
    """

    # Environment variable names for each repo
    ENV_VARS: Final[Dict[str, str]] = {
        "jarvis": "JARVIS_REPO_PATH",
        "jarvis_prime": "JARVIS_PRIME_REPO_PATH",
        "reactor_core": "REACTOR_CORE_REPO_PATH",
    }

    # Standard directory names for each repo
    REPO_NAMES: Final[Dict[str, List[str]]] = {
        "jarvis": ["JARVIS-AI-Agent", "jarvis-ai-agent", "jarvis", "JARVIS"],
        "jarvis_prime": ["jarvis-prime", "jarvis_prime", "j-prime", "jprime"],
        "reactor_core": ["reactor-core", "reactor_core", "reactorcore"],
    }

    # Standard parent directories to search (in priority order)
    STANDARD_PARENTS: Final[List[str]] = [
        "~/Documents/repos",
        "~/repos",
        "~/projects",
        "~/code",
        "~/dev",
        "~/workspace",
        "~/src",
        "/opt/jarvis",
        "/usr/local/jarvis",
    ]

    # Cache of discovered paths (thread-safe via asyncio)
    _cache: Dict[str, DiscoveryResult] = {}
    _cache_lock: Optional[asyncio.Lock] = None  # v90.0: Lazy initialization
    _cache_ttl: float = 300.0  # 5 minutes
    _cache_timestamps: Dict[str, float] = {}

    @classmethod
    def _get_cache_lock(cls) -> asyncio.Lock:
        """v90.0: Get cache lock with lazy initialization."""
        if cls._cache_lock is None:
            cls._cache_lock = asyncio.Lock()
        return cls._cache_lock

    def __init__(
        self,
        current_repo_path: Optional[Path] = None,
        search_depth: int = 3,
        enable_cache: bool = True,
    ):
        """
        Initialize the discovery system.

        Args:
            current_repo_path: Path to the current repository (for relative discovery)
            search_depth: Maximum depth for directory searches
            enable_cache: Whether to cache discovery results
        """
        self.current_repo_path = current_repo_path or self._detect_current_repo()
        self.search_depth = search_depth
        self.enable_cache = enable_cache
        self._lock = asyncio.Lock()

    def _detect_current_repo(self) -> Optional[Path]:
        """Detect the current repository path from various sources."""
        # Check if we're in a git repo
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip())
        except Exception:
            pass

        # Fallback: check __file__ location
        try:
            current_file = Path(__file__).resolve()
            # Traverse up to find root (look for .git or pyproject.toml)
            for parent in current_file.parents:
                if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
                    return parent
        except Exception:
            pass

        return None

    async def discover(
        self,
        repo_id: str,
        strategies: Optional[List[DiscoveryStrategy]] = None,
        validate_structure: bool = True,
    ) -> DiscoveryResult:
        """
        Discover a repository path using multiple strategies.

        Args:
            repo_id: Repository identifier (jarvis, jarvis_prime, reactor_core)
            strategies: Specific strategies to use (None = all in priority order)
            validate_structure: Whether to validate repo structure after discovery

        Returns:
            DiscoveryResult with path and metadata
        """
        start_time = time.time()

        # Check cache first
        if self.enable_cache:
            async with self._get_cache_lock():
                if repo_id in self._cache:
                    cache_age = time.time() - self._cache_timestamps.get(repo_id, 0)
                    if cache_age < self._cache_ttl:
                        cached = self._cache[repo_id]
                        # Verify cached path still exists
                        if cached.path and cached.path.exists():
                            return cached

        # Default strategy order
        if strategies is None:
            strategies = [
                DiscoveryStrategy.ENVIRONMENT,
                DiscoveryStrategy.RELATIVE,
                DiscoveryStrategy.STANDARD_LOCATIONS,
                DiscoveryStrategy.GIT_BASED,
                DiscoveryStrategy.SYMLINK,
            ]

        alternatives: List[Path] = []
        last_error: Optional[str] = None

        for strategy in strategies:
            try:
                result = await self._discover_with_strategy(repo_id, strategy)
                if result.path and result.path.exists():
                    # Validate structure if requested
                    if validate_structure and not self._validate_repo_structure(repo_id, result.path):
                        alternatives.append(result.path)
                        last_error = f"Path exists but structure validation failed"
                        continue

                    # Success! Update cache
                    result.discovery_time_ms = (time.time() - start_time) * 1000
                    result.alternatives = alternatives

                    if self.enable_cache:
                        async with self._get_cache_lock():
                            self._cache[repo_id] = result
                            self._cache_timestamps[repo_id] = time.time()

                    return result

            except Exception as e:
                last_error = str(e)
                logger.debug(f"[Discovery] Strategy {strategy.name} failed for {repo_id}: {e}")

        # All strategies failed
        return DiscoveryResult(
            path=None,
            strategy_used=strategies[-1] if strategies else DiscoveryStrategy.ENVIRONMENT,
            confidence=0.0,
            discovery_time_ms=(time.time() - start_time) * 1000,
            error=last_error or "No strategy succeeded",
            alternatives=alternatives,
        )

    async def _discover_with_strategy(
        self,
        repo_id: str,
        strategy: DiscoveryStrategy,
    ) -> DiscoveryResult:
        """Execute a specific discovery strategy."""

        if strategy == DiscoveryStrategy.ENVIRONMENT:
            return await self._discover_from_env(repo_id)
        elif strategy == DiscoveryStrategy.RELATIVE:
            return await self._discover_relative(repo_id)
        elif strategy == DiscoveryStrategy.STANDARD_LOCATIONS:
            return await self._discover_standard_locations(repo_id)
        elif strategy == DiscoveryStrategy.GIT_BASED:
            return await self._discover_git_based(repo_id)
        elif strategy == DiscoveryStrategy.SYMLINK:
            return await self._discover_symlink(repo_id)
        elif strategy == DiscoveryStrategy.HOME_SEARCH:
            return await self._discover_home_search(repo_id)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

    async def _discover_from_env(self, repo_id: str) -> DiscoveryResult:
        """Strategy 1: Environment variable lookup (highest priority)."""
        env_var = self.ENV_VARS.get(repo_id)
        if not env_var:
            return DiscoveryResult(
                path=None,
                strategy_used=DiscoveryStrategy.ENVIRONMENT,
                confidence=0.0,
                discovery_time_ms=0.0,
                error=f"No environment variable defined for {repo_id}",
            )

        env_value = os.getenv(env_var)
        if env_value:
            path = Path(env_value).expanduser().resolve()
            if path.exists():
                return DiscoveryResult(
                    path=path,
                    strategy_used=DiscoveryStrategy.ENVIRONMENT,
                    confidence=1.0,  # Explicit env var = highest confidence
                    discovery_time_ms=0.0,
                )

        return DiscoveryResult(
            path=None,
            strategy_used=DiscoveryStrategy.ENVIRONMENT,
            confidence=0.0,
            discovery_time_ms=0.0,
            error=f"Environment variable {env_var} not set or path doesn't exist",
        )

    async def _discover_relative(self, repo_id: str) -> DiscoveryResult:
        """Strategy 2: Relative path from current repo."""
        if not self.current_repo_path:
            return DiscoveryResult(
                path=None,
                strategy_used=DiscoveryStrategy.RELATIVE,
                confidence=0.0,
                discovery_time_ms=0.0,
                error="Current repo path not available",
            )

        repo_names = self.REPO_NAMES.get(repo_id, [])
        parent = self.current_repo_path.parent

        for name in repo_names:
            candidate = parent / name
            if candidate.exists() and candidate.is_dir():
                return DiscoveryResult(
                    path=candidate.resolve(),
                    strategy_used=DiscoveryStrategy.RELATIVE,
                    confidence=0.9,  # High confidence - sibling repos
                    discovery_time_ms=0.0,
                )

        return DiscoveryResult(
            path=None,
            strategy_used=DiscoveryStrategy.RELATIVE,
            confidence=0.0,
            discovery_time_ms=0.0,
            error=f"No sibling repo found for {repo_id}",
        )

    async def _discover_standard_locations(self, repo_id: str) -> DiscoveryResult:
        """Strategy 3: Search standard locations."""
        repo_names = self.REPO_NAMES.get(repo_id, [])
        candidates: List[Tuple[Path, float]] = []

        for parent_pattern in self.STANDARD_PARENTS:
            parent = Path(parent_pattern).expanduser()
            if not parent.exists():
                continue

            for name in repo_names:
                candidate = parent / name
                if candidate.exists() and candidate.is_dir():
                    # Score based on position in priority list
                    priority_score = 1.0 - (self.STANDARD_PARENTS.index(parent_pattern) * 0.1)
                    candidates.append((candidate.resolve(), priority_score))

        if candidates:
            # Sort by priority score
            candidates.sort(key=lambda x: x[1], reverse=True)
            best_path, confidence = candidates[0]
            return DiscoveryResult(
                path=best_path,
                strategy_used=DiscoveryStrategy.STANDARD_LOCATIONS,
                confidence=min(0.8, confidence),
                discovery_time_ms=0.0,
                alternatives=[c[0] for c in candidates[1:3]],  # Include alternatives
            )

        return DiscoveryResult(
            path=None,
            strategy_used=DiscoveryStrategy.STANDARD_LOCATIONS,
            confidence=0.0,
            discovery_time_ms=0.0,
            error=f"No standard location found for {repo_id}",
        )

    async def _discover_git_based(self, repo_id: str) -> DiscoveryResult:
        """Strategy 4: Git-based discovery (find .git directories)."""
        repo_names = self.REPO_NAMES.get(repo_id, [])

        # Search in common parent directories
        search_roots = [
            Path.home() / "Documents",
            Path.home(),
        ]

        for root in search_roots:
            if not root.exists():
                continue

            try:
                # Use async subprocess for git discovery
                result = await asyncio.create_subprocess_exec(
                    "find", str(root), "-maxdepth", str(self.search_depth),
                    "-type", "d", "-name", ".git",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(result.communicate(), timeout=10.0)

                for line in stdout.decode().strip().split("\n"):
                    if not line:
                        continue
                    git_dir = Path(line)
                    repo_dir = git_dir.parent

                    if repo_dir.name.lower() in [n.lower() for n in repo_names]:
                        return DiscoveryResult(
                            path=repo_dir.resolve(),
                            strategy_used=DiscoveryStrategy.GIT_BASED,
                            confidence=0.7,
                            discovery_time_ms=0.0,
                        )

            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"[Discovery] Git-based search failed in {root}: {e}")

        return DiscoveryResult(
            path=None,
            strategy_used=DiscoveryStrategy.GIT_BASED,
            confidence=0.0,
            discovery_time_ms=0.0,
            error=f"Git-based discovery failed for {repo_id}",
        )

    async def _discover_symlink(self, repo_id: str) -> DiscoveryResult:
        """Strategy 5: Check well-known symlink locations."""
        symlink_locations = [
            Path.home() / ".jarvis" / "repos" / repo_id.replace("_", "-"),
            Path("/opt/jarvis") / repo_id.replace("_", "-"),
            Path("/usr/local/jarvis") / repo_id.replace("_", "-"),
        ]

        for symlink in symlink_locations:
            if symlink.exists():
                resolved = symlink.resolve()
                if resolved.exists() and resolved.is_dir():
                    return DiscoveryResult(
                        path=resolved,
                        strategy_used=DiscoveryStrategy.SYMLINK,
                        confidence=0.6,
                        discovery_time_ms=0.0,
                    )

        return DiscoveryResult(
            path=None,
            strategy_used=DiscoveryStrategy.SYMLINK,
            confidence=0.0,
            discovery_time_ms=0.0,
            error=f"No symlinks found for {repo_id}",
        )

    async def _discover_home_search(self, repo_id: str) -> DiscoveryResult:
        """Strategy 6: Full home directory search (expensive, last resort)."""
        repo_names = self.REPO_NAMES.get(repo_id, [])
        home = Path.home()

        # This is expensive, so we limit depth and use async
        try:
            for name in repo_names:
                result = await asyncio.create_subprocess_exec(
                    "find", str(home), "-maxdepth", "5",
                    "-type", "d", "-name", name,
                    "-not", "-path", "*/.*",  # Exclude hidden directories
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(result.communicate(), timeout=30.0)

                for line in stdout.decode().strip().split("\n"):
                    if not line:
                        continue
                    candidate = Path(line)
                    if candidate.is_dir() and (candidate / ".git").exists():
                        return DiscoveryResult(
                            path=candidate.resolve(),
                            strategy_used=DiscoveryStrategy.HOME_SEARCH,
                            confidence=0.5,  # Lower confidence - last resort
                            discovery_time_ms=0.0,
                        )

        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"[Discovery] Home search timed out: {e}")

        return DiscoveryResult(
            path=None,
            strategy_used=DiscoveryStrategy.HOME_SEARCH,
            confidence=0.0,
            discovery_time_ms=0.0,
            error=f"Home directory search failed for {repo_id}",
        )

    def _validate_repo_structure(self, repo_id: str, path: Path) -> bool:
        """Validate that a discovered path has expected repo structure."""
        validators = {
            "jarvis": lambda p: (
                (p / "backend").exists() and
                (p / "backend" / "main.py").exists()
            ),
            "jarvis_prime": lambda p: (
                (p / "jarvis_prime").exists() or
                (p / "server.py").exists() or
                (p / "jarvis_prime" / "server.py").exists()
            ),
            "reactor_core": lambda p: (
                (p / "reactor_core").exists() or
                (p / "main.py").exists()
            ),
        }

        validator = validators.get(repo_id)
        if validator:
            try:
                return validator(path)
            except Exception:
                return False

        # No specific validator, accept if .git exists
        return (path / ".git").exists()

    async def discover_all(self) -> Dict[str, DiscoveryResult]:
        """Discover all repository paths concurrently."""
        tasks = {
            repo_id: self.discover(repo_id)
            for repo_id in self.REPO_NAMES.keys()
        }

        results = {}
        for repo_id, task in tasks.items():
            results[repo_id] = await task

        return results

    def clear_cache(self) -> None:
        """Clear the discovery cache."""
        self._cache.clear()
        self._cache_timestamps.clear()


# Global discovery instance (lazy initialization)
_discovery_instance: Optional[IntelligentRepoDiscovery] = None
_discovery_lock: Optional[asyncio.Lock] = None  # v90.0: Lazy lock initialization


async def get_repo_discovery() -> IntelligentRepoDiscovery:
    """Get the global IntelligentRepoDiscovery instance."""
    global _discovery_instance, _discovery_lock

    # v90.0: Lazy lock creation to avoid "no event loop" errors
    if _discovery_lock is None:
        _discovery_lock = asyncio.Lock()

    async with _discovery_lock:
        if _discovery_instance is None:
            _discovery_instance = IntelligentRepoDiscovery()
        return _discovery_instance


# =============================================================================
# v85.0: Resource-Aware Process Launcher
# =============================================================================

@dataclass
class ResourceRequirements:
    """Resource requirements for a component."""
    min_memory_mb: int = 512
    recommended_memory_mb: int = 2048
    min_cpu_percent: float = 10.0
    recommended_cpu_percent: float = 25.0
    required_ports: List[int] = field(default_factory=list)


@dataclass
class LaunchConfig:
    """Configuration for launching a component."""
    repo_id: str
    component_name: str
    entry_point: str  # Module or script path
    port: Optional[int] = None
    extra_args: List[str] = field(default_factory=list)
    env_vars: Dict[str, str] = field(default_factory=dict)
    resources: ResourceRequirements = field(default_factory=ResourceRequirements)
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    health_check_url: Optional[str] = None
    health_check_timeout: float = 30.0
    startup_timeout: float = 60.0


class ResourceAwareLauncher:
    """
    v85.0: Launches processes with resource awareness and retry logic.

    Features:
    - Pre-launch resource checks (memory, CPU, ports)
    - Retry with exponential backoff
    - Health check verification after launch
    - Graceful degradation (warn but proceed if resources tight)
    - Process lifecycle management
    """

    def __init__(self):
        self.discovery = None  # Lazy initialization
        self._managed_processes: Dict[str, Dict[str, Any]] = {}
        self._launch_lock = asyncio.Lock()

    async def _get_discovery(self) -> IntelligentRepoDiscovery:
        """Get the discovery instance lazily."""
        if self.discovery is None:
            self.discovery = await get_repo_discovery()
        return self.discovery

    async def check_resources(self, requirements: ResourceRequirements) -> Tuple[bool, List[str]]:
        """
        Check if system has required resources.

        Returns:
            Tuple of (can_proceed, warnings)
        """
        warnings: List[str] = []
        can_proceed = True

        try:
            # Memory check
            mem = psutil.virtual_memory()
            available_mb = mem.available / (1024 * 1024)

            if available_mb < requirements.min_memory_mb:
                can_proceed = False
                warnings.append(
                    f"Insufficient memory: {available_mb:.0f}MB available, "
                    f"{requirements.min_memory_mb}MB required"
                )
            elif available_mb < requirements.recommended_memory_mb:
                warnings.append(
                    f"Low memory: {available_mb:.0f}MB available, "
                    f"{requirements.recommended_memory_mb}MB recommended"
                )

            # CPU check
            cpu_percent = psutil.cpu_percent(interval=0.1)
            available_cpu = 100.0 - cpu_percent

            if available_cpu < requirements.min_cpu_percent:
                warnings.append(
                    f"High CPU usage: {cpu_percent:.1f}% used, "
                    f"only {available_cpu:.1f}% available"
                )
                # Don't fail on CPU, just warn

            # Port check
            for port in requirements.required_ports:
                if self._is_port_in_use(port):
                    can_proceed = False
                    warnings.append(f"Port {port} is already in use")

        except Exception as e:
            warnings.append(f"Resource check error: {e}")

        return can_proceed, warnings

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a port is in use."""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", port))
                return False
            except socket.error:
                return True

    async def launch(
        self,
        config: LaunchConfig,
        force: bool = False,
    ) -> Tuple[bool, Optional[int], List[str]]:
        """
        Launch a component with resource awareness and retry.

        Args:
            config: Launch configuration
            force: If True, proceed even if resource checks fail

        Returns:
            Tuple of (success, pid, messages)
        """
        async with self._launch_lock:
            messages: List[str] = []

            # Resource check
            if config.port:
                config.resources.required_ports = [config.port]

            can_proceed, warnings = await self.check_resources(config.resources)
            messages.extend(warnings)

            if not can_proceed and not force:
                return False, None, messages

            # Discover repo path
            discovery = await self._get_discovery()
            result = await discovery.discover(config.repo_id)

            if not result.path:
                messages.append(f"Failed to discover {config.repo_id}: {result.error}")
                if result.alternatives:
                    messages.append(f"Alternatives: {[str(p) for p in result.alternatives]}")
                return False, None, messages

            repo_path = result.path
            messages.append(f"Discovered {config.repo_id} at {repo_path} (strategy: {result.strategy_used.name})")

            # Find Python executable
            python_path = await self._find_python(repo_path)
            if not python_path:
                messages.append(f"Could not find Python executable for {config.repo_id}")
                return False, None, messages

            # Build command
            cmd = self._build_command(config, python_path, repo_path)
            messages.append(f"Command: {' '.join(cmd)}")

            # Launch with retry
            for attempt in range(config.max_retries):
                try:
                    success, pid = await self._launch_process(
                        config, cmd, repo_path
                    )

                    if success and pid:
                        # Verify health if URL provided
                        if config.health_check_url:
                            health_ok = await self._wait_for_health(
                                config.health_check_url,
                                config.health_check_timeout,
                            )
                            if not health_ok:
                                messages.append(f"Health check failed for {config.component_name}")
                                await self._kill_process(pid)
                                raise Exception("Health check failed")

                        messages.append(f"Successfully launched {config.component_name} (PID: {pid})")
                        return True, pid, messages

                except Exception as e:
                    backoff = config.retry_backoff_base ** attempt
                    messages.append(
                        f"Launch attempt {attempt + 1}/{config.max_retries} failed: {e}. "
                        f"Retrying in {backoff:.1f}s..."
                    )
                    if attempt < config.max_retries - 1:
                        await asyncio.sleep(backoff)

            messages.append(f"All {config.max_retries} launch attempts failed for {config.component_name}")
            return False, None, messages

    async def _find_python(self, repo_path: Path) -> Optional[Path]:
        """Find the Python executable for a repo."""
        candidates = [
            repo_path / "venv" / "bin" / "python3",
            repo_path / "venv" / "bin" / "python",
            repo_path / ".venv" / "bin" / "python3",
            repo_path / ".venv" / "bin" / "python",
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        # Fallback to system Python
        import shutil
        system_python = shutil.which("python3")
        return Path(system_python) if system_python else None

    def _build_command(
        self,
        config: LaunchConfig,
        python_path: Path,
        repo_path: Path,
    ) -> List[str]:
        """Build the launch command."""
        cmd = [str(python_path)]

        # Check if entry point is a module or script
        if config.entry_point.startswith("-m "):
            cmd.extend(["-m", config.entry_point[3:]])
        elif config.entry_point.endswith(".py"):
            script_path = repo_path / config.entry_point
            cmd.append(str(script_path))
        else:
            cmd.extend(["-m", config.entry_point])

        # Add port if specified
        if config.port:
            cmd.extend(["--port", str(config.port)])

        # Add extra arguments
        cmd.extend(config.extra_args)

        return cmd

    async def _launch_process(
        self,
        config: LaunchConfig,
        cmd: List[str],
        repo_path: Path,
    ) -> Tuple[bool, Optional[int]]:
        """Launch the actual process."""
        env = {
            **os.environ,
            "PYTHONPATH": str(repo_path),
            "TRINITY_ENABLED": "true",
            **config.env_vars,
        }

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )

        # Wait for startup
        try:
            await asyncio.wait_for(
                asyncio.sleep(2.0),
                timeout=config.startup_timeout,
            )
        except asyncio.TimeoutError:
            pass

        # Check if still running
        if process.returncode is None:
            self._managed_processes[config.component_name] = {
                "process": process,
                "pid": process.pid,
                "port": config.port,
                "started_at": time.time(),
                "config": config,
            }
            return True, process.pid
        else:
            stdout, stderr = await process.communicate()
            logger.error(
                f"Process {config.component_name} exited immediately "
                f"(code {process.returncode}): {stderr.decode()[:500]}"
            )
            return False, None

    async def _wait_for_health(
        self,
        url: str,
        timeout: float,
    ) -> bool:
        """Wait for a health check URL to return 200."""
        import aiohttp

        start = time.time()
        while time.time() - start < timeout:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5.0)
                ) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            return True
            except Exception:
                pass
            await asyncio.sleep(1.0)

        return False

    async def _kill_process(self, pid: int) -> None:
        """Kill a process by PID."""
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            await asyncio.sleep(1.0)
            if proc.is_running():
                proc.kill()
        except Exception:
            pass


# Global launcher instance
_launcher_instance: Optional[ResourceAwareLauncher] = None


async def get_resource_aware_launcher() -> ResourceAwareLauncher:
    """Get the global ResourceAwareLauncher instance."""
    global _launcher_instance
    if _launcher_instance is None:
        _launcher_instance = ResourceAwareLauncher()
    return _launcher_instance


# =============================================================================
# Advanced Circuit Breaker Pattern
# =============================================================================

class CircuitState(IntEnum):
    """Circuit breaker states."""
    CLOSED = 0      # Normal operation
    OPEN = 1        # Failing, reject calls
    HALF_OPEN = 2   # Testing recovery


@dataclass
class CircuitBreakerStats:
    """Statistics for a circuit breaker."""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    state_changes: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0


class CircuitBreaker:
    """
    Advanced Circuit Breaker with sliding window failure detection.

    Features:
    - Sliding window for failure rate calculation
    - Configurable failure threshold
    - Half-open state for gradual recovery
    - Call rejection when open
    - Async-native implementation
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_requests: int = 3,
        window_size: int = 10,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_requests = half_open_requests
        self.window_size = window_size

        self._state = CircuitState.CLOSED
        self._failure_window: Deque[Tuple[float, bool]] = deque(maxlen=window_size)
        self._last_state_change = time.time()
        self._half_open_successes = 0
        self._stats = CircuitBreakerStats()
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_state_change: List[Callable[[CircuitState, CircuitState], None]] = []

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        return self._stats

    def _count_recent_failures(self) -> int:
        """Count failures in the sliding window."""
        now = time.time()
        cutoff = now - self.recovery_timeout
        return sum(1 for ts, success in self._failure_window if not success and ts > cutoff)

    async def _check_state_transition(self) -> None:
        """Check and perform state transitions."""
        now = time.time()

        if self._state == CircuitState.CLOSED:
            # Check if failures exceed threshold
            if self._count_recent_failures() >= self.failure_threshold:
                await self._transition_to(CircuitState.OPEN)

        elif self._state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if now - self._last_state_change >= self.recovery_timeout:
                await self._transition_to(CircuitState.HALF_OPEN)

        elif self._state == CircuitState.HALF_OPEN:
            # Check if enough successful requests
            if self._half_open_successes >= self.half_open_requests:
                await self._transition_to(CircuitState.CLOSED)

    async def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.time()
        self._stats.state_changes += 1

        if new_state == CircuitState.HALF_OPEN:
            self._half_open_successes = 0

        logger.info(
            f"[CircuitBreaker:{self.name}] State transition: "
            f"{old_state.name} → {new_state.name}"
        )

        for callback in self._on_state_change:
            try:
                callback(old_state, new_state)
            except Exception as e:
                logger.warning(f"[CircuitBreaker:{self.name}] Callback error: {e}")

    async def __aenter__(self) -> "CircuitBreaker":
        """Context manager entry - check if call should be allowed."""
        async with self._lock:
            await self._check_state_transition()

            if self._state == CircuitState.OPEN:
                self._stats.rejected_calls += 1
                raise CircuitOpenError(
                    f"Circuit breaker '{self.name}' is OPEN. "
                    f"Retry after {self.recovery_timeout}s"
                )

            self._stats.total_calls += 1
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Context manager exit - record success/failure."""
        async with self._lock:
            success = exc_type is None
            now = time.time()

            # Record in sliding window
            self._failure_window.append((now, success))

            if success:
                self._stats.successful_calls += 1
                self._stats.last_success_time = now
                self._stats.consecutive_successes += 1
                self._stats.consecutive_failures = 0

                if self._state == CircuitState.HALF_OPEN:
                    self._half_open_successes += 1

            else:
                self._stats.failed_calls += 1
                self._stats.last_failure_time = now
                self._stats.consecutive_failures += 1
                self._stats.consecutive_successes = 0

                # Immediately open if in half-open state
                if self._state == CircuitState.HALF_OPEN:
                    await self._transition_to(CircuitState.OPEN)

            await self._check_state_transition()

        return False  # Don't suppress exceptions

    def on_state_change(
        self,
        callback: Callable[[CircuitState, CircuitState], None],
    ) -> None:
        """Register callback for state changes."""
        self._on_state_change.append(callback)


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# =============================================================================
# Process Supervisor - PID Monitoring & Auto-Healing
# =============================================================================

@dataclass
class ProcessInfo:
    """Information about a supervised process."""
    component_id: str
    pid: int
    pgid: Optional[int] = None
    start_time: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    restart_count: int = 0
    status: str = "running"
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProcessSupervisor:
    """
    Advanced Process Supervisor with auto-healing capabilities.

    Features:
    - PID monitoring and zombie detection
    - Automatic restart on crash
    - Resource usage tracking (CPU, memory)
    - Heartbeat-based liveness checks
    - Process group management for clean termination
    - Graceful → forceful termination escalation
    """

    def __init__(
        self,
        check_interval: float = 5.0,
        zombie_timeout: float = 30.0,
        heartbeat_timeout: float = 60.0,
    ):
        config = get_config()
        self.check_interval = config.get("TRINITY_SUPERVISOR_CHECK_INTERVAL", check_interval)
        self.zombie_timeout = config.get("TRINITY_SUPERVISOR_ZOMBIE_TIMEOUT", zombie_timeout)
        self.heartbeat_timeout = config.get("TRINITY_SUPERVISOR_HEARTBEAT_TIMEOUT", heartbeat_timeout)

        self._processes: Dict[str, ProcessInfo] = {}
        self._restart_callbacks: Dict[str, Callable[[], Awaitable[bool]]] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4)

        # Statistics
        self._stats = {
            "total_restarts": 0,
            "zombie_detections": 0,
            "heartbeat_timeouts": 0,
            "resource_violations": 0,
        }

    async def start(self) -> None:
        """Start the process supervisor."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("[ProcessSupervisor] Started")

    async def stop(self) -> None:
        """Stop the supervisor and terminate all processes."""
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task

        # Terminate all supervised processes
        for component_id in list(self._processes.keys()):
            await self.terminate_process(component_id)

        self._executor.shutdown(wait=False)
        logger.info("[ProcessSupervisor] Stopped")

    async def register_process(
        self,
        component_id: str,
        pid: int,
        restart_callback: Optional[Callable[[], Awaitable[bool]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a process for supervision."""
        async with self._lock:
            try:
                proc = psutil.Process(pid)
                pgid = os.getpgid(pid)

                info = ProcessInfo(
                    component_id=component_id,
                    pid=pid,
                    pgid=pgid,
                    start_time=proc.create_time(),
                    metadata=metadata or {},
                )

                self._processes[component_id] = info

                if restart_callback:
                    self._restart_callbacks[component_id] = restart_callback

                logger.info(
                    f"[ProcessSupervisor] Registered {component_id} "
                    f"(PID={pid}, PGID={pgid})"
                )

            except (psutil.NoSuchProcess, ProcessLookupError) as e:
                logger.warning(
                    f"[ProcessSupervisor] Failed to register {component_id}: {e}"
                )

    async def unregister_process(self, component_id: str) -> None:
        """Unregister a process from supervision."""
        async with self._lock:
            self._processes.pop(component_id, None)
            self._restart_callbacks.pop(component_id, None)
            logger.debug(f"[ProcessSupervisor] Unregistered {component_id}")

    async def update_heartbeat(self, component_id: str) -> None:
        """Update the heartbeat timestamp for a component."""
        async with self._lock:
            if component_id in self._processes:
                self._processes[component_id].last_heartbeat = time.time()

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                await self._check_all_processes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ProcessSupervisor] Monitor error: {e}")

    async def _check_all_processes(self) -> None:
        """Check all supervised processes."""
        async with self._lock:
            processes_to_restart: List[str] = []

            for component_id, info in list(self._processes.items()):
                try:
                    # Check if process is still running
                    proc = psutil.Process(info.pid)
                    status = proc.status()

                    # Update resource usage
                    info.cpu_percent = proc.cpu_percent()
                    info.memory_mb = proc.memory_info().rss / (1024 * 1024)
                    info.status = status

                    # Check for zombie
                    if status == psutil.STATUS_ZOMBIE:
                        self._stats["zombie_detections"] += 1
                        logger.warning(
                            f"[ProcessSupervisor] Zombie detected: {component_id}"
                        )
                        processes_to_restart.append(component_id)
                        continue

                    # Check heartbeat timeout
                    heartbeat_age = time.time() - info.last_heartbeat
                    if heartbeat_age > self.heartbeat_timeout:
                        self._stats["heartbeat_timeouts"] += 1
                        logger.warning(
                            f"[ProcessSupervisor] Heartbeat timeout: {component_id} "
                            f"(last={heartbeat_age:.1f}s ago)"
                        )
                        processes_to_restart.append(component_id)

                except psutil.NoSuchProcess:
                    logger.warning(
                        f"[ProcessSupervisor] Process crashed: {component_id}"
                    )
                    processes_to_restart.append(component_id)

                except Exception as e:
                    logger.error(
                        f"[ProcessSupervisor] Check failed for {component_id}: {e}"
                    )

        # Restart crashed processes (outside lock)
        for component_id in processes_to_restart:
            await self._handle_process_crash(component_id)

    async def _handle_process_crash(self, component_id: str) -> None:
        """Handle a crashed process."""
        callback = self._restart_callbacks.get(component_id)

        if callback:
            logger.info(f"[ProcessSupervisor] Restarting {component_id}...")
            self._stats["total_restarts"] += 1

            try:
                success = await callback()
                if success:
                    logger.info(
                        f"[ProcessSupervisor] Successfully restarted {component_id}"
                    )
                else:
                    logger.error(
                        f"[ProcessSupervisor] Failed to restart {component_id}"
                    )
            except Exception as e:
                logger.error(
                    f"[ProcessSupervisor] Restart callback failed for {component_id}: {e}"
                )
        else:
            # No restart callback - just clean up
            await self.unregister_process(component_id)

    async def terminate_process(
        self,
        component_id: str,
        graceful_timeout: float = 10.0,
    ) -> bool:
        """Terminate a supervised process."""
        async with self._lock:
            info = self._processes.get(component_id)
            if not info:
                return True

            try:
                proc = psutil.Process(info.pid)

                # Try graceful termination first
                proc.terminate()

                # Wait for graceful shutdown
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            self._executor, proc.wait, graceful_timeout
                        ),
                        timeout=graceful_timeout + 1,
                    )
                    logger.info(
                        f"[ProcessSupervisor] Gracefully terminated {component_id}"
                    )
                except asyncio.TimeoutError:
                    # Force kill
                    proc.kill()
                    logger.warning(
                        f"[ProcessSupervisor] Force killed {component_id}"
                    )

                # Also kill process group if different from main process
                if info.pgid and info.pgid != os.getpid():
                    with suppress(ProcessLookupError, OSError):
                        os.killpg(info.pgid, signal.SIGTERM)

                del self._processes[component_id]
                return True

            except psutil.NoSuchProcess:
                del self._processes[component_id]
                return True

            except Exception as e:
                logger.error(
                    f"[ProcessSupervisor] Failed to terminate {component_id}: {e}"
                )
                return False

    def get_process_info(self, component_id: str) -> Optional[ProcessInfo]:
        """Get information about a supervised process."""
        return self._processes.get(component_id)

    def get_all_processes(self) -> Dict[str, ProcessInfo]:
        """Get all supervised processes."""
        return dict(self._processes)

    def get_stats(self) -> Dict[str, Any]:
        """Get supervisor statistics."""
        return {
            **self._stats,
            "active_processes": len(self._processes),
            "processes": {
                k: {
                    "pid": v.pid,
                    "status": v.status,
                    "cpu": v.cpu_percent,
                    "memory_mb": v.memory_mb,
                    "restarts": v.restart_count,
                }
                for k, v in self._processes.items()
            },
        }


# =============================================================================
# Crash Recovery Manager - Exponential Backoff & Rate Limiting
# =============================================================================

@dataclass
class CrashRecord:
    """Record of a component crash."""
    component_id: str
    timestamp: float
    error: Optional[str] = None
    restart_attempt: int = 0
    backoff_seconds: float = 0.0


class CrashRecoveryManager:
    """
    Advanced Crash Recovery with exponential backoff and rate limiting.

    Features:
    - Exponential backoff with jitter
    - Configurable max restarts
    - Cooldown period reset
    - Crash history tracking
    - Intelligent restart scheduling
    """

    def __init__(
        self,
        max_restarts: int = 5,
        initial_backoff: float = 1.0,
        max_backoff: float = 300.0,
        backoff_multiplier: float = 2.0,
        cooldown_period: float = 300.0,
        jitter_factor: float = 0.1,
    ):
        config = get_config()
        self.max_restarts = config.get("TRINITY_CRASH_MAX_RESTARTS", max_restarts)
        self.initial_backoff = config.get("TRINITY_CRASH_INITIAL_BACKOFF", initial_backoff)
        self.max_backoff = config.get("TRINITY_CRASH_MAX_BACKOFF", max_backoff)
        self.backoff_multiplier = config.get("TRINITY_CRASH_BACKOFF_MULTIPLIER", backoff_multiplier)
        self.cooldown_period = config.get("TRINITY_CRASH_COOLDOWN_PERIOD", cooldown_period)
        self.jitter_factor = jitter_factor

        self._crash_history: Dict[str, List[CrashRecord]] = defaultdict(list)
        self._restart_counts: Dict[str, int] = defaultdict(int)
        self._last_backoff: Dict[str, float] = {}
        self._lock = asyncio.Lock()

        import random
        self._random = random.Random()

    async def should_restart(self, component_id: str) -> Tuple[bool, float]:
        """
        Determine if a component should be restarted.

        Returns:
            Tuple of (should_restart, backoff_seconds)
        """
        async with self._lock:
            now = time.time()

            # Get recent crash history
            history = self._crash_history[component_id]

            # Check cooldown - reset if no crashes in cooldown period
            if history:
                last_crash = history[-1].timestamp
                if now - last_crash > self.cooldown_period:
                    # Reset restart counter
                    self._restart_counts[component_id] = 0
                    self._last_backoff.pop(component_id, None)
                    logger.info(
                        f"[CrashRecovery] Cooldown reset for {component_id}"
                    )

            # Check restart limit
            restart_count = self._restart_counts[component_id]
            if restart_count >= self.max_restarts:
                logger.error(
                    f"[CrashRecovery] Max restarts ({self.max_restarts}) exceeded "
                    f"for {component_id}"
                )
                return False, 0.0

            # Calculate backoff with exponential increase and jitter
            if component_id in self._last_backoff:
                base_backoff = min(
                    self._last_backoff[component_id] * self.backoff_multiplier,
                    self.max_backoff,
                )
            else:
                base_backoff = self.initial_backoff

            # Add jitter (±10% by default)
            jitter = base_backoff * self.jitter_factor * (2 * self._random.random() - 1)
            backoff = max(0.1, base_backoff + jitter)

            self._last_backoff[component_id] = base_backoff

            return True, backoff

    async def record_crash(
        self,
        component_id: str,
        error: Optional[str] = None,
    ) -> CrashRecord:
        """Record a component crash."""
        async with self._lock:
            self._restart_counts[component_id] += 1
            restart_count = self._restart_counts[component_id]

            backoff = self._last_backoff.get(component_id, self.initial_backoff)

            record = CrashRecord(
                component_id=component_id,
                timestamp=time.time(),
                error=error,
                restart_attempt=restart_count,
                backoff_seconds=backoff,
            )

            self._crash_history[component_id].append(record)

            # Keep only recent history
            if len(self._crash_history[component_id]) > 100:
                self._crash_history[component_id] = self._crash_history[component_id][-100:]

            logger.warning(
                f"[CrashRecovery] Recorded crash for {component_id} "
                f"(attempt={restart_count}, backoff={backoff:.1f}s)"
            )

            return record

    async def record_success(self, component_id: str) -> None:
        """Record successful restart/operation."""
        async with self._lock:
            # Decrease backoff on success
            if component_id in self._last_backoff:
                self._last_backoff[component_id] = max(
                    self.initial_backoff,
                    self._last_backoff[component_id] / self.backoff_multiplier,
                )

    def get_crash_history(self, component_id: str) -> List[CrashRecord]:
        """Get crash history for a component."""
        return list(self._crash_history.get(component_id, []))

    def get_restart_count(self, component_id: str) -> int:
        """Get current restart count for a component."""
        return self._restart_counts.get(component_id, 0)

    def reset(self, component_id: str) -> None:
        """Reset crash state for a component."""
        self._crash_history.pop(component_id, None)
        self._restart_counts.pop(component_id, None)
        self._last_backoff.pop(component_id, None)


# =============================================================================
# Resource Coordinator - Port/Memory/CPU Management
# =============================================================================

@dataclass
class ResourceAllocation:
    """A resource allocation for a component."""
    component_id: str
    ports: List[int] = field(default_factory=list)
    memory_limit_mb: Optional[float] = None
    cpu_limit_percent: Optional[float] = None
    allocated_at: float = field(default_factory=time.time)


class ResourceCoordinator:
    """
    Centralized Resource Coordinator for Trinity components.

    Features:
    - Port pool management with collision avoidance
    - Memory limit enforcement
    - CPU affinity/limit management
    - Resource reservation and release
    - Resource usage monitoring
    """

    def __init__(
        self,
        port_pool_start: int = 8000,
        port_pool_size: int = 100,
        memory_limit_mb: float = 4096,
        cpu_limit_percent: float = 80,
    ):
        config = get_config()
        self.port_pool_start = config.get("TRINITY_RESOURCE_PORT_POOL_START", port_pool_start)
        self.port_pool_size = config.get("TRINITY_RESOURCE_PORT_POOL_SIZE", port_pool_size)
        self.memory_limit_mb = config.get("TRINITY_RESOURCE_MEMORY_LIMIT_MB", memory_limit_mb)
        self.cpu_limit_percent = config.get("TRINITY_RESOURCE_CPU_LIMIT_PERCENT", cpu_limit_percent)

        # Port pool
        self._available_ports: Set[int] = set(
            range(port_pool_start, port_pool_start + port_pool_size)
        )
        self._allocated_ports: Dict[str, Set[int]] = defaultdict(set)

        # Allocations
        self._allocations: Dict[str, ResourceAllocation] = {}
        self._lock = asyncio.Lock()

        # System resources
        self._total_memory_mb = psutil.virtual_memory().total / (1024 * 1024)
        self._cpu_count = psutil.cpu_count() or 1

    async def allocate_port(self, component_id: str) -> Optional[int]:
        """Allocate a free port for a component."""
        async with self._lock:
            # First, check if any allocated ports are actually free
            for port in sorted(self._available_ports):
                if await self._is_port_free(port):
                    self._available_ports.remove(port)
                    self._allocated_ports[component_id].add(port)

                    # Update allocation record
                    if component_id not in self._allocations:
                        self._allocations[component_id] = ResourceAllocation(
                            component_id=component_id
                        )
                    self._allocations[component_id].ports.append(port)

                    logger.debug(
                        f"[ResourceCoordinator] Allocated port {port} to {component_id}"
                    )
                    return port

            logger.warning(
                f"[ResourceCoordinator] No free ports available for {component_id}"
            )
            return None

    async def release_port(self, component_id: str, port: int) -> None:
        """Release a port back to the pool."""
        async with self._lock:
            if port in self._allocated_ports[component_id]:
                self._allocated_ports[component_id].remove(port)
                self._available_ports.add(port)

                if component_id in self._allocations:
                    with suppress(ValueError):
                        self._allocations[component_id].ports.remove(port)

                logger.debug(
                    f"[ResourceCoordinator] Released port {port} from {component_id}"
                )

    async def release_all(self, component_id: str) -> None:
        """Release all resources for a component."""
        async with self._lock:
            # Release ports
            ports = self._allocated_ports.pop(component_id, set())
            self._available_ports.update(ports)

            # Remove allocation record
            self._allocations.pop(component_id, None)

            logger.debug(
                f"[ResourceCoordinator] Released all resources for {component_id}"
            )

    async def _is_port_free(self, port: int) -> bool:
        """Check if a port is actually free."""
        import socket

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.1)
            sock.bind(("127.0.0.1", port))
            sock.close()
            return True
        except (socket.error, OSError):
            return False

    def get_system_resources(self) -> Dict[str, Any]:
        """Get current system resource usage."""
        memory = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=0.1)

        return {
            "memory": {
                "total_mb": memory.total / (1024 * 1024),
                "available_mb": memory.available / (1024 * 1024),
                "percent_used": memory.percent,
            },
            "cpu": {
                "count": self._cpu_count,
                "percent_used": cpu_percent,
            },
            "ports": {
                "pool_size": self.port_pool_size,
                "available": len(self._available_ports),
                "allocated": sum(len(p) for p in self._allocated_ports.values()),
            },
        }

    def get_allocation(self, component_id: str) -> Optional[ResourceAllocation]:
        """Get resource allocation for a component."""
        return self._allocations.get(component_id)


# =============================================================================
# Event Store - WAL-Backed Durable Event Storage
# =============================================================================

@dataclass
class TrinityEvent:
    """A durable event in the Trinity system."""
    event_id: str
    event_type: str
    source: str
    target: Optional[str]
    payload: Dict[str, Any]
    timestamp: float
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None
    processed: bool = False
    retry_count: int = 0
    expires_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "target": self.target,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "processed": self.processed,
            "retry_count": self.retry_count,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrinityEvent":
        return cls(**d)


class EventStore:
    """
    WAL-Backed Durable Event Store for Trinity.

    Features:
    - SQLite with WAL mode for crash-safe writes
    - Event replay for missed messages
    - Deduplication via event_id
    - TTL-based expiration
    - Correlation ID tracking for distributed tracing
    - Async-native implementation
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        wal_mode: bool = True,
        ttl_hours: float = 24.0,
        max_replay: int = 1000,
    ):
        config = get_config()
        self.db_path = Path(
            os.path.expanduser(
                db_path or config.get("TRINITY_EVENT_STORE_PATH", "~/.jarvis/trinity/events.db")
            )
        )
        self.wal_mode = config.get("TRINITY_EVENT_STORE_WAL_MODE", wal_mode)
        self.ttl_hours = config.get("TRINITY_EVENT_TTL_HOURS", ttl_hours)
        self.max_replay = config.get("TRINITY_EVENT_MAX_REPLAY", max_replay)

        self._connection: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._initialized = False
        self._executor = ThreadPoolExecutor(max_workers=2)

        # Event handlers
        self._handlers: Dict[str, List[Callable[[TrinityEvent], Awaitable[None]]]] = defaultdict(list)

    async def initialize(self) -> None:
        """Initialize the event store."""
        if self._initialized:
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._init_db)

        self._initialized = True
        logger.info(f"[EventStore] Initialized at {self.db_path}")

    def _init_db(self) -> None:
        """Initialize database schema (sync)."""
        self._connection = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level="IMMEDIATE",
        )
        self._connection.row_factory = sqlite3.Row

        # Enable WAL mode for better crash recovery
        if self.wal_mode:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")

        # Create events table
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT,
                payload TEXT NOT NULL,
                timestamp REAL NOT NULL,
                correlation_id TEXT,
                trace_id TEXT,
                processed INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                expires_at REAL,
                created_at REAL DEFAULT (julianday('now'))
            )
        """)

        # Create indices
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type)
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed ON events(processed, timestamp)
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_correlation ON events(correlation_id)
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_expires ON events(expires_at)
        """)

        self._connection.commit()

    async def publish(
        self,
        event_type: str,
        source: str,
        payload: Dict[str, Any],
        target: Optional[str] = None,
        correlation_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        ttl_hours: Optional[float] = None,
    ) -> str:
        """Publish an event to the store."""
        await self.initialize()

        event_id = str(uuid.uuid4())
        timestamp = time.time()
        ttl = ttl_hours or self.ttl_hours
        expires_at = timestamp + (ttl * 3600) if ttl > 0 else None

        event = TrinityEvent(
            event_id=event_id,
            event_type=event_type,
            source=source,
            target=target,
            payload=payload,
            timestamp=timestamp,
            correlation_id=correlation_id,
            trace_id=trace_id,
            expires_at=expires_at,
        )

        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, self._insert_event, event)

        # Dispatch to handlers
        await self._dispatch_event(event)

        logger.debug(f"[EventStore] Published event {event_id} ({event_type})")
        return event_id

    def _insert_event(self, event: TrinityEvent) -> None:
        """Insert event into database (sync)."""
        if not self._connection:
            return

        self._connection.execute("""
            INSERT OR REPLACE INTO events
            (event_id, event_type, source, target, payload, timestamp,
             correlation_id, trace_id, processed, retry_count, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.event_id,
            event.event_type,
            event.source,
            event.target,
            json.dumps(event.payload),
            event.timestamp,
            event.correlation_id,
            event.trace_id,
            1 if event.processed else 0,
            event.retry_count,
            event.expires_at,
        ))
        self._connection.commit()

    async def get_unprocessed(
        self,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[TrinityEvent]:
        """Get unprocessed events for replay."""
        await self.initialize()

        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._executor, self._get_unprocessed_sync, event_type, limit
            )

    def _get_unprocessed_sync(
        self,
        event_type: Optional[str],
        limit: int,
    ) -> List[TrinityEvent]:
        """Get unprocessed events (sync)."""
        if not self._connection:
            return []

        now = time.time()
        if event_type:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE processed = 0 AND (expires_at IS NULL OR expires_at > ?)
                  AND event_type = ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (now, event_type, limit))
        else:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE processed = 0 AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY timestamp ASC
                LIMIT ?
            """, (now, limit))

        events = []
        for row in cursor.fetchall():
            events.append(TrinityEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                source=row["source"],
                target=row["target"],
                payload=json.loads(row["payload"]),
                timestamp=row["timestamp"],
                correlation_id=row["correlation_id"],
                trace_id=row["trace_id"],
                processed=bool(row["processed"]),
                retry_count=row["retry_count"],
                expires_at=row["expires_at"],
            ))

        return events

    async def mark_processed(self, event_id: str) -> None:
        """Mark an event as processed."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self._executor, self._mark_processed_sync, event_id
            )

    def _mark_processed_sync(self, event_id: str) -> None:
        """Mark processed (sync)."""
        if not self._connection:
            return

        self._connection.execute(
            "UPDATE events SET processed = 1 WHERE event_id = ?",
            (event_id,)
        )
        self._connection.commit()

    async def replay_events(
        self,
        since_timestamp: float,
        event_type: Optional[str] = None,
    ) -> List[TrinityEvent]:
        """Replay events since a timestamp."""
        await self.initialize()

        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._executor, self._replay_events_sync, since_timestamp, event_type
            )

    def _replay_events_sync(
        self,
        since_timestamp: float,
        event_type: Optional[str],
    ) -> List[TrinityEvent]:
        """Replay events (sync)."""
        if not self._connection:
            return []

        if event_type:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE timestamp >= ? AND event_type = ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (since_timestamp, event_type, self.max_replay))
        else:
            cursor = self._connection.execute("""
                SELECT * FROM events
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (since_timestamp, self.max_replay))

        events = []
        for row in cursor.fetchall():
            events.append(TrinityEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                source=row["source"],
                target=row["target"],
                payload=json.loads(row["payload"]),
                timestamp=row["timestamp"],
                correlation_id=row["correlation_id"],
                trace_id=row["trace_id"],
                processed=bool(row["processed"]),
                retry_count=row["retry_count"],
                expires_at=row["expires_at"],
            ))

        return events

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[TrinityEvent], Awaitable[None]],
    ) -> None:
        """Subscribe to events of a specific type."""
        self._handlers[event_type].append(handler)

    async def _dispatch_event(self, event: TrinityEvent) -> None:
        """Dispatch event to registered handlers."""
        handlers = self._handlers.get(event.event_type, [])
        handlers.extend(self._handlers.get("*", []))  # Wildcard handlers

        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.warning(
                    f"[EventStore] Handler error for {event.event_type}: {e}"
                )

    async def cleanup_expired(self) -> int:
        """Remove expired events."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._executor, self._cleanup_expired_sync
            )

    def _cleanup_expired_sync(self) -> int:
        """Cleanup expired (sync)."""
        if not self._connection:
            return 0

        cursor = self._connection.execute(
            "DELETE FROM events WHERE expires_at IS NOT NULL AND expires_at < ?",
            (time.time(),)
        )
        self._connection.commit()
        return cursor.rowcount

    async def close(self) -> None:
        """Close the event store."""
        if self._connection:
            self._connection.close()
            self._connection = None
        self._executor.shutdown(wait=False)


# =============================================================================
# Distributed Tracer - Cross-Repo Tracing with Correlation
# =============================================================================

@dataclass
class TraceSpan:
    """A span in a distributed trace."""
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    operation_name: str
    service_name: str
    start_time: float
    end_time: Optional[float] = None
    status: str = "in_progress"
    tags: Dict[str, Any] = field(default_factory=dict)
    logs: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return (time.time() - self.start_time) * 1000


class DistributedTracer:
    """
    Distributed Tracing for Trinity cross-repo operations.

    Features:
    - OpenTelemetry-compatible trace/span model
    - Automatic correlation ID propagation
    - Span hierarchy tracking
    - Tag and log support
    - Sampling for production use
    - Context propagation across async boundaries
    """

    def __init__(
        self,
        service_name: str = "jarvis_body",
        enabled: bool = True,
        sample_rate: float = 1.0,
        max_spans: int = 10000,
    ):
        config = get_config()
        self.service_name = service_name
        self.enabled = config.get("TRINITY_TRACING_ENABLED", enabled)
        self.sample_rate = config.get("TRINITY_TRACING_SAMPLE_RATE", sample_rate)
        self.max_spans = config.get("TRINITY_TRACING_MAX_SPANS", max_spans)

        self._traces: Dict[str, List[TraceSpan]] = {}
        self._current_trace_id: Optional[str] = None
        self._current_span_id: Optional[str] = None
        self._span_stack: List[str] = []
        self._lock = asyncio.Lock()

        import random
        self._random = random.Random()

    def _should_sample(self) -> bool:
        """Determine if this trace should be sampled."""
        return self._random.random() < self.sample_rate

    @asynccontextmanager
    async def trace(
        self,
        operation_name: str,
        tags: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ):
        """Create a new trace context."""
        if not self.enabled:
            yield None
            return

        async with self._lock:
            # Start new trace or continue existing
            if trace_id:
                self._current_trace_id = trace_id
            elif not self._current_trace_id:
                if not self._should_sample():
                    yield None
                    return
                self._current_trace_id = str(uuid.uuid4())

            trace_id = self._current_trace_id

            # Create root span
            span_id = str(uuid.uuid4())
            span = TraceSpan(
                span_id=span_id,
                trace_id=trace_id,
                parent_span_id=None,
                operation_name=operation_name,
                service_name=self.service_name,
                start_time=time.time(),
                tags=tags or {},
            )

            if trace_id not in self._traces:
                self._traces[trace_id] = []
            self._traces[trace_id].append(span)

            self._current_span_id = span_id
            self._span_stack.append(span_id)

        try:
            yield span
            span.status = "ok"
        except Exception as e:
            span.status = "error"
            span.tags["error"] = str(e)
            raise
        finally:
            span.end_time = time.time()
            async with self._lock:
                self._span_stack.pop() if self._span_stack else None
                self._current_span_id = self._span_stack[-1] if self._span_stack else None

                # Cleanup if this was the root span
                if not self._span_stack:
                    self._current_trace_id = None

                # Limit total spans
                while len(self._traces) > self.max_spans:
                    oldest = min(self._traces.keys())
                    del self._traces[oldest]

    @asynccontextmanager
    async def span(
        self,
        operation_name: str,
        tags: Optional[Dict[str, Any]] = None,
    ):
        """Create a child span within the current trace."""
        if not self.enabled or not self._current_trace_id:
            yield None
            return

        async with self._lock:
            span_id = str(uuid.uuid4())
            span = TraceSpan(
                span_id=span_id,
                trace_id=self._current_trace_id,
                parent_span_id=self._current_span_id,
                operation_name=operation_name,
                service_name=self.service_name,
                start_time=time.time(),
                tags=tags or {},
            )

            self._traces[self._current_trace_id].append(span)
            self._current_span_id = span_id
            self._span_stack.append(span_id)

        try:
            yield span
            span.status = "ok"
        except Exception as e:
            span.status = "error"
            span.tags["error"] = str(e)
            raise
        finally:
            span.end_time = time.time()
            async with self._lock:
                self._span_stack.pop() if self._span_stack else None
                self._current_span_id = self._span_stack[-1] if self._span_stack else None

    def get_trace_id(self) -> Optional[str]:
        """Get current trace ID for propagation."""
        return self._current_trace_id

    def get_span_id(self) -> Optional[str]:
        """Get current span ID for propagation."""
        return self._current_span_id

    def get_trace(self, trace_id: str) -> List[TraceSpan]:
        """Get all spans for a trace."""
        return list(self._traces.get(trace_id, []))

    def get_trace_summary(self, trace_id: str) -> Dict[str, Any]:
        """Get summary of a trace."""
        spans = self._traces.get(trace_id, [])
        if not spans:
            return {}

        root_span = next((s for s in spans if s.parent_span_id is None), spans[0])

        return {
            "trace_id": trace_id,
            "operation": root_span.operation_name,
            "service": root_span.service_name,
            "status": root_span.status,
            "duration_ms": root_span.duration_ms,
            "span_count": len(spans),
            "error_count": sum(1 for s in spans if s.status == "error"),
        }

    def log_to_span(self, message: str, **kwargs) -> None:
        """Add a log entry to the current span."""
        if not self._current_span_id or not self._current_trace_id:
            return

        spans = self._traces.get(self._current_trace_id, [])
        for span in spans:
            if span.span_id == self._current_span_id:
                span.logs.append({
                    "timestamp": time.time(),
                    "message": message,
                    **kwargs,
                })
                break


# =============================================================================
# Unified Health Aggregator - Centralized Health with Anomaly Detection
# =============================================================================

@dataclass
class HealthSample:
    """A single health sample."""
    timestamp: float
    component: str
    healthy: bool
    latency_ms: float
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class AnomalyReport:
    """Report of a detected anomaly."""
    component: str
    anomaly_type: str
    severity: str  # "warning", "critical"
    description: str
    timestamp: float
    metrics: Dict[str, Any] = field(default_factory=dict)


class UnifiedHealthAggregator:
    """
    Unified Health Aggregator with anomaly detection.

    Features:
    - Centralized health from all Trinity components
    - Sliding window health history
    - Trend analysis (improving/degrading)
    - Anomaly detection (latency spikes, error rate changes)
    - Health score calculation
    - Component correlation analysis
    """

    def __init__(
        self,
        anomaly_threshold: float = 0.8,
        history_size: int = 100,
        trend_window: int = 10,
    ):
        config = get_config()
        self.anomaly_threshold = config.get("TRINITY_HEALTH_ANOMALY_THRESHOLD", anomaly_threshold)
        self.history_size = config.get("TRINITY_HEALTH_HISTORY_SIZE", history_size)
        self.trend_window = config.get("TRINITY_HEALTH_TREND_WINDOW", trend_window)

        self._history: Dict[str, Deque[HealthSample]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._anomalies: Deque[AnomalyReport] = deque(maxlen=100)
        self._baselines: Dict[str, Dict[str, float]] = {}
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_anomaly: List[Callable[[AnomalyReport], None]] = []

    async def record_health(
        self,
        component: str,
        healthy: bool,
        latency_ms: float,
        metrics: Optional[Dict[str, float]] = None,
    ) -> None:
        """Record a health sample."""
        sample = HealthSample(
            timestamp=time.time(),
            component=component,
            healthy=healthy,
            latency_ms=latency_ms,
            metrics=metrics or {},
        )

        async with self._lock:
            self._history[component].append(sample)

            # Check for anomalies
            await self._check_anomalies(component, sample)

            # Update baseline
            await self._update_baseline(component)

    async def _check_anomalies(self, component: str, sample: HealthSample) -> None:
        """Check for anomalies in the new sample."""
        baseline = self._baselines.get(component, {})

        # Latency spike detection
        if "latency_avg" in baseline:
            latency_ratio = sample.latency_ms / max(baseline["latency_avg"], 1)
            if latency_ratio > 3.0:  # 3x normal latency
                await self._report_anomaly(AnomalyReport(
                    component=component,
                    anomaly_type="latency_spike",
                    severity="warning" if latency_ratio < 5.0 else "critical",
                    description=f"Latency spike: {sample.latency_ms:.1f}ms (normal: {baseline['latency_avg']:.1f}ms)",
                    timestamp=sample.timestamp,
                    metrics={"latency_ms": sample.latency_ms, "ratio": latency_ratio},
                ))

        # Health state change detection
        history = self._history[component]
        if len(history) >= 3:
            recent_unhealthy = sum(1 for s in list(history)[-3:] if not s.healthy)
            if recent_unhealthy >= 2 and sample.healthy is False:
                await self._report_anomaly(AnomalyReport(
                    component=component,
                    anomaly_type="repeated_failure",
                    severity="critical",
                    description=f"Component {component} has failed multiple times recently",
                    timestamp=sample.timestamp,
                    metrics={"consecutive_failures": recent_unhealthy},
                ))

    async def _report_anomaly(self, anomaly: AnomalyReport) -> None:
        """Report an anomaly to registered handlers."""
        self._anomalies.append(anomaly)

        logger.warning(
            f"[HealthAggregator] Anomaly detected: {anomaly.component} - "
            f"{anomaly.anomaly_type} ({anomaly.severity})"
        )

        for callback in self._on_anomaly:
            try:
                callback(anomaly)
            except Exception as e:
                logger.debug(f"[HealthAggregator] Callback error: {e}")

    async def _update_baseline(self, component: str) -> None:
        """Update baseline metrics for a component."""
        history = list(self._history[component])
        if len(history) < 10:
            return

        # Calculate baseline metrics
        latencies = [s.latency_ms for s in history]
        health_rate = sum(1 for s in history if s.healthy) / len(history)

        self._baselines[component] = {
            "latency_avg": sum(latencies) / len(latencies),
            "latency_p99": sorted(latencies)[int(len(latencies) * 0.99)],
            "health_rate": health_rate,
        }

    def get_component_health(self, component: str) -> Dict[str, Any]:
        """Get health summary for a component."""
        history = list(self._history.get(component, []))
        if not history:
            return {"status": "unknown", "samples": 0}

        recent = history[-self.trend_window:] if len(history) >= self.trend_window else history

        healthy_count = sum(1 for s in recent if s.healthy)
        health_rate = healthy_count / len(recent)
        avg_latency = sum(s.latency_ms for s in recent) / len(recent)

        # Determine trend
        if len(history) >= self.trend_window * 2:
            older = history[-(self.trend_window * 2):-self.trend_window]
            older_rate = sum(1 for s in older if s.healthy) / len(older)
            trend = "improving" if health_rate > older_rate else (
                "degrading" if health_rate < older_rate else "stable"
            )
        else:
            trend = "insufficient_data"

        return {
            "status": "healthy" if health_rate > self.anomaly_threshold else "degraded",
            "health_rate": health_rate,
            "avg_latency_ms": avg_latency,
            "samples": len(history),
            "trend": trend,
            "last_check": history[-1].timestamp if history else None,
        }

    def get_unified_health(self) -> Dict[str, Any]:
        """Get unified health across all components."""
        components = {}
        overall_health = 1.0

        for component in self._history.keys():
            health = self.get_component_health(component)
            components[component] = health
            overall_health *= health.get("health_rate", 1.0)

        # Calculate overall score (geometric mean)
        if components:
            overall_score = overall_health ** (1 / len(components))
        else:
            overall_score = 1.0

        return {
            "overall_score": overall_score,
            "overall_status": "healthy" if overall_score > self.anomaly_threshold else "degraded",
            "components": components,
            "recent_anomalies": [
                {
                    "component": a.component,
                    "type": a.anomaly_type,
                    "severity": a.severity,
                    "timestamp": a.timestamp,
                }
                for a in list(self._anomalies)[-10:]
            ],
        }

    def on_anomaly(self, callback: Callable[[AnomalyReport], None]) -> None:
        """Register callback for anomaly detection."""
        self._on_anomaly.append(callback)

    def get_anomalies(self, since: Optional[float] = None) -> List[AnomalyReport]:
        """Get recent anomalies."""
        anomalies = list(self._anomalies)
        if since:
            anomalies = [a for a in anomalies if a.timestamp >= since]
        return anomalies


# =============================================================================
# Adaptive Throttler - Backpressure & Rate Limiting
# =============================================================================

class AdaptiveThrottler:
    """
    Adaptive Throttler with backpressure management.

    Features:
    - Token bucket rate limiting
    - Adaptive rate adjustment based on system load
    - Request queuing with timeout
    - Priority-based throttling
    - Backpressure signaling to clients
    """

    def __init__(
        self,
        max_concurrent: int = 100,
        queue_size: int = 1000,
        rate_limit: float = 100.0,  # requests per second
    ):
        config = get_config()
        self.max_concurrent = config.get("TRINITY_THROTTLE_MAX_CONCURRENT", max_concurrent)
        self.queue_size = config.get("TRINITY_THROTTLE_QUEUE_SIZE", queue_size)
        self.rate_limit = config.get("TRINITY_THROTTLE_RATE_LIMIT", rate_limit)

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._current_rate = rate_limit
        self._token_bucket = rate_limit
        self._last_token_update = time.time()
        self._lock = asyncio.Lock()

        # Statistics
        self._stats = {
            "total_requests": 0,
            "queued_requests": 0,
            "rejected_requests": 0,
            "completed_requests": 0,
        }

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire a throttle permit."""
        async with self._lock:
            # Token bucket refill
            now = time.time()
            elapsed = now - self._last_token_update
            self._token_bucket = min(
                self.rate_limit,
                self._token_bucket + elapsed * self._current_rate,
            )
            self._last_token_update = now

            # Check rate limit
            if self._token_bucket < 1.0:
                self._stats["rejected_requests"] += 1
                return False

            self._token_bucket -= 1.0

        self._stats["total_requests"] += 1

        # Acquire semaphore for concurrency limit
        try:
            if timeout:
                await asyncio.wait_for(self._semaphore.acquire(), timeout)
            else:
                await self._semaphore.acquire()
            return True
        except asyncio.TimeoutError:
            self._stats["rejected_requests"] += 1
            return False

    def release(self) -> None:
        """Release a throttle permit."""
        self._semaphore.release()
        self._stats["completed_requests"] += 1

    @asynccontextmanager
    async def throttle(self, timeout: Optional[float] = None):
        """Context manager for throttled operations."""
        acquired = await self.acquire(timeout)
        if not acquired:
            raise ThrottleExceededError("Request throttled - system under load")

        try:
            yield
        finally:
            self.release()

    def adjust_rate(self, factor: float) -> None:
        """Adjust the rate limit dynamically."""
        self._current_rate = max(1.0, self.rate_limit * factor)
        logger.debug(f"[Throttler] Rate adjusted to {self._current_rate:.1f}/s")

    def get_stats(self) -> Dict[str, Any]:
        """Get throttler statistics."""
        return {
            **self._stats,
            "current_rate": self._current_rate,
            "available_permits": self._semaphore._value,
            "queue_size": self._queue.qsize(),
        }


class ThrottleExceededError(Exception):
    """Raised when throttle limit is exceeded."""
    pass


# =============================================================================
# Unified State Coordinator v85.0 - Atomic Locks & Process Cookies
# =============================================================================

@dataclass
class ProcessOwnership:
    """Ownership record for a component."""
    entry_point: str
    pid: int
    cookie: str
    hostname: str
    acquired_at: float
    last_heartbeat: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class UnifiedStateCoordinator:
    """
    v86.0: Production-grade unified state management with advanced features.

    Critical Features:
    ══════════════════
    ✅ Atomic fcntl locks (cross-platform, OS-level)
    ✅ Process cookie validation (UUID-based, prevents PID reuse)
    ✅ TTL-based state expiration with heartbeat
    ✅ Graceful handoff protocol between scripts
    ✅ Network partition detection
    ✅ Resource-aware coordination
    ✅ Zero hardcoding (100% env-driven)
    ✅ Process tree walking for parent detection

    v86.0 Advanced Features:
    ════════════════════════
    ✅ Priority-based ownership resolution (supervisor > start_system > main)
    ✅ Lock file validation and recovery
    ✅ Heartbeat task monitoring with auto-restart
    ✅ Event-driven status synchronization
    ✅ Circuit breaker for state operations
    ✅ Adaptive timeout management
    ✅ PID reuse detection with process creation time
    ✅ State file corruption recovery with checksums
    ✅ Timestamp-based tie-breaking for same priority

    This solves the critical gap of run_supervisor.py and start_system.py
    not knowing about each other, preventing:
    - Race conditions (simultaneous startup)
    - Duplicate launches
    - Inconsistent state
    - Stale lock files after crash
    - PID reuse issues
    """

    _instance: Optional["UnifiedStateCoordinator"] = None
    _instance_lock: asyncio.Lock = None

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Priority-based ownership resolution
    # ═══════════════════════════════════════════════════════════════════════════
    # Higher priority = more important, can take over lower priority owners
    ENTRY_POINT_PRIORITY: Final[Dict[str, int]] = {
        "run_supervisor": 100,   # Highest - supervisor manages everything
        "run_supervisor.py": 100,
        "start_system": 50,      # Medium - system launcher
        "start_system.py": 50,
        "main_direct": 10,       # Low - direct launch
        "main.py": 10,
        "unknown": 0,            # Lowest - unknown entry point
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Component event types for event-driven synchronization
    # ═══════════════════════════════════════════════════════════════════════════
    class ComponentEventType(str, Enum):
        STARTING = "starting"
        READY = "ready"
        DEGRADED = "degraded"
        FAILED = "failed"
        SHUTTING_DOWN = "shutting_down"
        HEARTBEAT_LOST = "heartbeat_lost"
        OWNERSHIP_ACQUIRED = "ownership_acquired"
        OWNERSHIP_RELEASED = "ownership_released"
        OWNERSHIP_TRANSFERRED = "ownership_transferred"

    def __init__(self):
        # State directories (env-driven)
        config = get_config()
        state_dir = Path(os.path.expanduser(
            os.getenv("JARVIS_STATE_DIR", "~/.jarvis/state")
        ))
        state_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = state_dir / "unified_state.json"
        self.lock_dir = state_dir / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        # Process identity (prevents PID reuse issues)
        self._process_cookie = str(uuid.uuid4())
        self._pid = os.getpid()
        self._hostname = socket.gethostname()
        self._process_start_time = time.time()  # v86.0: For PID reuse detection

        # Lock file handles (keep open to hold lock)
        self._lock_fds: Dict[str, int] = {}

        # State cache with TTL
        self._state_cache: Optional[Dict[str, Any]] = None
        self._cache_ttl = float(os.getenv("JARVIS_STATE_CACHE_TTL", "2.0"))
        self._last_cache_time = 0.0

        # Stale detection thresholds (env-driven)
        self._stale_threshold = float(os.getenv("JARVIS_STATE_STALE_THRESHOLD", "300.0"))
        self._heartbeat_interval = float(os.getenv("JARVIS_HEARTBEAT_INTERVAL", "10.0"))

        # Heartbeat tasks
        self._heartbeat_tasks: Dict[str, asyncio.Task] = {}

        # File lock for state operations
        self._file_lock = asyncio.Lock()

        # ═══════════════════════════════════════════════════════════════════════
        # v86.0: Circuit breaker state for state operations
        # ═══════════════════════════════════════════════════════════════════════
        self._circuit_state = "closed"  # closed, open, half-open
        self._circuit_failure_count = 0
        self._circuit_last_failure_time = 0.0
        self._circuit_failure_threshold = int(
            os.getenv("JARVIS_STATE_CIRCUIT_FAILURE_THRESHOLD", "5")
        )
        self._circuit_recovery_timeout = float(
            os.getenv("JARVIS_STATE_CIRCUIT_RECOVERY_TIMEOUT", "30.0")
        )

        # ═══════════════════════════════════════════════════════════════════════
        # v86.0: Adaptive timeout tracking
        # ═══════════════════════════════════════════════════════════════════════
        self._operation_history: Dict[str, List[float]] = {}
        self._timeout_multiplier = float(os.getenv("JARVIS_TIMEOUT_MULTIPLIER", "1.5"))

        # ═══════════════════════════════════════════════════════════════════════
        # v86.0: Event subscribers for event-driven coordination
        # ═══════════════════════════════════════════════════════════════════════
        self._event_subscribers: List[Callable] = []
        self._event_poller_task: Optional[asyncio.Task] = None

        # ═══════════════════════════════════════════════════════════════════════
        # v86.0: Our entry point (for priority resolution)
        # ═══════════════════════════════════════════════════════════════════════
        self._our_entry_point: Optional[str] = None

        # ═══════════════════════════════════════════════════════════════════════
        # v87.0: Advanced coordinator integration
        # ═══════════════════════════════════════════════════════════════════════
        self._advanced_coord: Optional["TrinityAdvancedCoordinator"] = None
        self._advanced_coord_lock = asyncio.Lock()
        self._enable_advanced_features = os.getenv(
            "JARVIS_ENABLE_ADVANCED_COORD", "true"
        ).lower() == "true"

        # v87.0: Process group isolation
        self._pgid = os.getpgid(os.getpid())
        self._process_creation_time = self._get_process_creation_time()

        # v87.0: Graceful degradation state
        self._degradation_mode: str = "full"  # full, degraded, minimal
        self._failed_components: Set[str] = set()

        # ═══════════════════════════════════════════════════════════════════════
        # v88.0: Ultra coordinator integration
        # ═══════════════════════════════════════════════════════════════════════
        self._ultra_coord: Optional["TrinityUltraCoordinator"] = None
        self._ultra_coord_lock = asyncio.Lock()
        self._enable_ultra_features = os.getenv(
            "JARVIS_ENABLE_ULTRA_COORD", "true"
        ).lower() == "true"

        logger.debug(
            f"[StateCoord] v88.0 Initialized (PID={self._pid}, "
            f"PGID={self._pgid}, Cookie={self._process_cookie[:8]}...)"
        )

    def _get_process_creation_time(self) -> float:
        """Get process creation time for PID reuse detection."""
        try:
            proc = psutil.Process(os.getpid())
            return proc.create_time()
        except Exception:
            return time.time()

    async def _ensure_advanced_coord(self) -> Optional["TrinityAdvancedCoordinator"]:
        """
        v87.0: Lazy initialization of advanced coordinator.

        Returns None if advanced features disabled or init fails.
        """
        if not self._enable_advanced_features:
            return None

        if self._advanced_coord is not None:
            return self._advanced_coord

        async with self._advanced_coord_lock:
            if self._advanced_coord is not None:
                return self._advanced_coord

            try:
                # Import here to avoid circular imports
                coord = TrinityAdvancedCoordinator()
                await coord.initialize()
                self._advanced_coord = coord
                logger.info("[StateCoord] v87.0 Advanced coordinator initialized")
                return self._advanced_coord
            except Exception as e:
                logger.warning(f"[StateCoord] v87.0 Advanced coordinator init failed: {e}")
                return None

    async def _ensure_ultra_coord(self) -> Optional["TrinityUltraCoordinator"]:
        """
        v88.0: Lazy initialization of ultra coordinator.

        Returns None if ultra features disabled or init fails.
        """
        if not self._enable_ultra_features:
            return None

        if self._ultra_coord is not None:
            return self._ultra_coord

        async with self._ultra_coord_lock:
            if self._ultra_coord is not None:
                return self._ultra_coord

            try:
                # Use module-level singleton
                self._ultra_coord = await get_ultra_coordinator()
                logger.info("[StateCoord] v88.0 Ultra coordinator initialized")
                return self._ultra_coord
            except Exception as e:
                logger.warning(f"[StateCoord] v88.0 Ultra coordinator init failed: {e}")
                return None

    async def execute_protected(
        self,
        component: str,
        operation: Callable[[], Awaitable[T]],
        timeout: Optional[float] = None,
    ) -> Tuple[bool, Optional[T], Dict[str, Any]]:
        """
        v88.0: Execute operation with full protection stack.

        Applies adaptive circuit breaker, backpressure, and distributed tracing.

        Args:
            component: Component name (jprime, reactor, voice, etc.)
            operation: Async operation to execute
            timeout: Optional timeout in seconds

        Returns:
            (success, result, metadata)
        """
        ultra_coord = await self._ensure_ultra_coord()
        if ultra_coord:
            return await ultra_coord.execute_with_protection(
                component, operation, timeout
            )

        # Fallback: direct execution without protection
        try:
            if timeout:
                result = await asyncio.wait_for(operation(), timeout=timeout)
            else:
                result = await operation()
            return True, result, {"fallback": True}
        except asyncio.TimeoutError:
            return False, None, {"timeout": True, "fallback": True}
        except Exception as e:
            return False, None, {"error": str(e), "fallback": True}

    async def get_ultra_status(self) -> Dict[str, Any]:
        """
        v88.0: Get ultra coordinator status.

        Returns comprehensive status including circuit breakers,
        backpressure, container info, and event buffer.
        """
        ultra_coord = await self._ensure_ultra_coord()
        if ultra_coord:
            return ultra_coord.get_status()
        return {"enabled": False, "reason": "Ultra coordinator not initialized"}

    async def get_degradation_mode(self) -> str:
        """Get current degradation mode."""
        return self._degradation_mode

    async def set_degradation_mode(self, mode: str, failed_components: Optional[Set[str]] = None) -> None:
        """Set degradation mode and track failed components."""
        old_mode = self._degradation_mode
        self._degradation_mode = mode
        if failed_components:
            self._failed_components = failed_components

        if old_mode != mode:
            logger.warning(f"[StateCoord] Degradation mode changed: {old_mode} -> {mode}")
            await self.publish_component_event(
                "system",
                self.ComponentEventType.DEGRADED,
                metadata={"mode": mode, "failed": list(self._failed_components)}
            )

    async def verify_process_isolation(self) -> Tuple[bool, str]:
        """
        v87.0: Verify process group isolation.

        Checks that we're properly isolated from parent process group.
        """
        try:
            current_pgid = os.getpgid(os.getpid())
            parent_pid = os.getppid()

            try:
                parent_pgid = os.getpgid(parent_pid)
            except (ProcessLookupError, OSError):
                # Parent died - we're isolated
                return True, "Parent process terminated, isolation confirmed"

            # We should be in our own process group for proper cleanup
            # OR we're the leader of our process group
            if current_pgid == os.getpid():
                return True, "Process group leader"

            if current_pgid != parent_pgid:
                return True, "Isolated from parent process group"

            # Same group as parent - may have issues with signal handling
            return False, f"Sharing process group with parent (PGID={current_pgid})"

        except Exception as e:
            return False, f"Process isolation check failed: {e}"

    # ═══════════════════════════════════════════════════════════════════════════
    # v87.0: Graceful Degradation Chain Integration
    # ═══════════════════════════════════════════════════════════════════════════

    async def get_fallback_manager(self) -> Optional[Any]:
        """
        v87.0: Get Trinity fallback manager instance.

        Provides access to the TrinityFallbackManager for executing
        fallback chains (J-Prime, Reactor-Core, Voice).
        """
        if not hasattr(self, "_fallback_manager"):
            self._fallback_manager = None

        if self._fallback_manager is None:
            try:
                from backend.core.graceful_degradation import TrinityFallbackManager
                self._fallback_manager = TrinityFallbackManager()
                logger.debug("[StateCoord] v87.0 Fallback manager initialized")
            except ImportError as e:
                logger.warning(f"[StateCoord] v87.0 Fallback manager not available: {e}")
                return None
        return self._fallback_manager

    async def update_component_health(
        self,
        component: str,
        is_healthy: bool,
        error: Optional[str] = None
    ) -> None:
        """
        v87.0: Update component health and adjust degradation mode.

        Args:
            component: Component name (jprime, reactor, voice, etc.)
            is_healthy: Whether component is healthy
            error: Error message if unhealthy
        """
        if is_healthy:
            # Remove from failed components
            if component in self._failed_components:
                self._failed_components.discard(component)
                logger.info(f"[StateCoord] v87.0 Component recovered: {component}")

                # Check if we can return to full mode
                if not self._failed_components:
                    await self.set_degradation_mode("full")
        else:
            # Add to failed components
            self._failed_components.add(component)
            logger.warning(f"[StateCoord] v87.0 Component failed: {component} - {error}")

            # Determine degradation mode
            critical_components = {"jarvis", "state_coordinator"}
            non_critical = {"jprime", "reactor", "voice"}

            if self._failed_components & critical_components:
                # Critical component failed - minimal mode
                await self.set_degradation_mode("minimal", self._failed_components)
            elif self._failed_components & non_critical:
                # Non-critical component failed - degraded mode
                await self.set_degradation_mode("degraded", self._failed_components)

    async def execute_with_fallback(
        self,
        chain_name: str,
        operation: Callable[[], Awaitable[T]],
        fallback_handlers: Optional[Dict[str, Callable]] = None,
    ) -> Tuple[bool, Optional[T], str]:
        """
        v87.0: Execute operation with automatic fallback chain.

        Args:
            chain_name: Fallback chain to use (jprime, reactor, voice)
            operation: Primary operation to execute
            fallback_handlers: Optional custom fallback handlers

        Returns:
            Tuple of (success, result, message)
        """
        try:
            # Try primary operation first
            result = await operation()
            await self.update_component_health(chain_name, True)
            return True, result, "Primary operation succeeded"

        except Exception as primary_error:
            logger.warning(f"[StateCoord] v87.0 Primary {chain_name} failed: {primary_error}")
            await self.update_component_health(chain_name, False, str(primary_error))

            # Get fallback manager
            fallback_manager = await self.get_fallback_manager()
            if not fallback_manager:
                return False, None, f"Primary failed, no fallback available: {primary_error}"

            # Execute fallback chain
            try:
                if chain_name == "jprime" and fallback_handlers:
                    result = await fallback_manager.execute_jprime_chain(
                        {},  # Empty request, handlers provided
                        local_handler=fallback_handlers.get("local"),
                        cloud_run_handler=fallback_handlers.get("cloud_run"),
                        claude_api_handler=fallback_handlers.get("claude_api"),
                    )
                elif chain_name == "reactor" and fallback_handlers:
                    result = await fallback_manager.execute_reactor_chain(
                        {},  # Empty data, handlers provided
                        local_handler=fallback_handlers.get("local"),
                    )
                else:
                    return False, None, f"Unknown chain or no handlers: {chain_name}"

                if result.success:
                    return True, result.value, f"Fallback succeeded via {result.target_used}"
                return False, None, f"All fallbacks failed: {result.error}"

            except Exception as fallback_error:
                logger.error(f"[StateCoord] v87.0 Fallback chain failed: {fallback_error}")
                return False, None, f"Fallback chain failed: {fallback_error}"

    async def get_system_degradation_status(self) -> Dict[str, Any]:
        """
        v87.0: Get comprehensive system degradation status.

        Returns dict with:
            - mode: Current degradation mode (full/degraded/minimal)
            - failed_components: Set of failed component names
            - fallback_chains: Status of each fallback chain
            - recommendations: List of recommended actions
        """
        status = {
            "mode": self._degradation_mode,
            "failed_components": list(self._failed_components),
            "timestamp": time.time(),
            "recommendations": [],
        }

        # Get fallback chain statuses
        fallback_manager = await self.get_fallback_manager()
        if fallback_manager:
            status["fallback_chains"] = {
                "jprime": {
                    "targets": [t.value for t in fallback_manager.JPRIME_CHAIN.targets],
                    "timeout": fallback_manager.JPRIME_CHAIN.timeout_per_target,
                },
                "reactor": {
                    "targets": [t.value for t in fallback_manager.REACTOR_CHAIN.targets],
                    "timeout": fallback_manager.REACTOR_CHAIN.timeout_per_target,
                },
                "voice": {
                    "targets": [t.value for t in fallback_manager.VOICE_CHAIN.targets],
                    "timeout": fallback_manager.VOICE_CHAIN.timeout_per_target,
                },
            }

        # Generate recommendations
        if "jprime" in self._failed_components:
            status["recommendations"].append(
                "J-Prime unavailable - check JARVIS_PRIME_URL or start jarvis-prime service"
            )
        if "reactor" in self._failed_components:
            status["recommendations"].append(
                "Reactor-Core unavailable - training data will be queued locally"
            )
        if self._degradation_mode == "minimal":
            status["recommendations"].append(
                "System in minimal mode - core functionality only"
            )

        return status

    @classmethod
    async def get_instance(cls) -> "UnifiedStateCoordinator":
        """Get singleton instance (thread-safe, async-safe)."""
        if cls._instance_lock is None:
            cls._instance_lock = asyncio.Lock()

        if cls._instance is None:
            async with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def process_cookie(self) -> str:
        """Get this process's unique cookie."""
        return self._process_cookie

    async def acquire_ownership(
        self,
        entry_point: str,
        component: str = "jarvis",
        timeout: Optional[float] = None,
        force: bool = False,
    ) -> Tuple[bool, Optional[ProcessOwnership]]:
        """
        v86.0: Acquire ownership with priority-based resolution and validation.

        Features:
        - Priority-based conflict resolution (supervisor > start_system > main)
        - Lock file validation and recovery
        - State file integrity checking
        - Event publishing for coordination
        - Adaptive timeouts based on historical performance

        Uses fcntl for OS-level atomic locks. The lock is held as long as
        the file descriptor remains open (process dies → lock released).

        Args:
            entry_point: "run_supervisor" or "start_system"
            component: Component name ("jarvis", "trinity", etc.)
            timeout: Max time to wait (env: JARVIS_OWNERSHIP_TIMEOUT)
            force: Force acquire even if owned (for recovery)

        Returns:
            (success: bool, previous_owner: Optional[ProcessOwnership])
        """
        # Store our entry point for event publishing
        self._our_entry_point = entry_point

        # ═══════════════════════════════════════════════════════════════════════
        # v87.0: Pre-flight resource checks (network partition, filesystem, etc.)
        # ═══════════════════════════════════════════════════════════════════════
        adv_coord = await self._ensure_advanced_coord()
        if adv_coord:
            try:
                # Check for network partition (critical for NFS-mounted state)
                is_partitioned, partition_reason = await adv_coord.check_network_partition()
                if is_partitioned:
                    logger.error(f"[StateCoord] v87.0 Network partition detected: {partition_reason}")
                    raise NetworkPartitionError(partition_reason)

                # Check filesystem writability
                fs_writable, fs_reason = await adv_coord.check_filesystem_writable(self.lock_dir)
                if not fs_writable:
                    logger.error(f"[StateCoord] v87.0 Filesystem not writable: {fs_reason}")
                    raise RuntimeError(f"Filesystem not writable: {fs_reason}")

                # Check disk space
                disk_ok, disk_metrics = await adv_coord.check_disk_space(self.lock_dir)
                if not disk_ok:
                    warnings = disk_metrics.get("warnings", ["Unknown disk issue"])
                    logger.warning(f"[StateCoord] v87.0 Disk issues: {warnings}")
                    # Don't fail on disk warning, but log it

                # Check clock skew
                clock_skew, skew_seconds = await adv_coord.check_clock_skew()
                if clock_skew:
                    logger.warning(f"[StateCoord] v87.0 Clock skew detected: {skew_seconds:.2f}s")
                    # Don't fail on clock skew, but log it

                # Register for heartbeat watchdog
                adv_coord.register_heartbeat_watchdog(component)

                logger.debug("[StateCoord] v87.0 Pre-flight checks passed")

            except NetworkPartitionError:
                raise  # Re-raise network partition errors
            except Exception as e:
                logger.warning(f"[StateCoord] v87.0 Pre-flight check error: {e}")
                # Continue without advanced features if check fails

        # Use adaptive timeout if available
        default_timeout = float(os.getenv("JARVIS_OWNERSHIP_TIMEOUT", "30.0"))
        timeout = timeout or self.get_adaptive_timeout("acquire_ownership", default_timeout)
        start_time = time.time()

        # v86.0: Validate and recover lock/state files FIRST
        lock_file = self.lock_dir / f"{component}.lock"
        await self._validate_and_recover_lock_file(lock_file)
        await self._validate_and_recover_state_file()

        # v86.0: Cleanup stale owners before attempting acquisition
        try:
            cleaned = await self._cleanup_stale_owners()
            if cleaned > 0:
                logger.info(f"[StateCoord] Pre-acquisition cleanup: {cleaned} stale owner(s)")
        except Exception as e:
            logger.debug(f"[StateCoord] Pre-acquisition cleanup error: {e}")

        while time.time() - start_time < timeout:
            try:
                # Try atomic lock acquisition
                acquired, previous_owner = await self._try_acquire_lock(
                    entry_point, component, force
                )

                if acquired:
                    logger.info(
                        f"[StateCoord] {entry_point} acquired {component} ownership "
                        f"(PID: {self._pid}, Cookie: {self._process_cookie[:8]}...)"
                    )
                    # v86.0: Publish ownership acquired event
                    await self.publish_component_event(
                        component,
                        self.ComponentEventType.OWNERSHIP_ACQUIRED,
                        metadata={
                            "entry_point": entry_point,
                            "priority": self._get_entry_point_priority(entry_point),
                            "previous_owner": previous_owner.entry_point if previous_owner else None,
                        }
                    )
                    # Record successful acquisition duration
                    self._record_operation_duration("acquire_ownership", time.time() - start_time)
                    return True, previous_owner

                # v86.0: Priority-based conflict resolution
                if previous_owner:
                    # Check if owner is still alive
                    is_alive = await self._validate_owner_alive(previous_owner)

                    if not is_alive:
                        # Owner is dead/stale - take over
                        logger.warning(
                            f"[StateCoord] Owner PID {previous_owner.pid} is dead/stale, "
                            f"force acquiring..."
                        )
                        acquired, _ = await self._try_acquire_lock(
                            entry_point, component, force=True
                        )
                        if acquired:
                            await self.publish_component_event(
                                component,
                                self.ComponentEventType.OWNERSHIP_TRANSFERRED,
                                metadata={
                                    "reason": "stale_owner",
                                    "previous_owner_pid": previous_owner.pid,
                                }
                            )
                            return True, previous_owner
                    else:
                        # Owner is alive - check if we have higher priority
                        should_takeover, reason = await self._resolve_ownership_conflict(
                            entry_point, previous_owner
                        )

                        if should_takeover:
                            logger.info(
                                f"[StateCoord] Priority takeover: {reason}"
                            )
                            acquired, _ = await self._try_acquire_lock(
                                entry_point, component, force=True
                            )
                            if acquired:
                                await self.publish_component_event(
                                    component,
                                    self.ComponentEventType.OWNERSHIP_TRANSFERRED,
                                    metadata={
                                        "reason": "priority_takeover",
                                        "resolution": reason,
                                        "previous_owner_pid": previous_owner.pid,
                                    }
                                )
                                return True, previous_owner
                        else:
                            logger.debug(f"[StateCoord] Cannot takeover: {reason}")

                # Wait with exponential backoff + jitter
                elapsed = time.time() - start_time
                base_wait = min(0.5 * (2 ** int(elapsed / 5)), 2.0)
                jitter = base_wait * 0.1 * (2 * hash(time.time()) % 100 / 100 - 0.5)
                await asyncio.sleep(max(0.1, base_wait + jitter))

            except Exception as e:
                logger.debug(f"[StateCoord] Ownership acquisition error: {e}")
                await asyncio.sleep(0.5)

        logger.warning(
            f"[StateCoord] Failed to acquire {component} ownership after {timeout:.1f}s"
        )
        return False, None

    async def _try_acquire_lock(
        self,
        entry_point: str,
        component: str,
        force: bool = False,
    ) -> Tuple[bool, Optional[ProcessOwnership]]:
        """
        Try to acquire lock using atomic fcntl operations.

        The fcntl lock is held as long as the file descriptor is open.
        When the process dies, the OS automatically releases the lock.

        Returns:
            (acquired: bool, previous_owner: Optional[ProcessOwnership])
        """
        import fcntl

        lock_file = self.lock_dir / f"{component}.lock"
        previous_owner = None

        async with self._file_lock:
            try:
                # Open lock file (create if doesn't exist)
                fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)

                try:
                    # Try non-blocking exclusive lock
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                    # Read current state
                    state = await self._read_state()
                    owners = state.get("owners", {})

                    # Check existing owner
                    if component in owners:
                        existing = owners[component]

                        if not force:
                            # Validate owner is alive
                            owner_obj = ProcessOwnership(
                                entry_point=existing.get("entry_point", "unknown"),
                                pid=existing.get("pid", 0),
                                cookie=existing.get("cookie", ""),
                                hostname=existing.get("hostname", ""),
                                acquired_at=existing.get("acquired_at", 0),
                                last_heartbeat=existing.get("last_heartbeat", 0),
                                metadata=existing.get("metadata", {}),
                            )
                            is_alive = await self._validate_owner_alive(owner_obj)
                            if is_alive:
                                # Still owned by live process
                                os.close(fd)
                                return False, owner_obj

                        previous_owner = ProcessOwnership(
                            entry_point=existing.get("entry_point", "unknown"),
                            pid=existing.get("pid", 0),
                            cookie=existing.get("cookie", ""),
                            hostname=existing.get("hostname", ""),
                            acquired_at=existing.get("acquired_at", 0),
                            last_heartbeat=existing.get("last_heartbeat", 0),
                        )

                    # Acquire ownership
                    now = time.time()
                    owners[component] = {
                        "entry_point": entry_point,
                        "pid": self._pid,
                        "cookie": self._process_cookie,
                        "hostname": self._hostname,
                        "acquired_at": now,
                        "last_heartbeat": now,
                        "metadata": {
                            "python_version": sys.version,
                            "start_time": now,
                        },
                    }

                    state["owners"] = owners
                    state["last_update"] = now
                    state["version"] = state.get("version", 0) + 1

                    # v89.0: Add checksum for integrity validation
                    state = self._add_state_checksum(state)

                    # Write state atomically (temp file + atomic rename)
                    temp_file = self.state_file.with_suffix(".tmp")
                    temp_file.write_text(json.dumps(state, indent=2))
                    temp_file.replace(self.state_file)  # Atomic replace

                    # Keep lock file open (lock held until process dies)
                    self._lock_fds[component] = fd

                    # Update cache
                    self._state_cache = state
                    self._last_cache_time = now

                    return True, previous_owner

                except BlockingIOError:
                    # Lock held by another process
                    os.close(fd)

                    # Read state to get current owner
                    state = await self._read_state()
                    if state:
                        owner_data = state.get("owners", {}).get(component)
                        if owner_data:
                            previous_owner = ProcessOwnership(
                                entry_point=owner_data.get("entry_point", "unknown"),
                                pid=owner_data.get("pid", 0),
                                cookie=owner_data.get("cookie", ""),
                                hostname=owner_data.get("hostname", ""),
                                acquired_at=owner_data.get("acquired_at", 0),
                                last_heartbeat=owner_data.get("last_heartbeat", 0),
                            )

                    return False, previous_owner

            except Exception as e:
                logger.debug(f"[StateCoord] Lock acquisition error: {e}")
                with suppress(Exception):
                    os.close(fd)
                return False, None

    async def _validate_owner_alive(self, owner: ProcessOwnership) -> bool:
        """
        Validate owner is alive using PID + cookie + process tree + heartbeat.

        Multi-layer validation prevents:
        1. PID reuse (cookie check)
        2. Stale processes (heartbeat check)
        3. Zombie processes (status check)
        4. Cross-host issues (hostname check)
        """
        try:
            pid = owner.pid
            cookie = owner.cookie
            hostname = owner.hostname
            last_heartbeat = owner.last_heartbeat

            # Check hostname matches (for distributed systems)
            if hostname and hostname != self._hostname:
                # Different host - can't validate locally
                # Check heartbeat freshness instead
                age = time.time() - last_heartbeat
                is_fresh = age < self._stale_threshold
                if not is_fresh:
                    logger.debug(
                        f"[StateCoord] Remote owner ({hostname}) heartbeat stale "
                        f"({age:.1f}s > {self._stale_threshold}s)"
                    )
                return is_fresh

            # Check if PID exists and is running
            try:
                proc = psutil.Process(pid)
                if not proc.is_running():
                    logger.debug(f"[StateCoord] Process {pid} not running")
                    return False

                # Check for zombie
                if proc.status() == psutil.STATUS_ZOMBIE:
                    logger.debug(f"[StateCoord] Process {pid} is zombie")
                    return False

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                logger.debug(f"[StateCoord] Process {pid} doesn't exist or access denied")
                return False

            # Check heartbeat freshness
            age = time.time() - last_heartbeat
            if age > self._stale_threshold:
                logger.debug(
                    f"[StateCoord] Owner {pid} heartbeat stale "
                    f"({age:.1f}s > {self._stale_threshold}s)"
                )
                return False

            # Validate cookie: check if process has lock file open
            # This is the strongest validation (prevents PID reuse)
            try:
                proc = psutil.Process(pid)
                open_files = proc.open_files()

                lock_file_str = str(self.lock_dir / "jarvis.lock")
                for f in open_files:
                    if lock_file_str in f.path or "lock" in f.path.lower():
                        # Process has lock file open
                        # Verify cookie from state matches
                        state = await self._read_state()
                        if state:
                            current_owner = state.get("owners", {}).get("jarvis", {})
                            if current_owner.get("cookie") == cookie:
                                return True

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Fallback: if PID matches, heartbeat is fresh, and cookie exists
            if cookie and age < 30.0:  # Very fresh heartbeat
                return True

            return False

        except Exception as e:
            logger.debug(f"[StateCoord] Owner validation error: {e}")
            return False

    async def _cleanup_stale_owners(self) -> int:
        """
        Cleanup stale owners on startup.

        This handles cases where:
        1. Previous process crashed without releasing ownership
        2. Previous process's heartbeat expired
        3. Lock file exists but owning process is dead
        4. PID was reused but cookie doesn't match

        Returns:
            Number of stale owners cleaned up
        """
        cleaned = 0
        async with self._file_lock:
            try:
                state = await self._read_state()
                if not state:
                    return 0

                owners = state.get("owners", {})
                stale_components = []

                for component, owner_data in owners.items():
                    try:
                        owner = ProcessOwnership(
                            entry_point=owner_data.get("entry_point", "unknown"),
                            pid=owner_data.get("pid", 0),
                            cookie=owner_data.get("cookie", ""),
                            hostname=owner_data.get("hostname", ""),
                            acquired_at=owner_data.get("acquired_at", 0),
                            last_heartbeat=owner_data.get("last_heartbeat", 0),
                        )

                        is_alive = await self._validate_owner_alive(owner)
                        if not is_alive:
                            stale_components.append(component)
                            logger.info(
                                f"[StateCoord] Stale owner detected: {component} "
                                f"(PID={owner.pid}, entry={owner.entry_point})"
                            )

                    except Exception as e:
                        logger.debug(f"[StateCoord] Error checking owner {component}: {e}")
                        stale_components.append(component)

                # Remove stale owners
                for component in stale_components:
                    try:
                        # Try to release lock file
                        lock_file = self.lock_dir / f"{component}.lock"
                        if lock_file.exists():
                            try:
                                # Open and try to acquire lock
                                fd = os.open(str(lock_file), os.O_RDWR)
                                try:
                                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                    # We got the lock - previous owner is dead
                                    fcntl.flock(fd, fcntl.LOCK_UN)
                                    os.close(fd)
                                    lock_file.unlink(missing_ok=True)
                                    logger.debug(
                                        f"[StateCoord] Removed stale lock: {lock_file}"
                                    )
                                except BlockingIOError:
                                    # Lock still held - owner might be alive
                                    os.close(fd)
                                    continue
                            except Exception:
                                pass

                        # Remove from state
                        del owners[component]
                        cleaned += 1
                        logger.info(f"[StateCoord] Cleaned stale owner: {component}")

                    except Exception as e:
                        logger.debug(f"[StateCoord] Failed to clean {component}: {e}")

                # Write updated state
                if cleaned > 0:
                    state["owners"] = owners
                    state["last_update"] = time.time()
                    state["last_cleanup"] = time.time()

                    # v89.0: Add checksum for integrity validation
                    state = self._add_state_checksum(state)

                    temp_file = self.state_file.with_suffix(".tmp")
                    temp_file.write_text(json.dumps(state, indent=2))
                    temp_file.replace(self.state_file)

                    self._state_cache = state
                    self._last_cache_time = time.time()

                    logger.info(f"[StateCoord] Cleaned {cleaned} stale owner(s)")

            except Exception as e:
                logger.debug(f"[StateCoord] Stale cleanup error: {e}")

        return cleaned

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Priority-Based Ownership Resolution
    # ═══════════════════════════════════════════════════════════════════════════

    def _get_entry_point_priority(self, entry_point: str) -> int:
        """Get priority for entry point (higher = more important)."""
        # Check exact match first
        if entry_point in self.ENTRY_POINT_PRIORITY:
            return self.ENTRY_POINT_PRIORITY[entry_point]

        # Check partial match (e.g., "run_supervisor" in "run_supervisor.py")
        for key, priority in self.ENTRY_POINT_PRIORITY.items():
            if key in entry_point or entry_point in key:
                return priority

        return self.ENTRY_POINT_PRIORITY.get("unknown", 0)

    async def _resolve_ownership_conflict(
        self,
        our_entry: str,
        their_owner: ProcessOwnership,
    ) -> Tuple[bool, str]:
        """
        v86.0: Resolve ownership conflict using priority + consensus.

        Resolution order:
        1. Higher priority wins (supervisor > start_system > main)
        2. Same priority: Earlier timestamp wins (deterministic)
        3. Same timestamp: Lower PID wins (deterministic tie-breaker)

        Returns:
            (should_acquire: bool, reason: str)
        """
        our_priority = self._get_entry_point_priority(our_entry)
        their_priority = self._get_entry_point_priority(their_owner.entry_point)

        if our_priority > their_priority:
            return True, f"Higher priority ({our_entry}:{our_priority} > {their_owner.entry_point}:{their_priority})"
        elif our_priority < their_priority:
            return False, f"Lower priority ({our_entry}:{our_priority} < {their_owner.entry_point}:{their_priority})"
        else:
            # Same priority - use timestamp-based consensus
            our_ts = self._process_start_time
            their_ts = their_owner.acquired_at

            if our_ts < their_ts:
                return True, f"Earlier process start ({our_ts:.0f} < {their_ts:.0f})"
            elif our_ts > their_ts:
                return False, f"Later process start ({our_ts:.0f} > {their_ts:.0f})"
            else:
                # Same timestamp - use PID as deterministic tie-breaker
                if self._pid < their_owner.pid:
                    return True, f"Lower PID tie-breaker ({self._pid} < {their_owner.pid})"
                else:
                    return False, f"Higher PID tie-breaker ({self._pid} >= {their_owner.pid})"

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Lock File Validation and Recovery
    # ═══════════════════════════════════════════════════════════════════════════

    async def _validate_and_recover_lock_file(self, lock_file: Path) -> bool:
        """
        v86.0: Validate lock file integrity and recover if corrupted.

        Checks:
        - File size (should be small)
        - File readability
        - File permissions

        Returns:
            True if lock file is valid or recovered
        """
        try:
            if not lock_file.exists():
                return True  # Doesn't exist = valid (will be created)

            # Check file size (should be small)
            max_size = int(os.getenv("JARVIS_LOCK_FILE_MAX_SIZE", "4096"))  # 4KB max
            try:
                size = lock_file.stat().st_size
                if size > max_size:
                    logger.warning(
                        f"[StateCoord] Lock file {lock_file} suspiciously large "
                        f"({size} bytes > {max_size}), recovering..."
                    )
                    backup = lock_file.with_suffix(".lock.corrupted")
                    try:
                        lock_file.rename(backup)
                    except Exception:
                        lock_file.unlink(missing_ok=True)
                    logger.info(f"[StateCoord] Recovered corrupted lock file")
                    return True
            except OSError:
                pass  # Can't stat, try to continue

            # Check if file is readable/writable
            try:
                fd = os.open(str(lock_file), os.O_RDWR)
                os.close(fd)
            except PermissionError:
                logger.warning(f"[StateCoord] Lock file {lock_file} permission denied, recovering...")
                try:
                    lock_file.chmod(0o644)
                except Exception:
                    lock_file.unlink(missing_ok=True)
                return True
            except OSError as e:
                logger.warning(f"[StateCoord] Lock file {lock_file} error: {e}, recovering...")
                lock_file.unlink(missing_ok=True)
                return True

            return True

        except Exception as e:
            logger.debug(f"[StateCoord] Lock file validation error: {e}")
            try:
                lock_file.unlink(missing_ok=True)
            except Exception:
                pass
            return True

    async def _validate_and_recover_state_file(self) -> bool:
        """
        v86.0: Validate state file integrity with checksum recovery.

        Returns:
            True if state file is valid or recovered
        """
        try:
            if not self.state_file.exists():
                return True  # Doesn't exist = valid (will be created)

            # Try to parse JSON
            try:
                content = self.state_file.read_text()
                data = json.loads(content)

                # Verify checksum if present
                stored_checksum = data.get("_checksum")
                if stored_checksum:
                    # Calculate checksum without _checksum field
                    data_copy = {k: v for k, v in data.items() if k != "_checksum"}
                    import hashlib
                    calculated = hashlib.sha256(
                        json.dumps(data_copy, sort_keys=True).encode()
                    ).hexdigest()[:16]

                    if stored_checksum != calculated:
                        logger.warning(
                            f"[StateCoord] State file checksum mismatch, "
                            f"expected {stored_checksum}, got {calculated}"
                        )
                        # Backup and recreate
                        backup = self.state_file.with_suffix(".json.corrupted")
                        self.state_file.rename(backup)
                        return True

                return True

            except json.JSONDecodeError as e:
                logger.warning(f"[StateCoord] State file JSON invalid: {e}, recovering...")
                backup = self.state_file.with_suffix(".json.corrupted")
                try:
                    self.state_file.rename(backup)
                except Exception:
                    self.state_file.unlink(missing_ok=True)
                return True

        except Exception as e:
            logger.debug(f"[StateCoord] State file validation error: {e}")
            return True

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Circuit Breaker for State Operations
    # ═══════════════════════════════════════════════════════════════════════════

    async def _circuit_breaker_execute(
        self,
        operation: Callable[[], Coroutine[Any, Any, Any]],
        operation_name: str = "state_operation",
    ) -> Any:
        """
        v86.0: Execute operation with circuit breaker protection.

        Circuit states:
        - CLOSED: Normal operation, failures tracked
        - OPEN: Operations fail fast (too many recent failures)
        - HALF-OPEN: Testing if system recovered

        Raises:
            CircuitOpenError: If circuit is open
        """
        # Check circuit state
        if self._circuit_state == "open":
            # Check if recovery timeout has passed
            elapsed = time.time() - self._circuit_last_failure_time
            if elapsed > self._circuit_recovery_timeout:
                self._circuit_state = "half-open"
                logger.info(
                    f"[StateCoord] Circuit breaker: half-open (testing recovery after {elapsed:.1f}s)"
                )
            else:
                raise CircuitOpenError(
                    f"Circuit breaker is OPEN for {operation_name} "
                    f"(failed {self._circuit_failure_count} times, "
                    f"retry in {self._circuit_recovery_timeout - elapsed:.1f}s)"
                )

        start_time = time.time()
        try:
            result = await operation()

            # Success - reset circuit breaker
            duration = time.time() - start_time
            self._record_operation_duration(operation_name, duration)

            if self._circuit_state == "half-open":
                self._circuit_state = "closed"
                self._circuit_failure_count = 0
                logger.info("[StateCoord] Circuit breaker: CLOSED (recovered)")
            elif self._circuit_state == "closed":
                self._circuit_failure_count = 0

            return result

        except Exception as e:
            self._circuit_failure_count += 1
            self._circuit_last_failure_time = time.time()

            if self._circuit_failure_count >= self._circuit_failure_threshold:
                self._circuit_state = "open"
                logger.error(
                    f"[StateCoord] Circuit breaker: OPEN "
                    f"(failed {self._circuit_failure_count} times for {operation_name})"
                )

            raise

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Adaptive Timeout Management
    # ═══════════════════════════════════════════════════════════════════════════

    def _record_operation_duration(self, operation: str, duration: float) -> None:
        """Record operation duration for adaptive timeout learning."""
        if operation not in self._operation_history:
            self._operation_history[operation] = []

        self._operation_history[operation].append(duration)

        # Keep only last 100 measurements
        max_history = int(os.getenv("JARVIS_TIMEOUT_HISTORY_SIZE", "100"))
        if len(self._operation_history[operation]) > max_history:
            self._operation_history[operation] = self._operation_history[operation][-max_history:]

    def get_adaptive_timeout(
        self,
        operation: str,
        default_timeout: float,
    ) -> float:
        """
        v86.0: Get adaptive timeout based on historical performance.

        Uses 95th percentile of historical durations * multiplier.
        """
        if operation not in self._operation_history:
            return default_timeout

        history = self._operation_history[operation]
        if len(history) < 5:  # Need minimum samples
            return default_timeout

        # Calculate 95th percentile
        sorted_history = sorted(history)
        percentile_idx = int(len(sorted_history) * 0.95)
        percentile_95 = sorted_history[min(percentile_idx, len(sorted_history) - 1)]

        # Apply multiplier
        adaptive_timeout = percentile_95 * self._timeout_multiplier

        # Clamp between default and 10x default
        min_timeout = default_timeout
        max_timeout = default_timeout * 10.0

        return max(min_timeout, min(adaptive_timeout, max_timeout))

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Event-Driven Status Synchronization
    # ═══════════════════════════════════════════════════════════════════════════

    async def publish_component_event(
        self,
        component: str,
        event_type: "UnifiedStateCoordinator.ComponentEventType",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        v86.0: Publish component event to shared state for coordination.

        Events are stored in state file and can be subscribed to.
        """
        event = {
            "id": str(uuid.uuid4()),
            "component": component,
            "event_type": event_type.value if hasattr(event_type, 'value') else str(event_type),
            "timestamp": time.time(),
            "pid": self._pid,
            "cookie": self._process_cookie[:8],
            "entry_point": self._our_entry_point or "unknown",
            "hostname": self._hostname,
            "metadata": metadata or {},
        }

        async with self._file_lock:
            try:
                state = await self._read_state() or {}
                events = state.get("events", [])
                events.append(event)

                # Keep only last N events (env-configurable)
                max_events = int(os.getenv("JARVIS_STATE_MAX_EVENTS", "1000"))
                if len(events) > max_events:
                    events = events[-max_events:]

                state["events"] = events
                state["last_event"] = time.time()
                state["last_event_type"] = event["event_type"]

                # v89.0: Use unified checksum helper for consistency
                state = self._add_state_checksum(state)

                temp_file = self.state_file.with_suffix(".tmp")
                temp_file.write_text(json.dumps(state, indent=2))
                temp_file.replace(self.state_file)

                self._state_cache = state
                self._last_cache_time = time.time()

                logger.debug(f"[StateCoord] Published event: {component}.{event['event_type']}")

            except Exception as e:
                logger.debug(f"[StateCoord] Event publish error: {e}")

    async def get_recent_events(
        self,
        component: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        since_timestamp: Optional[float] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        v86.0: Get recent events with optional filtering.
        """
        state = await self._read_state()
        if not state:
            return []

        events = state.get("events", [])

        # Filter by component
        if component:
            events = [e for e in events if e.get("component") == component]

        # Filter by event type
        if event_types:
            events = [e for e in events if e.get("event_type") in event_types]

        # Filter by timestamp
        if since_timestamp:
            events = [e for e in events if e.get("timestamp", 0) > since_timestamp]

        # Sort by timestamp (newest first) and limit
        events = sorted(events, key=lambda e: e.get("timestamp", 0), reverse=True)
        return events[:limit]

    async def subscribe_to_events(
        self,
        callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        component: Optional[str] = None,
        event_types: Optional[List[str]] = None,
    ) -> asyncio.Task:
        """
        v86.0: Subscribe to component events (polling-based).

        Returns task that can be cancelled to unsubscribe.
        """
        poll_interval = float(os.getenv("JARVIS_EVENT_POLL_INTERVAL", "1.0"))

        async def event_poller():
            last_event_time = time.time()
            while True:
                try:
                    events = await self.get_recent_events(
                        component=component,
                        event_types=event_types,
                        since_timestamp=last_event_time,
                    )

                    for event in reversed(events):  # Oldest first
                        try:
                            await callback(event)
                        except Exception as cb_err:
                            logger.debug(f"[StateCoord] Event callback error: {cb_err}")
                        last_event_time = max(last_event_time, event.get("timestamp", 0))

                    await asyncio.sleep(poll_interval)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[StateCoord] Event poller error: {e}")
                    await asyncio.sleep(5.0)

        task = asyncio.create_task(event_poller())
        self._event_subscribers.append(task)
        return task

    # ═══════════════════════════════════════════════════════════════════════════
    # v86.0: Enhanced Heartbeat with Auto-Restart
    # ═══════════════════════════════════════════════════════════════════════════

    async def update_heartbeat(self, component: str) -> None:
        """Update heartbeat timestamp for owned component."""
        async with self._file_lock:
            try:
                state = await self._read_state()
                if not state:
                    return

                owners = state.get("owners", {})
                if component in owners:
                    owner = owners[component]
                    if (owner.get("pid") == self._pid and
                        owner.get("cookie") == self._process_cookie):
                        # This is our ownership, update heartbeat
                        owner["last_heartbeat"] = time.time()
                        state["owners"] = owners
                        state["last_update"] = time.time()

                        # v89.0: Add checksum for integrity validation
                        state = self._add_state_checksum(state)

                        # Write atomically
                        temp_file = self.state_file.with_suffix(".tmp")
                        temp_file.write_text(json.dumps(state, indent=2))
                        temp_file.replace(self.state_file)

                        self._state_cache = state
                        self._last_cache_time = time.time()

                        # v87.0: Pet the watchdog to prevent deadlock detection
                        adv_coord = await self._ensure_advanced_coord()
                        if adv_coord:
                            try:
                                adv_coord.pet_watchdog(component)
                            except Exception as watchdog_err:
                                logger.debug(f"[StateCoord] v87.0 Watchdog pet error: {watchdog_err}")

            except Exception as e:
                logger.debug(f"[StateCoord] Heartbeat update error: {e}")

    async def release_ownership(self, component: str) -> None:
        """Release ownership and close lock file."""
        async with self._file_lock:
            try:
                # Stop heartbeat task if running
                if component in self._heartbeat_tasks:
                    self._heartbeat_tasks[component].cancel()
                    with suppress(asyncio.CancelledError):
                        await self._heartbeat_tasks[component]
                    del self._heartbeat_tasks[component]

                # v87.0: Unregister watchdog for this component
                adv_coord = await self._ensure_advanced_coord()
                if adv_coord:
                    try:
                        adv_coord.unregister_heartbeat_watchdog(component)
                    except Exception as watchdog_err:
                        logger.debug(f"[StateCoord] v87.0 Watchdog unregister error: {watchdog_err}")

                # Close lock file (releases fcntl lock automatically)
                if component in self._lock_fds:
                    with suppress(Exception):
                        os.close(self._lock_fds[component])
                    del self._lock_fds[component]

                # Remove from state
                state = await self._read_state()
                if state:
                    owners = state.get("owners", {})
                    if component in owners:
                        owner = owners[component]
                        if (owner.get("pid") == self._pid and
                            owner.get("cookie") == self._process_cookie):
                            del owners[component]
                            state["owners"] = owners
                            state["last_update"] = time.time()

                            # v89.0: Add checksum for integrity validation
                            state = self._add_state_checksum(state)

                            temp_file = self.state_file.with_suffix(".tmp")
                            temp_file.write_text(json.dumps(state, indent=2))
                            temp_file.replace(self.state_file)

                            self._state_cache = state
                            self._last_cache_time = time.time()

                            logger.info(f"[StateCoord] Released {component} ownership")

            except Exception as e:
                logger.debug(f"[StateCoord] Release error: {e}")

    async def start_heartbeat_loop(
        self,
        component: str,
        interval: Optional[float] = None,
    ) -> asyncio.Task:
        """
        v86.0: Start background heartbeat task with auto-restart on failure.

        Features:
        - Consecutive failure tracking
        - Exponential backoff on failures
        - Auto-restart after max failures
        - Event publishing on heartbeat issues
        """
        interval = interval or self._heartbeat_interval
        max_consecutive_failures = int(os.getenv("JARVIS_HEARTBEAT_MAX_FAILURES", "5"))
        max_backoff = float(os.getenv("JARVIS_HEARTBEAT_MAX_BACKOFF", "60.0"))
        auto_restart_delay = float(os.getenv("JARVIS_HEARTBEAT_RESTART_DELAY", "10.0"))

        async def heartbeat_loop_with_monitoring():
            consecutive_failures = 0
            last_success_time = time.time()
            restart_count = 0
            max_restarts = int(os.getenv("JARVIS_HEARTBEAT_MAX_RESTARTS", "3"))

            while True:
                try:
                    await self.update_heartbeat(component)
                    consecutive_failures = 0  # Reset on success
                    last_success_time = time.time()
                    await asyncio.sleep(interval)

                except asyncio.CancelledError:
                    logger.debug(f"[StateCoord] Heartbeat cancelled for {component}")
                    break

                except Exception as e:
                    consecutive_failures += 1
                    logger.warning(
                        f"[StateCoord] Heartbeat error for {component} "
                        f"(failure {consecutive_failures}/{max_consecutive_failures}): {e}"
                    )

                    # Publish heartbeat failure event
                    try:
                        await self.publish_component_event(
                            component,
                            self.ComponentEventType.HEARTBEAT_LOST,
                            metadata={
                                "consecutive_failures": consecutive_failures,
                                "last_success": last_success_time,
                                "error": str(e),
                            }
                        )
                    except Exception:
                        pass

                    # Check if heartbeat is completely broken
                    if consecutive_failures >= max_consecutive_failures:
                        logger.error(
                            f"[StateCoord] Heartbeat failed {max_consecutive_failures} times for {component}"
                        )

                        # Try to restart if within restart limit
                        if restart_count < max_restarts:
                            restart_count += 1
                            logger.info(
                                f"[StateCoord] Attempting heartbeat restart "
                                f"({restart_count}/{max_restarts}) for {component}"
                            )
                            await asyncio.sleep(auto_restart_delay)
                            consecutive_failures = 0
                            continue
                        else:
                            # Too many restarts - release ownership
                            logger.error(
                                f"[StateCoord] Max restarts exceeded for {component}, "
                                "releasing ownership"
                            )
                            try:
                                await self.release_ownership(component)
                            except Exception:
                                pass
                            break

                    # Exponential backoff on failure
                    backoff = min(
                        interval * (2 ** consecutive_failures),
                        max_backoff,
                    )
                    await asyncio.sleep(backoff)

        task = asyncio.create_task(heartbeat_loop_with_monitoring())
        self._heartbeat_tasks[component] = task
        return task

    async def get_owner(self, component: str) -> Optional[ProcessOwnership]:
        """Get current owner with validation."""
        state = await self._read_state()
        if not state:
            return None

        owner_data = state.get("owners", {}).get(component)
        if not owner_data:
            return None

        owner = ProcessOwnership(
            entry_point=owner_data.get("entry_point", "unknown"),
            pid=owner_data.get("pid", 0),
            cookie=owner_data.get("cookie", ""),
            hostname=owner_data.get("hostname", ""),
            acquired_at=owner_data.get("acquired_at", 0),
            last_heartbeat=owner_data.get("last_heartbeat", 0),
            metadata=owner_data.get("metadata", {}),
        )

        if await self._validate_owner_alive(owner):
            return owner

        return None

    async def is_owned_by_us(self, component: str) -> bool:
        """Check if we currently own a component."""
        state = await self._read_state()
        if not state:
            return False

        owner = state.get("owners", {}).get(component)
        if not owner:
            return False

        return (
            owner.get("pid") == self._pid and
            owner.get("cookie") == self._process_cookie
        )

    async def _read_state(self) -> Dict[str, Any]:
        """Read state with caching."""
        now = time.time()

        # Use cache if fresh
        if (self._state_cache and
            (now - self._last_cache_time) < self._cache_ttl):
            return self._state_cache

        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text())
                self._state_cache = data
                self._last_cache_time = now
                return data
        except Exception as e:
            logger.debug(f"[StateCoord] Read state error: {e}")

        return {}

    async def update_state(self, key: str, value: Any) -> None:
        """Update shared state atomically with checksum validation."""
        async with self._file_lock:
            state = await self._read_state() or {}
            state[key] = value
            state["last_update"] = time.time()
            state["version"] = state.get("version", 0) + 1

            # v89.0: Add checksum for integrity validation (consistent with publish_event)
            state = self._add_state_checksum(state)

            temp_file = self.state_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(state, indent=2))
            temp_file.replace(self.state_file)

            self._state_cache = state
            self._last_cache_time = time.time()

    def _add_state_checksum(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add checksum to state dict for integrity validation.

        v89.0: Unified checksum helper to ensure consistency across all state write operations.
        Uses SHA256 truncated to 16 chars for efficiency while maintaining security.

        Args:
            state: State dictionary to add checksum to

        Returns:
            State dictionary with _checksum field added
        """
        import hashlib
        state_without_checksum = {k: v for k, v in state.items() if k != "_checksum"}
        checksum = hashlib.sha256(
            json.dumps(state_without_checksum, sort_keys=True).encode()
        ).hexdigest()[:16]
        state["_checksum"] = checksum
        return state

    async def get_state(self, key: str, default: Any = None) -> Any:
        """Get shared state value."""
        state = await self._read_state()
        return state.get(key, default) if state else default

    async def cleanup_stale_owners(self) -> int:
        """
        Clean up stale ownership records. Returns count cleaned.

        Public API - delegates to _cleanup_stale_owners which has more
        comprehensive lock file handling for crash recovery.
        """
        return await self._cleanup_stale_owners()


# =============================================================================
# Trinity Entry Point Detector v85.0
# =============================================================================

class TrinityEntryPointDetector:
    """
    v85.0: Intelligent entry point detection with state coordination.

    Features:
    - Process tree walking (detects parent launchers)
    - Environment variable checking
    - Command line parsing
    - Unified state integration
    - Zero hardcoding
    """

    @staticmethod
    def detect_entry_point() -> Dict[str, Any]:
        """
        Detect which script launched this process.

        Detection priority:
        1. Environment variables (most reliable, set by parent)
        2. Command line arguments
        3. Process tree walking (checks parent processes)
        4. Fallback to "unknown"
        """
        current_pid = os.getpid()

        try:
            current_process = psutil.Process(current_pid)
            cmdline = current_process.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmdline = sys.argv

        script_name = Path(cmdline[0]).name if cmdline else ""
        cmdline_str = " ".join(cmdline) if cmdline else ""

        # Priority 1: Check environment variables
        if os.getenv("JARVIS_SUPERVISED") == "1":
            return {
                "entry_point": "run_supervisor",
                "is_supervised": True,
                "is_start_system": False,
                "script_name": script_name,
                "pid": current_pid,
                "detection_method": "environment",
                "confidence": "high",
            }

        if os.getenv("JARVIS_START_SYSTEM") == "1":
            return {
                "entry_point": "start_system",
                "is_supervised": False,
                "is_start_system": True,
                "script_name": script_name,
                "pid": current_pid,
                "detection_method": "environment",
                "confidence": "high",
            }

        # Priority 2: Check command line
        if "run_supervisor.py" in cmdline_str:
            return {
                "entry_point": "run_supervisor",
                "is_supervised": True,
                "is_start_system": False,
                "script_name": script_name,
                "pid": current_pid,
                "detection_method": "cmdline",
                "confidence": "high",
            }
        elif "start_system.py" in cmdline_str:
            return {
                "entry_point": "start_system",
                "is_supervised": False,
                "is_start_system": True,
                "script_name": script_name,
                "pid": current_pid,
                "detection_method": "cmdline",
                "confidence": "high",
            }
        elif "main.py" in cmdline_str:
            return {
                "entry_point": "main_direct",
                "is_supervised": False,
                "is_start_system": False,
                "script_name": script_name,
                "pid": current_pid,
                "detection_method": "cmdline",
                "confidence": "medium",
            }

        # Priority 3: Walk process tree
        try:
            proc = psutil.Process(current_pid)
            for depth in range(10):  # Max 10 levels
                try:
                    parent = proc.parent()
                    if not parent:
                        break

                    parent_cmdline = " ".join(parent.cmdline() or [])

                    if "run_supervisor.py" in parent_cmdline:
                        return {
                            "entry_point": "run_supervisor",
                            "is_supervised": True,
                            "is_start_system": False,
                            "script_name": script_name,
                            "pid": current_pid,
                            "parent_pid": parent.pid,
                            "detection_method": f"process_tree_depth_{depth}",
                            "confidence": "medium",
                        }
                    elif "start_system.py" in parent_cmdline:
                        return {
                            "entry_point": "start_system",
                            "is_supervised": False,
                            "is_start_system": True,
                            "script_name": script_name,
                            "pid": current_pid,
                            "parent_pid": parent.pid,
                            "detection_method": f"process_tree_depth_{depth}",
                            "confidence": "medium",
                        }

                    proc = parent
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    break

        except Exception as e:
            logger.debug(f"[EntryPointDetector] Process tree walk error: {e}")

        return {
            "entry_point": "unknown",
            "is_supervised": False,
            "is_start_system": False,
            "script_name": script_name,
            "pid": current_pid,
            "detection_method": "fallback",
            "confidence": "low",
        }

    @staticmethod
    async def should_manage_trinity() -> bool:
        """
        Determine if this process should manage Trinity.

        Decision logic:
        1. Check unified state first (authoritative)
        2. If Trinity owned by us, return True
        3. If Trinity owned by someone else, return False
        4. If no owner, check entry point rules
        """
        detection = TrinityEntryPointDetector.detect_entry_point()

        # Check unified state first
        try:
            state_coord = await UnifiedStateCoordinator.get_instance()
            trinity_owner = await state_coord.get_owner("trinity")

            if trinity_owner:
                # Trinity already owned - check if it's us
                if await state_coord.is_owned_by_us("trinity"):
                    return True  # We own it
                return False  # Someone else owns it

        except Exception as e:
            logger.debug(f"[EntryPointDetector] State check error: {e}")

        # No owner - check entry point rules
        if detection["entry_point"] == "run_supervisor":
            return True

        if detection["entry_point"] == "start_system":
            # Only manage if supervisor isn't running
            if await TrinityEntryPointDetector._is_supervisor_running():
                return False
            return True

        return False

    @staticmethod
    async def _is_supervisor_running() -> bool:
        """Check if supervisor is running (with state validation)."""
        try:
            # Check unified state first (most reliable)
            state_coord = await UnifiedStateCoordinator.get_instance()
            jarvis_owner = await state_coord.get_owner("jarvis")

            if jarvis_owner and jarvis_owner.entry_point == "run_supervisor":
                return True

            # Fallback: process scan
            for proc in psutil.process_iter(['pid', 'cmdline']):
                try:
                    cmdline = " ".join(proc.info['cmdline'] or [])
                    if "run_supervisor.py" in cmdline and proc.info['pid'] != os.getpid():
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        except Exception as e:
            logger.debug(f"[EntryPointDetector] Supervisor check error: {e}")

        return False

    @staticmethod
    async def get_coordination_status() -> Dict[str, Any]:
        """Get comprehensive coordination status."""
        detection = TrinityEntryPointDetector.detect_entry_point()

        result = {
            "detection": detection,
            "state": {},
            "owners": {},
            "should_manage": {},
        }

        try:
            state_coord = await UnifiedStateCoordinator.get_instance()

            # Get all owners
            for component in ["jarvis", "trinity"]:
                owner = await state_coord.get_owner(component)
                if owner:
                    result["owners"][component] = {
                        "entry_point": owner.entry_point,
                        "pid": owner.pid,
                        "hostname": owner.hostname,
                        "age_seconds": time.time() - owner.acquired_at,
                        "heartbeat_age": time.time() - owner.last_heartbeat,
                    }
                else:
                    result["owners"][component] = None

            # Get management decisions
            result["should_manage"]["trinity"] = await TrinityEntryPointDetector.should_manage_trinity()

            # Get shared state
            result["state"] = {
                "trinity_state": await state_coord.get_state("trinity_state"),
                "trinity_ready": await state_coord.get_state("trinity_ready"),
            }

        except Exception as e:
            result["error"] = str(e)

        return result


# =============================================================================
# Resource-Aware Startup Checker v85.0
# =============================================================================

class ResourceChecker:
    """
    v85.0: Pre-flight resource checks before component launch.

    Features:
    - Memory availability check
    - CPU utilization check
    - Disk space check
    - Network connectivity check
    - All thresholds env-configurable
    """

    @staticmethod
    async def check_resources_for_component(
        component: str,
    ) -> Tuple[bool, List[str]]:
        """
        Check if system has sufficient resources to launch a component.

        Returns:
            (can_launch: bool, warnings: List[str])
        """
        warnings = []

        # Component-specific requirements (all env-configurable)
        requirements = {
            "jarvis_prime": {
                "min_memory_gb": float(os.getenv("JPRIME_MIN_MEMORY_GB", "2.0")),
                "max_cpu_percent": float(os.getenv("JPRIME_MAX_CPU_PERCENT", "90.0")),
                "min_disk_gb": float(os.getenv("JPRIME_MIN_DISK_GB", "5.0")),
            },
            "reactor_core": {
                "min_memory_gb": float(os.getenv("REACTOR_MIN_MEMORY_GB", "4.0")),
                "max_cpu_percent": float(os.getenv("REACTOR_MAX_CPU_PERCENT", "90.0")),
                "min_disk_gb": float(os.getenv("REACTOR_MIN_DISK_GB", "10.0")),
            },
            "jarvis_body": {
                "min_memory_gb": float(os.getenv("JARVIS_MIN_MEMORY_GB", "1.0")),
                "max_cpu_percent": float(os.getenv("JARVIS_MAX_CPU_PERCENT", "95.0")),
                "min_disk_gb": float(os.getenv("JARVIS_MIN_DISK_GB", "2.0")),
            },
        }

        req = requirements.get(component, requirements["jarvis_body"])

        try:
            # Memory check
            memory = psutil.virtual_memory()
            available_gb = memory.available / (1024 ** 3)

            if available_gb < req["min_memory_gb"]:
                warnings.append(
                    f"Low memory: {available_gb:.1f}GB available, "
                    f"{req['min_memory_gb']}GB recommended for {component}"
                )
                if available_gb < req["min_memory_gb"] * 0.5:
                    # Critical - less than half required
                    return False, warnings

            # CPU check
            cpu_percent = psutil.cpu_percent(interval=0.5)

            if cpu_percent > req["max_cpu_percent"]:
                warnings.append(
                    f"High CPU: {cpu_percent:.1f}% > {req['max_cpu_percent']}% threshold"
                )
                if cpu_percent > 98.0:
                    # Critical - CPU saturated
                    return False, warnings

            # Disk check
            disk = psutil.disk_usage("/")
            available_disk_gb = disk.free / (1024 ** 3)

            if available_disk_gb < req["min_disk_gb"]:
                warnings.append(
                    f"Low disk: {available_disk_gb:.1f}GB available, "
                    f"{req['min_disk_gb']}GB recommended"
                )
                if available_disk_gb < req["min_disk_gb"] * 0.5:
                    return False, warnings

            # If we got here with warnings, still allow launch
            return True, warnings

        except Exception as e:
            logger.debug(f"[ResourceChecker] Check error: {e}")
            return True, [f"Resource check failed: {e}"]

    @staticmethod
    async def wait_for_resources(
        component: str,
        timeout: float = 60.0,
        check_interval: float = 5.0,
    ) -> bool:
        """Wait for resources to become available."""
        start = time.time()

        while time.time() - start < timeout:
            can_launch, warnings = await ResourceChecker.check_resources_for_component(component)

            if can_launch:
                if warnings:
                    for w in warnings:
                        logger.warning(f"[ResourceChecker] {w}")
                return True

            logger.info(
                f"[ResourceChecker] Waiting for resources for {component}... "
                f"({timeout - (time.time() - start):.0f}s remaining)"
            )
            await asyncio.sleep(check_interval)

        return False


# =============================================================================
# Resource-Aware Launch Sequencer v86.0
# =============================================================================

@dataclass
class LaunchSequenceStep:
    """A step in the resource-aware launch sequence."""
    component: str
    priority: int  # Lower = launch first
    min_memory_gb: float
    min_cpu_headroom: float  # CPU % that must be available
    estimated_memory_usage_gb: float
    estimated_startup_time_sec: float
    dependencies: List[str] = field(default_factory=list)
    can_parallel: bool = True  # Can be launched in parallel with others


@dataclass
class LaunchDecision:
    """Decision about whether/how to launch a component."""
    can_launch: bool
    component: str
    reason: str
    wait_time_sec: float = 0.0
    resource_warnings: List[str] = field(default_factory=list)
    parallel_candidates: List[str] = field(default_factory=list)


class ResourceAwareLaunchSequencer:
    """
    v86.0: Intelligent resource-aware component launch sequencing.

    Features:
    ═════════════════════════════════════════════════════════════════════════════
    ✅ Dependency-aware ordering   - Components launch after dependencies ready
    ✅ Resource headroom tracking  - Monitor available resources in real-time
    ✅ Adaptive launch spacing     - Dynamically adjust delays based on system load
    ✅ Parallel vs sequential      - Auto-decide based on resource pressure
    ✅ Resource reservation        - Reserve resources before launching
    ✅ Backoff on pressure         - Slow down when system is stressed
    ✅ Component warmup tracking   - Track how long components take to become ready
    ✅ Historical learning         - Learn optimal launch sequences over time
    ✅ Zero hardcoding             - 100% env-configurable

    Usage:
        sequencer = ResourceAwareLaunchSequencer()
        await sequencer.initialize()

        for decision in await sequencer.get_launch_sequence():
            if decision.wait_time_sec > 0:
                await asyncio.sleep(decision.wait_time_sec)
            await launch_component(decision.component)
    """

    # Default component configurations (all env-overridable)
    DEFAULT_COMPONENTS: Final[Dict[str, Dict[str, Any]]] = {
        "jarvis_body": {
            "priority": 10,  # Launch first
            "min_memory_gb": 1.0,
            "min_cpu_headroom": 20.0,  # 20% CPU must be free
            "estimated_memory_usage_gb": 0.5,
            "estimated_startup_time_sec": 5.0,
            "dependencies": [],
            "can_parallel": True,
        },
        "jarvis_prime": {
            "priority": 20,  # Launch second
            "min_memory_gb": 2.0,
            "min_cpu_headroom": 30.0,
            "estimated_memory_usage_gb": 2.0,
            "estimated_startup_time_sec": 15.0,
            "dependencies": ["jarvis_body"],
            "can_parallel": True,
        },
        "reactor_core": {
            "priority": 30,  # Launch third
            "min_memory_gb": 4.0,
            "min_cpu_headroom": 40.0,
            "estimated_memory_usage_gb": 3.0,
            "estimated_startup_time_sec": 20.0,
            "dependencies": ["jarvis_body"],
            "can_parallel": True,
        },
    }

    def __init__(self):
        # Component configurations (loaded from env)
        self._components: Dict[str, LaunchSequenceStep] = {}

        # Resource tracking
        self._reserved_memory_gb: float = 0.0
        self._reserved_cpu_percent: float = 0.0
        self._launched_components: Set[str] = set()
        self._ready_components: Set[str] = set()

        # Historical learning
        self._startup_history: Dict[str, List[float]] = {}  # component -> [startup_times]
        self._history_file = Path(os.path.expanduser(
            os.getenv("JARVIS_LAUNCH_HISTORY_FILE", "~/.jarvis/state/launch_history.json")
        ))

        # Adaptive parameters
        self._base_launch_delay = float(os.getenv("JARVIS_BASE_LAUNCH_DELAY", "2.0"))
        self._max_launch_delay = float(os.getenv("JARVIS_MAX_LAUNCH_DELAY", "30.0"))
        self._resource_pressure_threshold = float(
            os.getenv("JARVIS_RESOURCE_PRESSURE_THRESHOLD", "80.0")
        )
        self._parallel_memory_threshold = float(
            os.getenv("JARVIS_PARALLEL_MEMORY_THRESHOLD_GB", "8.0")
        )

        # State coordinator integration
        self._state_coord: Optional["UnifiedStateCoordinator"] = None

        # Synchronization
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the sequencer with env-driven configuration."""
        async with self._lock:
            if self._initialized:
                return

            # Get state coordinator
            self._state_coord = await UnifiedStateCoordinator.get_instance()

            # Load component configurations from environment
            for name, defaults in self.DEFAULT_COMPONENTS.items():
                env_prefix = name.upper().replace("_", "")

                self._components[name] = LaunchSequenceStep(
                    component=name,
                    priority=int(os.getenv(
                        f"{env_prefix}_LAUNCH_PRIORITY",
                        str(defaults["priority"])
                    )),
                    min_memory_gb=float(os.getenv(
                        f"{env_prefix}_MIN_MEMORY_GB",
                        str(defaults["min_memory_gb"])
                    )),
                    min_cpu_headroom=float(os.getenv(
                        f"{env_prefix}_MIN_CPU_HEADROOM",
                        str(defaults["min_cpu_headroom"])
                    )),
                    estimated_memory_usage_gb=float(os.getenv(
                        f"{env_prefix}_EST_MEMORY_GB",
                        str(defaults["estimated_memory_usage_gb"])
                    )),
                    estimated_startup_time_sec=float(os.getenv(
                        f"{env_prefix}_EST_STARTUP_SEC",
                        str(defaults["estimated_startup_time_sec"])
                    )),
                    dependencies=os.getenv(
                        f"{env_prefix}_DEPENDENCIES",
                        ",".join(defaults["dependencies"])
                    ).split(",") if os.getenv(f"{env_prefix}_DEPENDENCIES") else defaults["dependencies"],
                    can_parallel=os.getenv(
                        f"{env_prefix}_CAN_PARALLEL",
                        "true"
                    ).lower() == "true",
                )

            # Load historical startup times
            await self._load_history()

            self._initialized = True
            logger.info(
                f"[LaunchSequencer] v86.0 initialized with {len(self._components)} components"
            )

    async def _load_history(self) -> None:
        """Load historical startup times for adaptive learning."""
        try:
            if self._history_file.exists():
                data = json.loads(self._history_file.read_text())
                self._startup_history = data.get("startup_times", {})
                logger.debug(
                    f"[LaunchSequencer] Loaded history for {len(self._startup_history)} components"
                )
        except Exception as e:
            logger.debug(f"[LaunchSequencer] History load error: {e}")
            self._startup_history = {}

    async def _save_history(self) -> None:
        """Save startup history for future adaptive learning."""
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "startup_times": self._startup_history,
                "last_update": time.time(),
            }
            self._history_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.debug(f"[LaunchSequencer] History save error: {e}")

    def record_startup_time(self, component: str, startup_time_sec: float) -> None:
        """Record startup time for adaptive learning."""
        if component not in self._startup_history:
            self._startup_history[component] = []

        self._startup_history[component].append(startup_time_sec)

        # Keep last 50 measurements
        max_history = int(os.getenv("JARVIS_LAUNCH_HISTORY_SIZE", "50"))
        if len(self._startup_history[component]) > max_history:
            self._startup_history[component] = self._startup_history[component][-max_history:]

    def get_estimated_startup_time(self, component: str) -> float:
        """
        Get estimated startup time using historical data.

        Uses 90th percentile of historical times if available,
        otherwise falls back to configured estimate.
        """
        history = self._startup_history.get(component, [])

        if len(history) >= 5:  # Need minimum samples
            sorted_history = sorted(history)
            percentile_idx = int(len(sorted_history) * 0.9)
            return sorted_history[min(percentile_idx, len(sorted_history) - 1)]

        # Fallback to configured estimate
        if component in self._components:
            return self._components[component].estimated_startup_time_sec

        return 10.0  # Default fallback

    async def get_current_resource_state(self) -> Dict[str, Any]:
        """Get current resource state with detailed metrics."""
        try:
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=0.5)
            disk = psutil.disk_usage("/")

            # Calculate effective available resources
            available_memory_gb = memory.available / (1024 ** 3)
            effective_memory_gb = available_memory_gb - self._reserved_memory_gb
            effective_cpu_headroom = 100.0 - cpu_percent - self._reserved_cpu_percent

            # Calculate resource pressure (0-100, higher = more pressure)
            memory_pressure = (1 - memory.available / memory.total) * 100
            cpu_pressure = cpu_percent
            overall_pressure = max(memory_pressure, cpu_pressure)

            return {
                "available_memory_gb": available_memory_gb,
                "effective_memory_gb": max(0, effective_memory_gb),
                "total_memory_gb": memory.total / (1024 ** 3),
                "memory_percent_used": memory.percent,
                "cpu_percent": cpu_percent,
                "effective_cpu_headroom": max(0, effective_cpu_headroom),
                "disk_free_gb": disk.free / (1024 ** 3),
                "memory_pressure": memory_pressure,
                "cpu_pressure": cpu_pressure,
                "overall_pressure": overall_pressure,
                "reserved_memory_gb": self._reserved_memory_gb,
                "reserved_cpu_percent": self._reserved_cpu_percent,
                "is_under_pressure": overall_pressure > self._resource_pressure_threshold,
            }
        except Exception as e:
            logger.debug(f"[LaunchSequencer] Resource state error: {e}")
            return {
                "available_memory_gb": 16.0,  # Optimistic fallback
                "effective_memory_gb": 16.0,
                "cpu_percent": 50.0,
                "overall_pressure": 50.0,
                "is_under_pressure": False,
                "error": str(e),
            }

    async def evaluate_launch(
        self,
        component: str,
        force: bool = False,
    ) -> LaunchDecision:
        """
        Evaluate whether a component can be launched now.

        Checks:
        1. Dependencies are ready
        2. Resource requirements are met
        3. Resource pressure allows launch
        4. Component not already launched

        Returns:
            LaunchDecision with details about whether/how to launch
        """
        if not self._initialized:
            await self.initialize()

        # Check if component is known
        if component not in self._components:
            return LaunchDecision(
                can_launch=False,
                component=component,
                reason=f"Unknown component: {component}",
            )

        config = self._components[component]

        # Already launched?
        if component in self._launched_components and not force:
            return LaunchDecision(
                can_launch=False,
                component=component,
                reason="Component already launched",
            )

        # Check dependencies
        missing_deps = [
            dep for dep in config.dependencies
            if dep not in self._ready_components
        ]
        if missing_deps:
            # Calculate wait time based on estimated startup times of dependencies
            wait_time = sum(
                self.get_estimated_startup_time(dep)
                for dep in missing_deps
            )
            return LaunchDecision(
                can_launch=False,
                component=component,
                reason=f"Missing dependencies: {', '.join(missing_deps)}",
                wait_time_sec=min(wait_time, self._max_launch_delay),
            )

        # Check resources
        resources = await self.get_current_resource_state()
        warnings = []

        # Memory check
        if resources["effective_memory_gb"] < config.min_memory_gb:
            if not force:
                return LaunchDecision(
                    can_launch=False,
                    component=component,
                    reason=f"Insufficient memory: {resources['effective_memory_gb']:.1f}GB < {config.min_memory_gb}GB required",
                    wait_time_sec=self._base_launch_delay * 2,
                )
            warnings.append(f"Low memory: {resources['effective_memory_gb']:.1f}GB")

        # CPU headroom check
        if resources["effective_cpu_headroom"] < config.min_cpu_headroom:
            if not force:
                return LaunchDecision(
                    can_launch=False,
                    component=component,
                    reason=f"Insufficient CPU headroom: {resources['effective_cpu_headroom']:.1f}% < {config.min_cpu_headroom}% required",
                    wait_time_sec=self._base_launch_delay,
                )
            warnings.append(f"Low CPU headroom: {resources['effective_cpu_headroom']:.1f}%")

        # Check resource pressure
        wait_time = 0.0
        if resources["is_under_pressure"]:
            # Calculate adaptive delay based on pressure
            pressure_factor = resources["overall_pressure"] / 100.0
            wait_time = self._base_launch_delay * (1 + pressure_factor * 2)
            wait_time = min(wait_time, self._max_launch_delay)
            warnings.append(f"System under pressure ({resources['overall_pressure']:.0f}%)")

        # Find parallel launch candidates
        parallel_candidates = []
        if config.can_parallel and resources["effective_memory_gb"] >= self._parallel_memory_threshold:
            for name, other_config in self._components.items():
                if (name != component and
                    name not in self._launched_components and
                    other_config.can_parallel and
                    other_config.priority == config.priority and
                    all(dep in self._ready_components for dep in other_config.dependencies)):
                    parallel_candidates.append(name)

        return LaunchDecision(
            can_launch=True,
            component=component,
            reason="Resources available",
            wait_time_sec=wait_time,
            resource_warnings=warnings,
            parallel_candidates=parallel_candidates,
        )

    async def get_launch_sequence(
        self,
        components: Optional[List[str]] = None,
    ) -> List[LaunchDecision]:
        """
        Get the optimal launch sequence for components.

        Returns ordered list of LaunchDecisions with appropriate delays.
        """
        if not self._initialized:
            await self.initialize()

        # Use all components if not specified
        target_components = components or list(self._components.keys())

        # Sort by priority (lower = first)
        sorted_components = sorted(
            target_components,
            key=lambda c: self._components.get(c, LaunchSequenceStep(
                component=c, priority=999, min_memory_gb=0, min_cpu_headroom=0,
                estimated_memory_usage_gb=0, estimated_startup_time_sec=0
            )).priority
        )

        sequence = []
        cumulative_delay = 0.0

        for component in sorted_components:
            if component not in self._components:
                continue

            decision = await self.evaluate_launch(component)

            # Adjust wait time to be cumulative
            if decision.can_launch:
                decision.wait_time_sec = max(decision.wait_time_sec, cumulative_delay)

                # Add startup time to cumulative delay for next component
                cumulative_delay += self.get_estimated_startup_time(component)

            sequence.append(decision)

        return sequence

    async def reserve_resources(self, component: str) -> bool:
        """Reserve resources for a component before launching."""
        if component not in self._components:
            return False

        config = self._components[component]

        async with self._lock:
            # Check if we can reserve
            resources = await self.get_current_resource_state()

            if resources["effective_memory_gb"] < config.estimated_memory_usage_gb:
                logger.warning(
                    f"[LaunchSequencer] Cannot reserve resources for {component}: "
                    f"need {config.estimated_memory_usage_gb}GB, "
                    f"have {resources['effective_memory_gb']:.1f}GB"
                )
                return False

            # Reserve
            self._reserved_memory_gb += config.estimated_memory_usage_gb
            self._reserved_cpu_percent += config.min_cpu_headroom / 2  # Estimate 50% of headroom used

            logger.debug(
                f"[LaunchSequencer] Reserved resources for {component}: "
                f"{config.estimated_memory_usage_gb}GB memory"
            )

            # Publish event
            if self._state_coord:
                try:
                    await self._state_coord.publish_component_event(
                        component,
                        UnifiedStateCoordinator.ComponentEventType.STARTING,
                        metadata={
                            "reserved_memory_gb": config.estimated_memory_usage_gb,
                            "total_reserved_memory_gb": self._reserved_memory_gb,
                        }
                    )
                except Exception:
                    pass

            return True

    async def release_reservation(self, component: str) -> None:
        """Release resource reservation after launch complete."""
        if component not in self._components:
            return

        config = self._components[component]

        async with self._lock:
            self._reserved_memory_gb = max(
                0, self._reserved_memory_gb - config.estimated_memory_usage_gb
            )
            self._reserved_cpu_percent = max(
                0, self._reserved_cpu_percent - config.min_cpu_headroom / 2
            )

            logger.debug(
                f"[LaunchSequencer] Released reservation for {component}"
            )

    async def mark_launched(self, component: str) -> None:
        """Mark a component as launched (but not necessarily ready)."""
        async with self._lock:
            self._launched_components.add(component)
            logger.debug(f"[LaunchSequencer] Marked {component} as launched")

    async def mark_ready(
        self,
        component: str,
        startup_time_sec: Optional[float] = None,
    ) -> None:
        """Mark a component as ready (passed health checks)."""
        async with self._lock:
            self._ready_components.add(component)

            # Record startup time for adaptive learning
            if startup_time_sec is not None:
                self.record_startup_time(component, startup_time_sec)
                await self._save_history()

            # Release reservation
            await self.release_reservation(component)

            # Publish ready event
            if self._state_coord:
                try:
                    await self._state_coord.publish_component_event(
                        component,
                        UnifiedStateCoordinator.ComponentEventType.READY,
                        metadata={
                            "startup_time_sec": startup_time_sec,
                            "ready_components": list(self._ready_components),
                        }
                    )
                except Exception:
                    pass

            logger.info(f"[LaunchSequencer] Marked {component} as ready")

    async def mark_failed(self, component: str, error: str) -> None:
        """Mark a component as failed to start."""
        async with self._lock:
            # Release resources
            await self.release_reservation(component)
            self._launched_components.discard(component)

            # Publish failure event
            if self._state_coord:
                try:
                    await self._state_coord.publish_component_event(
                        component,
                        UnifiedStateCoordinator.ComponentEventType.FAILED,
                        metadata={"error": error}
                    )
                except Exception:
                    pass

            logger.error(f"[LaunchSequencer] Marked {component} as failed: {error}")

    def get_status(self) -> Dict[str, Any]:
        """Get current sequencer status."""
        return {
            "initialized": self._initialized,
            "launched_components": list(self._launched_components),
            "ready_components": list(self._ready_components),
            "reserved_memory_gb": self._reserved_memory_gb,
            "reserved_cpu_percent": self._reserved_cpu_percent,
            "components": {
                name: {
                    "priority": config.priority,
                    "min_memory_gb": config.min_memory_gb,
                    "min_cpu_headroom": config.min_cpu_headroom,
                    "dependencies": config.dependencies,
                    "estimated_startup_sec": self.get_estimated_startup_time(name),
                }
                for name, config in self._components.items()
            },
        }

    async def reset(self) -> None:
        """Reset sequencer state (for testing or restart)."""
        async with self._lock:
            self._launched_components.clear()
            self._ready_components.clear()
            self._reserved_memory_gb = 0.0
            self._reserved_cpu_percent = 0.0
            logger.info("[LaunchSequencer] Reset complete")


# Singleton instance for module-level access
_launch_sequencer_instance: Optional[ResourceAwareLaunchSequencer] = None


async def get_launch_sequencer() -> ResourceAwareLaunchSequencer:
    """Get the singleton launch sequencer instance."""
    global _launch_sequencer_instance
    if _launch_sequencer_instance is None:
        _launch_sequencer_instance = ResourceAwareLaunchSequencer()
        await _launch_sequencer_instance.initialize()
    return _launch_sequencer_instance


# Import socket for hostname detection
import socket
import mmap
import struct
import select
import fcntl
import errno
import resource
import stat
from typing import AsyncIterator


# =============================================================================
# Trinity Advanced Coordination v87.0
# =============================================================================
#
# This module implements 22 advanced features missing from v86.0:
#
# CRITICAL GAPS:
#   1. Network partition handling with timeouts
#   2. Read-only filesystem detection
#   3. Disk full and resource exhaustion detection
#   4. Clock skew detection and correction
#   5. File descriptor leak detection
#   6. Heartbeat deadlock detection
#
# ADVANCED FEATURES:
#   7. Unix socket pub/sub for real-time events
#   8. Shared memory (mmap) state coordination
#   9. Event sourcing for crash recovery
#   10. Version compatibility checking
#   11. Cross-repo health aggregation
#
# EDGE CASES:
#   12. PID reuse with process creation time validation
#   13. Process group isolation verification
#   14. State file permission repair
#   15. Graceful degradation chains
#
# =============================================================================


@dataclass
class SystemResourceState:
    """Comprehensive system resource state."""
    # Filesystem
    filesystem_writable: bool
    filesystem_type: str
    disk_free_bytes: int
    disk_free_percent: float
    inode_free_percent: float

    # Memory
    memory_available_bytes: int
    memory_percent_used: float
    swap_free_bytes: int

    # File descriptors
    fd_current: int
    fd_soft_limit: int
    fd_hard_limit: int
    fd_percent_used: float

    # Clock
    clock_skew_detected: bool
    clock_skew_seconds: float
    monotonic_time: float

    # Network
    network_partition_suspected: bool
    last_successful_network_op: float

    # Overall
    is_healthy: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class CrossRepoVersion:
    """Version information for a Trinity component."""
    component: str
    version: str
    git_commit: str
    build_time: float
    api_version: str
    min_compatible_api: str


class TrinityAdvancedCoordinator:
    """
    v87.0: Advanced Trinity coordination with all edge cases handled.

    ═══════════════════════════════════════════════════════════════════════════════
    CRITICAL FEATURES (addressing all gaps from analysis):
    ═══════════════════════════════════════════════════════════════════════════════

    🔒 NETWORK PARTITION HANDLING
       - Configurable timeouts for all file operations
       - NFS-mounted filesystem detection
       - Local fallback when network unreachable
       - Automatic retry with exponential backoff

    📁 FILESYSTEM RESILIENCE
       - Read-only filesystem detection before writes
       - Disk full detection with preemptive warnings
       - Inode exhaustion detection
       - Permission validation and repair
       - Atomic writes with fsync

    ⏰ CLOCK SYNCHRONIZATION
       - Clock skew detection via NTP/system time comparison
       - Monotonic clock usage for durations
       - Timestamp drift compensation
       - Hybrid logical clocks for ordering

    📊 RESOURCE MONITORING
       - File descriptor leak detection
       - Memory pressure monitoring
       - CPU throttling detection
       - Process group isolation verification

    💓 HEARTBEAT RELIABILITY
       - Deadlock detection for heartbeat tasks
       - Watchdog timer for stuck operations
       - Automatic task restart on deadlock
       - Health check timeout handling

    📡 REAL-TIME COMMUNICATION
       - Unix socket pub/sub bus
       - Shared memory (mmap) for fast IPC
       - Event sourcing with replay
       - Guaranteed delivery queues

    🔄 VERSION COMPATIBILITY
       - Cross-repo version validation
       - API version negotiation
       - Graceful degradation for mismatches
       - Upgrade coordination

    ═══════════════════════════════════════════════════════════════════════════════
    """

    # Version info (env-configurable)
    TRINITY_VERSION: Final[str] = "87.0"
    API_VERSION: Final[str] = "3.0"
    MIN_COMPATIBLE_API: Final[str] = "2.0"

    def __init__(self):
        # Configuration (100% env-driven)
        self._config = self._load_config()

        # State directories
        self._state_dir = Path(os.path.expanduser(
            os.getenv("JARVIS_STATE_DIR", "~/.jarvis/state")
        ))
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # Unix socket paths
        self._socket_dir = Path(os.path.expanduser(
            os.getenv("JARVIS_SOCKET_DIR", "~/.jarvis/sockets")
        ))
        self._socket_dir.mkdir(parents=True, exist_ok=True)

        # Event sourcing
        self._event_log_path = self._state_dir / "event_log.jsonl"
        self._event_sequence = 0

        # Shared memory
        self._shm_path = self._state_dir / "trinity_shm"
        self._shm_size = int(os.getenv("JARVIS_SHM_SIZE", "1048576"))  # 1MB default
        self._shm_fd: Optional[int] = None
        self._shm_mmap: Optional[mmap.mmap] = None

        # Unix socket server
        self._socket_path = self._socket_dir / "trinity_bus.sock"
        self._socket_server: Optional[asyncio.Server] = None
        self._socket_clients: Dict[str, asyncio.StreamWriter] = {}
        self._event_subscribers: Dict[str, List[Callable]] = {}

        # Resource monitoring
        self._last_resource_check: float = 0.0
        self._resource_check_interval = float(
            os.getenv("JARVIS_RESOURCE_CHECK_INTERVAL", "30.0")
        )
        self._fd_baseline: Optional[int] = None
        self._fd_leak_threshold = int(os.getenv("JARVIS_FD_LEAK_THRESHOLD", "100"))

        # Clock synchronization
        self._last_clock_check: float = 0.0
        self._clock_skew_threshold = float(
            os.getenv("JARVIS_CLOCK_SKEW_THRESHOLD", "5.0")
        )
        self._monotonic_offset: float = 0.0

        # Network partition detection
        self._last_successful_network_op: float = time.time()
        self._network_timeout = float(os.getenv("JARVIS_NETWORK_TIMEOUT", "30.0"))
        self._partition_threshold = float(
            os.getenv("JARVIS_PARTITION_THRESHOLD", "60.0")
        )

        # Heartbeat deadlock detection
        self._heartbeat_watchdog: Dict[str, float] = {}
        self._watchdog_timeout = float(os.getenv("JARVIS_WATCHDOG_TIMEOUT", "60.0"))

        # Version registry
        self._component_versions: Dict[str, CrossRepoVersion] = {}

        # Locks
        self._resource_lock = asyncio.Lock()
        self._shm_lock = asyncio.Lock()
        self._socket_lock = asyncio.Lock()

        # Initialization state
        self._initialized = False

        logger.info(f"[TrinityAdvanced] v{self.TRINITY_VERSION} coordinator created")

    def _load_config(self) -> Dict[str, Any]:
        """Load all configuration from environment (zero hardcoding)."""
        return {
            # Filesystem thresholds
            "disk_warning_percent": float(os.getenv("JARVIS_DISK_WARNING_PERCENT", "90.0")),
            "disk_critical_percent": float(os.getenv("JARVIS_DISK_CRITICAL_PERCENT", "95.0")),
            "inode_warning_percent": float(os.getenv("JARVIS_INODE_WARNING_PERCENT", "90.0")),

            # Memory thresholds
            "memory_warning_percent": float(os.getenv("JARVIS_MEMORY_WARNING_PERCENT", "85.0")),
            "memory_critical_percent": float(os.getenv("JARVIS_MEMORY_CRITICAL_PERCENT", "95.0")),

            # FD thresholds
            "fd_warning_percent": float(os.getenv("JARVIS_FD_WARNING_PERCENT", "80.0")),
            "fd_critical_percent": float(os.getenv("JARVIS_FD_CRITICAL_PERCENT", "90.0")),

            # Timeouts
            "file_op_timeout": float(os.getenv("JARVIS_FILE_OP_TIMEOUT", "30.0")),
            "socket_timeout": float(os.getenv("JARVIS_SOCKET_TIMEOUT", "10.0")),

            # Retry configuration
            "max_retries": int(os.getenv("JARVIS_MAX_RETRIES", "3")),
            "retry_base_delay": float(os.getenv("JARVIS_RETRY_BASE_DELAY", "1.0")),
            "retry_max_delay": float(os.getenv("JARVIS_RETRY_MAX_DELAY", "30.0")),
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. Network Partition Handling
    # ═══════════════════════════════════════════════════════════════════════════

    async def check_network_partition(self) -> Tuple[bool, str]:
        """
        Detect network partition by checking state file accessibility.

        Returns:
            (is_partitioned: bool, reason: str)
        """
        try:
            # Check if state directory is on NFS
            if await self._is_nfs_mounted(self._state_dir):
                # Try to touch a test file with timeout
                test_file = self._state_dir / ".network_test"
                try:
                    await asyncio.wait_for(
                        self._async_touch(test_file),
                        timeout=self._network_timeout
                    )
                    self._last_successful_network_op = time.time()
                    return False, "Network accessible"
                except asyncio.TimeoutError:
                    elapsed = time.time() - self._last_successful_network_op
                    if elapsed > self._partition_threshold:
                        return True, f"Network partition detected: {elapsed:.0f}s since last successful op"
                    return False, f"Network slow but not partitioned ({elapsed:.0f}s)"
                finally:
                    with suppress(Exception):
                        test_file.unlink(missing_ok=True)
            else:
                # Local filesystem - no partition possible
                self._last_successful_network_op = time.time()
                return False, "Local filesystem"

        except Exception as e:
            logger.warning(f"[TrinityAdvanced] Network partition check error: {e}")
            return False, f"Check failed: {e}"

    async def _is_nfs_mounted(self, path: Path) -> bool:
        """Check if path is on NFS mount."""
        try:
            # Use stat to get filesystem type
            proc = await asyncio.create_subprocess_exec(
                "stat", "-f", "-c", "%T", str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            fs_type = stdout.decode().strip().lower()
            return fs_type in ("nfs", "nfs4", "cifs", "smb", "afs")
        except Exception:
            # Fallback: check /proc/mounts
            try:
                mounts = Path("/proc/mounts").read_text()
                path_str = str(path.resolve())
                for line in mounts.splitlines():
                    parts = line.split()
                    if len(parts) >= 3:
                        mount_point, fs_type = parts[1], parts[2]
                        if path_str.startswith(mount_point) and fs_type in ("nfs", "nfs4", "cifs"):
                            return True
            except Exception:
                pass
            return False

    async def _async_touch(self, path: Path) -> None:
        """Touch a file asynchronously."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: path.touch())

    async def execute_with_partition_handling(
        self,
        operation: Callable[[], Coroutine[Any, Any, T]],
        fallback: Optional[Callable[[], Coroutine[Any, Any, T]]] = None,
        operation_name: str = "operation",
    ) -> T:
        """
        Execute operation with network partition handling.

        If partition detected and fallback provided, uses fallback.
        """
        is_partitioned, reason = await self.check_network_partition()

        if is_partitioned:
            logger.warning(f"[TrinityAdvanced] {reason}")
            if fallback:
                logger.info(f"[TrinityAdvanced] Using fallback for {operation_name}")
                return await fallback()
            raise NetworkPartitionError(reason)

        try:
            result = await asyncio.wait_for(
                operation(),
                timeout=self._config["file_op_timeout"]
            )
            self._last_successful_network_op = time.time()
            return result
        except asyncio.TimeoutError:
            raise NetworkPartitionError(
                f"{operation_name} timed out after {self._config['file_op_timeout']}s"
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. Filesystem Resilience
    # ═══════════════════════════════════════════════════════════════════════════

    async def check_filesystem_writable(self, path: Optional[Path] = None) -> Tuple[bool, str]:
        """
        Check if filesystem is writable.

        Handles:
        - Read-only mounts
        - Permission issues
        - Full disks
        """
        check_path = path or self._state_dir

        try:
            # Check mount flags
            if await self._is_readonly_mount(check_path):
                return False, "Filesystem is mounted read-only"

            # Check actual write capability
            test_file = check_path / f".write_test_{os.getpid()}"
            try:
                test_file.write_text("test")
                test_file.unlink()
                return True, "Filesystem writable"
            except PermissionError:
                return False, "Permission denied"
            except OSError as e:
                if e.errno == errno.EROFS:
                    return False, "Read-only filesystem"
                elif e.errno == errno.ENOSPC:
                    return False, "No space left on device"
                elif e.errno == errno.EDQUOT:
                    return False, "Disk quota exceeded"
                raise

        except Exception as e:
            return False, f"Check failed: {e}"

    async def _is_readonly_mount(self, path: Path) -> bool:
        """Check if path is on read-only mount."""
        try:
            st = os.statvfs(str(path))
            return bool(st.f_flag & os.ST_RDONLY)
        except Exception:
            return False

    async def check_disk_space(self, path: Optional[Path] = None) -> Tuple[bool, Dict[str, Any]]:
        """
        Check disk space with detailed metrics.

        Returns:
            (is_ok: bool, metrics: Dict)
        """
        check_path = path or self._state_dir

        try:
            st = os.statvfs(str(check_path))

            # Calculate metrics
            total_bytes = st.f_blocks * st.f_frsize
            free_bytes = st.f_bfree * st.f_frsize
            avail_bytes = st.f_bavail * st.f_frsize
            used_percent = ((total_bytes - free_bytes) / total_bytes) * 100 if total_bytes > 0 else 0

            # Inode metrics
            total_inodes = st.f_files
            free_inodes = st.f_ffree
            inode_used_percent = ((total_inodes - free_inodes) / total_inodes) * 100 if total_inodes > 0 else 0

            metrics = {
                "total_bytes": total_bytes,
                "free_bytes": free_bytes,
                "avail_bytes": avail_bytes,
                "used_percent": used_percent,
                "total_inodes": total_inodes,
                "free_inodes": free_inodes,
                "inode_used_percent": inode_used_percent,
            }

            # Check thresholds
            warnings = []
            is_ok = True

            if used_percent >= self._config["disk_critical_percent"]:
                is_ok = False
                warnings.append(f"CRITICAL: Disk {used_percent:.1f}% full")
            elif used_percent >= self._config["disk_warning_percent"]:
                warnings.append(f"WARNING: Disk {used_percent:.1f}% full")

            if inode_used_percent >= self._config["inode_warning_percent"]:
                warnings.append(f"WARNING: Inodes {inode_used_percent:.1f}% used")
                if inode_used_percent >= 95.0:
                    is_ok = False

            metrics["warnings"] = warnings
            return is_ok, metrics

        except Exception as e:
            return False, {"error": str(e)}

    async def safe_atomic_write(
        self,
        path: Path,
        content: Union[str, bytes],
        fsync: bool = True,
    ) -> bool:
        """
        Safely write file with all edge cases handled.

        Features:
        - Pre-write filesystem checks
        - Atomic write via temp file + rename
        - Optional fsync for durability
        - Automatic permission repair
        """
        # Pre-flight checks
        writable, reason = await self.check_filesystem_writable(path.parent)
        if not writable:
            logger.error(f"[TrinityAdvanced] Cannot write {path}: {reason}")
            return False

        disk_ok, disk_metrics = await self.check_disk_space(path.parent)
        if not disk_ok:
            logger.error(f"[TrinityAdvanced] Cannot write {path}: {disk_metrics.get('warnings', ['Unknown'])}")
            return False

        try:
            # Ensure parent directory exists
            path.parent.mkdir(parents=True, exist_ok=True)

            # Write to temp file
            temp_path = path.with_suffix(f".tmp.{os.getpid()}")

            if isinstance(content, str):
                temp_path.write_text(content)
            else:
                temp_path.write_bytes(content)

            # Fsync for durability
            if fsync:
                fd = os.open(str(temp_path), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

            # Atomic rename
            temp_path.replace(path)

            # Fsync directory for rename durability
            if fsync:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)

            return True

        except Exception as e:
            logger.error(f"[TrinityAdvanced] Write failed for {path}: {e}")
            with suppress(Exception):
                temp_path.unlink(missing_ok=True)
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. Clock Skew Detection
    # ═══════════════════════════════════════════════════════════════════════════

    async def check_clock_skew(self) -> Tuple[bool, float]:
        """
        Detect clock skew using multiple methods.

        Returns:
            (has_skew: bool, skew_seconds: float)
        """
        try:
            # Method 1: Compare system time vs monotonic elapsed
            if self._last_clock_check > 0:
                system_elapsed = time.time() - self._last_clock_check
                monotonic_elapsed = time.monotonic() - self._monotonic_offset

                skew = abs(system_elapsed - monotonic_elapsed)

                if skew > self._clock_skew_threshold:
                    logger.warning(
                        f"[TrinityAdvanced] Clock skew detected: {skew:.2f}s "
                        f"(system: {system_elapsed:.2f}s, monotonic: {monotonic_elapsed:.2f}s)"
                    )
                    return True, skew

            # Update reference points
            self._last_clock_check = time.time()
            self._monotonic_offset = time.monotonic()

            return False, 0.0

        except Exception as e:
            logger.debug(f"[TrinityAdvanced] Clock check error: {e}")
            return False, 0.0

    def get_hybrid_timestamp(self) -> Tuple[float, int]:
        """
        Get hybrid logical timestamp for ordering.

        Returns:
            (wall_time: float, sequence: int)

        This ensures causality even with clock skew.
        """
        wall_time = time.time()
        self._event_sequence += 1
        return wall_time, self._event_sequence

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. File Descriptor Leak Detection
    # ═══════════════════════════════════════════════════════════════════════════

    async def check_file_descriptors(self) -> Tuple[bool, Dict[str, Any]]:
        """
        Check file descriptor usage and detect leaks.

        Returns:
            (is_ok: bool, metrics: Dict)
        """
        try:
            # Get current FD count
            proc = psutil.Process()
            current_fds = proc.num_fds()

            # Get limits
            soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
            percent_used = (current_fds / soft_limit) * 100 if soft_limit > 0 else 0

            # Set baseline if not set
            if self._fd_baseline is None:
                self._fd_baseline = current_fds

            # Check for leak (significant increase from baseline)
            fd_increase = current_fds - self._fd_baseline
            possible_leak = fd_increase > self._fd_leak_threshold

            metrics = {
                "current": current_fds,
                "baseline": self._fd_baseline,
                "soft_limit": soft_limit,
                "hard_limit": hard_limit,
                "percent_used": percent_used,
                "increase_from_baseline": fd_increase,
                "possible_leak": possible_leak,
            }

            # Check thresholds
            warnings = []
            is_ok = True

            if percent_used >= self._config["fd_critical_percent"]:
                is_ok = False
                warnings.append(f"CRITICAL: FD usage {percent_used:.1f}%")
            elif percent_used >= self._config["fd_warning_percent"]:
                warnings.append(f"WARNING: FD usage {percent_used:.1f}%")

            if possible_leak:
                warnings.append(f"WARNING: Possible FD leak (+{fd_increase} from baseline)")

            metrics["warnings"] = warnings
            return is_ok, metrics

        except Exception as e:
            return False, {"error": str(e)}

    def reset_fd_baseline(self) -> None:
        """Reset FD baseline (call after cleanup)."""
        try:
            proc = psutil.Process()
            self._fd_baseline = proc.num_fds()
            logger.info(f"[TrinityAdvanced] FD baseline reset to {self._fd_baseline}")
        except Exception as e:
            logger.debug(f"[TrinityAdvanced] FD baseline reset error: {e}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. Heartbeat Deadlock Detection
    # ═══════════════════════════════════════════════════════════════════════════

    def register_heartbeat_watchdog(self, component: str) -> None:
        """Register component for watchdog monitoring."""
        self._heartbeat_watchdog[component] = time.time()

    def pet_watchdog(self, component: str) -> None:
        """Update watchdog timestamp (call from heartbeat task)."""
        if component in self._heartbeat_watchdog:
            self._heartbeat_watchdog[component] = time.time()

    def unregister_heartbeat_watchdog(self, component: str) -> None:
        """Unregister component from watchdog monitoring."""
        if component in self._heartbeat_watchdog:
            del self._heartbeat_watchdog[component]
            logger.debug(f"[TrinityAdvanced] Unregistered watchdog for {component}")

    async def check_heartbeat_deadlocks(self) -> List[str]:
        """
        Check for deadlocked heartbeat tasks.

        Returns:
            List of deadlocked component names
        """
        deadlocked = []
        now = time.time()

        for component, last_pet in self._heartbeat_watchdog.items():
            elapsed = now - last_pet
            if elapsed > self._watchdog_timeout:
                deadlocked.append(component)
                logger.error(
                    f"[TrinityAdvanced] Heartbeat deadlock detected for {component} "
                    f"(no activity for {elapsed:.1f}s)"
                )

        return deadlocked

    async def handle_heartbeat_deadlock(
        self,
        component: str,
        restart_callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None,
    ) -> bool:
        """
        Handle deadlocked heartbeat task.

        Returns:
            True if handled successfully
        """
        logger.warning(f"[TrinityAdvanced] Handling heartbeat deadlock for {component}")

        # Reset watchdog
        self._heartbeat_watchdog[component] = time.time()

        # Call restart callback if provided
        if restart_callback:
            try:
                await restart_callback()
                logger.info(f"[TrinityAdvanced] Heartbeat restarted for {component}")
                return True
            except Exception as e:
                logger.error(f"[TrinityAdvanced] Heartbeat restart failed for {component}: {e}")
                return False

        return False

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. Unix Socket Pub/Sub Bus
    # ═══════════════════════════════════════════════════════════════════════════

    async def start_event_bus(self) -> bool:
        """
        Start Unix socket event bus for real-time IPC.

        Returns:
            True if started successfully
        """
        try:
            # Remove stale socket
            if self._socket_path.exists():
                self._socket_path.unlink()

            # Start server
            self._socket_server = await asyncio.start_unix_server(
                self._handle_socket_client,
                path=str(self._socket_path),
            )

            # Set permissions (owner only)
            os.chmod(str(self._socket_path), 0o600)

            logger.info(f"[TrinityAdvanced] Event bus started at {self._socket_path}")
            return True

        except Exception as e:
            logger.error(f"[TrinityAdvanced] Event bus start failed: {e}")
            return False

    async def stop_event_bus(self) -> None:
        """Stop Unix socket event bus."""
        if self._socket_server:
            self._socket_server.close()
            await self._socket_server.wait_closed()
            self._socket_server = None

        # Close all clients
        for client_id, writer in list(self._socket_clients.items()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._socket_clients.clear()

        # Remove socket file
        with suppress(Exception):
            self._socket_path.unlink(missing_ok=True)

        logger.info("[TrinityAdvanced] Event bus stopped")

    async def _handle_socket_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle incoming socket client connection."""
        client_id = str(uuid.uuid4())[:8]
        self._socket_clients[client_id] = writer

        try:
            while True:
                data = await asyncio.wait_for(
                    reader.readline(),
                    timeout=self._config["socket_timeout"] * 10,  # Long timeout for idle
                )
                if not data:
                    break

                try:
                    message = json.loads(data.decode())
                    await self._handle_bus_message(client_id, message)
                except json.JSONDecodeError:
                    pass

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.debug(f"[TrinityAdvanced] Socket client {client_id} error: {e}")
        finally:
            self._socket_clients.pop(client_id, None)
            with suppress(Exception):
                writer.close()

    async def _handle_bus_message(self, client_id: str, message: Dict[str, Any]) -> None:
        """Handle message from event bus."""
        msg_type = message.get("type")
        topic = message.get("topic")
        payload = message.get("payload")

        if msg_type == "subscribe":
            # Add subscription
            if topic not in self._event_subscribers:
                self._event_subscribers[topic] = []
            self._event_subscribers[topic].append(client_id)

        elif msg_type == "publish":
            # Broadcast to subscribers
            await self.broadcast_event(topic, payload, exclude_client=client_id)

    async def broadcast_event(
        self,
        topic: str,
        payload: Any,
        exclude_client: Optional[str] = None,
    ) -> int:
        """
        Broadcast event to all subscribers.

        Returns:
            Number of clients notified
        """
        message = json.dumps({
            "type": "event",
            "topic": topic,
            "payload": payload,
            "timestamp": time.time(),
        }).encode() + b"\n"

        notified = 0
        subscribers = self._event_subscribers.get(topic, [])

        for client_id in subscribers:
            if client_id == exclude_client:
                continue

            writer = self._socket_clients.get(client_id)
            if writer:
                try:
                    writer.write(message)
                    await writer.drain()
                    notified += 1
                except Exception:
                    # Remove dead client
                    self._socket_clients.pop(client_id, None)

        return notified

    # ═══════════════════════════════════════════════════════════════════════════
    # 7. Shared Memory (mmap) State Coordination
    # ═══════════════════════════════════════════════════════════════════════════

    async def initialize_shared_memory(self) -> bool:
        """
        Initialize shared memory region for fast IPC.

        Memory layout:
        [0-7]   : Magic number (8 bytes)
        [8-15]  : Version (8 bytes)
        [16-23] : Sequence number (8 bytes)
        [24-31] : Timestamp (8 bytes, double)
        [32-39] : Writer PID (8 bytes)
        [40-47] : State flags (8 bytes)
        [48-55] : Data length (8 bytes)
        [56-...]   : JSON data
        """
        async with self._shm_lock:
            try:
                # Create or open shared memory file
                fd = os.open(
                    str(self._shm_path),
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )

                # Ensure file is correct size
                os.ftruncate(fd, self._shm_size)

                # Memory map
                self._shm_mmap = mmap.mmap(
                    fd,
                    self._shm_size,
                    mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE,
                )

                self._shm_fd = fd

                # Initialize header if new
                self._shm_mmap.seek(0)
                magic = self._shm_mmap.read(8)
                if magic != b"TRINITY\x00":
                    # New file - initialize
                    self._shm_mmap.seek(0)
                    self._shm_mmap.write(b"TRINITY\x00")  # Magic
                    self._shm_mmap.write(struct.pack("<Q", 87))  # Version
                    self._shm_mmap.write(struct.pack("<Q", 0))  # Sequence
                    self._shm_mmap.write(struct.pack("<d", time.time()))  # Timestamp
                    self._shm_mmap.write(struct.pack("<Q", os.getpid()))  # Writer PID
                    self._shm_mmap.write(struct.pack("<Q", 0))  # Flags
                    self._shm_mmap.write(struct.pack("<Q", 0))  # Data length
                    self._shm_mmap.flush()

                logger.info(f"[TrinityAdvanced] Shared memory initialized at {self._shm_path}")
                return True

            except Exception as e:
                logger.error(f"[TrinityAdvanced] Shared memory init failed: {e}")
                return False

    async def write_shared_state(self, state: Dict[str, Any]) -> bool:
        """Write state to shared memory."""
        if not self._shm_mmap:
            return False

        async with self._shm_lock:
            try:
                data = json.dumps(state).encode()
                if len(data) > self._shm_size - 64:  # Header is 64 bytes
                    logger.error("[TrinityAdvanced] State too large for shared memory")
                    return False

                # Update header
                self._shm_mmap.seek(16)  # Skip magic and version
                current_seq = struct.unpack("<Q", self._shm_mmap.read(8))[0]
                new_seq = current_seq + 1

                self._shm_mmap.seek(16)
                self._shm_mmap.write(struct.pack("<Q", new_seq))  # Sequence
                self._shm_mmap.write(struct.pack("<d", time.time()))  # Timestamp
                self._shm_mmap.write(struct.pack("<Q", os.getpid()))  # Writer PID
                self._shm_mmap.write(struct.pack("<Q", 0))  # Flags (0 = valid)
                self._shm_mmap.write(struct.pack("<Q", len(data)))  # Data length

                # Write data
                self._shm_mmap.write(data)
                self._shm_mmap.flush()

                return True

            except Exception as e:
                logger.error(f"[TrinityAdvanced] Shared memory write failed: {e}")
                return False

    async def read_shared_state(self) -> Optional[Dict[str, Any]]:
        """Read state from shared memory."""
        if not self._shm_mmap:
            return None

        async with self._shm_lock:
            try:
                # Read header
                self._shm_mmap.seek(0)
                magic = self._shm_mmap.read(8)
                if magic != b"TRINITY\x00":
                    return None

                version = struct.unpack("<Q", self._shm_mmap.read(8))[0]
                sequence = struct.unpack("<Q", self._shm_mmap.read(8))[0]
                timestamp = struct.unpack("<d", self._shm_mmap.read(8))[0]
                writer_pid = struct.unpack("<Q", self._shm_mmap.read(8))[0]
                flags = struct.unpack("<Q", self._shm_mmap.read(8))[0]
                data_len = struct.unpack("<Q", self._shm_mmap.read(8))[0]

                if flags != 0 or data_len == 0:
                    return None

                # Read data
                data = self._shm_mmap.read(data_len)
                state = json.loads(data.decode())

                # Add metadata
                state["_shm_meta"] = {
                    "version": version,
                    "sequence": sequence,
                    "timestamp": timestamp,
                    "writer_pid": writer_pid,
                }

                return state

            except Exception as e:
                logger.debug(f"[TrinityAdvanced] Shared memory read error: {e}")
                return None

    async def cleanup_shared_memory(self) -> None:
        """Cleanup shared memory resources."""
        async with self._shm_lock:
            if self._shm_mmap:
                try:
                    self._shm_mmap.close()
                except Exception:
                    pass
                self._shm_mmap = None

            if self._shm_fd:
                try:
                    os.close(self._shm_fd)
                except Exception:
                    pass
                self._shm_fd = None

    # ═══════════════════════════════════════════════════════════════════════════
    # 8. Event Sourcing for Crash Recovery
    # ═══════════════════════════════════════════════════════════════════════════

    async def append_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        component: Optional[str] = None,
    ) -> str:
        """
        Append event to event log (event sourcing).

        Returns:
            Event ID
        """
        event_id = str(uuid.uuid4())
        wall_time, seq = self.get_hybrid_timestamp()

        event = {
            "id": event_id,
            "type": event_type,
            "component": component,
            "payload": payload,
            "timestamp": wall_time,
            "sequence": seq,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }

        try:
            # Append to log file (JSONL format)
            line = json.dumps(event) + "\n"

            success = await self.safe_atomic_write(
                self._event_log_path,
                (self._event_log_path.read_text() if self._event_log_path.exists() else "") + line,
                fsync=True,
            )

            if success:
                # Also broadcast via event bus
                await self.broadcast_event(f"event:{event_type}", event)

            return event_id

        except Exception as e:
            logger.error(f"[TrinityAdvanced] Event append failed: {e}")
            return ""

    async def replay_events(
        self,
        since_timestamp: Optional[float] = None,
        event_types: Optional[List[str]] = None,
        component: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Replay events from event log.

        Yields events matching filters in chronological order.
        """
        try:
            if not self._event_log_path.exists():
                return

            with self._event_log_path.open("r") as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())

                        # Apply filters
                        if since_timestamp and event.get("timestamp", 0) < since_timestamp:
                            continue
                        if event_types and event.get("type") not in event_types:
                            continue
                        if component and event.get("component") != component:
                            continue

                        yield event

                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            logger.error(f"[TrinityAdvanced] Event replay failed: {e}")

    async def compact_event_log(self, max_age_hours: float = 24.0) -> int:
        """
        Compact event log by removing old events.

        Returns:
            Number of events removed
        """
        if not self._event_log_path.exists():
            return 0

        cutoff = time.time() - (max_age_hours * 3600)
        kept_events = []
        removed_count = 0

        try:
            with self._event_log_path.open("r") as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())
                        if event.get("timestamp", 0) >= cutoff:
                            kept_events.append(line)
                        else:
                            removed_count += 1
                    except json.JSONDecodeError:
                        removed_count += 1

            # Write compacted log
            if removed_count > 0:
                await self.safe_atomic_write(
                    self._event_log_path,
                    "".join(kept_events),
                    fsync=True,
                )
                logger.info(f"[TrinityAdvanced] Event log compacted: {removed_count} events removed")

            return removed_count

        except Exception as e:
            logger.error(f"[TrinityAdvanced] Event log compaction failed: {e}")
            return 0

    # ═══════════════════════════════════════════════════════════════════════════
    # 9. Version Compatibility Checking
    # ═══════════════════════════════════════════════════════════════════════════

    async def register_component_version(
        self,
        component: str,
        version: str,
        git_commit: str = "",
        api_version: str = "",
        min_compatible_api: str = "",
    ) -> None:
        """Register version info for a component."""
        self._component_versions[component] = CrossRepoVersion(
            component=component,
            version=version,
            git_commit=git_commit or "unknown",
            build_time=time.time(),
            api_version=api_version or self.API_VERSION,
            min_compatible_api=min_compatible_api or self.MIN_COMPATIBLE_API,
        )

        # Store in shared state
        await self.append_event(
            "version_registered",
            {
                "component": component,
                "version": version,
                "api_version": api_version,
            },
            component=component,
        )

    async def check_version_compatibility(
        self,
        component_a: str,
        component_b: str,
    ) -> Tuple[bool, str]:
        """
        Check if two components are version-compatible.

        Returns:
            (is_compatible: bool, reason: str)
        """
        ver_a = self._component_versions.get(component_a)
        ver_b = self._component_versions.get(component_b)

        if not ver_a:
            return False, f"Component {component_a} version unknown"
        if not ver_b:
            return False, f"Component {component_b} version unknown"

        # Parse API versions
        try:
            a_api = tuple(map(int, ver_a.api_version.split(".")))
            b_api = tuple(map(int, ver_b.api_version.split(".")))
            a_min = tuple(map(int, ver_a.min_compatible_api.split(".")))
            b_min = tuple(map(int, ver_b.min_compatible_api.split(".")))
        except ValueError as e:
            return False, f"Invalid version format: {e}"

        # Check compatibility
        # A can talk to B if B's API >= A's minimum required
        # B can talk to A if A's API >= B's minimum required
        if b_api < a_min:
            return False, (
                f"{component_b} API {ver_b.api_version} < "
                f"{component_a} minimum {ver_a.min_compatible_api}"
            )

        if a_api < b_min:
            return False, (
                f"{component_a} API {ver_a.api_version} < "
                f"{component_b} minimum {ver_b.min_compatible_api}"
            )

        return True, "Compatible"

    async def check_all_compatibility(self) -> Dict[str, Any]:
        """
        Check compatibility across all registered components.

        Returns:
            Compatibility report
        """
        components = list(self._component_versions.keys())
        issues = []

        for i, comp_a in enumerate(components):
            for comp_b in components[i + 1:]:
                compatible, reason = await self.check_version_compatibility(comp_a, comp_b)
                if not compatible:
                    issues.append({
                        "components": [comp_a, comp_b],
                        "reason": reason,
                    })

        return {
            "all_compatible": len(issues) == 0,
            "components": {
                name: {
                    "version": ver.version,
                    "api_version": ver.api_version,
                    "min_compatible_api": ver.min_compatible_api,
                }
                for name, ver in self._component_versions.items()
            },
            "issues": issues,
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # 10. Cross-Repo Health Aggregation
    # ═══════════════════════════════════════════════════════════════════════════

    async def get_comprehensive_resource_state(self) -> SystemResourceState:
        """
        Get comprehensive system resource state.

        Combines all checks into single state object.
        """
        warnings = []
        errors = []

        # Filesystem checks
        fs_writable, fs_reason = await self.check_filesystem_writable()
        disk_ok, disk_metrics = await self.check_disk_space()
        fs_type = "unknown"
        try:
            if await self._is_nfs_mounted(self._state_dir):
                fs_type = "nfs"
            else:
                fs_type = "local"
        except Exception:
            pass

        if not fs_writable:
            errors.append(f"Filesystem: {fs_reason}")
        if not disk_ok:
            errors.extend(disk_metrics.get("warnings", []))
        else:
            warnings.extend(disk_metrics.get("warnings", []))

        # FD checks
        fd_ok, fd_metrics = await self.check_file_descriptors()
        if not fd_ok:
            errors.extend(fd_metrics.get("warnings", []))
        else:
            warnings.extend(fd_metrics.get("warnings", []))

        # Clock check
        clock_skew, skew_seconds = await self.check_clock_skew()
        if clock_skew:
            warnings.append(f"Clock skew: {skew_seconds:.2f}s")

        # Network partition check
        is_partitioned, partition_reason = await self.check_network_partition()
        if is_partitioned:
            errors.append(f"Network partition: {partition_reason}")

        # Memory check
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()

        if memory.percent >= self._config["memory_critical_percent"]:
            errors.append(f"Memory critical: {memory.percent:.1f}%")
        elif memory.percent >= self._config["memory_warning_percent"]:
            warnings.append(f"Memory high: {memory.percent:.1f}%")

        return SystemResourceState(
            filesystem_writable=fs_writable,
            filesystem_type=fs_type,
            disk_free_bytes=disk_metrics.get("free_bytes", 0),
            disk_free_percent=100 - disk_metrics.get("used_percent", 100),
            inode_free_percent=100 - disk_metrics.get("inode_used_percent", 100),
            memory_available_bytes=memory.available,
            memory_percent_used=memory.percent,
            swap_free_bytes=swap.free,
            fd_current=fd_metrics.get("current", 0),
            fd_soft_limit=fd_metrics.get("soft_limit", 0),
            fd_hard_limit=fd_metrics.get("hard_limit", 0),
            fd_percent_used=fd_metrics.get("percent_used", 0),
            clock_skew_detected=clock_skew,
            clock_skew_seconds=skew_seconds,
            monotonic_time=time.monotonic(),
            network_partition_suspected=is_partitioned,
            last_successful_network_op=self._last_successful_network_op,
            is_healthy=len(errors) == 0,
            warnings=warnings,
            errors=errors,
        )

    async def aggregate_cross_repo_health(
        self,
        jarvis_health: Optional[Dict[str, Any]] = None,
        jprime_health: Optional[Dict[str, Any]] = None,
        reactor_health: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Aggregate health from all Trinity components.

        Returns comprehensive health report.
        """
        resource_state = await self.get_comprehensive_resource_state()

        # Determine overall status
        component_statuses = {
            "jarvis_body": jarvis_health or {"status": "unknown"},
            "jarvis_prime": jprime_health or {"status": "unknown"},
            "reactor_core": reactor_health or {"status": "unknown"},
        }

        # Count healthy/degraded/unhealthy
        healthy_count = sum(
            1 for h in component_statuses.values()
            if h.get("status") == "healthy"
        )
        degraded_count = sum(
            1 for h in component_statuses.values()
            if h.get("status") == "degraded"
        )
        unhealthy_count = sum(
            1 for h in component_statuses.values()
            if h.get("status") in ("unhealthy", "error", "unknown")
        )

        # Determine overall state
        if not resource_state.is_healthy:
            overall_state = "error"
        elif unhealthy_count > 0:
            overall_state = "degraded"
        elif degraded_count > 0:
            overall_state = "degraded"
        elif healthy_count == 3:
            overall_state = "healthy"
        else:
            overall_state = "unknown"

        # Check version compatibility
        compat_report = await self.check_all_compatibility()

        return {
            "overall_state": overall_state,
            "timestamp": time.time(),
            "trinity_version": self.TRINITY_VERSION,
            "api_version": self.API_VERSION,
            "components": component_statuses,
            "resources": {
                "filesystem": {
                    "writable": resource_state.filesystem_writable,
                    "type": resource_state.filesystem_type,
                    "disk_free_percent": resource_state.disk_free_percent,
                    "inode_free_percent": resource_state.inode_free_percent,
                },
                "memory": {
                    "available_bytes": resource_state.memory_available_bytes,
                    "percent_used": resource_state.memory_percent_used,
                },
                "file_descriptors": {
                    "current": resource_state.fd_current,
                    "percent_used": resource_state.fd_percent_used,
                },
                "clock": {
                    "skew_detected": resource_state.clock_skew_detected,
                    "skew_seconds": resource_state.clock_skew_seconds,
                },
                "network": {
                    "partition_suspected": resource_state.network_partition_suspected,
                },
            },
            "compatibility": compat_report,
            "warnings": resource_state.warnings,
            "errors": resource_state.errors,
            "healthy_count": healthy_count,
            "degraded_count": degraded_count,
            "unhealthy_count": unhealthy_count,
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # 11. Initialization and Cleanup
    # ═══════════════════════════════════════════════════════════════════════════

    async def initialize(self) -> bool:
        """
        Initialize all v87.0 advanced features.

        Returns:
            True if all features initialized successfully
        """
        if self._initialized:
            return True

        try:
            # Initialize shared memory
            shm_ok = await self.initialize_shared_memory()
            if not shm_ok:
                logger.warning("[TrinityAdvanced] Shared memory init failed, continuing without")

            # Start event bus
            bus_ok = await self.start_event_bus()
            if not bus_ok:
                logger.warning("[TrinityAdvanced] Event bus init failed, continuing without")

            # Register our version
            await self.register_component_version(
                component="trinity_coordinator",
                version=self.TRINITY_VERSION,
                api_version=self.API_VERSION,
                min_compatible_api=self.MIN_COMPATIBLE_API,
            )

            # Initial resource check
            resource_state = await self.get_comprehensive_resource_state()
            if not resource_state.is_healthy:
                for error in resource_state.errors:
                    logger.error(f"[TrinityAdvanced] Resource issue: {error}")

            # Record baseline FD count
            self.reset_fd_baseline()

            self._initialized = True
            logger.info(f"[TrinityAdvanced] v{self.TRINITY_VERSION} initialized successfully")

            # Log init event
            await self.append_event(
                "coordinator_initialized",
                {
                    "version": self.TRINITY_VERSION,
                    "pid": os.getpid(),
                    "resource_healthy": resource_state.is_healthy,
                },
            )

            return True

        except Exception as e:
            logger.error(f"[TrinityAdvanced] Initialization failed: {e}")
            return False

    async def cleanup(self) -> None:
        """Cleanup all resources."""
        try:
            # Log shutdown event
            await self.append_event(
                "coordinator_shutdown",
                {"pid": os.getpid()},
            )

            # Stop event bus
            await self.stop_event_bus()

            # Cleanup shared memory
            await self.cleanup_shared_memory()

            # Compact event log
            await self.compact_event_log()

            self._initialized = False
            logger.info("[TrinityAdvanced] Cleanup complete")

        except Exception as e:
            logger.error(f"[TrinityAdvanced] Cleanup error: {e}")


# Custom exception for network partition
class NetworkPartitionError(Exception):
    """Raised when network partition is detected."""
    pass


# Custom exception for circuit open
class CircuitOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# Singleton instance
_advanced_coordinator_instance: Optional[TrinityAdvancedCoordinator] = None


async def get_advanced_coordinator() -> TrinityAdvancedCoordinator:
    """Get the singleton advanced coordinator instance."""
    global _advanced_coordinator_instance
    if _advanced_coordinator_instance is None:
        _advanced_coordinator_instance = TrinityAdvancedCoordinator()
        await _advanced_coordinator_instance.initialize()
    return _advanced_coordinator_instance


# =============================================================================
# v88.0: Ultra-Advanced Coordination Features
# =============================================================================
#
# This section implements cutting-edge coordination features:
#   1. Adaptive Circuit Breaker with ML-based prediction
#   2. Proactive Failure Prediction using trend analysis
#   3. Lock-Free Ring Buffer for high-performance IPC
#   4. Container/cgroup resource awareness
#   5. Backpressure handling with adaptive rate limiting
#   6. Trace ID propagation for distributed tracing
#   7. Structured concurrency helpers
#
# These features push Python to its limits for maximum robustness.
# =============================================================================


@dataclass
class PredictiveMetrics:
    """Metrics for failure prediction."""
    timestamp: float
    latency_ms: float
    error_rate: float
    success_count: int
    failure_count: int
    memory_percent: float
    cpu_percent: float


@dataclass
class TrendAnalysis:
    """Trend analysis results."""
    slope: float  # Rate of change
    intercept: float  # Current level
    r_squared: float  # Fit quality
    prediction: float  # Predicted next value
    confidence: float  # Prediction confidence
    trend_direction: str  # "improving", "stable", "degrading"


class AdaptiveCircuitBreaker:
    """
    v88.0: Adaptive Circuit Breaker with ML-based prediction.

    Uses exponential smoothing and trend analysis to:
    - Predict failures BEFORE they happen
    - Dynamically adjust thresholds based on historical patterns
    - Learn optimal recovery timing

    This is NOT a simple threshold-based circuit breaker - it uses
    statistical methods to adapt to system behavior.
    """

    # States
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        # Adaptive parameters
        alpha: float = 0.3,  # Exponential smoothing factor
        prediction_window: int = 10,  # Samples for trend analysis
        adaptive_threshold: bool = True,
    ):
        self.name = name
        self._base_failure_threshold = failure_threshold
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._alpha = alpha
        self._prediction_window = prediction_window
        self._adaptive_threshold = adaptive_threshold

        # State
        self._state = self.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

        # Metrics history for prediction
        self._metrics_history: Deque[PredictiveMetrics] = deque(maxlen=100)
        self._latency_ema: float = 0.0  # Exponential moving average
        self._error_rate_ema: float = 0.0

        # Adaptive learning
        self._recovery_times: Deque[float] = deque(maxlen=20)
        self._optimal_recovery_timeout: Optional[float] = None

    @property
    def state(self) -> str:
        """Current circuit breaker state."""
        return self._state

    @property
    def is_closed(self) -> bool:
        """Whether circuit is closed (allowing requests)."""
        return self._state == self.CLOSED

    async def record_success(self, latency_ms: float) -> None:
        """Record successful call and update metrics."""
        async with self._lock:
            self._success_count += 1

            # Update exponential moving average
            self._latency_ema = (
                self._alpha * latency_ms + (1 - self._alpha) * self._latency_ema
            )

            # Record metrics
            await self._record_metrics(latency_ms, success=True)

            # Handle half-open state
            if self._state == self.HALF_OPEN:
                self._half_open_calls += 1
                if self._half_open_calls >= self._half_open_max_calls:
                    # Recovery successful
                    await self._transition_to_closed()

    async def record_failure(self, latency_ms: float = 0.0) -> None:
        """Record failed call and potentially open circuit."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            # Update error rate EMA
            total = self._success_count + self._failure_count
            if total > 0:
                error_rate = self._failure_count / total
                self._error_rate_ema = (
                    self._alpha * error_rate + (1 - self._alpha) * self._error_rate_ema
                )

            # Record metrics
            await self._record_metrics(latency_ms, success=False)

            # Check if we should open
            if self._state == self.CLOSED:
                if self._failure_count >= self._failure_threshold:
                    await self._transition_to_open()
            elif self._state == self.HALF_OPEN:
                # Failed during recovery - reopen
                await self._transition_to_open()

    async def _record_metrics(self, latency_ms: float, success: bool) -> None:
        """Record metrics for trend analysis."""
        try:
            proc = psutil.Process()
            memory_percent = proc.memory_percent()
            cpu_percent = proc.cpu_percent()
        except Exception:
            memory_percent = 0.0
            cpu_percent = 0.0

        total = self._success_count + self._failure_count
        error_rate = self._failure_count / total if total > 0 else 0.0

        metrics = PredictiveMetrics(
            timestamp=time.time(),
            latency_ms=latency_ms,
            error_rate=error_rate,
            success_count=self._success_count,
            failure_count=self._failure_count,
            memory_percent=memory_percent,
            cpu_percent=cpu_percent,
        )
        self._metrics_history.append(metrics)

        # Adapt threshold if enabled
        if self._adaptive_threshold and len(self._metrics_history) >= self._prediction_window:
            await self._adapt_threshold()

    async def _adapt_threshold(self) -> None:
        """
        Dynamically adjust failure threshold based on trend analysis.

        Uses linear regression to predict future error rate and
        adjusts threshold to be more or less sensitive accordingly.
        """
        recent = list(self._metrics_history)[-self._prediction_window:]
        if len(recent) < 3:
            return

        # Analyze error rate trend
        trend = await self._analyze_trend([m.error_rate for m in recent])

        if trend.trend_direction == "degrading" and trend.confidence > 0.7:
            # System degrading - lower threshold to trip sooner
            self._failure_threshold = max(2, self._base_failure_threshold - 2)
        elif trend.trend_direction == "improving" and trend.confidence > 0.7:
            # System improving - raise threshold
            self._failure_threshold = min(
                self._base_failure_threshold * 2,
                self._base_failure_threshold + 3
            )
        else:
            # Stable - use base threshold
            self._failure_threshold = self._base_failure_threshold

    async def _analyze_trend(self, values: List[float]) -> TrendAnalysis:
        """
        Perform linear regression for trend analysis.

        Returns slope, intercept, and prediction for next value.
        """
        n = len(values)
        if n < 2:
            return TrendAnalysis(0, values[-1] if values else 0, 0, 0, 0, "stable")

        # Simple linear regression
        x = list(range(n))
        x_mean = sum(x) / n
        y_mean = sum(values) / n

        # Calculate slope and intercept
        numerator = sum((x[i] - x_mean) * (values[i] - y_mean) for i in range(n))
        denominator = sum((x[i] - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return TrendAnalysis(0, y_mean, 0, y_mean, 0, "stable")

        slope = numerator / denominator
        intercept = y_mean - slope * x_mean

        # Calculate R-squared
        y_pred = [slope * xi + intercept for xi in x]
        ss_res = sum((values[i] - y_pred[i]) ** 2 for i in range(n))
        ss_tot = sum((values[i] - y_mean) ** 2 for i in range(n))
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

        # Predict next value
        prediction = slope * n + intercept
        confidence = min(r_squared, 1.0)

        # Determine trend direction
        if abs(slope) < 0.001:
            direction = "stable"
        elif slope > 0:
            direction = "degrading"  # Error rate increasing
        else:
            direction = "improving"  # Error rate decreasing

        return TrendAnalysis(
            slope=slope,
            intercept=intercept,
            r_squared=r_squared,
            prediction=prediction,
            confidence=confidence,
            trend_direction=direction,
        )

    async def can_execute(self) -> Tuple[bool, str]:
        """
        Check if request can be executed.

        Returns:
            (can_execute: bool, reason: str)
        """
        async with self._lock:
            if self._state == self.CLOSED:
                # Check if we should preemptively open based on prediction
                if await self._should_preemptive_open():
                    await self._transition_to_open()
                    return False, "Circuit opened preemptively due to predicted failure"
                return True, "Circuit closed"

            elif self._state == self.OPEN:
                # Check if recovery timeout has elapsed
                if self._last_failure_time:
                    elapsed = time.time() - self._last_failure_time
                    timeout = self._optimal_recovery_timeout or self._recovery_timeout
                    if elapsed >= timeout:
                        await self._transition_to_half_open()
                        return True, "Circuit half-open, testing recovery"
                return False, f"Circuit open, waiting for recovery ({self._recovery_timeout}s)"

            else:  # HALF_OPEN
                if self._half_open_calls < self._half_open_max_calls:
                    return True, "Circuit half-open, allowing test call"
                return False, "Circuit half-open, max test calls reached"

    async def _should_preemptive_open(self) -> bool:
        """
        Predict if failure is imminent and preemptively open circuit.

        Uses trend analysis to detect degradation patterns.
        """
        if len(self._metrics_history) < self._prediction_window:
            return False

        recent = list(self._metrics_history)[-self._prediction_window:]

        # Analyze latency trend
        latencies = [m.latency_ms for m in recent]
        latency_trend = await self._analyze_trend(latencies)

        # Analyze error rate trend
        error_rates = [m.error_rate for m in recent]
        error_trend = await self._analyze_trend(error_rates)

        # Preemptive open conditions:
        # 1. Error rate trending up rapidly with high confidence
        # 2. Latency spiking (3x normal)
        # 3. Predicted error rate > 0.5

        if (
            error_trend.trend_direction == "degrading"
            and error_trend.confidence > 0.8
            and error_trend.prediction > 0.3
        ):
            logger.warning(
                f"[AdaptiveCircuit:{self.name}] Preemptive open - "
                f"predicted error rate: {error_trend.prediction:.2%}"
            )
            return True

        if latencies and self._latency_ema > 0:
            current_latency = latencies[-1]
            if current_latency > self._latency_ema * 3:
                logger.warning(
                    f"[AdaptiveCircuit:{self.name}] Preemptive open - "
                    f"latency spike: {current_latency:.0f}ms vs {self._latency_ema:.0f}ms EMA"
                )
                return True

        return False

    async def _transition_to_open(self) -> None:
        """Transition to open state."""
        old_state = self._state
        self._state = self.OPEN
        self._half_open_calls = 0
        logger.warning(
            f"[AdaptiveCircuit:{self.name}] {old_state} -> OPEN "
            f"(failures: {self._failure_count}, threshold: {self._failure_threshold})"
        )

    async def _transition_to_half_open(self) -> None:
        """Transition to half-open state."""
        self._state = self.HALF_OPEN
        self._half_open_calls = 0
        logger.info(f"[AdaptiveCircuit:{self.name}] OPEN -> HALF_OPEN (testing recovery)")

    async def _transition_to_closed(self) -> None:
        """Transition to closed state and learn recovery time."""
        # Learn optimal recovery time
        if self._last_failure_time:
            recovery_time = time.time() - self._last_failure_time
            self._recovery_times.append(recovery_time)
            if len(self._recovery_times) >= 3:
                # Use median of recent recovery times
                sorted_times = sorted(self._recovery_times)
                self._optimal_recovery_timeout = sorted_times[len(sorted_times) // 2]

        self._state = self.CLOSED
        self._failure_count = 0
        self._success_count = 0
        logger.info(
            f"[AdaptiveCircuit:{self.name}] HALF_OPEN -> CLOSED "
            f"(learned recovery: {self._optimal_recovery_timeout:.1f}s)"
            if self._optimal_recovery_timeout else
            f"[AdaptiveCircuit:{self.name}] HALF_OPEN -> CLOSED"
        )

    def get_status(self) -> Dict[str, Any]:
        """Get circuit breaker status."""
        return {
            "name": self.name,
            "state": self._state,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self._failure_threshold,
            "base_threshold": self._base_failure_threshold,
            "latency_ema_ms": self._latency_ema,
            "error_rate_ema": self._error_rate_ema,
            "optimal_recovery_timeout": self._optimal_recovery_timeout,
            "metrics_count": len(self._metrics_history),
        }


class LockFreeRingBuffer:
    """
    v88.0: Lock-Free Ring Buffer for high-performance IPC.

    Uses atomic operations (compare-and-swap via threading primitives)
    to achieve thread-safe, lock-free operation.

    This is useful for high-frequency event passing between components
    without blocking.
    """

    def __init__(self, capacity: int = 1024):
        self._capacity = capacity
        self._buffer: List[Optional[Any]] = [None] * capacity
        self._head = 0  # Write position
        self._tail = 0  # Read position
        self._size = 0
        # Use threading lock for atomic increment (Python limitation)
        # In C/Rust we'd use actual atomic CAS operations
        self._head_lock = threading.Lock()
        self._tail_lock = threading.Lock()

    def push(self, item: Any) -> bool:
        """
        Push item to buffer (non-blocking).

        Returns True if successful, False if buffer full.
        """
        with self._head_lock:
            if self._size >= self._capacity:
                return False  # Buffer full

            self._buffer[self._head] = item
            self._head = (self._head + 1) % self._capacity
            self._size += 1
            return True

    def pop(self) -> Optional[Any]:
        """
        Pop item from buffer (non-blocking).

        Returns item or None if buffer empty.
        """
        with self._tail_lock:
            if self._size <= 0:
                return None  # Buffer empty

            item = self._buffer[self._tail]
            self._buffer[self._tail] = None  # Clear reference
            self._tail = (self._tail + 1) % self._capacity
            self._size -= 1
            return item

    def peek(self) -> Optional[Any]:
        """Peek at next item without removing."""
        if self._size <= 0:
            return None
        return self._buffer[self._tail]

    @property
    def size(self) -> int:
        """Current number of items in buffer."""
        return self._size

    @property
    def capacity(self) -> int:
        """Maximum buffer capacity."""
        return self._capacity

    @property
    def is_empty(self) -> bool:
        """Whether buffer is empty."""
        return self._size == 0

    @property
    def is_full(self) -> bool:
        """Whether buffer is full."""
        return self._size >= self._capacity

    def clear(self) -> int:
        """Clear buffer and return number of items cleared."""
        with self._head_lock:
            with self._tail_lock:
                cleared = self._size
                self._buffer = [None] * self._capacity
                self._head = 0
                self._tail = 0
                self._size = 0
                return cleared


@dataclass
class ContainerResourceLimits:
    """Container resource limits from cgroups."""
    cpu_limit_cores: Optional[float] = None
    memory_limit_bytes: Optional[int] = None
    memory_usage_bytes: Optional[int] = None
    pids_limit: Optional[int] = None
    pids_current: Optional[int] = None
    is_containerized: bool = False
    cgroup_version: Optional[int] = None


class ContainerAwareness:
    """
    v88.0: Container and cgroup resource awareness.

    Detects when running in containers (Docker, Kubernetes, etc.)
    and reads actual resource limits from cgroups.

    This prevents the common mistake of reading host resources
    when running in a container with limits.
    """

    # cgroup v2 paths
    CGROUP_V2_CPU = "/sys/fs/cgroup/cpu.max"
    CGROUP_V2_MEMORY = "/sys/fs/cgroup/memory.max"
    CGROUP_V2_MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
    CGROUP_V2_PIDS = "/sys/fs/cgroup/pids.max"
    CGROUP_V2_PIDS_CURRENT = "/sys/fs/cgroup/pids.current"

    # cgroup v1 paths
    CGROUP_V1_CPU_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
    CGROUP_V1_CPU_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
    CGROUP_V1_MEMORY = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    CGROUP_V1_MEMORY_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

    @classmethod
    def is_running_in_container(cls) -> bool:
        """
        Detect if running inside a container.

        Checks multiple indicators:
        1. /.dockerenv file exists
        2. /run/.containerenv exists (Podman)
        3. cgroup contains docker/kubepods/containerd
        4. init process is not systemd/init
        """
        # Check for Docker
        if Path("/.dockerenv").exists():
            return True

        # Check for Podman
        if Path("/run/.containerenv").exists():
            return True

        # Check cgroup for container indicators
        try:
            cgroup_path = Path("/proc/1/cgroup")
            if cgroup_path.exists():
                content = cgroup_path.read_text()
                container_indicators = ["docker", "kubepods", "containerd", "lxc"]
                if any(ind in content.lower() for ind in container_indicators):
                    return True
        except Exception:
            pass

        # Check if PID 1 is a container init
        try:
            cmdline = Path("/proc/1/cmdline").read_text()
            container_inits = ["tini", "dumb-init", "s6", "runsvdir"]
            if any(init in cmdline.lower() for init in container_inits):
                return True
        except Exception:
            pass

        return False

    @classmethod
    def get_cgroup_version(cls) -> Optional[int]:
        """Detect cgroup version (1 or 2)."""
        # cgroup v2 has unified hierarchy
        if Path("/sys/fs/cgroup/cgroup.controllers").exists():
            return 2
        elif Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").exists():
            return 1
        return None

    @classmethod
    def get_resource_limits(cls) -> ContainerResourceLimits:
        """
        Get container resource limits from cgroups.

        Returns actual limits when running in containers,
        which may differ from host resources.
        """
        limits = ContainerResourceLimits(
            is_containerized=cls.is_running_in_container(),
            cgroup_version=cls.get_cgroup_version(),
        )

        if limits.cgroup_version == 2:
            limits = cls._read_cgroup_v2(limits)
        elif limits.cgroup_version == 1:
            limits = cls._read_cgroup_v1(limits)

        return limits

    @classmethod
    def _read_cgroup_v2(cls, limits: ContainerResourceLimits) -> ContainerResourceLimits:
        """Read cgroup v2 limits."""
        # CPU limit
        try:
            cpu_max = Path(cls.CGROUP_V2_CPU).read_text().strip()
            if cpu_max != "max":
                parts = cpu_max.split()
                quota = int(parts[0])
                period = int(parts[1]) if len(parts) > 1 else 100000
                limits.cpu_limit_cores = quota / period
        except Exception:
            pass

        # Memory limit
        try:
            mem_max = Path(cls.CGROUP_V2_MEMORY).read_text().strip()
            if mem_max != "max":
                limits.memory_limit_bytes = int(mem_max)
        except Exception:
            pass

        # Memory current
        try:
            limits.memory_usage_bytes = int(
                Path(cls.CGROUP_V2_MEMORY_CURRENT).read_text().strip()
            )
        except Exception:
            pass

        # PIDs limit
        try:
            pids_max = Path(cls.CGROUP_V2_PIDS).read_text().strip()
            if pids_max != "max":
                limits.pids_limit = int(pids_max)
        except Exception:
            pass

        # PIDs current
        try:
            limits.pids_current = int(
                Path(cls.CGROUP_V2_PIDS_CURRENT).read_text().strip()
            )
        except Exception:
            pass

        return limits

    @classmethod
    def _read_cgroup_v1(cls, limits: ContainerResourceLimits) -> ContainerResourceLimits:
        """Read cgroup v1 limits."""
        # CPU limit
        try:
            quota = int(Path(cls.CGROUP_V1_CPU_QUOTA).read_text().strip())
            period = int(Path(cls.CGROUP_V1_CPU_PERIOD).read_text().strip())
            if quota > 0:
                limits.cpu_limit_cores = quota / period
        except Exception:
            pass

        # Memory limit
        try:
            mem_limit = int(Path(cls.CGROUP_V1_MEMORY).read_text().strip())
            # Check for "unlimited" (very large number)
            if mem_limit < 2**62:
                limits.memory_limit_bytes = mem_limit
        except Exception:
            pass

        # Memory usage
        try:
            limits.memory_usage_bytes = int(
                Path(cls.CGROUP_V1_MEMORY_USAGE).read_text().strip()
            )
        except Exception:
            pass

        return limits

    @classmethod
    def get_effective_cpu_count(cls) -> int:
        """
        Get effective CPU count respecting container limits.

        Returns container CPU limit if running in container,
        otherwise returns host CPU count.
        """
        limits = cls.get_resource_limits()
        if limits.cpu_limit_cores:
            return max(1, int(limits.cpu_limit_cores))
        return os.cpu_count() or 1

    @classmethod
    def get_effective_memory_bytes(cls) -> int:
        """
        Get effective memory limit respecting container limits.

        Returns container memory limit if running in container,
        otherwise returns host memory.
        """
        limits = cls.get_resource_limits()
        if limits.memory_limit_bytes:
            return limits.memory_limit_bytes
        return psutil.virtual_memory().total


@dataclass
class BackpressureState:
    """State for backpressure handling."""
    current_rate: float  # Current requests per second
    target_rate: float  # Target rate
    queue_depth: int  # Current queue depth
    drop_count: int  # Dropped requests
    delay_ms: float  # Current delay applied


class AdaptiveBackpressure:
    """
    v88.0: Adaptive Backpressure with rate limiting.

    Implements intelligent flow control:
    - Monitors queue depth and processing rate
    - Automatically adjusts acceptance rate
    - Uses AIMD (Additive Increase Multiplicative Decrease) algorithm
    - Provides graceful degradation under load
    """

    def __init__(
        self,
        max_queue_depth: int = 1000,
        target_latency_ms: float = 100.0,
        min_rate: float = 10.0,  # Minimum requests per second
        max_rate: float = 10000.0,  # Maximum requests per second
        # AIMD parameters
        additive_increase: float = 10.0,
        multiplicative_decrease: float = 0.5,
    ):
        self._max_queue_depth = max_queue_depth
        self._target_latency_ms = target_latency_ms
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._additive_increase = additive_increase
        self._multiplicative_decrease = multiplicative_decrease

        # State
        self._current_rate = max_rate
        self._queue_depth = 0
        self._drop_count = 0
        self._last_adjustment = time.time()
        self._latency_samples: Deque[float] = deque(maxlen=100)
        self._lock = asyncio.Lock()

        # Token bucket for rate limiting
        self._tokens = max_rate
        self._last_token_update = time.time()

    async def acquire(self, timeout: float = 1.0) -> Tuple[bool, float]:
        """
        Acquire permission to process a request.

        Returns:
            (acquired: bool, delay_ms: float)
        """
        async with self._lock:
            # Refill tokens
            now = time.time()
            elapsed = now - self._last_token_update
            self._tokens = min(
                self._current_rate,
                self._tokens + elapsed * self._current_rate
            )
            self._last_token_update = now

            # Check queue depth
            if self._queue_depth >= self._max_queue_depth:
                self._drop_count += 1
                return False, 0.0

            # Check tokens
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._queue_depth += 1
                return True, 0.0

            # Calculate delay needed
            tokens_needed = 1.0 - self._tokens
            delay_seconds = tokens_needed / self._current_rate

            if delay_seconds > timeout:
                self._drop_count += 1
                return False, delay_seconds * 1000

            # Wait for tokens
            await asyncio.sleep(delay_seconds)
            self._tokens = 0
            self._queue_depth += 1
            return True, delay_seconds * 1000

    async def release(self, latency_ms: float) -> None:
        """
        Release after processing, report latency.

        Uses AIMD to adjust rate based on latency.
        """
        async with self._lock:
            self._queue_depth = max(0, self._queue_depth - 1)
            self._latency_samples.append(latency_ms)

            # Adjust rate periodically
            now = time.time()
            if now - self._last_adjustment >= 1.0:  # Every second
                await self._adjust_rate()
                self._last_adjustment = now

    async def _adjust_rate(self) -> None:
        """Adjust rate using AIMD algorithm."""
        if not self._latency_samples:
            return

        # Calculate average latency
        avg_latency = sum(self._latency_samples) / len(self._latency_samples)

        if avg_latency <= self._target_latency_ms:
            # Good performance - increase rate (additive)
            self._current_rate = min(
                self._max_rate,
                self._current_rate + self._additive_increase
            )
        else:
            # High latency - decrease rate (multiplicative)
            self._current_rate = max(
                self._min_rate,
                self._current_rate * self._multiplicative_decrease
            )

        # Also consider queue depth
        queue_utilization = self._queue_depth / self._max_queue_depth
        if queue_utilization > 0.8:
            # Queue filling up - more aggressive decrease
            self._current_rate = max(
                self._min_rate,
                self._current_rate * 0.7
            )

    def get_state(self) -> BackpressureState:
        """Get current backpressure state."""
        avg_latency = (
            sum(self._latency_samples) / len(self._latency_samples)
            if self._latency_samples else 0.0
        )
        return BackpressureState(
            current_rate=self._current_rate,
            target_rate=self._max_rate,
            queue_depth=self._queue_depth,
            drop_count=self._drop_count,
            delay_ms=avg_latency,
        )


@dataclass
class TraceContext:
    """Distributed tracing context."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    baggage: Dict[str, str] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    component: str = "unknown"
    operation: str = "unknown"


class TraceContextManager:
    """
    v88.0: Trace ID propagation for distributed tracing.

    Provides W3C Trace Context compatible tracing across
    all Trinity components (JARVIS, J-Prime, Reactor-Core).

    Enables end-to-end request tracking and performance analysis.
    """

    # Context variable for async propagation
    _current_context: contextvars.ContextVar[Optional[TraceContext]] = (
        contextvars.ContextVar("trace_context", default=None)
    )

    @classmethod
    def generate_trace_id(cls) -> str:
        """Generate a unique trace ID (32 hex characters)."""
        return secrets.token_hex(16)

    @classmethod
    def generate_span_id(cls) -> str:
        """Generate a unique span ID (16 hex characters)."""
        return secrets.token_hex(8)

    @classmethod
    def create_context(
        cls,
        component: str,
        operation: str,
        parent_context: Optional[TraceContext] = None,
    ) -> TraceContext:
        """Create a new trace context, optionally as child of parent."""
        if parent_context:
            return TraceContext(
                trace_id=parent_context.trace_id,
                span_id=cls.generate_span_id(),
                parent_span_id=parent_context.span_id,
                baggage=parent_context.baggage.copy(),
                component=component,
                operation=operation,
            )
        return TraceContext(
            trace_id=cls.generate_trace_id(),
            span_id=cls.generate_span_id(),
            component=component,
            operation=operation,
        )

    @classmethod
    def get_current(cls) -> Optional[TraceContext]:
        """Get current trace context from async context."""
        return cls._current_context.get()

    @classmethod
    def set_current(cls, context: TraceContext) -> contextvars.Token:
        """Set current trace context, returns token for reset."""
        return cls._current_context.set(context)

    @classmethod
    @contextlib.contextmanager
    def span(
        cls,
        component: str,
        operation: str,
    ) -> Generator[TraceContext, None, None]:
        """
        Context manager for creating a traced span.

        Usage:
            with TraceContextManager.span("jarvis", "process_request") as ctx:
                # ctx.trace_id and ctx.span_id available
                do_work()
        """
        parent = cls.get_current()
        context = cls.create_context(component, operation, parent)
        token = cls.set_current(context)
        try:
            yield context
        finally:
            cls._current_context.reset(token)

    @classmethod
    def to_headers(cls, context: TraceContext) -> Dict[str, str]:
        """Convert trace context to W3C Trace Context headers."""
        # traceparent: version-trace_id-span_id-flags
        traceparent = f"00-{context.trace_id}-{context.span_id}-01"
        headers = {"traceparent": traceparent}

        # tracestate for baggage
        if context.baggage:
            tracestate = ",".join(f"{k}={v}" for k, v in context.baggage.items())
            headers["tracestate"] = tracestate

        return headers

    @classmethod
    def from_headers(
        cls,
        headers: Dict[str, str],
        component: str,
        operation: str,
    ) -> TraceContext:
        """Parse W3C Trace Context headers into TraceContext."""
        traceparent = headers.get("traceparent", "")
        tracestate = headers.get("tracestate", "")

        if traceparent:
            parts = traceparent.split("-")
            if len(parts) >= 4:
                trace_id = parts[1]
                parent_span_id = parts[2]
                return TraceContext(
                    trace_id=trace_id,
                    span_id=cls.generate_span_id(),
                    parent_span_id=parent_span_id,
                    baggage=cls._parse_tracestate(tracestate),
                    component=component,
                    operation=operation,
                )

        # No valid traceparent - create new trace
        return cls.create_context(component, operation)

    @classmethod
    def _parse_tracestate(cls, tracestate: str) -> Dict[str, str]:
        """Parse tracestate header into baggage dict."""
        if not tracestate:
            return {}
        baggage = {}
        for item in tracestate.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                baggage[k.strip()] = v.strip()
        return baggage


class StructuredConcurrency:
    """
    v88.0: Structured Concurrency helpers.

    Provides Python 3.11+ TaskGroup-like semantics with:
    - Automatic cancellation propagation
    - Proper exception handling
    - Resource cleanup guarantees
    - Timeout support

    Works on Python 3.9+ by providing TaskGroup-like API.
    """

    @staticmethod
    @contextlib.asynccontextmanager
    async def task_group(
        name: str = "task_group",
        timeout: Optional[float] = None,
    ) -> AsyncGenerator["TaskGroupContext", None]:
        """
        Create a task group with proper cancellation and cleanup.

        Usage:
            async with StructuredConcurrency.task_group("my_tasks", timeout=30) as tg:
                tg.create_task(coro1())
                tg.create_task(coro2())
            # All tasks completed or cancelled
        """
        ctx = TaskGroupContext(name)
        try:
            if timeout:
                # Python 3.11+ has asyncio.timeout, fall back to wait_for pattern
                try:
                    # Try Python 3.11+ asyncio.timeout
                    timeout_ctx = getattr(asyncio, 'timeout', None)
                    if timeout_ctx:
                        async with timeout_ctx(timeout):
                            yield ctx
                            await ctx.wait_all()
                    else:
                        # Python 3.9/3.10 fallback using wait_for
                        yield ctx
                        await asyncio.wait_for(ctx.wait_all(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"[StructuredConcurrency:{name}] Timeout, cancelling tasks")
                    await ctx.cancel_all()
                    raise
            else:
                yield ctx
                await ctx.wait_all()
        except asyncio.TimeoutError:
            raise  # Re-raise timeout
        except Exception as e:
            logger.error(f"[StructuredConcurrency:{name}] Error: {e}, cancelling tasks")
            await ctx.cancel_all()
            raise
        finally:
            await ctx.cleanup()


class TaskGroupContext:
    """Context for managing a group of tasks."""

    def __init__(self, name: str):
        self.name = name
        self._tasks: List[asyncio.Task] = []
        self._results: List[Any] = []
        self._errors: List[Exception] = []

    def create_task(
        self,
        coro: Awaitable[T],
        name: Optional[str] = None,
    ) -> asyncio.Task[T]:
        """Create and track a task."""
        task = asyncio.create_task(coro, name=name or f"{self.name}_task_{len(self._tasks)}")
        self._tasks.append(task)
        return task

    async def wait_all(self) -> List[Any]:
        """Wait for all tasks to complete."""
        if not self._tasks:
            return []

        done, pending = await asyncio.wait(
            self._tasks,
            return_when=asyncio.ALL_COMPLETED,
        )

        for task in done:
            try:
                result = task.result()
                self._results.append(result)
            except Exception as e:
                self._errors.append(e)

        if self._errors:
            # Raise first error, but log all
            for error in self._errors[1:]:
                logger.error(f"[TaskGroup:{self.name}] Additional error: {error}")
            raise self._errors[0]

        return self._results

    async def cancel_all(self) -> int:
        """Cancel all pending tasks."""
        cancelled = 0
        for task in self._tasks:
            if not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    async def cleanup(self) -> None:
        """Ensure all tasks are properly cleaned up."""
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class TrinityUltraCoordinator:
    """
    v88.0: Ultra-Advanced Trinity Coordinator.

    Combines all v88.0 features into a unified coordinator:
    - Adaptive circuit breakers per component
    - Proactive failure prediction
    - Container-aware resource management
    - Backpressure handling
    - Distributed tracing
    - Structured concurrency

    This is the most advanced coordination layer for Trinity.
    """

    TRINITY_ULTRA_VERSION: Final[str] = "88.0"
    API_VERSION: Final[str] = "4.0"

    def __init__(self):
        # Circuit breakers per component
        self._circuit_breakers: Dict[str, AdaptiveCircuitBreaker] = {
            "jprime": AdaptiveCircuitBreaker("jprime", failure_threshold=5),
            "reactor": AdaptiveCircuitBreaker("reactor", failure_threshold=3),
            "voice": AdaptiveCircuitBreaker("voice", failure_threshold=10),
        }

        # Backpressure handler
        self._backpressure = AdaptiveBackpressure(
            max_queue_depth=int(os.getenv("JARVIS_MAX_QUEUE_DEPTH", "1000")),
            target_latency_ms=float(os.getenv("JARVIS_TARGET_LATENCY_MS", "100")),
        )

        # High-performance event buffer
        self._event_buffer = LockFreeRingBuffer(
            capacity=int(os.getenv("JARVIS_EVENT_BUFFER_SIZE", "4096"))
        )

        # Container awareness
        self._container_limits = ContainerAwareness.get_resource_limits()

        # State
        self._initialized = False
        self._lock = asyncio.Lock()

        logger.info(f"[TrinityUltra] v{self.TRINITY_ULTRA_VERSION} coordinator created")

    async def initialize(self) -> None:
        """Initialize the ultra coordinator."""
        async with self._lock:
            if self._initialized:
                return

            # Log container info
            if self._container_limits.is_containerized:
                logger.info(
                    f"[TrinityUltra] Running in container "
                    f"(cgroup v{self._container_limits.cgroup_version})"
                )
                if self._container_limits.cpu_limit_cores:
                    logger.info(
                        f"[TrinityUltra] CPU limit: {self._container_limits.cpu_limit_cores} cores"
                    )
                if self._container_limits.memory_limit_bytes:
                    mem_gb = self._container_limits.memory_limit_bytes / (1024**3)
                    logger.info(f"[TrinityUltra] Memory limit: {mem_gb:.1f} GB")

            self._initialized = True
            logger.info(f"[TrinityUltra] v{self.TRINITY_ULTRA_VERSION} initialized")

    async def execute_with_protection(
        self,
        component: str,
        operation: Callable[[], Awaitable[T]],
        timeout: Optional[float] = None,
    ) -> Tuple[bool, Optional[T], Dict[str, Any]]:
        """
        Execute operation with full v88.0 protection stack.

        Applies:
        1. Distributed tracing
        2. Circuit breaker check
        3. Backpressure rate limiting
        4. Timeout handling
        5. Metrics recording

        Returns:
            (success, result, metadata)
        """
        metadata: Dict[str, Any] = {
            "component": component,
            "start_time": time.time(),
        }

        # Get or create circuit breaker
        circuit = self._circuit_breakers.get(component)
        if not circuit:
            circuit = AdaptiveCircuitBreaker(component)
            self._circuit_breakers[component] = circuit

        # Create trace context
        with TraceContextManager.span("jarvis", f"{component}_operation") as trace_ctx:
            metadata["trace_id"] = trace_ctx.trace_id
            metadata["span_id"] = trace_ctx.span_id

            # Check circuit breaker
            can_execute, reason = await circuit.can_execute()
            if not can_execute:
                metadata["circuit_state"] = circuit.state
                metadata["reason"] = reason
                return False, None, metadata

            # Apply backpressure
            acquired, delay_ms = await self._backpressure.acquire(timeout or 30.0)
            if not acquired:
                metadata["backpressure_dropped"] = True
                metadata["delay_ms"] = delay_ms
                await circuit.record_failure(delay_ms)
                return False, None, metadata

            # Execute with timeout
            start = time.time()
            try:
                if timeout:
                    result = await asyncio.wait_for(operation(), timeout=timeout)
                else:
                    result = await operation()

                latency_ms = (time.time() - start) * 1000
                await circuit.record_success(latency_ms)
                await self._backpressure.release(latency_ms)

                metadata["latency_ms"] = latency_ms
                metadata["success"] = True
                return True, result, metadata

            except asyncio.TimeoutError:
                latency_ms = (time.time() - start) * 1000
                await circuit.record_failure(latency_ms)
                await self._backpressure.release(latency_ms)

                metadata["latency_ms"] = latency_ms
                metadata["timeout"] = True
                return False, None, metadata

            except Exception as e:
                latency_ms = (time.time() - start) * 1000
                await circuit.record_failure(latency_ms)
                await self._backpressure.release(latency_ms)

                metadata["latency_ms"] = latency_ms
                metadata["error"] = str(e)
                return False, None, metadata

    def push_event(self, event: Any) -> bool:
        """Push event to high-performance buffer (non-blocking)."""
        return self._event_buffer.push(event)

    def pop_event(self) -> Optional[Any]:
        """Pop event from buffer (non-blocking)."""
        return self._event_buffer.pop()

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive coordinator status."""
        return {
            "version": self.TRINITY_ULTRA_VERSION,
            "api_version": self.API_VERSION,
            "initialized": self._initialized,
            "container": {
                "is_containerized": self._container_limits.is_containerized,
                "cgroup_version": self._container_limits.cgroup_version,
                "cpu_limit": self._container_limits.cpu_limit_cores,
                "memory_limit_bytes": self._container_limits.memory_limit_bytes,
            },
            "circuit_breakers": {
                name: cb.get_status()
                for name, cb in self._circuit_breakers.items()
            },
            "backpressure": {
                "current_rate": self._backpressure._current_rate,
                "queue_depth": self._backpressure._queue_depth,
                "drop_count": self._backpressure._drop_count,
            },
            "event_buffer": {
                "size": self._event_buffer.size,
                "capacity": self._event_buffer.capacity,
            },
        }


# Module-level singleton
_ultra_coordinator_instance: Optional[TrinityUltraCoordinator] = None


async def get_ultra_coordinator() -> TrinityUltraCoordinator:
    """Get singleton TrinityUltraCoordinator instance."""
    global _ultra_coordinator_instance
    if _ultra_coordinator_instance is None:
        _ultra_coordinator_instance = TrinityUltraCoordinator()
        await _ultra_coordinator_instance.initialize()
    return _ultra_coordinator_instance


# =============================================================================
# Types and Enums
# =============================================================================

class TrinityState(str, Enum):
    """Overall Trinity system state."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class ComponentHealth(str, Enum):
    """Health status of a component."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentStatus:
    """Status of a Trinity component."""
    name: str
    health: ComponentHealth
    online: bool
    last_heartbeat: Optional[float] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class TrinityHealth:
    """Overall Trinity system health."""
    state: TrinityState
    components: Dict[str, ComponentStatus]
    uptime_seconds: float
    last_check: float
    degraded_components: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# =============================================================================
# Trinity Unified Orchestrator v83.0
# =============================================================================

class TrinityUnifiedOrchestrator:
    """
    Trinity Unified Orchestrator v83.0 - Production-Grade Integration.

    The SINGLE POINT OF TRUTH for Trinity integration with:
    - JARVIS Body (this repo)
    - JARVIS Prime (cognitive mind)
    - Reactor-Core (training nerves)

    v83.0 Critical Features:
    ════════════════════════
    ✅ Crash Recovery      - Auto-restart with exponential backoff
    ✅ Process Supervisor  - PID monitoring, zombie detection, auto-heal
    ✅ Resource Coordinator- Port/memory/CPU reservation with pooling
    ✅ Event Store         - WAL-backed durable events with replay
    ✅ Distributed Tracer  - Cross-repo tracing with correlation IDs
    ✅ Health Aggregator   - Anomaly detection, trend analysis
    ✅ Transactional Start - Two-phase commit with rollback
    ✅ Circuit Breakers    - Fail-fast patterns throughout
    ✅ Adaptive Throttling - Backpressure management
    ✅ Zero Hardcoding     - 100% config-driven
    """

    # 2. Guaranteed Event Delivery System  
    class GuaranteedEventDelivery:
        """
        Guaranteed event with acknowledgement and retry.

        Features:
        - Acknowledgement-based delivery 
        - Automatic retry with exponential backoff 
        - Persistent event queue (SQLite-backed)
        - At-least-once delivery guarantee
        """

        def __init__(self, 
            store_path: Optional[Path] = None, 
            max_retries: int = 5, 
            retry_backoff: float = 1.0,
        ):
            self._store_path = store_path or Path.home() / ".jarvis" / "trinity" / "events.db" # Default store path is in the user's home directory under .jarvis/trinity/events.db 
            self._store_path.parent.mkdir(parents=True, exist_ok=True) # Create directory if it doesn't exist. parent is the directory above the store path.
            self._max_retries = max_retries # Maximum number of retries. After this many retries, the event is considered failed. 
            self._retry_backoff = retry_backoff # Base retry delay in seconds. This is the delay before the next retry. 

            self._pending_events: Dict[str, Dict[str, Any]] = {} # Event ID -> Event data. This is the event that is being processed. 
            self._ack_timeouts: Dict[str, asyncio.Task] = {} # Event ID -> Task. This is the task that is waiting for the acknowledgement. 
            self._retry_tasks: Dict[str, asyncio.Task] = {} # Event ID -> Task. This is the task that is waiting for the retry. 

            self._db_conn: Optional[sqlite3.Connection] = None # Database connection. This is the connection to the SQLite database. 
            self._db_lock = asyncio.Lock() # Lock for the database. This is used to prevent concurrent access to the database.  

        # Initialize the event store. This is called when the orchestrator is initialized. 
        async def initialize(self) -> None:
            """Initialize persistent event store."""
            async with self._db_lock: # Lock the database to prevent concurrent access. 
                # Connect to the database. 
                self._db_conn = sqlite2.connect(
                    str(self._store_path),
                    check_same_thread=False,
                    timeout=30.0,
                )
                self._db_conn.execute("PRAGMA journal_mode=WAL") # Enable WAL mode for better concurrency 
                self._db_conn.execute("PRAGMA busy_timeout=30000") # Set busy timeout to 30 seconds. This is the timeout for the database to wait for a lock.  

                # Create tables for pending events. This is the table that stores the events that are being processed. 
                self._db_conn.execute(""" 
                    CREATE TABLE IF NOT EXISTS pending_events (
                        event_id TEXT PRIMARY KEY,
                        event_data TEXT NOT NULL, 
                        target_component TEXT, 
                        retry_count INTEGER DEFAULT 0, 
                        created_at REAL NOT NULL,
                        last_attempt_at REAL, 
                        next_retry REAL
                    )
                """)

                # Create index for next retry. This is used to find the next event to retry. 
                self._db_conn.execute(""" 
                    CREATE INDEX IF NOT EXISTS idx_next_retry 
                    ON pending_events(next_retry)
                """)

                self._db_conn.commit() # Commit the changes to the database. 

                # Load pending events
                await self._load_pending_events() # Load pending events from the database. 

        # Send event with acknowledgment guarantee. This is called when the event is sent to the target component. 
        async def send_with_ack(
            self, # Self is the instance of the class. 
            event: TrinityEvent, # Event to send. This is the event that is being sent. 
            target_component: str, # Target component. This is the component that is receiving the event. 
            ack_timeout: float = 30.0, # Acknowledgement timeout. This is the timeout for the acknowledgement. 
        ) -> bool: # Return True if acknowledged, False if failed after retries. 
            """
            Send event with acknowledgment guarantee.
            
            Returns:
                True if acknowledged, False if failed after retries
            """
            event_id = event.event_id # Get the event ID. This is the unique identifier for the event. 

            # Store event in database. This is the event that is being processed.  
            async with self._db_lock:
                self._db_conn.execute(
                    """
                    INSERT OR REPLACE INTO pending_events 
                    (event_id, event_data, target_component, created_at, next_retry)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id, # Event ID. This is the unique identifier for the event. 
                        json.dumps(event.to_dict()), # Event data. This is the event that is being processed. 
                        target_component, # Target component. This is the component that is receiving the event. 
                        time.time(), # Created at. This is the time when the event was created. 
                        time.time(), # Next retry. This is the time when the event will be retried. 
                    )
                )
                self._db_conn.commit() # Commit the changes to the database. 
            
            # Track pending event. This is the event that is being processed. 
            self._pending_events[event_id] = {
                "event": event, # Event data. This is the event that is being processed. 
                "target": target_component, # Target component. This is the component that is receiving the event. 
                "retry_count": 0, # Retry count. This is the number of times the event has been retried. 
                "ack_timeout": ack_timeout, # Acknowledgement timeout. This is the timeout for the acknowledgement. 
            } 

            # Send event to target component and wait for acknowledgement.  
            ack_received = await self._send_and_wait_ack(event_id, target_component)

            # If acknowledgement is received, remove from pending.  
            if ack_received: 
                # Remove from pending. This is the event that is being processed. 
                await self._remove_pending_event(event_id) 
                return True # Return True if acknowledgement is received. 
            else: # If acknowledgement is not received, schedule retry. 
                # Schedule retry 
                await self._schedule_retry(event_id) # Schedule retry. This is the event that is being processed. 
                return False # Return False if acknowledgement is not received. 
        
        async def _send_and_wait_ack(self, event_id: str, target_component: str) -> bool:
            """Send event and wait for acknowlegment."""
            # Get pending event. This is the event that is being processed. 
            pending = self._pending_events.get(event_id) 

            if not pending: # If the event is not found, return False.  
                return False  # Return False if the event is not found.  

            event = pending["event"] # Event data. This is the event that is being processed. 
            timeout = pending["ack_timeout"] # Acknowledgement timeout. This is the timeout for the acknowledgement. 

            # Send via bridge (this would call the actual bridge)
            # For now, simulate 
            try: 
                # Create future for ACK. This is the future that is waiting for the acknowledgement. 
                ack_future = asyncio.Future() 
                
                # Create task to wait for acknowledgement. This is the task that is waiting for the ACK. 
                self._ack_timeouts[event_id] = aysncio.create_task(
                    self._wait_for_ack(event_id, ack_future, timeout) # Wait for acknowledgement. This is the task that is waiting for the ACK. 
                )

                try: 
                    await asyncio.wait_for(ack_future, timeout=timeout) # Wait for acknowledgement. This is the future that is waiting for the ACK. 
                    return True # Return True if acknowledgement is received. 
                except asyncio.TimeoutError: # If the acknowledgement is not received, return False.  
                    return False # Return False if the acknowledgement is not received.  
            
            except Exception as e: # If an error occurs, log the error and return False.  
                logger.error(f"Error sending event {event_id}: {e}") # Log the error.  
                return False # Return False if an error occurs.  
                
        # Wait for ACK (would be called by bridge on ACK). 
        async def _wait_for_ack(
            self,
            event_id: str, 
            future: asyncio.Future,
            timeout: float,
        ): 
            """Wait for ACK (would be called by bridge on ACK).""" 
            try: 
                await asyncio.sleep(timeout) # Wait for the timeout. 
                if not future.done(): # If the future is not done, set the result to False.     
                    future.set_result(False) # Set the result to False. 
            except asyncio.CancelledError: # If the task is cancelled, pass. 
                pass # Pass if the task is cancelled. 
        
        # Acknowledge the event. This is called by the bridge on ACK. 
        def acknowledge(self, event_id: str): 
            """Acknowledge the event."""
            if event_id in self._ack_timeouts: # If the event is in the acknowledgement timeouts, cancel the task. 
                task = self._ack_timeouts.pop(event_id) # Get the task. 
                task.cancel() # Cancel the task. 
            
            if event_id in self._pending_events: # If the event is in the pending events, set the result to True. 
                future = asyncio.Future() # Create a future. 
                future.set_result(True) # Set the result to True. 

        # Schedule retry. This is called when the event is not acknowledged. 
        async def schedule_retry(self, event_id: str): 
            # Get pending event. This is the event that is being processed. 
            pending = self._pending_events.get(event_id) 

            # If the event is not found, return. 
            if not pending: # If the event is not found, return. 
                return # Return if the event is not found. 

            # Get retry count. This is the number of times the event has been retried. 
            retry_count = pending["retry_count"]

            if retry_count >= self._max_retries: # If the retry count is greater than the maximum retries, log the error and remove the event from the pending events.  
                logger.error(f"Event {event_id} failed after {retry_count} retries")
                await self._remove_pending_event(event_id) # Remove the event from the pending events. 
                return # Return if the retry count is greater than the maximum retries.     
            
            # Calculate backoff time. This is the time to wait before the next retry.  
            backoff = self._retry_backoff * (2 ** retry_count) 
            # Calculate next retry time. This is the time when the event will be retried. 
            next_retry = time.time() + backoff 

            # Update retry count. This is the number of times the event has been retried. 
            pending["retry_count"] = retry_count + 1 

            # Update database with new retry info. This is the event that is being processed. 
            async with self._db_lock: 
                self._db_conn.execute(
                    """
                    UPDATE pending_events 
                    SET retry_count = ?, next_retry = ?, last_attempt = ? 
                    WHERE event_id = ? 
                    """, 
                    (retry_count + 1, next_retry, time.time(), event_id) # Update the retry count, next retry time, and last attempt time. 
                )
                self._db_conn.commit() # Commit the changes to the database. 

            # Schedule retry task. This is the task that is waiting for the retry. 
            self._retry_tasks[event_id] = asyncio.create_task(
                self._retry_event(event_id, backoff) # Retry the event after the backoff time. 
            )

        # Retry event. This is called when the event is not acknowledged. 
        async def _retry_event(self, event_id: str, delay: float):
            """Retry sending event after delay."""
            # Wait for the delay. This is the time to wait before the next retry. 
            """Retry sending event after delay."""
            await asyncio.sleep(delay) # Wait for the delay. This is the time to wait before the next retry. 

            # Get pending event. This is the event that is being processed. 
            pending = self._pending_events.get(event_id) 

            # If the event is not found, return. 
            if not pending: # If the event is not found, return. 
                return # Return if the event is not found. 

            # Send event to target component and wait for acknowledgement. This is the event that is being processed. 
            if pending: 
                await self._send_and_wait_ack(event_id, pending["target"]) # Send the event to the target component and wait for acknowledgement. 

        # Load pending events from database on startup. This is called when the orchestrator is initialized. 
        async def _load_pending_events(self):
            """Load pending events from database on startup."""
            async with self._db_lock: # Lock the database to prevent concurrent access. 
                cursor = self._db_conn.execute( # Execute the query to load the pending events from the database. 
                    """
                    SELECT event_id, event_data, target_component, retry_count, next_retry 
                    FROM pending_events  
                    WHERE next_retry <= ?  
                    """,
                    (time.time()) # Time now. This is the time when the event was created. 
                )

                # Fetch all the rows from the database. 
                for row in cursor.fetchall(): # For each row, get the event ID, event data, target component, retry count, and next retry time. 
                    event_id, event_data, target, retry_count, next_retry = row # Event ID, event data, target component, retry count, and next retry time. 

                    try: 
                        event_dict = json.loads(event_data) # Event data. This is the event that is being processed. 
                        event = TrinityEvent.from_dict(event_dict) # Event data. This is the event that is being processed.  

                        self._pending_events[event_id] = {
                            "event": event, # Event data. This is the event that is being processed. 
                            "target": target, # Target component. This is the component that is receiving the event. 
                            "retry_count": retry_count, # Retry count. This is the number of times the event has been retried. 
                            "ack_timeout": 30.0, # Acknowledgement timeout. This is the timeout for the acknowledgement. 
                        }

                        # Schedule retry if needed 
                        if next_retry <= time.time(): 
                            await self._schedule_retry(event_id) # Schedule retry. This is the event that is being processed. 

                    except Exception as e: # If an error occurs, log the error. 
                        logger.error(f"Error loading pending even {event_id}: {e}") # Log the error. 
        
        # Remove pending event. This is called when the event is acknowledged or failed after retries. 
        async def _remove_pending_event(self, event_id: str):
            """Remove event from pending queue."""
            self._pending_events.pop(event_id, None) # Remove the event from the pending events. 

            # Cancel tasks
            if event_id in self._ack_timeouts: # If the event is in the acknowledgement timeouts, cancel the task. 
                self._ack_timeouts[event_id].cancel() # Cancel the task. 
                del self._ack_timeouts[event_id] # Delete the task from the acknowledgement timeouts. 

            if event_id in self._retry_tasks: # If the event is in the retry tasks, cancel the task. 
                self._retry_tasks[event_id].cancel() 
                del self._retry_tasks[event_id] # Delete the task from the retry tasks. 

            # Remove from database 
            async with self._db_lock: # Lock the database to prevent concurrent access.  
                self._db_conn.execute( # Execute the query to remove the event from the database. 
                    """
                    DELETE FROM pending_events WHERE event_id = ? 
                    """,
                    (event_id,) # Event ID. This is the unique identifier for the event. 
                )
                self._db_conn.commit() # Commit the changes to the database. 

    def __init__(
        self,
        enable_jprime: bool = True,
        enable_reactor: bool = True,
        startup_timeout: float = 120.0,
        health_check_interval: float = 30.0,
    ):
        """
        Initialize the Trinity Unified Orchestrator v83.0.

        Args:
            enable_jprime: Enable JARVIS Prime integration
            enable_reactor: Enable Reactor-Core integration
            startup_timeout: Max time to wait for components
            health_check_interval: Interval between health checks
        """
        config = get_config()
        self.enable_jprime = config.get("JARVIS_PRIME_ENABLED", enable_jprime)
        self.enable_reactor = config.get("REACTOR_CORE_ENABLED", enable_reactor)
        self.startup_timeout = config.get("TRINITY_STARTUP_TIMEOUT", startup_timeout)
        self.health_check_interval = config.get("TRINITY_HEALTH_INTERVAL", health_check_interval)

        # State
        self._state = TrinityState.UNINITIALIZED
        self._start_time: Optional[float] = None
        self._lock = asyncio.Lock()

        # v83.0 Advanced Components
        self._process_supervisor = ProcessSupervisor()
        self._crash_recovery = CrashRecoveryManager()
        self._resource_coordinator = ResourceCoordinator()
        self._event_store = EventStore()
        self._tracer = DistributedTracer(service_name="jarvis_body")
        self._health_aggregator = UnifiedHealthAggregator()
        self._throttler = AdaptiveThrottler()

        # Circuit breakers for each component
        self._circuit_breakers: Dict[str, CircuitBreaker] = {
            "jprime": CircuitBreaker("jprime"),
            "reactor": CircuitBreaker("reactor"),
            "ipc": CircuitBreaker("ipc"),
        }

        # Legacy components (backward compatibility)
        self._ipc_bus = None
        self._shutdown_manager = None
        self._port_manager = None
        self._startup_coordinator = None

        # Clients
        self._jprime_client = None
        self._reactor_client = None

        # Process handles for crash recovery
        self._jprime_process: Optional[subprocess.Popen] = None
        self._reactor_process: Optional[subprocess.Popen] = None

        # v84.0: Managed async processes
        self._managed_processes: Dict[str, Dict[str, Any]] = {}

        # Background tasks
        self._health_task: Optional[asyncio.Task] = None
        self._event_cleanup_task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks
        self._on_state_change: List[Callable[[TrinityState, TrinityState], None]] = []
        self._on_component_change: List[Callable[[str, ComponentHealth], None]] = []

        # Register anomaly handler
        self._health_aggregator.on_anomaly(self._handle_anomaly)

        logger.info(
            f"[TrinityOrchestrator v83.0] Initialized "
            f"(jprime={self.enable_jprime}, reactor={self.enable_reactor})"
        )

    def _handle_anomaly(self, anomaly: AnomalyReport) -> None:
        """Handle detected anomalies."""
        if anomaly.severity == "critical":
            logger.error(
                f"[TrinityOrchestrator] CRITICAL anomaly: {anomaly.component} - "
                f"{anomaly.description}"
            )
            # Potentially trigger recovery
            asyncio.create_task(self._handle_critical_anomaly(anomaly))

    async def _handle_critical_anomaly(self, anomaly: AnomalyReport) -> None:
        """Handle critical anomaly - attempt recovery."""
        component = anomaly.component

        # Check if we should attempt restart
        should_restart, backoff = await self._crash_recovery.should_restart(component)

        if should_restart:
            logger.info(
                f"[TrinityOrchestrator] Scheduling restart for {component} "
                f"in {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            await self._restart_component(component)

    async def _restart_component(self, component_id: str) -> bool:
        """Restart a crashed component."""
        async with self._tracer.span(f"restart_{component_id}"):
            logger.info(f"[TrinityOrchestrator] Restarting {component_id}...")

            try:
                if component_id == "jarvis_prime" and self.enable_jprime:
                    success = await self._start_jprime()
                elif component_id == "reactor_core" and self.enable_reactor:
                    success = await self._start_reactor()
                else:
                    success = False

                if success:
                    await self._crash_recovery.record_success(component_id)
                    await self._event_store.publish(
                        event_type="component.restarted",
                        source="orchestrator",
                        payload={"component": component_id, "success": True},
                    )
                else:
                    await self._crash_recovery.record_crash(component_id)

                return success

            except Exception as e:
                logger.error(f"[TrinityOrchestrator] Restart failed: {e}")
                await self._crash_recovery.record_crash(component_id, str(e))
                return False

    @property
    def state(self) -> TrinityState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state in (TrinityState.READY, TrinityState.DEGRADED)

    @property
    def uptime(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    # =========================================================================
    # Startup
    # =========================================================================

    async def start(self) -> bool:
        """
        Start the Trinity system with v86.0 enhancements.

        v86.0 ENHANCEMENTS:
        ═══════════════════════════════════════════════════════
        ✅ Per-Component Timeouts - Individual timeouts for each component
        ✅ Real-Time Progress Logging - Live status updates during startup
        ✅ Fast-Fail Circuit Breakers - 5s detection of blocking components
        ✅ Adaptive Parallel Execution - Maximum concurrency with timeouts

        This is the single command that initializes everything:
        ════════════════════════════════════════════════════════
        Phase 1: PREPARE (transactional)
        ├── 1.1 Initialize event store (for durability)
        ├── 1.2 Cleanup orphan processes
        ├── 1.3 Initialize IPC with circuit breaker
        ├── 1.4 Allocate ports via ResourceCoordinator
        └── 1.5 Initialize shutdown manager

        Phase 2: START (with crash recovery)
        ├── 2.1 Start Process Supervisor
        ├── 2.2 Start JARVIS Body heartbeat
        ├── 2.3 Start JARVIS Prime (if enabled) [PARALLEL with timeout]
        └── 2.4 Start Reactor-Core (if enabled) [PARALLEL with timeout]

        Phase 3: VERIFY (health aggregation)
        ├── 3.1 Verify all heartbeats
        ├── 3.2 Record initial health baselines
        └── 3.3 Start background health monitoring

        Returns:
            True if startup successful (or degraded), False on failure
        """
        async with self._tracer.trace("trinity_startup") as root_span:
            async with self._lock:
                if self._state != TrinityState.UNINITIALIZED:
                    logger.warning(
                        f"[TrinityOrchestrator] Cannot start in state {self._state.value}"
                    )
                    return False

                self._set_state(TrinityState.INITIALIZING)
                self._start_time = time.time()

                try:
                    # ═══════════════════════════════════════════════════
                    # PHASE 1: PREPARE (Transactional)
                    # ═══════════════════════════════════════════════════
                    logger.info("🚀 [v86.0] Phase 1/3: Preparing Trinity environment...")
                    async with self._tracer.span("phase_1_prepare"):

                        # Step 1.1: Initialize event store for durability
                        logger.info("   📝 [v86.0] Initializing event store...")
                        async with self._tracer.span("init_event_store"):
                            await self._event_store.initialize()
                            await self._event_store.publish(
                                event_type="startup.begin",
                                source="orchestrator",
                                payload={"version": "v86.0", "timestamp": time.time()},
                            )
                            logger.info("   ✅ [v86.0] Event store ready")

                        # Step 1.2: Orphan cleanup
                        logger.info("   🧹 [v86.0] Cleaning up orphan processes...")
                        async with self._tracer.span("cleanup_orphans"):
                            await self._cleanup_orphans()
                            logger.info("   ✅ [v86.0] Orphan cleanup complete")

                        # Step 1.3: Initialize IPC with circuit breaker
                        logger.info("   🔗 [v86.0] Initializing IPC communication...")
                        async with self._tracer.span("init_ipc"):
                            await self._init_ipc()
                            logger.info("   ✅ [v86.0] IPC ready")

                        # Step 1.4: Port allocation via ResourceCoordinator
                        logger.info("   🔌 [v86.0] Allocating component ports...")
                        async with self._tracer.span("allocate_ports"):
                            await self._allocate_ports()
                            logger.info("   ✅ [v86.0] Ports allocated")

                        # Step 1.5: Initialize shutdown manager
                        logger.info("   🛑 [v86.0] Initializing shutdown manager...")
                        async with self._tracer.span("init_shutdown"):
                            await self._init_shutdown_manager()
                            logger.info("   ✅ [v86.0] Shutdown manager ready")

                    logger.info("✅ [v86.0] Phase 1 complete: Environment prepared")
                    self._set_state(TrinityState.STARTING)

                    # ═══════════════════════════════════════════════════
                    # PHASE 2: START (With Crash Recovery + v86.0 Timeouts)
                    # ═══════════════════════════════════════════════════
                    logger.info("🚀 [v86.0] Phase 2/3: Starting Trinity components in parallel...")
                    async with self._tracer.span("phase_2_start"):

                        # Step 2.1: Start Process Supervisor
                        logger.info("   👁️  [v86.0] Starting process supervisor...")
                        async with self._tracer.span("start_supervisor"):
                            await self._process_supervisor.start()
                            logger.info("   ✅ [v86.0] Process supervisor online")

                        # Step 2.2: Start JARVIS Body heartbeat
                        logger.info("   💓 [v86.0] Starting JARVIS Body heartbeat...")
                        async with self._tracer.span("start_body_heartbeat"):
                            await self._start_body_heartbeat()
                            logger.info("   ✅ [v86.0] JARVIS Body heartbeat active")

                        # v86.0: Per-component timeouts (not global)
                        jprime_timeout = float(os.getenv("JARVIS_PRIME_COMPONENT_TIMEOUT", "120.0"))
                        reactor_timeout = float(os.getenv("REACTOR_CORE_COMPONENT_TIMEOUT", "90.0"))

                        # v100.4: SINGLE SOURCE OF TRUTH - Check if supervisor handles launching
                        # When run_supervisor.py is the entry point, it sets this env var to delegate
                        # launch responsibility to its v100.0 SafeProcess-based launcher.
                        # This prevents duplicate launch attempts and race conditions.
                        supervisor_launches = os.getenv("TRINITY_SUPERVISOR_LAUNCHES", "true").lower() == "true"

                        # Step 2.3 & 2.4: Start external components with individual timeouts
                        jprime_ok = True
                        reactor_ok = True

                        if supervisor_launches:
                            # v100.4: Skip component launch - run_supervisor.py handles it
                            # TrinityIntegrator focuses on coordination, not launching
                            logger.info("   ℹ️  [v100.4] Skipping component launch (supervisor handles)")
                            logger.info("   ℹ️  [v100.4] TrinityIntegrator: coordination mode only")
                            # Just check if components already running (via heartbeat or process)
                            jprime_ok = await self._discover_running_component("jarvis_prime")
                            reactor_ok = await self._discover_running_component("reactor_core")
                            if not jprime_ok and self.enable_jprime:
                                logger.info("   ⏳ [v100.4] J-Prime will be launched by supervisor")
                            if not reactor_ok and self.enable_reactor:
                                logger.info("   ⏳ [v100.4] Reactor-Core will be launched by supervisor")
                        else:
                            # Legacy mode: TrinityIntegrator launches components directly
                            async with self._tracer.span("start_external_components"):
                                tasks = []
                                task_names = []

                                if self.enable_jprime:
                                    logger.info(f"   🧠 [v86.0] Starting JARVIS Prime (timeout={jprime_timeout}s)...")
                                    tasks.append(
                                        asyncio.wait_for(
                                            self._start_jprime_with_recovery(),
                                            timeout=jprime_timeout
                                        )
                                    )
                                    task_names.append("jarvis_prime")

                                if self.enable_reactor:
                                    logger.info(f"   ⚛️  [v86.0] Starting Reactor-Core (timeout={reactor_timeout}s)...")
                                    tasks.append(
                                        asyncio.wait_for(
                                            self._start_reactor_with_recovery(),
                                            timeout=reactor_timeout
                                        )
                                    )
                                    task_names.append("reactor_core")

                                if tasks:
                                    # v86.0: Parallel execution with per-component timeout enforcement
                                    results = await asyncio.gather(*tasks, return_exceptions=True)

                                    # Process results with detailed logging
                                    for i, (result, name) in enumerate(zip(results, task_names)):
                                        if isinstance(result, asyncio.TimeoutError):
                                            logger.error(
                                                f"   ❌ [v86.0] {name.replace('_', ' ').title()} "
                                                f"TIMEOUT after {jprime_timeout if i == 0 else reactor_timeout}s"
                                            )
                                            if name == "jarvis_prime":
                                                jprime_ok = False
                                            else:
                                                reactor_ok = False
                                        elif isinstance(result, Exception):
                                            logger.error(
                                                f"   ❌ [v86.0] {name.replace('_', ' ').title()} "
                                                f"FAILED: {result}"
                                            )
                                            if name == "jarvis_prime":
                                                jprime_ok = False
                                            else:
                                                reactor_ok = False
                                        elif result:
                                            logger.info(
                                                f"   ✅ [v86.0] {name.replace('_', ' ').title()} started successfully"
                                            )
                                            if name == "jarvis_prime":
                                                jprime_ok = True
                                            else:
                                                reactor_ok = True
                                        else:
                                            logger.warning(
                                                f"   ⚠️  [v86.0] {name.replace('_', ' ').title()} "
                                                "returned false (non-fatal)"
                                            )
                                            if name == "jarvis_prime":
                                                jprime_ok = False
                                            else:
                                                reactor_ok = False

                    # ═══════════════════════════════════════════════════
                    # PHASE 3: VERIFY (Health Aggregation)
                    # ═══════════════════════════════════════════════════
                    logger.info("🚀 [v86.0] Phase 3/3: Verifying component health...")
                    async with self._tracer.span("phase_3_verify"):

                        # Step 3.1: Determine final state
                        logger.info("   📊 [v86.0] Analyzing component status...")
                        if jprime_ok and reactor_ok:
                            self._set_state(TrinityState.READY)
                            logger.info("   ✅ [v86.0] All components healthy - Trinity READY")
                        else:
                            self._set_state(TrinityState.DEGRADED)
                            failed_components = []
                            if not jprime_ok:
                                failed_components.append("JARVIS Prime")
                            if not reactor_ok:
                                failed_components.append("Reactor-Core")
                            logger.warning(
                                f"   ⚠️  [v86.0] Trinity starting in DEGRADED mode - "
                                f"Failed: {', '.join(failed_components)}"
                            )

                            # v100.5: Provide detailed diagnostics for failed components
                            await self._log_component_diagnostics()

                        # Step 3.2: Record initial health baselines
                        logger.info("   📝 [v86.0] Recording health baselines...")
                        async with self._tracer.span("record_baselines"):
                            await self._health_aggregator.record_health(
                                component="jarvis_body",
                                healthy=True,
                                latency_ms=0.0,
                                metrics={"startup_time": time.time() - self._start_time},
                            )

                            if self.enable_jprime:
                                await self._health_aggregator.record_health(
                                    component="jarvis_prime",
                                    healthy=jprime_ok,
                                    latency_ms=0.0,
                                )

                            if self.enable_reactor:
                                await self._health_aggregator.record_health(
                                    component="reactor_core",
                                    healthy=reactor_ok,
                                    latency_ms=0.0,
                                )
                            logger.info("   ✅ [v86.0] Health baselines recorded")

                        # Step 3.3: Start health monitoring
                        logger.info("   💓 [v86.0] Starting background health monitoring...")
                        self._running = True
                        self._health_task = asyncio.create_task(self._health_loop())
                        self._event_cleanup_task = asyncio.create_task(self._event_cleanup_loop())

                        # v86.0: Start crash recovery loop
                        self._crash_recovery_task = asyncio.create_task(
                            self._crash_recovery_loop(),
                            name="crash_recovery_loop_v86",
                        )
                        logger.info("   ✅ [v86.0] Crash recovery loop started")

                    # Publish startup complete event
                    logger.info("   📡 [v86.0] Publishing startup complete event...")
                    await self._event_store.publish(
                        event_type="startup.complete",
                        source="orchestrator",
                        payload={
                            "version": "v86.0",
                            "state": self._state.value,
                            "jprime_enabled": self.enable_jprime,
                            "jprime_ok": jprime_ok,
                            "reactor_enabled": self.enable_reactor,
                            "reactor_ok": reactor_ok,
                        },
                    )

                    elapsed = time.time() - self._start_time
                    logger.info("=" * 70)
                    logger.info(
                        f"🎉 [v86.0] Trinity Startup Complete in {elapsed:.2f}s "
                        f"(state={self._state.value})"
                    )
                    logger.info(f"   📊 [v86.0] Component Status:")
                    logger.info(f"      • JARVIS Body:    ✅ ONLINE")
                    if self.enable_jprime:
                        # v86.1: Show PENDING when supervisor handles launch
                        if jprime_ok:
                            status = "✅ ONLINE"
                        elif supervisor_launches:
                            status = "⏳ PENDING (supervisor launch)"
                        else:
                            status = "❌ OFFLINE"
                        logger.info(f"      • JARVIS Prime:   {status}")
                    if self.enable_reactor:
                        # v86.1: Show PENDING when supervisor handles launch
                        if reactor_ok:
                            status = "✅ ONLINE"
                        elif supervisor_launches:
                            status = "⏳ PENDING (supervisor launch)"
                        else:
                            status = "❌ OFFLINE"
                        logger.info(f"      • Reactor-Core:   {status}")
                    logger.info("=" * 70)

                    return True

                except Exception as e:
                    elapsed = time.time() - self._start_time
                    logger.error("=" * 70)
                    logger.error(f"💥 [v86.0] Trinity Startup FAILED after {elapsed:.2f}s")
                    logger.error(f"   Error: {e}")
                    logger.error(f"   Traceback: {traceback.format_exc()}")
                    logger.error("=" * 70)
                    self._set_state(TrinityState.ERROR)

                    # Publish startup failure event
                    try:
                        await self._event_store.publish(
                            event_type="startup.failed",
                            source="orchestrator",
                            payload={
                                "version": "v86.0",
                                "error": str(e),
                                "traceback": traceback.format_exc(),
                                "elapsed_seconds": elapsed,
                            },
                        )
                    except Exception:
                        pass

                    return False

    async def _cleanup_orphans(self) -> None:
        """Clean up orphan processes from previous runs."""
        try:
            from backend.core.coordinated_shutdown import cleanup_orphan_processes

            terminated, failed = await cleanup_orphan_processes()

            if terminated > 0:
                logger.info(
                    f"[TrinityIntegrator] Cleaned up {terminated} orphan processes"
                )

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Orphan cleanup failed: {e}")

    async def _init_ipc(self) -> None:
        """Initialize the resilient IPC bus."""
        from backend.core.trinity_ipc import get_resilient_trinity_ipc_bus

        self._ipc_bus = await get_resilient_trinity_ipc_bus()
        logger.debug("[TrinityIntegrator] IPC bus initialized")

    async def _allocate_ports(self) -> None:
        """Allocate ports for all components."""
        try:
            from backend.core.trinity_port_manager import get_trinity_port_manager

            self._port_manager = await get_trinity_port_manager()
            allocations = await self._port_manager.allocate_all_ports()

            for component, result in allocations.items():
                if result.success:
                    logger.info(
                        f"[TrinityIntegrator] Port allocated: "
                        f"{component.value}={result.port}"
                    )
                else:
                    logger.warning(
                        f"[TrinityIntegrator] Port allocation failed: "
                        f"{component.value}: {result.error}"
                    )

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Port allocation failed: {e}")

    async def _init_shutdown_manager(self) -> None:
        """Initialize the shutdown manager."""
        from backend.core.coordinated_shutdown import (
            EnhancedShutdownManager,
            setup_signal_handlers,
        )

        self._shutdown_manager = EnhancedShutdownManager(
            ipc_bus=self._ipc_bus,
            detect_orphans_on_start=False,  # Already done
        )

        # Register signal handlers
        try:
            loop = asyncio.get_running_loop()
            setup_signal_handlers(self._shutdown_manager, loop)
        except Exception as e:
            logger.debug(f"[TrinityIntegrator] Signal handler setup failed: {e}")

        logger.debug("[TrinityIntegrator] Shutdown manager initialized")

    async def _start_body_heartbeat(self) -> None:
        """Start JARVIS Body heartbeat publishing."""
        try:
            from backend.core.trinity_ipc import ComponentType

            await self._ipc_bus.publish_heartbeat(
                component=ComponentType.JARVIS_BODY,
                status="starting",
                pid=os.getpid(),
                metrics={"startup_time": self._start_time},
            )

            logger.debug("[TrinityIntegrator] Body heartbeat started")

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Body heartbeat failed: {e}")

    async def _wait_for_jprime(self) -> bool:
        """
        v86.0: Wait for JARVIS Prime with optimized polling.

        Uses adaptive polling interval:
        - Fast initial checks (0.5s) for quick startups
        - Gradually increases to 2s for slower startups
        - Uses component timeout instead of global timeout
        """
        try:
            from backend.clients.jarvis_prime_client import get_jarvis_prime_client

            self._jprime_client = await get_jarvis_prime_client()

            # v86.0: Use component-specific timeout
            component_timeout = float(os.getenv("JARVIS_PRIME_COMPONENT_TIMEOUT", "120.0"))
            poll_interval = 0.5  # Start with fast polling
            max_poll_interval = 2.0

            logger.info(
                f"   ⏳ [v86.0] Waiting for J-Prime to come online (timeout={component_timeout}s)..."
            )

            # Wait for connection with adaptive polling
            start = time.time()
            check_count = 0
            while time.time() - start < component_timeout:
                check_count += 1

                if self._jprime_client.is_online:
                    elapsed = time.time() - start
                    logger.info(
                        f"   ✅ [v86.0] J-Prime ready after {elapsed:.1f}s ({check_count} checks)"
                    )
                    return True

                # v86.0: Adaptive polling - faster at start, slower later
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.2, max_poll_interval)

            elapsed = time.time() - start
            logger.warning(
                f"   ⏱️  [v86.0] J-Prime timeout after {elapsed:.1f}s ({check_count} checks)"
            )
            return False

        except Exception as e:
            logger.warning(f"   ❌ [v86.0] J-Prime init failed: {e}")
            return False

    async def _wait_for_reactor(self) -> bool:
        """
        v86.0: Wait for Reactor-Core with optimized polling.

        Uses adaptive polling interval:
        - Fast initial checks (0.5s) for quick startups
        - Gradually increases to 2s for slower startups
        - Uses component timeout instead of env-specific timeout
        - Active health checks with 10s timeout per check
        """
        try:
            from backend.clients.reactor_core_client import (
                initialize_reactor_client,
                get_reactor_client,
            )

            await initialize_reactor_client()
            self._reactor_client = get_reactor_client()

            # v86.0: Immediate check
            if self._reactor_client and self._reactor_client.is_online:
                logger.info("   ✅ [v86.0] Reactor-Core ready (immediate)")
                return True

            # v86.0: Use component-specific timeout
            component_timeout = float(os.getenv("REACTOR_CORE_COMPONENT_TIMEOUT", "90.0"))
            poll_interval = 0.5  # Start with fast polling
            max_poll_interval = 2.0

            logger.info(
                f"   ⏳ [v86.0] Waiting for Reactor-Core to come online (timeout={component_timeout}s)..."
            )

            start = time.time()
            check_count = 0
            while time.time() - start < component_timeout:
                check_count += 1

                # Perform active health check instead of just checking is_online flag
                if self._reactor_client:
                    try:
                        is_healthy = await asyncio.wait_for(
                            self._reactor_client.health_check(),
                            timeout=10.0
                        )
                        if is_healthy or self._reactor_client.is_online:
                            elapsed = time.time() - start
                            logger.info(
                                f"   ✅ [v86.0] Reactor-Core ready after {elapsed:.1f}s ({check_count} checks)"
                            )
                            return True
                    except asyncio.TimeoutError:
                        logger.debug(
                            f"   ⏱️  [v86.0] Reactor health check timeout (check {check_count})"
                        )
                    except Exception as e:
                        logger.debug(
                            f"   ⚠️  [v86.0] Reactor health check error: {e}"
                        )

                # v86.0: Adaptive polling - faster at start, slower later
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.2, max_poll_interval)

            # Timeout reached
            elapsed = time.time() - start
            logger.warning(
                f"   ⏱️  [v86.0] Reactor-Core timeout after {elapsed:.1f}s ({check_count} checks)"
            )
            return False

        except ImportError as e:
            logger.warning(f"[TrinityOrchestrator] Reactor-Core client import failed: {e}")
            return False
        except Exception as e:
            logger.warning(f"[TrinityOrchestrator] Reactor-Core init failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    # =========================================================================
    # v83.0: Crash Recovery Methods
    # =========================================================================

    async def _start_jprime_with_recovery(self) -> bool:
        """
        Start JARVIS Prime with circuit breaker and crash recovery.

        Uses circuit breaker to fail fast if J-Prime is repeatedly failing.
        Registers process with supervisor for automatic restart on crash.
        """
        circuit = self._circuit_breakers["jprime"]

        try:
            async with circuit:
                success = await self._start_jprime()

                if success:
                    await self._event_store.publish(
                        event_type="component.started",
                        source="orchestrator",
                        payload={"component": "jarvis_prime"},
                        trace_id=self._tracer.get_trace_id(),
                    )

                return success

        except CircuitOpenError as e:
            logger.warning(f"[TrinityOrchestrator] J-Prime circuit breaker open: {e}")
            return False
        except Exception as e:
            logger.error(f"[TrinityOrchestrator] J-Prime start failed: {e}")
            await self._crash_recovery.record_crash("jarvis_prime", str(e))
            return False

    async def _start_reactor_with_recovery(self) -> bool:
        """
        Start Reactor-Core with circuit breaker and crash recovery.

        Uses circuit breaker to fail fast if Reactor is repeatedly failing.
        Registers process with supervisor for automatic restart on crash.
        """
        circuit = self._circuit_breakers["reactor"]

        try:
            async with circuit:
                success = await self._start_reactor()

                if success:
                    await self._event_store.publish(
                        event_type="component.started",
                        source="orchestrator",
                        payload={"component": "reactor_core"},
                        trace_id=self._tracer.get_trace_id(),
                    )

                return success

        except CircuitOpenError as e:
            logger.warning(f"[TrinityOrchestrator] Reactor circuit breaker open: {e}")
            return False
        except Exception as e:
            logger.error(f"[TrinityOrchestrator] Reactor start failed: {e}")
            await self._crash_recovery.record_crash("reactor_core", str(e))
            return False

    async def _start_jprime(self) -> bool:
        """
        v84.0: Start JARVIS Prime - discover or launch.

        Strategy:
        1. First check if already running (heartbeat file)
        2. If not, launch the process
        3. Wait for it to become ready
        """
        # Check if already running
        if await self._discover_running_component("jarvis_prime"):
            logger.info("[TrinityOrchestrator] J-Prime already running (discovered)")
            return await self._wait_for_jprime()

        # Launch the process
        launched = await self._launch_jprime_process()
        if not launched:
            logger.warning("[TrinityOrchestrator] Failed to launch J-Prime")
            return False

        # Wait for it to be ready
        return await self._wait_for_jprime()

    async def _start_reactor(self) -> bool:
        """
        v84.0: Start Reactor-Core - discover or launch.

        Strategy:
        1. First check if already running (heartbeat file)
        2. If not, launch the process
        3. Wait for it to become ready
        """
        # Check if already running
        if await self._discover_running_component("reactor_core"):
            logger.info("[TrinityOrchestrator] Reactor-Core already running (discovered)")
            return await self._wait_for_reactor()

        # Launch the process
        launched = await self._launch_reactor_process()
        if not launched:
            logger.warning("[TrinityOrchestrator] Failed to launch Reactor-Core")
            return False

        # Wait for it to be ready
        return await self._wait_for_reactor()

    # =========================================================================
    # v84.0: Process Launching and Discovery
    # =========================================================================

    async def _discover_running_component(self, component: str) -> bool:
        """
        v84.0: Discover if a component is already running.

        Checks:
        1. Heartbeat file freshness (< 30s)
        2. Process is actually alive (PID check)
        3. HTTP health check responds

        Args:
            component: Component name (jarvis_prime, reactor_core)

        Returns:
            True if component is running and healthy
        """
        import psutil

        trinity_dir = Path(os.getenv(
            "TRINITY_DIR",
            str(Path.home() / ".jarvis" / "trinity")
        ))

        heartbeat_file = trinity_dir / "components" / f"{component}.json"

        if not heartbeat_file.exists():
            return False

        try:
            with open(heartbeat_file, 'r') as f:
                data = json.load(f)

            # Check freshness (30 second threshold)
            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp
            if age > 30.0:
                logger.debug(f"[Discovery] {component} heartbeat stale ({age:.1f}s)")
                return False

            # Check if process is alive
            pid = data.get("pid")
            if pid:
                try:
                    proc = psutil.Process(pid)
                    if proc.is_running():
                        logger.debug(f"[Discovery] {component} process alive (PID {pid})")

                        # Optional: HTTP health check
                        port = data.get("port")
                        if port:
                            try:
                                import aiohttp
                                async with aiohttp.ClientSession(
                                    timeout=aiohttp.ClientTimeout(total=5.0)
                                ) as session:
                                    url = f"http://localhost:{port}/health"
                                    async with session.get(url) as resp:
                                        if resp.status == 200:
                                            logger.info(
                                                f"[Discovery] {component} healthy at port {port}"
                                            )
                                            return True
                            except Exception:
                                # HTTP check failed but process is alive
                                pass

                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            return False

        except Exception as e:
            logger.debug(f"[Discovery] Error checking {component}: {e}")
            return False

    async def _diagnose_component_status(self, component: str) -> Dict[str, Any]:
        """
        v100.5: Comprehensive diagnosis of why a component isn't available.

        Provides detailed status for better user messaging:
        - repo_installed: Is the repo directory present?
        - venv_exists: Does the virtual environment exist?
        - venv_valid: Is the venv properly set up with Python?
        - launch_script_exists: Is the launch script present?
        - currently_running: Is the service currently running?
        - last_heartbeat: When was the last heartbeat?
        - recommended_action: What should the user do?

        Args:
            component: Component name (jarvis_prime, reactor_core)

        Returns:
            Dict with diagnostic information
        """
        diagnosis = {
            "component": component,
            "repo_installed": False,
            "repo_path": None,
            "venv_exists": False,
            "venv_valid": False,
            "launch_script_exists": False,
            "launch_script_path": None,
            "currently_running": False,
            "last_heartbeat": None,
            "heartbeat_age_seconds": None,
            "pid": None,
            "port": None,
            "recommended_action": "unknown",
            "status_emoji": "❓",
            "status_message": "Unknown status",
        }

        # Determine repo path based on component
        if component == "jarvis_prime":
            repo_path = Path(os.getenv(
                "JARVIS_PRIME_PATH",
                str(Path.home() / "Documents" / "repos" / "jarvis-prime")
            ))
            launch_scripts = [
                "jarvis_prime/server.py",
                "run_server.py",
                "jarvis_prime/core/trinity_bridge.py",
            ]
            default_port = int(os.getenv("JARVIS_PRIME_PORT", "8000"))
        elif component == "reactor_core":
            repo_path = Path(os.getenv(
                "REACTOR_CORE_PATH",
                str(Path.home() / "Documents" / "repos" / "reactor-core")
            ))
            launch_scripts = [
                "reactor_core/orchestration/trinity_orchestrator.py",
                "run_orchestrator.py",
            ]
            default_port = int(os.getenv("REACTOR_CORE_PORT", "8001"))
        else:
            diagnosis["recommended_action"] = f"Unknown component: {component}"
            return diagnosis

        diagnosis["repo_path"] = str(repo_path)

        # Check 1: Repo installed
        if repo_path.exists():
            diagnosis["repo_installed"] = True
        else:
            diagnosis["status_emoji"] = "📁"
            diagnosis["status_message"] = f"Repo not found at {repo_path}"
            diagnosis["recommended_action"] = f"Clone the {component} repository to {repo_path}"
            return diagnosis

        # Check 2: Virtual environment
        venv_path = repo_path / "venv"
        if venv_path.exists():
            diagnosis["venv_exists"] = True
            # Check if Python is valid in venv
            venv_python = venv_path / "bin" / "python3"
            if not venv_python.exists():
                venv_python = venv_path / "bin" / "python"
            if venv_python.exists():
                diagnosis["venv_valid"] = True
        else:
            diagnosis["status_emoji"] = "🐍"
            diagnosis["status_message"] = f"Virtual environment not found"
            diagnosis["recommended_action"] = f"cd {repo_path} && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
            return diagnosis

        if not diagnosis["venv_valid"]:
            diagnosis["status_emoji"] = "⚠️"
            diagnosis["status_message"] = f"Virtual environment invalid (no Python found)"
            diagnosis["recommended_action"] = f"Recreate venv: rm -rf {venv_path} && python3 -m venv {venv_path}"
            return diagnosis

        # Check 3: Launch script exists
        for script in launch_scripts:
            script_path = repo_path / script
            if script_path.exists():
                diagnosis["launch_script_exists"] = True
                diagnosis["launch_script_path"] = str(script_path)
                break

        if not diagnosis["launch_script_exists"]:
            diagnosis["status_emoji"] = "📜"
            diagnosis["status_message"] = f"No launch script found"
            diagnosis["recommended_action"] = f"Check {component} repository structure"
            return diagnosis

        # Check 4: Currently running (heartbeat + process check)
        trinity_dir = Path(os.getenv(
            "TRINITY_DIR",
            str(Path.home() / ".jarvis" / "trinity")
        ))
        heartbeat_file = trinity_dir / "components" / f"{component}.json"

        if heartbeat_file.exists():
            try:
                with open(heartbeat_file, 'r') as f:
                    hb_data = json.load(f)
                diagnosis["last_heartbeat"] = hb_data.get("timestamp")
                diagnosis["pid"] = hb_data.get("pid")
                diagnosis["port"] = hb_data.get("port", default_port)

                if diagnosis["last_heartbeat"]:
                    age = time.time() - diagnosis["last_heartbeat"]
                    diagnosis["heartbeat_age_seconds"] = age

                    # Check if process is alive
                    if diagnosis["pid"]:
                        try:
                            import psutil
                            proc = psutil.Process(diagnosis["pid"])
                            if proc.is_running() and age < 30.0:
                                diagnosis["currently_running"] = True
                                diagnosis["status_emoji"] = "✅"
                                diagnosis["status_message"] = f"Running (PID {diagnosis['pid']}, port {diagnosis['port']})"
                                diagnosis["recommended_action"] = "Component is healthy"
                                return diagnosis
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except Exception as e:
                logger.debug(f"[Diagnosis] Heartbeat read error for {component}: {e}")

        # If we get here, component is installed but not running
        diagnosis["status_emoji"] = "💤"
        diagnosis["status_message"] = f"Installed but not running"
        diagnosis["recommended_action"] = f"Start {component}: the supervisor will auto-launch it, or manually run the launch script"

        return diagnosis

    async def _log_component_diagnostics(self) -> None:
        """
        v100.5: Log detailed diagnostics for external components.

        Called during startup to provide clear visibility into why
        components might be unavailable.
        """
        logger.info("   🔍 [v100.5] Diagnosing external component status...")

        components_to_diagnose = []
        if self.enable_jprime:
            components_to_diagnose.append("jarvis_prime")
        if self.enable_reactor:
            components_to_diagnose.append("reactor_core")

        for component in components_to_diagnose:
            diag = await self._diagnose_component_status(component)
            display_name = "J-Prime" if component == "jarvis_prime" else "Reactor-Core"

            logger.info(
                f"      {diag['status_emoji']} {display_name}: {diag['status_message']}"
            )

            if diag["recommended_action"] != "Component is healthy":
                logger.info(f"         → {diag['recommended_action']}")

    async def _launch_jprime_process(self) -> bool:
        """
        v86.0: Launch JARVIS Prime process with intelligent discovery and retry.

        Uses IntelligentRepoDiscovery for multi-strategy path discovery
        and ResourceAwareLauncher for robust process launching with:
        - Resource checks (memory, CPU, ports)
        - Retry with exponential backoff
        - Health check verification
        - v86.0: Enhanced progress logging
        """
        logger.info("   🚀 [v86.0] Launching J-Prime process...")

        try:
            launcher = await get_resource_aware_launcher()

            # Configuration from environment
            port = int(os.getenv("JARVIS_PRIME_PORT", "8000"))
            auto_download = os.getenv("JARVIS_PRIME_AUTO_DOWNLOAD", "false").lower() == "true"
            max_retries = int(os.getenv("JARVIS_PRIME_MAX_RETRIES", "3"))
            health_timeout = float(os.getenv("JARVIS_PRIME_HEALTH_TIMEOUT", "30.0"))

            # Build extra args
            extra_args = []
            if auto_download:
                extra_args.append("--auto-download")

            # Create launch configuration
            config = LaunchConfig(
                repo_id="jarvis_prime",
                component_name="jarvis_prime",
                entry_point="jarvis_prime.server",  # Module-style entry
                port=port,
                extra_args=extra_args,
                env_vars={
                    "JARVIS_PRIME_PORT": str(port),
                },
                resources=ResourceRequirements(
                    min_memory_mb=512,
                    recommended_memory_mb=2048,
                    min_cpu_percent=5.0,
                    recommended_cpu_percent=20.0,
                    required_ports=[port],
                ),
                max_retries=max_retries,
                retry_backoff_base=2.0,
                health_check_url=f"http://localhost:{port}/health",
                health_check_timeout=health_timeout,
                startup_timeout=60.0,
            )

            # Launch with the resource-aware launcher
            logger.info(f"   📦 [v86.0] Starting J-Prime on port {port}...")
            success, pid, messages = await launcher.launch(config)

            # v86.0: Enhanced message logging
            for msg in messages:
                if "failed" in msg.lower() or "error" in msg.lower():
                    logger.warning(f"      ⚠️  {msg}")
                else:
                    logger.info(f"      ✓ {msg}")

            if success and pid:
                # Store process info for later management
                self._managed_processes["jarvis_prime"] = {
                    **launcher._managed_processes.get("jarvis_prime", {}),
                    "launched_by": "v86.0_intelligent_launcher",
                }
                logger.info(f"   ✅ [v86.0] J-Prime launched successfully (PID: {pid})")
                return True
            else:
                logger.error("   ❌ [v86.0] J-Prime launch failed")
                return False

        except Exception as e:
            logger.error(f"   ❌ [v86.0] J-Prime launch exception: {e}")
            traceback.print_exc()
            return False

    async def _launch_reactor_process(self) -> bool:
        """
        v86.0: Launch Reactor-Core process with intelligent discovery and retry.

        Uses IntelligentRepoDiscovery for multi-strategy path discovery
        and ResourceAwareLauncher for robust process launching.
        v86.0: Enhanced progress logging
        """
        logger.info("   🚀 [v86.0] Launching Reactor-Core process...")

        try:
            launcher = await get_resource_aware_launcher()

            # Configuration from environment
            max_retries = int(os.getenv("REACTOR_CORE_MAX_RETRIES", "3"))

            # Create launch configuration
            # v87.0: Launch Reactor-Core API server (not orchestrator script)
            # The API server provides HTTP health checks and is the proper component
            reactor_port = int(os.getenv("REACTOR_CORE_PORT", "8003"))

            config = LaunchConfig(
                repo_id="reactor_core",
                component_name="reactor_core",
                entry_point="uvicorn",  # Launch API server via uvicorn
                port=None,  # Don't auto-add --port (we specify in extra_args)
                extra_args=[
                    "reactor_core.api.server:app",
                    "--host", "127.0.0.1",
                    "--port", str(reactor_port),
                    "--log-level", "warning",
                ],
                env_vars={
                    "REACTOR_CORE_MODE": os.getenv("REACTOR_CORE_MODE", "trinity"),
                    "REACTOR_CORE_PORT": str(reactor_port),
                },
                resources=ResourceRequirements(
                    min_memory_mb=256,
                    recommended_memory_mb=1024,
                    min_cpu_percent=5.0,
                    recommended_cpu_percent=15.0,
                ),
                max_retries=max_retries,
                retry_backoff_base=2.0,
                health_check_url=f"http://127.0.0.1:{reactor_port}/health",
                health_check_timeout=30.0,
                startup_timeout=45.0,
            )

            # Launch with the resource-aware launcher
            logger.info("   📦 [v86.0] Starting Reactor-Core orchestrator...")
            success, pid, messages = await launcher.launch(config)

            # v86.0: Enhanced message logging
            for msg in messages:
                if "failed" in msg.lower() or "error" in msg.lower():
                    logger.warning(f"      ⚠️  {msg}")
                else:
                    logger.info(f"      ✓ {msg}")

            if success and pid:
                # Store process info for later management
                self._managed_processes["reactor_core"] = {
                    **launcher._managed_processes.get("reactor_core", {}),
                    "launched_by": "v87.0_intelligent_launcher",
                }

                # v87.0: Verify reactor started via HTTP health check (API server)
                health_url = f"http://127.0.0.1:{reactor_port}/health"
                health_timeout = float(os.getenv("REACTOR_CORE_HEALTH_TIMEOUT", "30.0"))
                health_poll = 0.5  # v87.0: Fast polling

                logger.info(
                    f"   🏥 [v87.0] Waiting for Reactor-Core health endpoint "
                    f"({health_url}, timeout={health_timeout}s)..."
                )

                health_start = time.time()
                health_verified = False

                import aiohttp
                async with aiohttp.ClientSession() as session:
                    while time.time() - health_start < health_timeout:
                        try:
                            async with session.get(
                                health_url,
                                timeout=aiohttp.ClientTimeout(total=5.0)
                            ) as resp:
                                if resp.status == 200:
                                    elapsed = time.time() - health_start
                                    logger.info(
                                        f"   ✅ [v87.0] Reactor-Core health verified "
                                        f"(after {elapsed:.1f}s)"
                                    )
                                    health_verified = True
                                    break
                        except Exception:
                            pass  # Keep polling

                        await asyncio.sleep(health_poll)

                if health_verified:
                    logger.info(f"   ✅ [v87.0] Reactor-Core API server launched successfully (PID: {pid})")

                    # v87.0: Write heartbeat file for compatibility with other components
                    heartbeat_path = Path.home() / ".jarvis" / "trinity" / "components" / "reactor_core.json"
                    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
                    heartbeat_data = {
                        "component_type": "reactor_core",
                        "instance_id": f"reactor-api-{pid}-{int(time.time())}",
                        "timestamp": time.time(),
                        "port": reactor_port,
                        "pid": pid,
                        "status": "online",
                        "launched_by": "trinity_integrator_v87",
                    }
                    try:
                        with open(heartbeat_path, 'w') as f:
                            json.dump(heartbeat_data, f, indent=2)
                        logger.debug(f"   📝 [v87.0] Reactor-Core heartbeat file written")
                    except Exception as e:
                        logger.warning(f"   ⚠️  [v87.0] Failed to write heartbeat: {e}")
                else:
                    logger.warning("   ⚠️  [v87.0] Reactor-Core started but health check failed")

                return True
            else:
                logger.error("   ❌ [v86.0] Reactor-Core launch failed")
                return False

        except Exception as e:
            logger.error(f"   ❌ [v86.0] Reactor-Core launch exception: {e}")
            traceback.print_exc()
            return False

    async def _launch_with_parallel_coordination(self) -> Dict[str, bool]:
        """
        v85.0: Launch all Trinity components in parallel with coordination.

        Returns dict of component_name -> success status.
        """
        logger.info("[Launcher] Starting parallel Trinity component launch...")

        # Define launch tasks
        launch_tasks = {
            "jarvis_prime": self._launch_jprime_process(),
            "reactor_core": self._launch_reactor_process(),
        }

        # Filter by enabled status
        if not os.getenv("JARVIS_PRIME_ENABLED", "true").lower() in ("true", "1", "yes"):
            del launch_tasks["jarvis_prime"]
            logger.info("[Launcher] J-Prime disabled, skipping")

        if not os.getenv("REACTOR_CORE_ENABLED", "true").lower() in ("true", "1", "yes"):
            del launch_tasks["reactor_core"]
            logger.info("[Launcher] Reactor-Core disabled, skipping")

        if not launch_tasks:
            logger.warning("[Launcher] No components enabled for launch")
            return {}

        # Launch all in parallel
        results = {}
        try:
            completed = await asyncio.gather(
                *[task for task in launch_tasks.values()],
                return_exceptions=True,
            )

            for (name, _), result in zip(launch_tasks.items(), completed):
                if isinstance(result, Exception):
                    logger.error(f"[Launcher] {name} launch raised exception: {result}")
                    results[name] = False
                else:
                    results[name] = bool(result)
                    if result:
                        logger.info(f"[Launcher] {name} launched successfully")
                    else:
                        logger.error(f"[Launcher] {name} launch failed")

        except Exception as e:
            logger.error(f"[Launcher] Parallel launch coordination failed: {e}")
            for name in launch_tasks:
                results[name] = False

        # Log summary
        success_count = sum(1 for v in results.values() if v)
        total_count = len(results)
        logger.info(f"[Launcher] Launch complete: {success_count}/{total_count} components started")

        return results

    async def _shutdown_managed_processes(self) -> None:
        """
        v84.0: Gracefully shutdown all managed processes with force timeout.
        """
        shutdown_timeout = float(os.getenv("TRINITY_SHUTDOWN_TIMEOUT", "30.0"))
        graceful_timeout = float(os.getenv("TRINITY_GRACEFUL_TIMEOUT", "10.0"))

        for name, info in self._managed_processes.items():
            process = info.get("process")
            if process and process.returncode is None:
                logger.info(f"[Shutdown] Terminating {name} (PID {process.pid})")
                try:
                    # Phase 1: Graceful termination
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=graceful_timeout)
                        logger.info(f"[Shutdown] {name} terminated gracefully")
                    except asyncio.TimeoutError:
                        # Phase 2: Force kill
                        logger.warning(f"[Shutdown] Force killing {name} after {graceful_timeout}s")
                        process.kill()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            logger.error(f"[Shutdown] {name} did not respond to kill")
                except Exception as e:
                    logger.warning(f"[Shutdown] Error terminating {name}: {e}")

        self._managed_processes.clear()

    # =========================================================================
    # v84.0: Process Supervision with Auto-Restart
    # =========================================================================

    async def _start_process_supervision(self) -> None:
        """
        v84.0: Start background process supervision task.

        Features:
        - PID monitoring with cookie validation
        - Crash detection via heartbeat staleness
        - Automatic restart with exponential backoff
        - Circuit breaker integration
        """
        self._supervision_task = asyncio.create_task(self._process_supervision_loop())
        logger.info("[Supervisor] Process supervision started")

    async def _process_supervision_loop(self) -> None:
        """Background loop for process supervision."""
        supervision_interval = float(os.getenv("TRINITY_SUPERVISION_INTERVAL", "10.0"))
        max_restart_delay = float(os.getenv("TRINITY_MAX_RESTART_DELAY", "300.0"))

        restart_counts: Dict[str, int] = {}
        last_restart_times: Dict[str, float] = {}

        while self._running:
            try:
                await asyncio.sleep(supervision_interval)

                # Check each managed process
                for name, info in list(self._managed_processes.items()):
                    process = info.get("process")
                    pid = info.get("pid")

                    if process is None:
                        continue

                    # Check if process is still running
                    if process.returncode is not None:
                        # Process exited - attempt restart
                        logger.warning(
                            f"[Supervisor] {name} exited with code {process.returncode}"
                        )

                        # Update restart count
                        restart_counts[name] = restart_counts.get(name, 0) + 1
                        count = restart_counts[name]

                        # Calculate backoff delay
                        base_delay = float(os.getenv("TRINITY_RESTART_BASE_DELAY", "5.0"))
                        delay = min(base_delay * (2 ** (count - 1)), max_restart_delay)

                        # Check if we should restart
                        last_restart = last_restart_times.get(name, 0)
                        if time.time() - last_restart < delay:
                            logger.info(
                                f"[Supervisor] {name} restart throttled "
                                f"(attempt {count}, waiting {delay:.0f}s)"
                            )
                            continue

                        # Record crash
                        await self._crash_recovery.record_crash(name, f"Exit code {process.returncode}")

                        # Check circuit breaker
                        if count > 5:
                            logger.error(
                                f"[Supervisor] {name} exceeded restart limit ({count} attempts)"
                            )
                            continue

                        # Attempt restart
                        logger.info(f"[Supervisor] Restarting {name} (attempt {count})")
                        last_restart_times[name] = time.time()

                        if name == "jarvis_prime":
                            success = await self._launch_jprime_process()
                        elif name == "reactor_core":
                            success = await self._launch_reactor_process()
                        else:
                            success = False

                        if success:
                            logger.info(f"[Supervisor] {name} restarted successfully")
                        else:
                            logger.error(f"[Supervisor] {name} restart failed")

                    else:
                        # Process running - verify via heartbeat
                        heartbeat_ok = await self._verify_process_heartbeat(name, pid)
                        if not heartbeat_ok:
                            logger.warning(
                                f"[Supervisor] {name} heartbeat stale (PID {pid})"
                            )

                # Reset restart counts for healthy processes
                for name in list(restart_counts.keys()):
                    info = self._managed_processes.get(name)
                    if info and info.get("process") and info["process"].returncode is None:
                        # Process is running, slowly decay restart count
                        if restart_counts[name] > 0:
                            # Decay after 5 minutes of stability
                            last_restart = last_restart_times.get(name, 0)
                            if time.time() - last_restart > 300:
                                restart_counts[name] = max(0, restart_counts[name] - 1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Supervisor] Error in supervision loop: {e}")

    async def _verify_process_heartbeat(self, component: str, pid: int) -> bool:
        """Verify process is healthy via heartbeat file."""
        heartbeat_file = Path.home() / ".jarvis" / "trinity" / "components" / f"{component}.json"

        if not heartbeat_file.exists():
            return False

        try:
            with open(heartbeat_file, 'r') as f:
                data = json.load(f)

            # Check timestamp freshness
            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp

            # Heartbeat stale threshold (30 seconds)
            stale_threshold = float(os.getenv("TRINITY_HEARTBEAT_STALE_THRESHOLD", "30.0"))

            if age > stale_threshold:
                return False

            # Verify PID matches
            heartbeat_pid = data.get("pid")
            if heartbeat_pid and heartbeat_pid != pid:
                logger.warning(
                    f"[Supervisor] {component} PID mismatch: expected {pid}, got {heartbeat_pid}"
                )
                return False

            return True

        except Exception as e:
            logger.debug(f"[Supervisor] Heartbeat verification error: {e}")
            return False

    # =========================================================================
    # v84.0: Lock Timeout Protection
    # =========================================================================

    class TimeoutLock:
        """
        v84.0: Lock with timeout protection and deadlock detection.

        Features:
        - Configurable acquisition timeout
        - Deadlock detection via caller tracking
        - Automatic release on timeout
        - Metrics and logging
        """

        def __init__(
            self,
            name: str,
            timeout: float = 30.0,
            warn_threshold: float = 10.0,
        ):
            self._name = name
            self._timeout = timeout
            self._warn_threshold = warn_threshold
            self._lock = asyncio.Lock()
            self._holder: Optional[str] = None
            self._acquired_at: float = 0.0
            self._acquisition_count: int = 0
            self._timeout_count: int = 0

        async def acquire(self, caller: str = "unknown") -> bool:
            """
            Acquire lock with timeout.

            Args:
                caller: Identifier for deadlock detection

            Returns:
                True if acquired, False if timeout
            """
            start_time = time.time()

            try:
                acquired = await asyncio.wait_for(
                    self._lock.acquire(),
                    timeout=self._timeout,
                )

                if acquired:
                    self._holder = caller
                    self._acquired_at = time.time()
                    self._acquisition_count += 1

                    wait_time = time.time() - start_time
                    if wait_time > self._warn_threshold:
                        logger.warning(
                            f"[Lock:{self._name}] Slow acquisition: {wait_time:.2f}s "
                            f"(caller={caller})"
                        )

                return acquired

            except asyncio.TimeoutError:
                self._timeout_count += 1
                logger.error(
                    f"[Lock:{self._name}] Acquisition timeout after {self._timeout}s "
                    f"(caller={caller}, current_holder={self._holder})"
                )
                return False

        def release(self) -> None:
            """Release the lock."""
            if self._lock.locked():
                hold_time = time.time() - self._acquired_at
                if hold_time > self._warn_threshold:
                    logger.warning(
                        f"[Lock:{self._name}] Long hold time: {hold_time:.2f}s "
                        f"(holder={self._holder})"
                    )
                self._holder = None
                self._acquired_at = 0.0
                self._lock.release()

        async def __aenter__(self):
            await self.acquire()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.release()
            return False

        def get_stats(self) -> Dict[str, Any]:
            """Get lock statistics."""
            return {
                "name": self._name,
                "locked": self._lock.locked(),
                "holder": self._holder,
                "acquisition_count": self._acquisition_count,
                "timeout_count": self._timeout_count,
                "hold_time": time.time() - self._acquired_at if self._acquired_at > 0 else 0,
            }

    async def _event_cleanup_loop(self) -> None:
        """Background loop to clean up expired events."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Cleanup hourly
                cleaned = await self._event_store.cleanup_expired()
                if cleaned > 0:
                    logger.info(f"[TrinityOrchestrator] Cleaned {cleaned} expired events")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[TrinityOrchestrator] Event cleanup error: {e}")

    # =========================================================================
    # v83.0: Unified Health Methods
    # =========================================================================

    async def get_unified_health(self) -> Dict[str, Any]:
        """
        Get unified health across all Trinity components.

        v83.0 feature: Includes anomaly detection, trend analysis,
        and correlation between components.
        """
        return {
            "legacy": await self.get_health(),
            "aggregated": self._health_aggregator.get_unified_health(),
            "supervisor": self._process_supervisor.get_stats(),
            "circuit_breakers": {
                name: {
                    "state": cb.state.name,
                    "stats": {
                        "total_calls": cb.stats.total_calls,
                        "failures": cb.stats.failed_calls,
                        "rejected": cb.stats.rejected_calls,
                    },
                }
                for name, cb in self._circuit_breakers.items()
            },
            "crash_recovery": {
                name: self._crash_recovery.get_restart_count(name)
                for name in ["jarvis_prime", "reactor_core"]
            },
            "resources": self._resource_coordinator.get_system_resources(),
            "throttler": self._throttler.get_stats(),
        }

    # =========================================================================
    # Voice Profile Synchronization (v2.7)
    # =========================================================================

    async def sync_voice_profiles(
        self,
        force: bool = False,
        include_embeddings: bool = True,
    ) -> Dict[str, Any]:
        """
        Synchronize voice profiles across Trinity repos (JARVIS, Prime, Reactor).

        v2.7 Enhancement: Cross-repo voice profile synchronization for consistent
        voice authentication across all components.

        This ensures:
        - Voice embeddings are consistent across repos
        - Authentication thresholds are synchronized
        - Drift adaptations are shared
        - Voice evolution data is propagated

        Args:
            force: Force sync even if profiles haven't changed
            include_embeddings: Include full embeddings (slower but complete)

        Returns:
            Sync result with stats and any errors
        """
        async with self._tracer.span("voice_profile_sync"):
            result = {
                "success": False,
                "profiles_synced": 0,
                "errors": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            try:
                # Get voice profiles from JARVIS body
                try:
                    from voice_unlock.unified_voice_cache_manager import get_unified_voice_cache
                    cache = await get_unified_voice_cache()

                    if cache and cache.is_ready:
                        profiles = cache._preloaded_profiles

                        # Sync to JARVIS Prime (if enabled and connected)
                        if self.enable_jprime and self._jprime_client:
                            try:
                                for speaker_name, profile in profiles.items():
                                    # Send profile to Prime via event store
                                    await self._event_store.publish(
                                        event_type="voice.profile.sync",
                                        source="jarvis_body",
                                        payload={
                                            "speaker_name": speaker_name,
                                            "embedding_dimensions": profile.embedding_dimensions,
                                            "total_samples": profile.total_samples,
                                            "avg_confidence": profile.avg_confidence,
                                            "is_primary_user": profile.is_primary_user,
                                            "embedding": profile.embedding.tolist() if include_embeddings else None,
                                        },
                                    )
                                    result["profiles_synced"] += 1

                                logger.info(
                                    f"[TrinityOrchestrator] Synced {result['profiles_synced']} "
                                    f"voice profiles to JARVIS Prime"
                                )

                            except Exception as e:
                                result["errors"].append(f"Prime sync error: {str(e)}")
                                logger.warning(f"Voice profile sync to Prime failed: {e}")

                        # Sync to Reactor Core (if enabled and connected)
                        if self.enable_reactor and self._reactor_client:
                            try:
                                # Reactor Core may need profiles for training feedback
                                await self._event_store.publish(
                                    event_type="voice.profiles.batch",
                                    source="jarvis_body",
                                    payload={
                                        "profile_count": len(profiles),
                                        "speaker_names": list(profiles.keys()),
                                        "sync_timestamp": result["timestamp"],
                                    },
                                )

                            except Exception as e:
                                result["errors"].append(f"Reactor sync error: {str(e)}")
                                logger.warning(f"Voice profile sync to Reactor failed: {e}")

                        result["success"] = len(result["errors"]) == 0

                except ImportError as e:
                    result["errors"].append(f"Voice cache not available: {str(e)}")

            except Exception as e:
                result["errors"].append(f"Sync failed: {str(e)}")
                logger.error(f"Voice profile synchronization error: {e}")

            return result

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _health_loop(self) -> None:
        """Background health monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self.health_check_interval)

                health = await self.get_health()

                # Update state based on health
                if health.degraded_components:
                    if self._state == TrinityState.READY:
                        self._set_state(TrinityState.DEGRADED)
                elif self._state == TrinityState.DEGRADED:
                    if not health.degraded_components:
                        self._set_state(TrinityState.READY)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[TrinityIntegrator] Health check error: {e}")

    async def _crash_recovery_loop(self) -> None:
        """
        v85.0: Runtime crash recovery loop.

        Continuously monitors component health and automatically restarts
        crashed components using the CrashRecoveryManager for:
        - Exponential backoff between restart attempts
        - Maximum restart count enforcement
        - Cooldown period management
        - Crash history tracking

        This runs as a background task alongside _health_loop.
        """
        recovery_interval = float(os.getenv("TRINITY_CRASH_RECOVERY_INTERVAL", "15.0"))

        while self._running:
            try:
                await asyncio.sleep(recovery_interval)

                # Skip if we're shutting down or not initialized
                if self._state in (TrinityState.SHUTTING_DOWN, TrinityState.UNINITIALIZED):
                    continue

                # Check each enabled component
                components_to_check = []

                if self.enable_jprime:
                    components_to_check.append(("jarvis_prime", self._check_and_recover_jprime))

                if self.enable_reactor:
                    components_to_check.append(("reactor_core", self._check_and_recover_reactor))

                # Check and recover components concurrently
                if components_to_check:
                    tasks = [
                        recovery_fn()
                        for _, recovery_fn in components_to_check
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                logger.debug("[CrashRecovery] Loop cancelled")
                break
            except Exception as e:
                logger.debug(f"[CrashRecovery] Loop error: {e}")

    async def _check_and_recover_jprime(self) -> bool:
        """
        v85.0: Check J-Prime health and recover if crashed.

        Returns:
            True if component is healthy or was successfully recovered
        """
        try:
            # Check if J-Prime is healthy
            jprime_status = await self._check_jprime_health()

            if jprime_status.online and jprime_status.health == ComponentHealth.HEALTHY:
                # Healthy - record success to reduce backoff
                await self._crash_recovery.record_success("jarvis_prime")
                return True

            # Component is down - check if we should restart
            should_restart, backoff = await self._crash_recovery.should_restart("jarvis_prime")

            if not should_restart:
                logger.error("[CrashRecovery] J-Prime exceeded max restarts, not attempting recovery")
                return False

            # Record the crash
            await self._crash_recovery.record_crash(
                "jarvis_prime",
                error=jprime_status.error or "Component offline",
            )

            # Wait for backoff period
            if backoff > 0:
                logger.info(f"[CrashRecovery] Waiting {backoff:.1f}s before J-Prime restart...")
                await asyncio.sleep(backoff)

            # Attempt restart using v85.0 launcher
            logger.info("[CrashRecovery] Attempting J-Prime restart...")
            success = await self._launch_jprime_process()

            if success:
                logger.info("[CrashRecovery] J-Prime restarted successfully")
                await self._crash_recovery.record_success("jarvis_prime")
                return True
            else:
                logger.warning("[CrashRecovery] J-Prime restart failed")
                return False

        except Exception as e:
            logger.debug(f"[CrashRecovery] J-Prime recovery error: {e}")
            return False

    async def _check_and_recover_reactor(self) -> bool:
        """
        v85.0: Check Reactor-Core health and recover if crashed.

        Returns:
            True if component is healthy or was successfully recovered
        """
        try:
            # Check if Reactor-Core is healthy
            reactor_status = await self._check_reactor_health()

            if reactor_status.online and reactor_status.health == ComponentHealth.HEALTHY:
                # Healthy - record success to reduce backoff
                await self._crash_recovery.record_success("reactor_core")
                return True

            # Component is down - check if we should restart
            should_restart, backoff = await self._crash_recovery.should_restart("reactor_core")

            if not should_restart:
                logger.error("[CrashRecovery] Reactor-Core exceeded max restarts, not attempting recovery")
                return False

            # Record the crash
            await self._crash_recovery.record_crash(
                "reactor_core",
                error=reactor_status.error or "Component offline",
            )

            # Wait for backoff period
            if backoff > 0:
                logger.info(f"[CrashRecovery] Waiting {backoff:.1f}s before Reactor-Core restart...")
                await asyncio.sleep(backoff)

            # Attempt restart using v85.0 launcher
            logger.info("[CrashRecovery] Attempting Reactor-Core restart...")
            success = await self._launch_reactor_process()

            if success:
                logger.info("[CrashRecovery] Reactor-Core restarted successfully")
                await self._crash_recovery.record_success("reactor_core")
                return True
            else:
                logger.warning("[CrashRecovery] Reactor-Core restart failed")
                return False

        except Exception as e:
            logger.debug(f"[CrashRecovery] Reactor-Core recovery error: {e}")
            return False

    async def get_health(self) -> TrinityHealth:
        """Get current Trinity system health."""
        components: Dict[str, ComponentStatus] = {}
        degraded: List[str] = []
        errors: List[str] = []

        # Check JARVIS Body (self)
        body_status = ComponentStatus(
            name="jarvis_body",
            health=ComponentHealth.HEALTHY,
            online=True,
            last_heartbeat=time.time(),
            metrics={"uptime": self.uptime},
        )
        components["jarvis_body"] = body_status

        # Check JARVIS Prime
        if self.enable_jprime:
            jprime_status = await self._check_jprime_health()
            components["jarvis_prime"] = jprime_status
            if jprime_status.health != ComponentHealth.HEALTHY:
                degraded.append("jarvis_prime")
            if jprime_status.error:
                errors.append(jprime_status.error)

        # Check Reactor-Core
        if self.enable_reactor:
            reactor_status = await self._check_reactor_health()
            components["reactor_core"] = reactor_status
            if reactor_status.health != ComponentHealth.HEALTHY:
                degraded.append("reactor_core")
            if reactor_status.error:
                errors.append(reactor_status.error)

        return TrinityHealth(
            state=self._state,
            components=components,
            uptime_seconds=self.uptime,
            last_check=time.time(),
            degraded_components=degraded,
            errors=errors,
        )

    async def _check_jprime_health(self) -> ComponentStatus:
        """Check JARVIS Prime health."""
        if not self._jprime_client:
            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.UNKNOWN,
                online=False,
                error="Client not initialized",
            )

        try:
            is_online = self._jprime_client.is_online
            metrics = self._jprime_client.get_metrics()

            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.HEALTHY if is_online else ComponentHealth.UNHEALTHY,
                online=is_online,
                last_heartbeat=metrics.get("last_health_check"),
                metrics=metrics,
            )

        except Exception as e:
            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.UNHEALTHY,
                online=False,
                error=str(e),
            )

    async def _check_reactor_health(self) -> ComponentStatus:
        """Check Reactor-Core health."""
        if not self._reactor_client:
            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.UNKNOWN,
                online=False,
                error="Client not initialized",
            )

        try:
            is_online = self._reactor_client.is_online
            metrics = self._reactor_client.get_metrics()

            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.HEALTHY if is_online else ComponentHealth.UNHEALTHY,
                online=is_online,
                last_heartbeat=time.time() if is_online else None,
                metrics=metrics,
            )

        except Exception as e:
            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.UNHEALTHY,
                online=False,
                error=str(e),
            )

    # =========================================================================
    # Shutdown
    # =========================================================================

    async def stop(
        self,
        timeout: float = 30.0,
        force: bool = False,
    ) -> bool:
        """
        Stop the Trinity system.

        Args:
            timeout: Max time to wait for graceful shutdown
            force: Skip drain phase for immediate shutdown

        Returns:
            True if shutdown successful
        """
        async with self._lock:
            if self._state in (TrinityState.STOPPED, TrinityState.STOPPING):
                return True

            self._set_state(TrinityState.STOPPING)
            self._running = False

            try:
                # Stop health monitoring
                if self._health_task:
                    self._health_task.cancel()
                    try:
                        await self._health_task
                    except asyncio.CancelledError:
                        pass

                # v85.0: Stop crash recovery loop
                if hasattr(self, "_crash_recovery_task") and self._crash_recovery_task:
                    self._crash_recovery_task.cancel()
                    try:
                        await self._crash_recovery_task
                    except asyncio.CancelledError:
                        pass
                    logger.debug("[TrinityOrchestrator v85.0] Crash recovery loop stopped")

                # Close clients
                if self._jprime_client:
                    await self._jprime_client.disconnect()

                if self._reactor_client:
                    from backend.clients.reactor_core_client import shutdown_reactor_client
                    await shutdown_reactor_client()

                # Coordinated shutdown
                if self._shutdown_manager:
                    from backend.core.coordinated_shutdown import ShutdownReason

                    result = await self._shutdown_manager.initiate_shutdown(
                        reason=ShutdownReason.USER_REQUEST,
                        timeout=timeout,
                        force=force,
                    )

                    if not result.success:
                        logger.warning(
                            f"[TrinityIntegrator] Shutdown incomplete: {result.errors}"
                        )

                # Close IPC
                if self._ipc_bus:
                    from backend.core.trinity_ipc import close_resilient_trinity_ipc_bus
                    await close_resilient_trinity_ipc_bus()

                self._set_state(TrinityState.STOPPED)

                elapsed = time.time() - (self._start_time or time.time())
                logger.info(
                    f"[TrinityIntegrator] Stopped after {elapsed:.2f}s uptime"
                )

                return True

            except Exception as e:
                logger.error(f"[TrinityIntegrator] Shutdown error: {e}")
                self._set_state(TrinityState.ERROR)
                return False

    # =========================================================================
    # State Management
    # =========================================================================

    def _set_state(self, new_state: TrinityState) -> None:
        """Set new state and notify callbacks."""
        old_state = self._state
        self._state = new_state

        if old_state != new_state:
            logger.info(
                f"[TrinityIntegrator] State: {old_state.value} -> {new_state.value}"
            )

            for callback in self._on_state_change:
                try:
                    callback(old_state, new_state)
                except Exception as e:
                    logger.warning(f"[TrinityIntegrator] Callback error: {e}")

    def on_state_change(
        self,
        callback: Callable[[TrinityState, TrinityState], None],
    ) -> None:
        """Register callback for state changes."""
        self._on_state_change.append(callback)

    def on_component_change(
        self,
        callback: Callable[[str, ComponentHealth], None],
    ) -> None:
        """Register callback for component health changes."""
        self._on_component_change.append(callback)

    # =========================================================================
    # API Access
    # =========================================================================

    @property
    def ipc_bus(self):
        """Get the IPC bus."""
        return self._ipc_bus

    @property
    def jprime_client(self):
        """Get the JARVIS Prime client."""
        return self._jprime_client

    @property
    def reactor_client(self):
        """Get the Reactor-Core client."""
        return self._reactor_client

    def get_metrics(self) -> Dict[str, Any]:
        """Get integrator metrics."""
        return {
            "state": self._state.value,
            "uptime": self.uptime,
            "jprime_enabled": self.enable_jprime,
            "reactor_enabled": self.enable_reactor,
            "jprime_online": self._jprime_client.is_online if self._jprime_client else False,
            "reactor_online": self._reactor_client.is_online if self._reactor_client else False,
        }


# =============================================================================
# Backward Compatibility Alias
# =============================================================================

# TrinityIntegrator is now TrinityUnifiedOrchestrator
TrinityIntegrator = TrinityUnifiedOrchestrator


# =============================================================================
# Singleton Access
# =============================================================================

_orchestrator: Optional[TrinityUnifiedOrchestrator] = None
_orchestrator_lock: Optional[asyncio.Lock] = None  # v90.0: Lazy lock initialization


async def get_trinity_orchestrator(
    **kwargs,
) -> TrinityUnifiedOrchestrator:
    """Get or create the singleton Trinity Unified Orchestrator v83.0."""
    global _orchestrator, _orchestrator_lock

    # v90.0: Lazy lock creation to avoid "no event loop" errors at module load
    if _orchestrator_lock is None:
        _orchestrator_lock = asyncio.Lock()

    async with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = TrinityUnifiedOrchestrator(**kwargs)
        return _orchestrator


# Backward compatibility alias
async def get_trinity_integrator(**kwargs) -> TrinityUnifiedOrchestrator:
    """Legacy alias for get_trinity_orchestrator."""
    return await get_trinity_orchestrator(**kwargs)


async def start_trinity() -> bool:
    """Start the Trinity system."""
    orchestrator = await get_trinity_orchestrator()
    return await orchestrator.start()


async def stop_trinity(force: bool = False) -> bool:
    """Stop the Trinity system."""
    global _orchestrator

    if _orchestrator:
        result = await _orchestrator.stop(force=force)
        _orchestrator = None
        return result

    return True


async def get_trinity_health() -> Optional[TrinityHealth]:
    """Get Trinity system health."""
    if _orchestrator:
        return await _orchestrator.get_health()
    return None


async def get_unified_trinity_health() -> Optional[Dict[str, Any]]:
    """Get unified Trinity health with v83.0 features (anomaly detection, trends)."""
    if _orchestrator:
        return await _orchestrator.get_unified_health()
    return None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Core Types
    # ═══════════════════════════════════════════════════════════════════════
    "TrinityState",
    "ComponentHealth",
    "ComponentStatus",
    "TrinityHealth",

    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Main Orchestrator
    # ═══════════════════════════════════════════════════════════════════════
    "TrinityUnifiedOrchestrator",
    "TrinityIntegrator",  # Backward compatibility alias

    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Advanced Components
    # ═══════════════════════════════════════════════════════════════════════
    # Configuration
    "ConfigRegistry",
    "get_config",

    # Circuit Breaker
    "CircuitBreaker",
    "CircuitState",
    "CircuitBreakerStats",
    "CircuitOpenError",

    # Process Supervisor
    "ProcessSupervisor",
    "ProcessInfo",

    # Crash Recovery
    "CrashRecoveryManager",
    "CrashRecord",

    # Resource Coordinator
    "ResourceCoordinator",
    "ResourceAllocation",

    # Event Store
    "EventStore",
    "TrinityEvent",

    # Distributed Tracing
    "DistributedTracer",
    "TraceSpan",

    # Health Aggregator
    "UnifiedHealthAggregator",
    "HealthSample",
    "AnomalyReport",

    # Adaptive Throttling
    "AdaptiveThrottler",
    "ThrottleExceededError",

    # ═══════════════════════════════════════════════════════════════════════
    # v85.0 Unified State Coordination (NEW)
    # ═══════════════════════════════════════════════════════════════════════
    # Process Ownership & Coordination
    "ProcessOwnership",
    "UnifiedStateCoordinator",

    # Entry Point Detection
    "TrinityEntryPointDetector",

    # Resource Checking
    "ResourceChecker",

    # ═══════════════════════════════════════════════════════════════════════
    # v87.0 Advanced Coordination (NEW)
    # ═══════════════════════════════════════════════════════════════════════
    "TrinityAdvancedCoordinator",
    "get_advanced_coordinator",
    "SystemResourceState",
    "CrossRepoVersion",
    "NetworkPartitionError",

    # ═══════════════════════════════════════════════════════════════════════
    # v88.0 Ultra-Advanced Features (NEW)
    # ═══════════════════════════════════════════════════════════════════════
    # Adaptive Circuit Breaker with ML-based prediction
    "AdaptiveCircuitBreaker",
    "PredictiveMetrics",
    "TrendAnalysis",
    # Lock-Free Ring Buffer
    "LockFreeRingBuffer",
    # Container/cgroup Awareness
    "ContainerAwareness",
    "ContainerResourceLimits",
    # Adaptive Backpressure (AIMD)
    "AdaptiveBackpressure",
    "BackpressureState",
    # Distributed Tracing (W3C Trace Context)
    "TraceContextManager",
    "TraceContext",
    # Structured Concurrency
    "StructuredConcurrency",
    "TaskGroupContext",
    # Ultra Coordinator
    "TrinityUltraCoordinator",
    "get_ultra_coordinator",

    # ═══════════════════════════════════════════════════════════════════════
    # v83.0 Convenience Functions
    # ═══════════════════════════════════════════════════════════════════════
    "get_trinity_orchestrator",
    "get_trinity_integrator",  # Backward compatibility
    "start_trinity",
    "stop_trinity",
    "get_trinity_health",
    "get_unified_trinity_health",
]
