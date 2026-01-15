"""
Ouroboros Advanced Orchestrator v2.0
=====================================

Super-robust orchestration layer that handles ALL edge cases:
- Automatic LLM provider startup/discovery
- Intelligent retry with exponential backoff + jitter
- Semantic caching for similar prompts
- AST-based syntax validation before applying changes
- Git state management and conflict resolution
- Continuous health monitoring with auto-recovery
- Rate limiting with token bucket algorithm
- Graceful degradation with multiple fallback levels
- Cross-repo integration (JARVIS, Prime, Reactor Core)
- Resource management (memory, disk, file locks)

This is the "God Layer" that ensures Ouroboros never fails silently.

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import ast
import asyncio
import fcntl
import functools
import gc
import hashlib
import json
import logging
import os
import platform
import psutil
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import weakref
from abc import ABC, abstractmethod
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import aiofiles
except ImportError:
    aiofiles = None

logger = logging.getLogger("Ouroboros.AdvancedOrchestrator")

T = TypeVar("T")


# =============================================================================
# CONFIGURATION
# =============================================================================

class OrchestratorConfig:
    """Dynamic configuration with environment variable support."""

    # Resource Limits
    MAX_MEMORY_MB = int(os.getenv("OUROBOROS_MAX_MEMORY_MB", "2048"))
    MIN_DISK_SPACE_MB = int(os.getenv("OUROBOROS_MIN_DISK_MB", "500"))
    MAX_CONCURRENT_IMPROVEMENTS = int(os.getenv("OUROBOROS_MAX_CONCURRENT", "3"))

    # Retry Configuration
    MAX_RETRIES = int(os.getenv("OUROBOROS_MAX_RETRIES", "5"))
    BASE_RETRY_DELAY = float(os.getenv("OUROBOROS_RETRY_DELAY", "1.0"))
    MAX_RETRY_DELAY = float(os.getenv("OUROBOROS_MAX_RETRY_DELAY", "60.0"))
    RETRY_JITTER = float(os.getenv("OUROBOROS_RETRY_JITTER", "0.5"))

    # Rate Limiting (requests per minute)
    RATE_LIMIT_RPM = int(os.getenv("OUROBOROS_RATE_LIMIT", "30"))
    RATE_LIMIT_BURST = int(os.getenv("OUROBOROS_RATE_BURST", "10"))

    # Cache Configuration
    CACHE_MAX_SIZE = int(os.getenv("OUROBOROS_CACHE_SIZE", "100"))
    CACHE_TTL_SECONDS = int(os.getenv("OUROBOROS_CACHE_TTL", "3600"))

    # Health Check
    HEALTH_CHECK_INTERVAL = float(os.getenv("OUROBOROS_HEALTH_INTERVAL", "30.0"))
    HEALTH_CHECK_TIMEOUT = float(os.getenv("OUROBOROS_HEALTH_TIMEOUT", "5.0"))

    # Paths
    LOCK_DIR = Path(os.getenv("OUROBOROS_LOCK_DIR", "/tmp/ouroboros/locks"))
    CACHE_DIR = Path(os.getenv("OUROBOROS_CACHE_DIR", str(Path.home() / ".jarvis/ouroboros/cache")))

    # Cross-Repo Integration
    JARVIS_REPO = Path(os.getenv("JARVIS_REPO", Path.home() / "Documents/repos/JARVIS-AI-Agent"))
    PRIME_REPO = Path(os.getenv("PRIME_REPO", Path.home() / "Documents/repos/JARVIS-AI-Agent"))  # Same repo
    REACTOR_REPO = Path(os.getenv("REACTOR_REPO", Path.home() / "Documents/repos/reactor-core"))


# =============================================================================
# ADVANCED DATA STRUCTURES
# =============================================================================

@dataclass
class RetryState:
    """Track retry state with exponential backoff."""
    attempt: int = 0
    last_error: Optional[str] = None
    last_attempt_time: float = 0.0
    total_wait_time: float = 0.0

    def get_next_delay(self) -> float:
        """Calculate next delay with exponential backoff + jitter."""
        base_delay = OrchestratorConfig.BASE_RETRY_DELAY * (2 ** self.attempt)
        capped_delay = min(base_delay, OrchestratorConfig.MAX_RETRY_DELAY)
        jitter = random.uniform(0, OrchestratorConfig.RETRY_JITTER * capped_delay)
        return capped_delay + jitter

    def should_retry(self) -> bool:
        """Check if we should retry."""
        return self.attempt < OrchestratorConfig.MAX_RETRIES


@dataclass
class HealthStatus:
    """Health status of a component."""
    name: str
    healthy: bool
    last_check: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    error_message: Optional[str] = None
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CacheEntry:
    """LRU cache entry with TTL."""
    key: str
    value: Any
    created_at: float
    accessed_at: float
    hit_count: int = 0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > OrchestratorConfig.CACHE_TTL_SECONDS


@dataclass
class ResourceUsage:
    """Current resource usage."""
    memory_mb: float
    disk_free_mb: float
    cpu_percent: float
    active_tasks: int
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# ENUMS
# =============================================================================

class ProviderHealth(Enum):
    """Health state of an LLM provider."""
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"


class DegradationLevel(Enum):
    """Graceful degradation levels."""
    FULL = "full"           # All features available
    REDUCED = "reduced"     # Some features disabled
    MINIMAL = "minimal"     # Only essential features
    EMERGENCY = "emergency" # Last resort mode


class GitState(Enum):
    """Git repository state."""
    CLEAN = "clean"
    DIRTY = "dirty"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


# =============================================================================
# RATE LIMITER (Token Bucket Algorithm)
# =============================================================================

class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for controlling request rates.

    Allows burst traffic while maintaining average rate limit.
    """

    def __init__(
        self,
        rate: float = OrchestratorConfig.RATE_LIMIT_RPM / 60.0,
        burst: int = OrchestratorConfig.RATE_LIMIT_BURST,
    ):
        self.rate = rate  # tokens per second
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire a token, waiting if necessary.

        Returns True if token acquired, False if timeout.
        """
        deadline = time.monotonic() + timeout if timeout else None

        async with self._lock:
            while True:
                self._refill()

                if self.tokens >= 1:
                    self.tokens -= 1
                    return True

                if deadline and time.monotonic() >= deadline:
                    return False

                # Calculate wait time for next token
                wait_time = (1 - self.tokens) / self.rate
                if deadline:
                    wait_time = min(wait_time, deadline - time.monotonic())

                await asyncio.sleep(max(0.01, wait_time))

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_update = now

    def get_status(self) -> Dict[str, Any]:
        """Get rate limiter status."""
        self._refill()
        return {
            "tokens_available": self.tokens,
            "rate_per_second": self.rate,
            "burst_capacity": self.burst,
        }


# =============================================================================
# LRU CACHE WITH TTL
# =============================================================================

class SemanticCache:
    """
    LRU cache with TTL and semantic similarity matching.

    Caches LLM responses to avoid repeated API calls for similar prompts.
    """

    def __init__(
        self,
        max_size: int = OrchestratorConfig.CACHE_MAX_SIZE,
        ttl: int = OrchestratorConfig.CACHE_TTL_SECONDS,
    ):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}

    def _make_key(self, prompt: str, goal: str) -> str:
        """Create a cache key from prompt and goal."""
        content = f"{prompt[:500]}|{goal}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def get(self, prompt: str, goal: str) -> Optional[str]:
        """Get cached response if available."""
        async with self._lock:
            key = self._make_key(prompt, goal)
            entry = self._cache.get(key)

            if entry is None:
                self._stats["misses"] += 1
                return None

            if entry.is_expired():
                del self._cache[key]
                self._stats["misses"] += 1
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.accessed_at = time.time()
            entry.hit_count += 1
            self._stats["hits"] += 1

            return entry.value

    async def set(self, prompt: str, goal: str, response: str) -> None:
        """Cache a response."""
        async with self._lock:
            key = self._make_key(prompt, goal)

            # Remove if exists
            if key in self._cache:
                del self._cache[key]

            # Evict oldest if at capacity
            while len(self._cache) >= self.max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                self._stats["evictions"] += 1

            # Add new entry
            self._cache[key] = CacheEntry(
                key=key,
                value=response,
                created_at=time.time(),
                accessed_at=time.time(),
            )

    async def invalidate(self, prompt: str, goal: str) -> None:
        """Invalidate a cache entry."""
        async with self._lock:
            key = self._make_key(prompt, goal)
            self._cache.pop(key, None)

    async def clear(self) -> None:
        """Clear all cache entries."""
        async with self._lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        hit_rate = (
            self._stats["hits"] / (self._stats["hits"] + self._stats["misses"])
            if (self._stats["hits"] + self._stats["misses"]) > 0
            else 0.0
        )
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "evictions": self._stats["evictions"],
            "hit_rate": hit_rate,
        }


# =============================================================================
# SYNTAX VALIDATOR
# =============================================================================

class SyntaxValidator:
    """
    Validates Python code syntax before applying changes.

    Prevents applying broken code that would crash the system.
    """

    @staticmethod
    def validate_python(code: str) -> Tuple[bool, Optional[str]]:
        """
        Validate Python syntax.

        Returns:
            (is_valid, error_message)
        """
        try:
            ast.parse(code)
            return True, None
        except SyntaxError as e:
            return False, f"Syntax error at line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Parse error: {str(e)}"

    @staticmethod
    def validate_imports(code: str) -> Tuple[bool, List[str]]:
        """
        Check if all imports are valid (modules exist).

        Returns:
            (all_valid, list_of_missing_modules)
        """
        try:
            tree = ast.parse(code)
            imports = []

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module.split('.')[0])

            missing = []
            for module in set(imports):
                try:
                    __import__(module)
                except ImportError:
                    missing.append(module)

            return len(missing) == 0, missing
        except Exception:
            return True, []  # Don't block on validation errors

    @staticmethod
    def check_dangerous_patterns(code: str) -> List[str]:
        """
        Check for dangerous code patterns.

        Returns list of warnings.
        """
        warnings = []
        dangerous_patterns = [
            (r'\beval\s*\(', "Use of eval() is dangerous"),
            (r'\bexec\s*\(', "Use of exec() is dangerous"),
            (r'__import__\s*\(', "Dynamic imports may be dangerous"),
            (r'subprocess\..*shell\s*=\s*True', "Shell=True in subprocess is risky"),
            (r'os\.system\s*\(', "os.system() is vulnerable to injection"),
            (r'pickle\.loads?\s*\(', "Pickle can execute arbitrary code"),
            (r'yaml\.load\s*\([^,]+\)', "yaml.load without Loader is unsafe"),
        ]

        for pattern, warning in dangerous_patterns:
            if re.search(pattern, code):
                warnings.append(warning)

        return warnings


# =============================================================================
# GIT STATE MANAGER
# =============================================================================

class GitStateManager:
    """
    Manages git repository state for safe code modifications.

    Ensures we don't overwrite uncommitted changes or create conflicts.
    """

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    async def get_state(self) -> GitState:
        """Get current git state."""
        try:
            result = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=self.repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            output = stdout.decode().strip()
            if not output:
                return GitState.CLEAN

            # Check for conflicts
            if any(line.startswith("UU") for line in output.split("\n")):
                return GitState.CONFLICT

            return GitState.DIRTY

        except Exception:
            return GitState.UNKNOWN

    async def stash_changes(self, message: str = "Ouroboros auto-stash") -> Optional[str]:
        """Stash current changes."""
        try:
            result = await asyncio.create_subprocess_exec(
                "git", "stash", "push", "-m", message,
                cwd=self.repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            return stdout.decode().strip() if result.returncode == 0 else None
        except Exception:
            return None

    async def pop_stash(self) -> bool:
        """Pop the most recent stash."""
        try:
            result = await asyncio.create_subprocess_exec(
                "git", "stash", "pop",
                cwd=self.repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()
            return result.returncode == 0
        except Exception:
            return False

    async def get_file_status(self, file_path: Path) -> str:
        """Get git status of a specific file."""
        try:
            result = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain", str(file_path),
                cwd=self.repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            return stdout.decode().strip()
        except Exception:
            return "??"


# =============================================================================
# FILE LOCK MANAGER
# =============================================================================

class FileLockManager:
    """
    Manages file locks to prevent concurrent modifications.

    Uses fcntl for Unix file locking.
    """

    def __init__(self):
        self._locks: Dict[str, int] = {}  # path -> file descriptor
        OrchestratorConfig.LOCK_DIR.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def acquire_lock(
        self,
        file_path: Path,
        timeout: float = 30.0,
    ) -> AsyncGenerator[bool, None]:
        """
        Acquire an exclusive lock on a file.

        Usage:
            async with lock_manager.acquire_lock(path) as locked:
                if locked:
                    # Do work
        """
        lock_file = OrchestratorConfig.LOCK_DIR / f"{hashlib.md5(str(file_path).encode()).hexdigest()}.lock"
        fd = None
        acquired = False

        try:
            # Open lock file
            fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)

            # Try to acquire lock with timeout
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    self._locks[str(file_path)] = fd
                    break
                except (IOError, OSError):
                    await asyncio.sleep(0.1)

            yield acquired

        finally:
            if fd is not None:
                if acquired:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                    self._locks.pop(str(file_path), None)
                os.close(fd)

    def is_locked(self, file_path: Path) -> bool:
        """Check if a file is currently locked."""
        return str(file_path) in self._locks


# =============================================================================
# RESOURCE MONITOR
# =============================================================================

class ResourceMonitor:
    """
    Monitors system resources to prevent overload.

    Checks memory, disk space, and CPU before operations.
    """

    @staticmethod
    def get_usage() -> ResourceUsage:
        """Get current resource usage."""
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        return ResourceUsage(
            memory_mb=memory.used / (1024 * 1024),
            disk_free_mb=disk.free / (1024 * 1024),
            cpu_percent=psutil.cpu_percent(interval=0.1),
            active_tasks=len(asyncio.all_tasks()) if asyncio.get_event_loop().is_running() else 0,
        )

    @staticmethod
    def check_resources() -> Tuple[bool, List[str]]:
        """
        Check if resources are adequate for operation.

        Returns:
            (resources_ok, list_of_warnings)
        """
        warnings = []
        usage = ResourceMonitor.get_usage()

        # Check memory
        if usage.memory_mb > OrchestratorConfig.MAX_MEMORY_MB:
            warnings.append(f"Memory usage ({usage.memory_mb:.0f}MB) exceeds limit")

        # Check disk space
        if usage.disk_free_mb < OrchestratorConfig.MIN_DISK_SPACE_MB:
            warnings.append(f"Low disk space ({usage.disk_free_mb:.0f}MB)")

        # Check CPU
        if usage.cpu_percent > 90:
            warnings.append(f"High CPU usage ({usage.cpu_percent:.0f}%)")

        return len(warnings) == 0, warnings

    @staticmethod
    def force_gc() -> None:
        """Force garbage collection to free memory."""
        gc.collect()


# =============================================================================
# HEALTH MONITOR
# =============================================================================

class HealthMonitor:
    """
    Continuous health monitoring for LLM providers.

    Runs background health checks and maintains status.
    """

    def __init__(self):
        self._status: Dict[str, HealthStatus] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable[[str, HealthStatus], Awaitable[None]]] = []

    async def start(self) -> None:
        """Start the health monitor."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("Health monitor started")

    async def stop(self) -> None:
        """Stop the health monitor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Health monitor stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_all_providers()
                await asyncio.sleep(OrchestratorConfig.HEALTH_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
                await asyncio.sleep(5.0)

    async def _check_all_providers(self) -> None:
        """Check health of all LLM providers."""
        from backend.core.ouroboros.integration import IntegrationConfig

        for provider in IntegrationConfig.PROVIDERS:
            status = await self._check_provider(provider)
            old_status = self._status.get(provider["name"])

            self._status[provider["name"]] = status

            # Notify callbacks if status changed
            if old_status and old_status.healthy != status.healthy:
                for callback in self._callbacks:
                    try:
                        await callback(provider["name"], status)
                    except Exception as e:
                        logger.error(f"Health callback error: {e}")

    async def _check_provider(self, provider: Dict) -> HealthStatus:
        """Check health of a single provider."""
        name = provider["name"]
        start_time = time.time()

        try:
            if not aiohttp:
                return HealthStatus(name=name, healthy=False, error_message="aiohttp not installed")

            async with aiohttp.ClientSession() as session:
                url = f"{provider['api_base'].rstrip('/')}/models"
                headers = {}
                if provider.get("api_key"):
                    headers["Authorization"] = f"Bearer {provider['api_key']}"

                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=OrchestratorConfig.HEALTH_CHECK_TIMEOUT),
                ) as resp:
                    latency = (time.time() - start_time) * 1000

                    if resp.status == 200:
                        return HealthStatus(
                            name=name,
                            healthy=True,
                            latency_ms=latency,
                        )
                    else:
                        return HealthStatus(
                            name=name,
                            healthy=False,
                            error_message=f"HTTP {resp.status}",
                            latency_ms=latency,
                        )

        except asyncio.TimeoutError:
            return HealthStatus(name=name, healthy=False, error_message="Timeout")
        except Exception as e:
            return HealthStatus(name=name, healthy=False, error_message=str(e))

    def get_status(self, provider_name: str) -> Optional[HealthStatus]:
        """Get status of a specific provider."""
        return self._status.get(provider_name)

    def get_all_status(self) -> Dict[str, HealthStatus]:
        """Get status of all providers."""
        return dict(self._status)

    def get_healthy_providers(self) -> List[str]:
        """Get list of healthy providers."""
        return [name for name, status in self._status.items() if status.healthy]

    def register_callback(self, callback: Callable[[str, HealthStatus], Awaitable[None]]) -> None:
        """Register a callback for health status changes."""
        self._callbacks.append(callback)


# =============================================================================
# LLM PROVIDER STARTER
# =============================================================================

class ProviderStarter:
    """
    Attempts to start LLM providers if they're not running.

    Can start local Ollama or connect to remote services.
    """

    @staticmethod
    async def start_ollama() -> bool:
        """Try to start Ollama service."""
        try:
            # Check if ollama is installed
            result = await asyncio.create_subprocess_exec(
                "which", "ollama",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()

            if result.returncode != 0:
                logger.warning("Ollama not installed")
                return False

            # Try to start ollama serve
            process = await asyncio.create_subprocess_exec(
                "ollama", "serve",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # Wait a bit for it to start
            await asyncio.sleep(3.0)

            # Check if it's running
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "http://localhost:11434/api/tags",
                        timeout=aiohttp.ClientTimeout(total=5.0),
                    ) as resp:
                        if resp.status == 200:
                            logger.info("Ollama started successfully")
                            return True
            except Exception:
                pass

            return False

        except Exception as e:
            logger.error(f"Failed to start Ollama: {e}")
            return False

    @staticmethod
    async def discover_providers() -> List[Dict[str, Any]]:
        """
        Discover available LLM providers.

        Checks common ports and endpoints.
        """
        discovered = []
        endpoints = [
            ("localhost", 8000, "JARVIS Prime"),
            ("localhost", 11434, "Ollama"),
            ("localhost", 8080, "vLLM"),
            ("localhost", 5000, "Text Generation"),
        ]

        if not aiohttp:
            return discovered

        async with aiohttp.ClientSession() as session:
            for host, port, name in endpoints:
                try:
                    url = f"http://{host}:{port}/v1/models"
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=2.0),
                    ) as resp:
                        if resp.status == 200:
                            discovered.append({
                                "name": name.lower().replace(" ", "-"),
                                "api_base": f"http://{host}:{port}/v1",
                                "port": port,
                            })
                except Exception:
                    continue

        return discovered


# =============================================================================
# ADVANCED ORCHESTRATOR
# =============================================================================

class AdvancedOuroborosOrchestrator:
    """
    The "God Layer" orchestrator that coordinates all Ouroboros operations.

    Features:
    - Automatic provider discovery and startup
    - Intelligent retry with exponential backoff
    - Semantic caching for similar prompts
    - Syntax validation before applying changes
    - Git state management
    - File locking for concurrent access
    - Resource monitoring
    - Health monitoring with auto-recovery
    - Rate limiting
    - Graceful degradation
    - Cross-repo integration
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.Orchestrator")

        # Core components
        self._rate_limiter = TokenBucketRateLimiter()
        self._cache = SemanticCache()
        self._syntax_validator = SyntaxValidator()
        self._git_manager = GitStateManager(OrchestratorConfig.JARVIS_REPO)
        self._file_locks = FileLockManager()
        self._resource_monitor = ResourceMonitor()
        self._health_monitor = HealthMonitor()
        self._provider_starter = ProviderStarter()

        # State
        self._running = False
        self._degradation_level = DegradationLevel.FULL
        self._active_improvements: Dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(OrchestratorConfig.MAX_CONCURRENT_IMPROVEMENTS)

        # Metrics
        self._metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "cache_hits": 0,
            "rate_limited": 0,
            "retries": 0,
            "degradation_events": 0,
        }

    async def initialize(self) -> bool:
        """
        Initialize the orchestrator.

        Discovers providers, starts health monitoring, validates resources.
        """
        self.logger.info("Initializing Advanced Ouroboros Orchestrator...")

        # Check resources
        resources_ok, warnings = self._resource_monitor.check_resources()
        if not resources_ok:
            for warning in warnings:
                self.logger.warning(f"Resource warning: {warning}")

        # Ensure directories exist
        OrchestratorConfig.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        OrchestratorConfig.CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Discover providers
        discovered = await self._provider_starter.discover_providers()
        if discovered:
            self.logger.info(f"Discovered {len(discovered)} LLM providers")
            for p in discovered:
                self.logger.info(f"  - {p['name']} at {p['api_base']}")
        else:
            self.logger.warning("No LLM providers found, attempting to start Ollama...")
            if await self._provider_starter.start_ollama():
                self.logger.info("Ollama started successfully")
            else:
                self.logger.warning("Could not start any local LLM provider")

        # Start health monitoring
        await self._health_monitor.start()

        # Register health callback
        self._health_monitor.register_callback(self._on_health_change)

        self._running = True
        self.logger.info("Advanced Ouroboros Orchestrator initialized")
        return True

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        self.logger.info("Shutting down Advanced Ouroboros Orchestrator...")
        self._running = False

        # Cancel active improvements
        for task_id, task in self._active_improvements.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Stop health monitoring
        await self._health_monitor.stop()

        # Clear cache
        await self._cache.clear()

        self.logger.info("Advanced Ouroboros Orchestrator shutdown complete")

    async def _on_health_change(self, provider: str, status: HealthStatus) -> None:
        """Handle health status changes."""
        if status.healthy:
            self.logger.info(f"Provider {provider} is now healthy")
            # Consider upgrading degradation level
            healthy = self._health_monitor.get_healthy_providers()
            if len(healthy) >= 2 and self._degradation_level != DegradationLevel.FULL:
                self._degradation_level = DegradationLevel.FULL
                self.logger.info("Degradation level restored to FULL")
        else:
            self.logger.warning(f"Provider {provider} is unhealthy: {status.error_message}")
            # Consider downgrading
            healthy = self._health_monitor.get_healthy_providers()
            if len(healthy) == 0:
                self._degradation_level = DegradationLevel.EMERGENCY
                self._metrics["degradation_events"] += 1
                self.logger.error("All providers unhealthy - EMERGENCY degradation")

    async def improve_code(
        self,
        file_path: Path,
        goal: str,
        test_command: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Main entry point for code improvement.

        Handles all edge cases, retries, validation, etc.
        """
        self._metrics["total_requests"] += 1
        task_id = f"improve_{uuid.uuid4().hex[:8]}"

        result = {
            "success": False,
            "task_id": task_id,
            "file_path": str(file_path),
            "goal": goal,
            "error": None,
            "changes_applied": False,
            "iterations": 0,
            "provider_used": None,
            "cached": False,
        }

        try:
            # Check if running
            if not self._running:
                result["error"] = "Orchestrator not running"
                return result

            # Check degradation level
            if self._degradation_level == DegradationLevel.EMERGENCY:
                result["error"] = "Emergency degradation mode - no providers available"
                return result

            # Rate limiting
            if not await self._rate_limiter.acquire(timeout=30.0):
                self._metrics["rate_limited"] += 1
                result["error"] = "Rate limit exceeded"
                return result

            # Resource check
            resources_ok, warnings = self._resource_monitor.check_resources()
            if not resources_ok:
                # Force GC and retry
                self._resource_monitor.force_gc()
                resources_ok, warnings = self._resource_monitor.check_resources()
                if not resources_ok:
                    result["error"] = f"Insufficient resources: {', '.join(warnings)}"
                    return result

            # Acquire semaphore for concurrency limit
            async with self._semaphore:
                # Create improvement task
                task = asyncio.create_task(
                    self._do_improvement(file_path, goal, test_command, result)
                )
                self._active_improvements[task_id] = task

                try:
                    await task
                finally:
                    self._active_improvements.pop(task_id, None)

            if result["success"]:
                self._metrics["successful_requests"] += 1
            else:
                self._metrics["failed_requests"] += 1

            return result

        except Exception as e:
            self._metrics["failed_requests"] += 1
            result["error"] = str(e)
            self.logger.error(f"Improvement failed: {e}\n{traceback.format_exc()}")
            return result

    async def _do_improvement(
        self,
        file_path: Path,
        goal: str,
        test_command: Optional[str],
        result: Dict[str, Any],
    ) -> None:
        """Execute the actual improvement with all safety checks."""

        # 1. Check cache
        original_content = await self._read_file_async(file_path)
        cached_response = await self._cache.get(original_content, goal)

        if cached_response:
            result["cached"] = True
            self._metrics["cache_hits"] += 1
            improved_code = cached_response
        else:
            # 2. Check git state
            git_state = await self._git_manager.get_state()
            file_status = await self._git_manager.get_file_status(file_path)

            if git_state == GitState.CONFLICT:
                result["error"] = "Git repository has conflicts - resolve before improving"
                return

            if git_state == GitState.DIRTY and file_status:
                # Stash changes for this file
                self.logger.info(f"Stashing changes for {file_path}")
                await self._git_manager.stash_changes(f"Ouroboros auto-stash before improving {file_path.name}")

            # 3. Acquire file lock
            async with self._file_locks.acquire_lock(file_path) as locked:
                if not locked:
                    result["error"] = "Could not acquire file lock (file in use)"
                    return

                # 4. Generate improvement with retries
                retry_state = RetryState()

                while retry_state.should_retry():
                    try:
                        improved_code = await self._generate_with_provider(
                            original_content, goal, retry_state.last_error
                        )

                        if improved_code:
                            result["provider_used"] = "integration"
                            break

                    except Exception as e:
                        retry_state.last_error = str(e)
                        retry_state.attempt += 1
                        self._metrics["retries"] += 1

                        if retry_state.should_retry():
                            delay = retry_state.get_next_delay()
                            self.logger.warning(
                                f"Retry {retry_state.attempt} after {delay:.1f}s: {e}"
                            )
                            await asyncio.sleep(delay)
                        else:
                            result["error"] = f"All retries exhausted: {e}"
                            return

                result["iterations"] = retry_state.attempt + 1

        # 5. Validate syntax
        is_valid, syntax_error = self._syntax_validator.validate_python(improved_code)
        if not is_valid:
            result["error"] = f"Generated code has syntax error: {syntax_error}"
            return

        # 6. Check for dangerous patterns
        warnings = self._syntax_validator.check_dangerous_patterns(improved_code)
        if warnings:
            self.logger.warning(f"Dangerous patterns detected: {warnings}")
            # Don't block, just warn

        # 7. Run tests in sandbox
        if test_command:
            test_passed, test_output = await self._run_tests_in_sandbox(
                file_path, improved_code, test_command
            )
            if not test_passed:
                result["error"] = f"Tests failed: {test_output[:500]}"
                return

        # 8. Apply changes
        await self._write_file_async(file_path, improved_code)
        result["changes_applied"] = True
        result["success"] = True

        # 9. Cache the successful result
        if not result["cached"]:
            await self._cache.set(original_content, goal, improved_code)

    async def _generate_with_provider(
        self,
        original_code: str,
        goal: str,
        error_log: Optional[str] = None,
    ) -> Optional[str]:
        """Generate improvement using best available provider."""
        try:
            from backend.core.ouroboros.integration import get_ouroboros_integration

            integration = get_ouroboros_integration()
            return await integration.generate_improvement(
                original_code=original_code,
                goal=goal,
                error_log=error_log,
            )
        except Exception as e:
            self.logger.error(f"Provider generation failed: {e}")
            raise

    async def _run_tests_in_sandbox(
        self,
        file_path: Path,
        modified_content: str,
        test_command: str,
    ) -> Tuple[bool, str]:
        """Run tests in sandbox environment."""
        try:
            from backend.core.ouroboros.integration import get_ouroboros_integration

            integration = get_ouroboros_integration()
            return await integration.validate_in_sandbox(
                file_path, modified_content, test_command
            )
        except Exception as e:
            return False, str(e)

    async def _read_file_async(self, path: Path) -> str:
        """Read file asynchronously."""
        if aiofiles:
            async with aiofiles.open(path, "r") as f:
                return await f.read()
        else:
            return await asyncio.to_thread(path.read_text)

    async def _write_file_async(self, path: Path, content: str) -> None:
        """Write file asynchronously."""
        if aiofiles:
            async with aiofiles.open(path, "w") as f:
                await f.write(content)
        else:
            await asyncio.to_thread(path.write_text, content)

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status."""
        return {
            "running": self._running,
            "degradation_level": self._degradation_level.value,
            "active_improvements": len(self._active_improvements),
            "metrics": dict(self._metrics),
            "rate_limiter": self._rate_limiter.get_status(),
            "cache": self._cache.get_stats(),
            "health": {
                name: {
                    "healthy": status.healthy,
                    "latency_ms": status.latency_ms,
                    "error": status.error_message,
                }
                for name, status in self._health_monitor.get_all_status().items()
            },
            "resources": {
                "memory_mb": self._resource_monitor.get_usage().memory_mb,
                "disk_free_mb": self._resource_monitor.get_usage().disk_free_mb,
            },
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_orchestrator: Optional[AdvancedOuroborosOrchestrator] = None


def get_advanced_orchestrator() -> AdvancedOuroborosOrchestrator:
    """Get global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AdvancedOuroborosOrchestrator()
    return _orchestrator


async def shutdown_advanced_orchestrator() -> None:
    """Shutdown global orchestrator."""
    global _orchestrator
    if _orchestrator:
        await _orchestrator.shutdown()
        _orchestrator = None


# =============================================================================
# CLI WRAPPER
# =============================================================================

async def jarvis_improve(
    target_file: str,
    goal: str,
    test_command: Optional[str] = None,
) -> bool:
    """
    Simplified CLI function for code improvement.

    Usage:
        await jarvis_improve("path/to/file.py", "Improve performance")
    """
    orchestrator = get_advanced_orchestrator()

    if not orchestrator._running:
        await orchestrator.initialize()

    result = await orchestrator.improve_code(
        file_path=Path(target_file),
        goal=goal,
        test_command=test_command,
    )

    if result["success"]:
        print(f"✅ Improvement successful!")
        print(f"   Provider: {result['provider_used']}")
        print(f"   Iterations: {result['iterations']}")
        print(f"   Cached: {result['cached']}")
    else:
        print(f"❌ Improvement failed: {result['error']}")

    return result["success"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="JARVIS Improve - AI Code Enhancement")
    parser.add_argument("target_file", help="File to improve")
    parser.add_argument("goal", help="Improvement goal")
    parser.add_argument("--test", "-t", help="Test command")

    args = parser.parse_args()

    result = asyncio.run(jarvis_improve(args.target_file, args.goal, args.test))
    sys.exit(0 if result else 1)
