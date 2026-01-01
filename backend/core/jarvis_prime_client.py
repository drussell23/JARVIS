"""
JARVIS-Prime Intelligent Client - Memory-Aware Hybrid Routing
==============================================================

A sophisticated client for JARVIS-Prime (Tier-0 Brain) with:
- Memory-aware routing (local vs Cloud Run)
- Multi-tier fallback chain (Local → Cloud Run → Gemini API)
- Circuit breaker pattern for resilience
- Connection pooling and async operations
- Dynamic threshold adjustment based on system state
- Real-time telemetry and cost tracking

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │              JarvisPrimeClient (Memory-Aware)                 │
    │  ┌────────────────────────────────────────────────────────┐  │
    │  │                Memory Pressure Monitor                  │  │
    │  │  RAM > 8GB → LOCAL    RAM < 8GB → CLOUD    < 4GB → API │  │
    │  └─────────────────────────┬──────────────────────────────┘  │
    │                            │                                  │
    │  ┌─────────────┐   ┌──────┴─────┐   ┌──────────────────┐    │
    │  │   Local     │   │  Cloud Run │   │   Gemini API     │    │
    │  │ (Port 8002) │→→→│  (GCR URL) │→→→│  (Fallback)      │    │
    │  └─────────────┘   └────────────┘   └──────────────────┘    │
    │         ↓                ↓                   ↓               │
    │  ┌──────────────────────────────────────────────────────────┐│
    │  │              CircuitBreaker + Retry Logic                ││
    │  └──────────────────────────────────────────────────────────┘│
    └──────────────────────────────────────────────────────────────┘

Version: 1.0.0
Author: JARVIS AI System
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Callable, Awaitable

logger = logging.getLogger(__name__)


# =============================================================================
# Repo Map Enricher - Intelligent Context Injection for Coding Questions
# =============================================================================

class CodingQuestionDetector:
    """
    Intelligently detects if a prompt is a coding-related question.

    Uses multiple signals:
    - Keyword patterns (implement, fix, refactor, debug, etc.)
    - File references (.py, .ts, .js mentions)
    - Code symbol mentions (class names, function names)
    - Question structure analysis
    """

    # Coding-related keywords (weighted by relevance)
    CODING_KEYWORDS: Dict[str, float] = {
        # High relevance (0.8+)
        "implement": 0.9, "code": 0.85, "function": 0.85, "class": 0.85,
        "method": 0.85, "debug": 0.9, "fix": 0.8, "bug": 0.85, "error": 0.8,
        "refactor": 0.9, "optimize": 0.8, "test": 0.75, "api": 0.8,

        # Medium relevance (0.5-0.8)
        "variable": 0.7, "import": 0.7, "module": 0.7, "package": 0.65,
        "type": 0.6, "return": 0.7, "async": 0.8, "await": 0.75,
        "database": 0.6, "query": 0.55, "endpoint": 0.7, "route": 0.65,

        # Lower relevance (0.3-0.5)
        "file": 0.5, "folder": 0.4, "directory": 0.4, "project": 0.45,
        "architecture": 0.55, "pattern": 0.5, "structure": 0.45,
    }

    # File extension patterns
    FILE_PATTERNS = re.compile(
        r'\b[\w/-]+\.(py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|hpp|rb|swift|kt|json|yaml|yml|toml|sql)\b',
        re.IGNORECASE
    )

    # Code symbol patterns (CamelCase, snake_case, SCREAMING_SNAKE)
    SYMBOL_PATTERNS = [
        re.compile(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b'),  # CamelCase
        re.compile(r'\b[a-z]+(?:_[a-z]+)+\b'),  # snake_case
        re.compile(r'\b[A-Z]+(?:_[A-Z]+)+\b'),  # SCREAMING_SNAKE
        re.compile(r'\b(?:get|set|is|has|can|should|will|on|handle)_?\w+\b', re.IGNORECASE),  # Common function prefixes
    ]

    # Repo-specific patterns (detect which repos are relevant)
    REPO_PATTERNS: Dict[str, List[str]] = {
        "jarvis": [
            "jarvis", "voice", "unlock", "screen", "assistant", "backend",
            "computer_use", "vision", "audio", "supervisor", "transport",
        ],
        "jarvis_prime": [
            "prime", "orchestrat", "workflow", "llm", "gemini", "inference",
            "routing", "cloud_run", "tier-0", "brain",
        ],
        "reactor_core": [
            "reactor", "training", "learning", "experience", "dataset",
            "fine-tune", "lora", "scraping", "safe_scout",
        ],
    }

    def __init__(self):
        self._symbol_cache: Dict[str, Set[str]] = {}

    def detect(self, prompt: str) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Detect if the prompt is a coding question.

        Returns:
            Tuple of (is_coding_question, confidence, metadata)
        """
        prompt_lower = prompt.lower()
        signals = []
        metadata: Dict[str, Any] = {
            "mentioned_files": [],
            "mentioned_symbols": [],
            "relevant_repos": [],
            "detected_keywords": [],
        }

        # 1. Keyword detection
        keyword_score = 0.0
        detected_keywords = []
        for keyword, weight in self.CODING_KEYWORDS.items():
            if keyword in prompt_lower:
                keyword_score += weight
                detected_keywords.append(keyword)

        if keyword_score > 0:
            signals.append(min(keyword_score / 3.0, 1.0))  # Normalize
            metadata["detected_keywords"] = detected_keywords

        # 2. File reference detection
        file_matches = self.FILE_PATTERNS.findall(prompt)
        if file_matches:
            signals.append(0.9)  # High confidence if files mentioned
            # Extract full file paths
            file_paths = re.findall(r'[\w/-]+\.(?:py|ts|tsx|js|jsx|go|rs|java)', prompt, re.IGNORECASE)
            metadata["mentioned_files"] = list(set(file_paths))

        # 3. Symbol detection
        symbols = set()
        for pattern in self.SYMBOL_PATTERNS:
            matches = pattern.findall(prompt)
            symbols.update(m for m in matches if len(m) > 2 and m.lower() not in ["the", "and", "for"])

        if symbols:
            signals.append(0.7 * min(len(symbols) / 5.0, 1.0))  # More symbols = higher confidence
            metadata["mentioned_symbols"] = list(symbols)[:20]  # Limit to 20

        # 4. Repo relevance detection
        for repo_name, patterns in self.REPO_PATTERNS.items():
            if any(p in prompt_lower for p in patterns):
                metadata["relevant_repos"].append(repo_name)

        if metadata["relevant_repos"]:
            signals.append(0.6)

        # 5. Question structure (asking "how to", "where is", "why does", etc.)
        question_patterns = [
            r'\bhow\s+(?:do|can|should|to)\b',
            r'\bwhere\s+(?:is|are|does|do)\b',
            r'\bwhy\s+(?:is|does|do|doesn\'t)\b',
            r'\bwhat\s+(?:is|does|are)\b',
            r'\bcan\s+you\s+(?:show|explain|implement|fix|add)\b',
        ]
        for pattern in question_patterns:
            if re.search(pattern, prompt_lower):
                signals.append(0.5)
                break

        # Calculate final confidence
        if not signals:
            return False, 0.0, metadata

        confidence = min(sum(signals) / len(signals) + (len(signals) * 0.1), 1.0)
        is_coding = confidence >= 0.4  # Threshold

        return is_coding, confidence, metadata


class RepoMapEnricher:
    """
    Enriches prompts with repository context for coding questions.

    Features:
    - Intelligent coding question detection
    - Dynamic repo map generation with PageRank prioritization
    - Cross-repo context for multi-repo questions
    - Caching for performance
    - Token-aware truncation
    """

    def __init__(
        self,
        max_context_tokens: int = 2000,
        cache_ttl_seconds: int = 300,
        enable_cross_repo: bool = True,
    ):
        self.max_context_tokens = max_context_tokens
        self.cache_ttl_seconds = cache_ttl_seconds
        self.enable_cross_repo = enable_cross_repo

        self._detector = CodingQuestionDetector()
        self._mapper = None
        self._mapper_lock = asyncio.Lock()
        self._cache: Dict[str, Tuple[str, float]] = {}  # key -> (content, timestamp)
        self._initialized = False

    async def _get_mapper(self):
        """Lazy-load the repository mapper."""
        async with self._mapper_lock:
            if self._mapper is None:
                try:
                    from backend.intelligence.repository_intelligence import get_repository_mapper
                    self._mapper = await get_repository_mapper()
                    self._initialized = True
                    logger.info("[RepoMapEnricher] Repository mapper initialized")
                except ImportError as e:
                    logger.warning(f"[RepoMapEnricher] Repository intelligence not available: {e}")
                except Exception as e:
                    logger.error(f"[RepoMapEnricher] Failed to initialize mapper: {e}")
        return self._mapper

    def _get_cache_key(
        self,
        repos: List[str],
        files: List[str],
        symbols: List[str],
    ) -> str:
        """Generate a cache key for repo map queries."""
        import hashlib
        key_data = f"{sorted(repos)}:{sorted(files)}:{sorted(symbols)}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:16]

    def _check_cache(self, cache_key: str) -> Optional[str]:
        """Check cache for existing repo map."""
        if cache_key in self._cache:
            content, timestamp = self._cache[cache_key]
            if time.time() - timestamp < self.cache_ttl_seconds:
                return content
            else:
                del self._cache[cache_key]
        return None

    async def enrich_prompt(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        force_enrich: bool = False,
    ) -> Tuple[Optional[str], str, Dict[str, Any]]:
        """
        Enrich a prompt with repository context if it's a coding question.

        Args:
            prompt: The user's prompt
            system_prompt: Existing system prompt (if any)
            force_enrich: Force enrichment even if not detected as coding question

        Returns:
            Tuple of (enriched_system_prompt, original_prompt, metadata)
        """
        # Detect if this is a coding question
        is_coding, confidence, metadata = self._detector.detect(prompt)
        metadata["is_coding_question"] = is_coding
        metadata["coding_confidence"] = confidence

        if not is_coding and not force_enrich:
            return system_prompt, prompt, metadata

        # Get the mapper
        mapper = await self._get_mapper()
        if not mapper:
            metadata["repo_map_error"] = "Mapper not available"
            return system_prompt, prompt, metadata

        # Determine which repos to query
        relevant_repos = metadata.get("relevant_repos", [])
        if not relevant_repos:
            relevant_repos = ["jarvis"]  # Default to JARVIS

        if self.enable_cross_repo and len(relevant_repos) < 3:
            # Add adjacent repos for cross-repo context
            if "jarvis_prime" not in relevant_repos:
                relevant_repos.append("jarvis_prime")

        # Check cache
        cache_key = self._get_cache_key(
            relevant_repos,
            metadata.get("mentioned_files", []),
            metadata.get("mentioned_symbols", []),
        )
        cached_map = self._check_cache(cache_key)

        if cached_map:
            metadata["cache_hit"] = True
            repo_context = cached_map
        else:
            # Generate repo map
            try:
                repo_context = await self._generate_repo_context(
                    repos=relevant_repos,
                    mentioned_files=set(metadata.get("mentioned_files", [])),
                    mentioned_symbols=set(metadata.get("mentioned_symbols", [])),
                )
                # Cache the result
                self._cache[cache_key] = (repo_context, time.time())
                metadata["cache_hit"] = False
            except Exception as e:
                logger.error(f"[RepoMapEnricher] Failed to generate context: {e}")
                metadata["repo_map_error"] = str(e)
                return system_prompt, prompt, metadata

        # Build enriched system prompt
        enriched_system = self._build_enriched_system_prompt(
            system_prompt,
            repo_context,
            metadata,
        )

        metadata["enriched"] = True
        metadata["context_tokens_estimated"] = len(repo_context) // 4

        return enriched_system, prompt, metadata

    async def _generate_repo_context(
        self,
        repos: List[str],
        mentioned_files: Set[str],
        mentioned_symbols: Set[str],
    ) -> str:
        """Generate repository context from repo maps."""
        if not self._mapper:
            return ""

        tokens_per_repo = self.max_context_tokens // len(repos) if repos else self.max_context_tokens
        context_parts = []

        for repo in repos:
            try:
                result = await self._mapper.get_repo_map(
                    repository=repo,
                    max_tokens=tokens_per_repo,
                    mentioned_files=mentioned_files,
                    mentioned_symbols=mentioned_symbols,
                )

                if result and result.map_content:
                    header = f"## {repo.replace('_', ' ').title()} Codebase"
                    context_parts.append(f"{header}\n```\n{result.map_content}\n```")
            except Exception as e:
                logger.debug(f"[RepoMapEnricher] Failed to get map for {repo}: {e}")

        return "\n\n".join(context_parts)

    def _build_enriched_system_prompt(
        self,
        original_system: Optional[str],
        repo_context: str,
        metadata: Dict[str, Any],
    ) -> str:
        """Build the enriched system prompt with repo context."""
        parts = []

        # Add original system prompt if present
        if original_system:
            parts.append(original_system)

        # Add repository context section
        if repo_context:
            context_intro = """
# Codebase Context

You have access to the following codebase structure. Use this to understand:
- Where relevant code lives
- Important symbols (classes, functions) and their locations
- File organization and architecture

"""
            parts.append(context_intro + repo_context)

        # Add detected context hints
        if metadata.get("mentioned_symbols"):
            symbols_hint = f"\n**Mentioned symbols to focus on:** {', '.join(metadata['mentioned_symbols'][:10])}"
            parts.append(symbols_hint)

        if metadata.get("mentioned_files"):
            files_hint = f"\n**Files referenced:** {', '.join(metadata['mentioned_files'][:5])}"
            parts.append(files_hint)

        return "\n\n".join(parts)

    @property
    def is_available(self) -> bool:
        """Check if the enricher is available."""
        return self._initialized and self._mapper is not None


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class JarvisPrimeConfig:
    """Configuration for the JARVIS-Prime client."""

    # Memory thresholds (configurable via env vars)
    memory_threshold_local_gb: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_MEMORY_THRESHOLD_GB", "8.0"))
    )
    memory_threshold_cloud_gb: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_MEMORY_THRESHOLD_CLOUD_GB", "4.0"))
    )

    # Local JARVIS-Prime settings
    local_host: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_HOST", "127.0.0.1")
    )
    local_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8002"))
    )

    # Cloud Run settings
    cloud_run_url: str = field(
        default_factory=lambda: os.getenv(
            "JARVIS_PRIME_CLOUD_RUN_URL",
            "https://jarvis-prime-dev-888774109345.us-central1.run.app"
        )
    )
    use_cloud_run: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_USE_CLOUD_RUN", "true").lower() == "true"
    )

    # Gemini fallback settings
    gemini_api_key: str = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )
    gemini_model: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_GEMINI_MODEL", "gemini-1.5-flash")
    )
    use_gemini_fallback: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_GEMINI_FALLBACK", "true").lower() == "true"
    )

    # Timeouts
    local_timeout_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_LOCAL_TIMEOUT_MS", "5000"))
    )
    cloud_timeout_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_CLOUD_TIMEOUT_MS", "30000"))
    )
    api_timeout_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_API_TIMEOUT_MS", "10000"))
    )

    # Circuit breaker settings
    circuit_breaker_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_CB_THRESHOLD", "3"))
    )
    circuit_breaker_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_CB_TIMEOUT_S", "60"))
    )

    # Retry settings
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_MAX_RETRIES", "2"))
    )
    retry_delay_ms: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_RETRY_DELAY_MS", "500"))
    )

    # Force mode (for testing/override)
    force_mode: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_FORCE_MODE", None)
    )

    # Repo Map Enrichment settings
    enable_repo_map_enrichment: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLE_REPO_MAP", "true").lower() == "true"
    )
    repo_map_max_tokens: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_REPO_MAP_MAX_TOKENS", "2000"))
    )
    repo_map_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_REPO_MAP_CACHE_TTL", "300"))
    )
    enable_cross_repo_context: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_CROSS_REPO_CONTEXT", "true").lower() == "true"
    )


class RoutingMode(str, Enum):
    """Routing mode for JARVIS-Prime requests."""
    LOCAL = "local"           # Local subprocess (free, fast)
    CLOUD_RUN = "cloud_run"   # Cloud Run (pay-per-use)
    GEMINI_API = "gemini_api" # Gemini API fallback (cheapest)
    DISABLED = "disabled"     # All backends unavailable


class CircuitState(str, Enum):
    """Circuit breaker state."""
    CLOSED = "closed"     # Normal operation
    OPEN = "open"         # Failing, skip calls
    HALF_OPEN = "half_open"  # Testing recovery


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ChatMessage:
    """A chat message."""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class CompletionResponse:
    """Response from a completion request."""
    success: bool
    content: Optional[str] = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    backend: str = "unknown"
    tokens_used: int = 0
    cost_estimate: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthStatus:
    """Health status of a backend."""
    available: bool
    latency_ms: float = 0.0
    model_loaded: bool = False
    error: Optional[str] = None
    last_check: float = field(default_factory=time.time)


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitBreaker:
    """
    Circuit breaker pattern for backend resilience.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, skip requests
    - HALF_OPEN: Testing if backend recovered
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        timeout_seconds: float = 60,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._success_count = 0

    @property
    def state(self) -> CircuitState:
        """Get current state, checking for timeout."""
        if self._state == CircuitState.OPEN:
            if self._last_failure_time and \
               (time.time() - self._last_failure_time) > self.timeout_seconds:
                logger.info(f"[CircuitBreaker:{self.name}] Transitioning OPEN → HALF_OPEN")
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
        return self._state

    def allow_request(self) -> bool:
        """Check if request should be allowed."""
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        elif state == CircuitState.HALF_OPEN:
            return True  # Allow test request
        else:  # OPEN
            return False

    def record_success(self):
        """Record a successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= 2:  # 2 successes to close
                logger.info(f"[CircuitBreaker:{self.name}] Transitioning HALF_OPEN → CLOSED")
                self._state = CircuitState.CLOSED
                self._failure_count = 0
        else:
            self._failure_count = 0

    def record_failure(self):
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            logger.warning(f"[CircuitBreaker:{self.name}] Transitioning HALF_OPEN → OPEN")
            self._state = CircuitState.OPEN
        elif self._failure_count >= self.failure_threshold:
            logger.warning(
                f"[CircuitBreaker:{self.name}] Transitioning CLOSED → OPEN "
                f"(failures: {self._failure_count})"
            )
            self._state = CircuitState.OPEN


# =============================================================================
# Memory Monitor
# =============================================================================

class MemoryMonitor:
    """Monitors system memory for routing decisions."""

    def __init__(self):
        self._psutil = None
        self._last_check: Optional[float] = None
        self._cached_available_gb: float = 16.0  # Assume plenty
        self._cache_ttl_seconds = 5.0

    def _get_psutil(self):
        """Lazy load psutil."""
        if self._psutil is None:
            try:
                import psutil
                self._psutil = psutil
            except ImportError:
                logger.warning("[MemoryMonitor] psutil not available")
        return self._psutil

    def get_available_gb(self) -> float:
        """Get available memory in GB with caching."""
        now = time.time()

        # Use cache if fresh
        if self._last_check and (now - self._last_check) < self._cache_ttl_seconds:
            return self._cached_available_gb

        psutil = self._get_psutil()
        if psutil is None:
            return 16.0  # Optimistic default

        try:
            mem = psutil.virtual_memory()
            self._cached_available_gb = mem.available / (1024 ** 3)
            self._last_check = now
            return self._cached_available_gb
        except Exception as e:
            logger.debug(f"[MemoryMonitor] Error: {e}")
            return self._cached_available_gb

    def get_pressure_percent(self) -> float:
        """Get memory pressure as percentage (0-100)."""
        psutil = self._get_psutil()
        if psutil is None:
            return 0.0

        try:
            mem = psutil.virtual_memory()
            return mem.percent
        except Exception:
            return 0.0


# =============================================================================
# JARVIS-Prime Client
# =============================================================================

class JarvisPrimeClient:
    """
    Intelligent client for JARVIS-Prime with memory-aware routing.

    Features:
    - Automatic mode selection based on available RAM
    - Multi-tier fallback (Local → Cloud Run → Gemini)
    - Circuit breaker for each backend
    - Connection pooling and async operations
    - Cost tracking and telemetry
    - Dynamic memory monitoring with mode switching
    """

    def __init__(self, config: Optional[JarvisPrimeConfig] = None):
        self.config = config or JarvisPrimeConfig()

        # Memory monitor
        self._memory_monitor = MemoryMonitor()

        # Circuit breakers for each backend
        self._circuit_breakers = {
            RoutingMode.LOCAL: CircuitBreaker(
                "local",
                failure_threshold=self.config.circuit_breaker_threshold,
                timeout_seconds=self.config.circuit_breaker_timeout_s,
            ),
            RoutingMode.CLOUD_RUN: CircuitBreaker(
                "cloud_run",
                failure_threshold=self.config.circuit_breaker_threshold,
                timeout_seconds=self.config.circuit_breaker_timeout_s,
            ),
            RoutingMode.GEMINI_API: CircuitBreaker(
                "gemini_api",
                failure_threshold=self.config.circuit_breaker_threshold,
                timeout_seconds=self.config.circuit_breaker_timeout_s * 2,  # Longer for API
            ),
        }

        # Health status cache
        self._health_cache: Dict[RoutingMode, HealthStatus] = {}
        self._health_cache_ttl = 30.0  # seconds

        # HTTP client (lazy loaded)
        self._http_client = None

        # Stats
        self._request_count = 0
        self._local_count = 0
        self._cloud_count = 0
        self._api_count = 0
        self._fallback_count = 0
        self._total_cost = 0.0

        # Dynamic monitoring
        self._current_mode: Optional[RoutingMode] = None
        self._mode_change_callbacks: List[Callable[[RoutingMode, RoutingMode, str], Awaitable[None]]] = []
        self._monitoring_task: Optional[asyncio.Task] = None
        self._monitoring_interval_seconds = float(os.getenv("JARVIS_PRIME_MONITOR_INTERVAL", "30"))
        self._shutdown_event = asyncio.Event()

        # Repo Map Enricher for coding questions
        self._repo_enricher: Optional[RepoMapEnricher] = None
        if self.config.enable_repo_map_enrichment:
            self._repo_enricher = RepoMapEnricher(
                max_context_tokens=self.config.repo_map_max_tokens,
                cache_ttl_seconds=self.config.repo_map_cache_ttl_seconds,
                enable_cross_repo=self.config.enable_cross_repo_context,
            )

        logger.info(
            f"[JarvisPrimeClient] Initialized with thresholds: "
            f"local>{self.config.memory_threshold_local_gb}GB, "
            f"cloud>{self.config.memory_threshold_cloud_gb}GB, "
            f"repo_map={'enabled' if self._repo_enricher else 'disabled'}"
        )

    async def _get_http_client(self):
        """Lazy load HTTP client with registry registration."""
        if self._http_client is None:
            try:
                import httpx
                self._http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=10.0),
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                )
                # Register with HTTP client registry for proper cleanup on shutdown
                try:
                    from core.thread_manager import register_http_client
                    register_http_client(
                        self._http_client,
                        name="JarvisPrimeClient",
                        owner="core.jarvis_prime_client"
                    )
                except ImportError:
                    pass  # Registry not available
            except ImportError:
                logger.warning("[JarvisPrimeClient] httpx not available")
        return self._http_client

    # =========================================================================
    # Mode Selection
    # =========================================================================

    def decide_mode(self) -> Tuple[RoutingMode, str]:
        """
        Decide which routing mode to use based on memory and availability.

        Priority Order:
        1. Local (RAM >= 8GB) - FREE, fastest
        2. Cloud Run (always available if configured) - ~$0.02/request
        3. Gemini API (if API key configured) - cheapest fallback

        Returns:
            Tuple of (mode, reason)
        """
        # Check for forced mode (testing/override)
        if self.config.force_mode:
            mode = RoutingMode(self.config.force_mode)
            return mode, f"Forced mode: {mode.value}"

        available_gb = self._memory_monitor.get_available_gb()

        # Decision logic
        # Priority 1: Local if we have enough RAM
        if available_gb >= self.config.memory_threshold_local_gb:
            cb = self._circuit_breakers[RoutingMode.LOCAL]
            if cb.allow_request():
                return RoutingMode.LOCAL, f"Sufficient RAM ({available_gb:.1f}GB >= {self.config.memory_threshold_local_gb}GB)"
            else:
                logger.info(f"[JarvisPrimeClient] Local circuit breaker OPEN, trying Cloud Run")

        # Priority 2: Cloud Run (lightweight HTTP call, works even with low RAM)
        # Cloud Run is always available regardless of memory since it's just an HTTP call
        if self.config.use_cloud_run and self.config.cloud_run_url:
            cb = self._circuit_breakers[RoutingMode.CLOUD_RUN]
            if cb.allow_request():
                if available_gb < self.config.memory_threshold_local_gb:
                    return RoutingMode.CLOUD_RUN, f"Low RAM ({available_gb:.1f}GB) - using Cloud Run"
                else:
                    return RoutingMode.CLOUD_RUN, f"Cloud Run available (RAM: {available_gb:.1f}GB)"
            else:
                logger.info(f"[JarvisPrimeClient] Cloud Run circuit breaker OPEN, trying Gemini")

        # Priority 3: Gemini API (if configured)
        if self.config.use_gemini_fallback and self.config.gemini_api_key:
            cb = self._circuit_breakers[RoutingMode.GEMINI_API]
            if cb.allow_request():
                return RoutingMode.GEMINI_API, f"Using Gemini API fallback (RAM: {available_gb:.1f}GB)"
            else:
                logger.info(f"[JarvisPrimeClient] Gemini circuit breaker OPEN")

        # Priority 4: Try local anyway (might work with some memory pressure)
        if available_gb >= 2.0:  # Minimum 2GB for any operation
            cb = self._circuit_breakers[RoutingMode.LOCAL]
            if cb.allow_request():
                return RoutingMode.LOCAL, f"Attempting local with low RAM ({available_gb:.1f}GB)"

        return RoutingMode.DISABLED, f"All backends unavailable (RAM: {available_gb:.1f}GB)"

    # =========================================================================
    # Health Checks
    # =========================================================================

    async def check_health(self, mode: RoutingMode) -> HealthStatus:
        """Check health of a specific backend."""
        now = time.time()

        # Use cache if fresh
        if mode in self._health_cache:
            cached = self._health_cache[mode]
            if (now - cached.last_check) < self._health_cache_ttl:
                return cached

        if mode == RoutingMode.LOCAL:
            status = await self._check_local_health()
        elif mode == RoutingMode.CLOUD_RUN:
            status = await self._check_cloud_run_health()
        elif mode == RoutingMode.GEMINI_API:
            status = await self._check_gemini_health()
        else:
            status = HealthStatus(available=False, error="Unknown mode")

        self._health_cache[mode] = status
        return status

    async def _check_local_health(self) -> HealthStatus:
        """Check local JARVIS-Prime health."""
        client = await self._get_http_client()
        if client is None:
            return HealthStatus(available=False, error="HTTP client not available")

        url = f"http://{self.config.local_host}:{self.config.local_port}/health"
        start = time.time()

        try:
            resp = await client.get(url, timeout=5.0)
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                return HealthStatus(
                    available=True,
                    latency_ms=latency,
                    model_loaded=data.get("model_loaded", False),
                )
            else:
                return HealthStatus(
                    available=False,
                    error=f"HTTP {resp.status_code}",
                )
        except Exception as e:
            return HealthStatus(available=False, error=str(e))

    async def _check_cloud_run_health(self) -> HealthStatus:
        """Check Cloud Run JARVIS-Prime health."""
        if not self.config.cloud_run_url:
            return HealthStatus(available=False, error="Cloud Run URL not configured")

        client = await self._get_http_client()
        if client is None:
            return HealthStatus(available=False, error="HTTP client not available")

        url = f"{self.config.cloud_run_url}/health"
        start = time.time()

        try:
            resp = await client.get(url, timeout=30.0)  # Longer for cold start
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                # Service is available if it responds with 200
                # Model might not be loaded, but we can still route to it
                return HealthStatus(
                    available=True,
                    latency_ms=latency,
                    model_loaded=data.get("model_loaded", False),
                )
            else:
                return HealthStatus(available=False, error=f"HTTP {resp.status_code}")
        except asyncio.TimeoutError:
            return HealthStatus(available=False, error="Connection timeout (cold start?)")
        except Exception as e:
            logger.debug(f"[JarvisPrimeClient] Cloud Run health check error: {e}")
            return HealthStatus(available=False, error=str(e))

    async def _check_gemini_health(self) -> HealthStatus:
        """Check Gemini API availability."""
        if not self.config.gemini_api_key:
            return HealthStatus(available=False, error="Gemini API key not configured")

        # Just check if we have the key - actual availability checked on call
        return HealthStatus(available=True, model_loaded=True)

    async def is_available(self) -> bool:
        """Check if any backend is available."""
        mode, _ = self.decide_mode()
        if mode == RoutingMode.DISABLED:
            return False

        status = await self.check_health(mode)
        return status.available

    # =========================================================================
    # Completion
    # =========================================================================

    async def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        messages: Optional[List[ChatMessage]] = None,
        enrich_with_repo_map: bool = True,
    ) -> CompletionResponse:
        """
        Complete a prompt with automatic routing and fallback.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            max_tokens: Max tokens to generate
            temperature: Sampling temperature
            messages: Optional list of messages (overrides prompt)
            enrich_with_repo_map: Whether to enrich coding questions with repo context

        Returns:
            CompletionResponse with result
        """
        self._request_count += 1
        enrichment_metadata: Dict[str, Any] = {}

        # Enrich with repo map context for coding questions
        if enrich_with_repo_map and self._repo_enricher and messages is None:
            try:
                enriched_system, prompt, enrichment_metadata = await self._repo_enricher.enrich_prompt(
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                if enrichment_metadata.get("enriched"):
                    system_prompt = enriched_system
                    logger.info(
                        f"[JarvisPrimeClient] Enriched with repo context: "
                        f"confidence={enrichment_metadata.get('coding_confidence', 0):.2f}, "
                        f"repos={enrichment_metadata.get('relevant_repos', [])}, "
                        f"cache_hit={enrichment_metadata.get('cache_hit', False)}"
                    )
            except Exception as e:
                logger.warning(f"[JarvisPrimeClient] Repo enrichment failed: {e}")

        # Decide initial mode
        mode, reason = self.decide_mode()
        logger.info(f"[JarvisPrimeClient] Mode: {mode.value} ({reason})")

        if mode == RoutingMode.DISABLED:
            return CompletionResponse(
                success=False,
                error="All backends unavailable",
                backend="none",
            )

        # Build messages if not provided
        if messages is None:
            messages = []
            if system_prompt:
                messages.append(ChatMessage(role="system", content=system_prompt))
            messages.append(ChatMessage(role="user", content=prompt))

        # Try in order: decided mode → Cloud Run → Gemini
        fallback_order = self._get_fallback_order(mode)

        for try_mode in fallback_order:
            cb = self._circuit_breakers[try_mode]

            if not cb.allow_request():
                logger.debug(f"[JarvisPrimeClient] Skipping {try_mode.value} (circuit breaker OPEN)")
                continue

            try:
                response = await self._execute_completion(
                    mode=try_mode,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )

                if response.success:
                    cb.record_success()
                    # Add enrichment metadata to response
                    if enrichment_metadata:
                        response.metadata["enrichment"] = enrichment_metadata
                    self._update_stats(try_mode, response)
                    return response
                else:
                    cb.record_failure()
                    logger.warning(f"[JarvisPrimeClient] {try_mode.value} failed: {response.error}")
                    self._fallback_count += 1

            except Exception as e:
                cb.record_failure()
                logger.warning(f"[JarvisPrimeClient] {try_mode.value} exception: {e}")
                self._fallback_count += 1

        return CompletionResponse(
            success=False,
            error="All backends failed",
            backend="none",
        )

    def _get_fallback_order(self, initial_mode: RoutingMode) -> List[RoutingMode]:
        """Get fallback order based on initial mode."""
        order = []

        if initial_mode == RoutingMode.LOCAL:
            order = [RoutingMode.LOCAL]
            if self.config.use_cloud_run:
                order.append(RoutingMode.CLOUD_RUN)
            if self.config.use_gemini_fallback:
                order.append(RoutingMode.GEMINI_API)

        elif initial_mode == RoutingMode.CLOUD_RUN:
            order = [RoutingMode.CLOUD_RUN]
            if self.config.use_gemini_fallback:
                order.append(RoutingMode.GEMINI_API)

        elif initial_mode == RoutingMode.GEMINI_API:
            order = [RoutingMode.GEMINI_API]

        return order

    async def _execute_completion(
        self,
        mode: RoutingMode,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Execute completion on a specific backend."""
        if mode == RoutingMode.LOCAL:
            return await self._complete_local(messages, max_tokens, temperature)
        elif mode == RoutingMode.CLOUD_RUN:
            return await self._complete_cloud_run(messages, max_tokens, temperature)
        elif mode == RoutingMode.GEMINI_API:
            return await self._complete_gemini(messages, max_tokens, temperature)
        else:
            return CompletionResponse(success=False, error=f"Unknown mode: {mode}")

    async def _complete_local(
        self,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Complete via local JARVIS-Prime."""
        client = await self._get_http_client()
        if client is None:
            return CompletionResponse(success=False, error="HTTP client not available", backend="local")

        url = f"http://{self.config.local_host}:{self.config.local_port}/v1/chat/completions"
        timeout = self.config.local_timeout_ms / 1000.0

        payload = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        start = time.time()
        try:
            resp = await client.post(url, json=payload, timeout=timeout)
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return CompletionResponse(
                    success=True,
                    content=content,
                    latency_ms=latency,
                    backend="local",
                    tokens_used=data.get("usage", {}).get("total_tokens", 0),
                    cost_estimate=0.0,  # Local is free
                )
            elif resp.status_code == 503:
                data = resp.json()
                return CompletionResponse(
                    success=False,
                    error=data.get("detail", "Model not loaded"),
                    backend="local",
                )
            else:
                return CompletionResponse(
                    success=False,
                    error=f"HTTP {resp.status_code}",
                    backend="local",
                )
        except Exception as e:
            return CompletionResponse(success=False, error=str(e), backend="local")

    async def _complete_cloud_run(
        self,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Complete via Cloud Run JARVIS-Prime."""
        if not self.config.cloud_run_url:
            return CompletionResponse(success=False, error="Cloud Run URL not configured", backend="cloud_run")

        client = await self._get_http_client()
        if client is None:
            return CompletionResponse(success=False, error="HTTP client not available", backend="cloud_run")

        url = f"{self.config.cloud_run_url}/v1/chat/completions"
        timeout = self.config.cloud_timeout_ms / 1000.0

        payload = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        start = time.time()
        try:
            resp = await client.post(url, json=payload, timeout=timeout)
            latency = (time.time() - start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                tokens = data.get("usage", {}).get("total_tokens", 0)

                # Estimate Cloud Run cost (~$0.02 per request)
                cost = 0.02 if latency > 1000 else 0.01  # Cold start costs more

                return CompletionResponse(
                    success=True,
                    content=content,
                    latency_ms=latency,
                    backend="cloud_run",
                    tokens_used=tokens,
                    cost_estimate=cost,
                )
            elif resp.status_code == 503:
                data = resp.json()
                return CompletionResponse(
                    success=False,
                    error=data.get("detail", "Model not loaded"),
                    backend="cloud_run",
                )
            else:
                return CompletionResponse(
                    success=False,
                    error=f"HTTP {resp.status_code}",
                    backend="cloud_run",
                )
        except Exception as e:
            return CompletionResponse(success=False, error=str(e), backend="cloud_run")

    async def _complete_gemini(
        self,
        messages: List[ChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResponse:
        """Complete via Gemini API (fallback)."""
        if not self.config.gemini_api_key:
            return CompletionResponse(success=False, error="Gemini API key not configured", backend="gemini")

        # Try the new google-genai SDK first, fallback to deprecated SDK
        try:
            from google import genai
            use_new_sdk = True
        except ImportError:
            try:
                import google.generativeai as genai_old
                use_new_sdk = False
            except ImportError:
                return CompletionResponse(success=False, error="google-genai not installed", backend="gemini")

        # Build prompt from messages (convert to Gemini format)
        prompt_parts = []
        for m in messages:
            if m.role == "system":
                prompt_parts.append(f"System: {m.content}")
            elif m.role == "user":
                prompt_parts.append(f"User: {m.content}")
            elif m.role == "assistant":
                prompt_parts.append(f"Assistant: {m.content}")
        prompt_parts.append("Assistant:")
        prompt = "\n".join(prompt_parts)

        start = time.time()
        try:
            if use_new_sdk:
                # New google-genai SDK (recommended)
                client = genai.Client(api_key=self.config.gemini_api_key)

                # Use gemini-2.0-flash-lite for fastest/cheapest, fallback to configured model
                model_name = self.config.gemini_model
                if model_name == "gemini-1.5-flash":
                    model_name = "gemini-2.0-flash-lite"  # Upgrade to newer model

                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                    config={
                        "max_output_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
                content = response.text
            else:
                # Deprecated SDK (backwards compatibility)
                genai_old.configure(api_key=self.config.gemini_api_key)
                model = genai_old.GenerativeModel(self.config.gemini_model)
                response = await asyncio.to_thread(
                    model.generate_content,
                    prompt,
                    generation_config=genai_old.types.GenerationConfig(
                        max_output_tokens=max_tokens,
                        temperature=temperature,
                    ),
                )
                content = response.text

            latency = (time.time() - start) * 1000

            # Estimate Gemini cost (~$0.075/1M input, ~$0.30/1M output for Flash)
            input_tokens = len(prompt.split())
            output_tokens = len(content.split())
            cost = (input_tokens * 0.000000075) + (output_tokens * 0.0000003)

            return CompletionResponse(
                success=True,
                content=content,
                latency_ms=latency,
                backend="gemini",
                tokens_used=input_tokens + output_tokens,
                cost_estimate=cost,
            )
        except Exception as e:
            return CompletionResponse(success=False, error=str(e), backend="gemini")

    # =========================================================================
    # Repo Map Access
    # =========================================================================

    async def get_repo_map(
        self,
        repository: str = "jarvis",
        max_tokens: Optional[int] = None,
        mentioned_files: Optional[Set[str]] = None,
        mentioned_symbols: Optional[Set[str]] = None,
    ) -> Optional[str]:
        """
        Get a repository map directly.

        Args:
            repository: Repository name ("jarvis", "jarvis_prime", "reactor_core")
            max_tokens: Max tokens for the map (default from config)
            mentioned_files: Files to prioritize in the map
            mentioned_symbols: Symbols to prioritize

        Returns:
            Repository map content as string, or None if unavailable
        """
        if not self._repo_enricher:
            logger.warning("[JarvisPrimeClient] Repo enricher not enabled")
            return None

        mapper = await self._repo_enricher._get_mapper()
        if not mapper:
            return None

        try:
            result = await mapper.get_repo_map(
                repository=repository,
                max_tokens=max_tokens or self.config.repo_map_max_tokens,
                mentioned_files=mentioned_files or set(),
                mentioned_symbols=mentioned_symbols or set(),
            )
            return result.map_content if result else None
        except Exception as e:
            logger.error(f"[JarvisPrimeClient] Failed to get repo map: {e}")
            return None

    async def get_cross_repo_map(
        self,
        repositories: Optional[List[str]] = None,
        max_tokens: Optional[int] = None,
        focus_area: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get a map spanning multiple repositories.

        Args:
            repositories: List of repository names (default: all)
            max_tokens: Maximum total tokens
            focus_area: Area to focus on (e.g., "voice_auth", "training")

        Returns:
            Combined repository map content
        """
        if not self._repo_enricher:
            return None

        mapper = await self._repo_enricher._get_mapper()
        if not mapper:
            return None

        try:
            return await mapper.get_cross_repo_map(
                repositories=repositories,
                max_tokens=max_tokens or self.config.repo_map_max_tokens * 2,
                focus_area=focus_area,
            )
        except Exception as e:
            logger.error(f"[JarvisPrimeClient] Failed to get cross-repo map: {e}")
            return None

    def detect_coding_question(
        self,
        prompt: str,
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Detect if a prompt is a coding question.

        Args:
            prompt: The prompt to analyze

        Returns:
            Tuple of (is_coding_question, confidence, metadata)
        """
        if not self._repo_enricher:
            return False, 0.0, {}
        return self._repo_enricher._detector.detect(prompt)

    # =========================================================================
    # Stats and Cleanup
    # =========================================================================

    def _update_stats(self, mode: RoutingMode, response: CompletionResponse):
        """Update usage statistics."""
        if mode == RoutingMode.LOCAL:
            self._local_count += 1
        elif mode == RoutingMode.CLOUD_RUN:
            self._cloud_count += 1
        elif mode == RoutingMode.GEMINI_API:
            self._api_count += 1

        self._total_cost += response.cost_estimate

        # Record to observability hub (async-safe fire-and-forget)
        asyncio.create_task(self._record_to_observability(mode, response))

    async def _record_to_observability(self, mode: RoutingMode, response: CompletionResponse):
        """Record completion to the observability hub."""
        try:
            from observability import record_llm_call, CostTier

            # Map routing mode to cost tier
            tier_map = {
                RoutingMode.LOCAL: "local",
                RoutingMode.CLOUD_RUN: "cloud_run",
                RoutingMode.GEMINI_API: "gemini_api",
            }

            await record_llm_call(
                input_text=response.metadata.get("input", "")[:500] if response.metadata else "",
                output_text=response.content[:500] if response.content else "",
                tier=tier_map.get(mode, "local"),
                input_tokens=response.metadata.get("input_tokens", 0) if response.metadata else 0,
                output_tokens=response.tokens_used,
                latency_ms=response.latency_ms,
                routing_mode=mode.value,
                routing_reason=response.metadata.get("routing_reason", "") if response.metadata else "",
                memory_available_gb=self._memory_monitor.get_available_gb(),
                cached=response.metadata.get("cached", False) if response.metadata else False,
            )
        except ImportError:
            # Observability not available, skip silently
            pass
        except Exception as e:
            logger.debug(f"Observability recording failed: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get usage statistics."""
        total = self._request_count or 1
        stats = {
            "total_requests": self._request_count,
            "local_count": self._local_count,
            "cloud_count": self._cloud_count,
            "api_count": self._api_count,
            "fallback_count": self._fallback_count,
            "local_pct": (self._local_count / total) * 100,
            "cloud_pct": (self._cloud_count / total) * 100,
            "api_pct": (self._api_count / total) * 100,
            "total_cost": self._total_cost,
            "memory_available_gb": self._memory_monitor.get_available_gb(),
            "current_mode": self._current_mode.value if self._current_mode else "unknown",
            "monitoring_active": self._monitoring_task is not None and not self._monitoring_task.done(),
            "circuit_breakers": {
                mode.value: cb.state.value
                for mode, cb in self._circuit_breakers.items()
            },
        }

        # Add repo map enrichment stats
        if self._repo_enricher:
            stats["repo_map_enricher"] = {
                "enabled": True,
                "is_available": self._repo_enricher.is_available,
                "cache_size": len(self._repo_enricher._cache),
                "max_context_tokens": self._repo_enricher.max_context_tokens,
                "cross_repo_enabled": self._repo_enricher.enable_cross_repo,
            }
        else:
            stats["repo_map_enricher"] = {"enabled": False}

        return stats

    # =========================================================================
    # Dynamic Memory Monitoring
    # =========================================================================

    def register_mode_change_callback(
        self,
        callback: Callable[[RoutingMode, RoutingMode, str], Awaitable[None]]
    ) -> None:
        """
        Register a callback for mode changes.

        The callback receives (old_mode, new_mode, reason).
        """
        self._mode_change_callbacks.append(callback)

    def unregister_mode_change_callback(
        self,
        callback: Callable[[RoutingMode, RoutingMode, str], Awaitable[None]]
    ) -> None:
        """Unregister a mode change callback."""
        if callback in self._mode_change_callbacks:
            self._mode_change_callbacks.remove(callback)

    async def start_monitoring(self) -> None:
        """
        Start background memory monitoring.

        This periodically checks memory and switches modes if needed.
        """
        if self._monitoring_task and not self._monitoring_task.done():
            logger.warning("[JarvisPrimeClient] Monitoring already active")
            return

        self._shutdown_event.clear()
        self._current_mode, _ = self.decide_mode()
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info(
            f"[JarvisPrimeClient] Started memory monitoring "
            f"(interval: {self._monitoring_interval_seconds}s)"
        )

    async def stop_monitoring(self) -> None:
        """Stop background memory monitoring."""
        if self._monitoring_task:
            self._shutdown_event.set()
            try:
                await asyncio.wait_for(self._monitoring_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._monitoring_task.cancel()
            self._monitoring_task = None
            logger.info("[JarvisPrimeClient] Stopped memory monitoring")

    async def _monitoring_loop(self) -> None:
        """Background loop for memory monitoring."""
        while not self._shutdown_event.is_set():
            try:
                # Check for mode change
                new_mode, reason = self.decide_mode()

                if self._current_mode and new_mode != self._current_mode:
                    old_mode = self._current_mode
                    self._current_mode = new_mode

                    logger.info(
                        f"[JarvisPrimeClient] Mode change: "
                        f"{old_mode.value} → {new_mode.value} ({reason})"
                    )

                    # Notify callbacks
                    for callback in self._mode_change_callbacks:
                        try:
                            await callback(old_mode, new_mode, reason)
                        except Exception as e:
                            logger.error(f"[JarvisPrimeClient] Callback error: {e}")

                elif not self._current_mode:
                    self._current_mode = new_mode

                # Wait for next check
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._monitoring_interval_seconds
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    continue  # Normal timeout, continue monitoring

            except Exception as e:
                logger.error(f"[JarvisPrimeClient] Monitoring error: {e}")
                await asyncio.sleep(5.0)  # Brief pause on error

    async def force_mode_check(self) -> Tuple[RoutingMode, str]:
        """
        Force an immediate mode check and potential switch.

        Returns:
            Tuple of (new_mode, reason)
        """
        new_mode, reason = self.decide_mode()

        if self._current_mode and new_mode != self._current_mode:
            old_mode = self._current_mode
            self._current_mode = new_mode

            logger.info(
                f"[JarvisPrimeClient] Forced mode change: "
                f"{old_mode.value} → {new_mode.value} ({reason})"
            )

            # Notify callbacks
            for callback in self._mode_change_callbacks:
                try:
                    await callback(old_mode, new_mode, reason)
                except Exception as e:
                    logger.error(f"[JarvisPrimeClient] Callback error: {e}")

        return new_mode, reason

    def get_current_mode(self) -> Optional[RoutingMode]:
        """Get the current active routing mode."""
        return self._current_mode

    async def close(self):
        """Clean up resources."""
        await self.stop_monitoring()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# =============================================================================
# Singleton Access
# =============================================================================

_client_instance: Optional[JarvisPrimeClient] = None


def get_jarvis_prime_client() -> JarvisPrimeClient:
    """Get the global JARVIS-Prime client instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = JarvisPrimeClient()
    return _client_instance


def set_jarvis_prime_client(client: JarvisPrimeClient):
    """Set the global JARVIS-Prime client instance."""
    global _client_instance
    _client_instance = client


async def create_jarvis_prime_client(config: Optional[JarvisPrimeConfig] = None) -> JarvisPrimeClient:
    """Create and configure a new JARVIS-Prime client."""
    global _client_instance
    _client_instance = JarvisPrimeClient(config)
    return _client_instance


# =============================================================================
# Legacy Compatibility Alias
# =============================================================================
# For backward compatibility with existing code that uses JarvisPrimeClientConfig
JarvisPrimeClientConfig = JarvisPrimeConfig


# =============================================================================
# Memory-Aware Mode Decision Helper
# =============================================================================

def decide_jarvis_prime_mode() -> Tuple[str, str, float]:
    """
    Determine the optimal JARVIS-Prime mode based on current memory.

    Returns:
        Tuple of (mode, reason, available_gb)

    Modes:
        - "local": Sufficient RAM (>8GB) - run local subprocess (FREE)
        - "cloud_run": Low RAM (4-8GB) - use Cloud Run (pay-per-use)
        - "gemini_api": Very low RAM (<4GB) - use Gemini API (cheapest fallback)
        - "disabled": All backends unavailable
    """
    client = get_jarvis_prime_client()
    mode, reason = client.decide_mode()
    available_gb = client._memory_monitor.get_available_gb()

    return mode.value, reason, available_gb


async def get_system_memory_status() -> Dict[str, Any]:
    """
    Get comprehensive system memory status for routing decisions.

    Returns:
        Dict with memory info and recommended mode
    """
    try:
        import psutil
        mem = psutil.virtual_memory()

        available_gb = mem.available / (1024 ** 3)
        total_gb = mem.total / (1024 ** 3)
        percent_used = mem.percent

        # Determine recommended mode
        if available_gb >= 8.0:
            recommended_mode = "local"
            mode_reason = "Sufficient RAM for local inference"
        elif available_gb >= 4.0:
            recommended_mode = "cloud_run"
            mode_reason = "Low RAM - use Cloud Run for inference"
        else:
            recommended_mode = "gemini_api"
            mode_reason = "Very low RAM - use Gemini API fallback"

        return {
            "available_gb": round(available_gb, 2),
            "total_gb": round(total_gb, 2),
            "percent_used": round(percent_used, 1),
            "recommended_mode": recommended_mode,
            "mode_reason": mode_reason,
            "can_run_local": available_gb >= 8.0,
            "can_run_cloud": available_gb >= 4.0,
            "timestamp": time.time(),
        }
    except ImportError:
        return {
            "available_gb": 16.0,  # Optimistic default
            "total_gb": 32.0,
            "percent_used": 50.0,
            "recommended_mode": "local",
            "mode_reason": "psutil not available - assuming sufficient RAM",
            "can_run_local": True,
            "can_run_cloud": True,
            "timestamp": time.time(),
        }
