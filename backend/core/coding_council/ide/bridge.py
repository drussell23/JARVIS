"""
v77.3: IDE Bridge - Real-time IDE Context Management
=====================================================

The central bridge between IDE (VS Code/Cursor) and JARVIS Coding Council.

Features:
- Real-time context tracking (open files, cursor, errors)
- Async event streaming
- Intelligent context compression (for large codebases)
- Priority-based context management
- Cross-repo Trinity synchronization
- Adaptive caching with LRU eviction
- Bloom filters for fast file presence checking
- Trie-based path matching for context relevance

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                          IDEBridge                                  │
    ├─────────────────────────────────────────────────────────────────────┤
    │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
    │  │ContextTracker│  │ EventStream  │  │ TrinitySync  │               │
    │  │ (LRU Cache)  │  │ (AsyncQueue) │  │ (Cross-Repo) │               │
    │  └──────────────┘  └──────────────┘  └──────────────┘               │
    │         │                 │                 │                       │
    │         └─────────────────┴─────────────────┘                       │
    │                           │                                         │
    │              ┌────────────▼────────────┐                            │
    │              │  IntelligentRouter      │                            │
    │              │  (Priority + Relevance) │                            │
    │              └────────────┬────────────┘                            │
    │                           │                                         │
    │              ┌────────────▼────────────┐                            │
    │              │  AnthropicUnifiedEngine │                            │
    │              └─────────────────────────┘                            │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS v77.3
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

class IDEBridgeConfig:
    """Dynamic configuration from environment variables."""

    # Connection settings
    WEBSOCKET_PORT: int = int(os.getenv("IDE_BRIDGE_WS_PORT", "8015"))
    HTTP_PORT: int = int(os.getenv("IDE_BRIDGE_HTTP_PORT", "8016"))
    MAX_CONNECTIONS: int = int(os.getenv("IDE_BRIDGE_MAX_CONNECTIONS", "10"))

    # Context settings
    MAX_CONTEXT_FILES: int = int(os.getenv("IDE_BRIDGE_MAX_CONTEXT_FILES", "50"))
    MAX_CONTEXT_BYTES: int = int(os.getenv("IDE_BRIDGE_MAX_CONTEXT_BYTES", "500000"))
    CONTEXT_TTL_SECONDS: float = float(os.getenv("IDE_BRIDGE_CONTEXT_TTL", "300"))

    # Suggestion settings
    SUGGESTION_DEBOUNCE_MS: int = int(os.getenv("IDE_BRIDGE_DEBOUNCE_MS", "150"))
    MAX_SUGGESTION_TOKENS: int = int(os.getenv("IDE_BRIDGE_MAX_SUGGESTION_TOKENS", "500"))
    INLINE_SUGGESTION_ENABLED: bool = os.getenv("IDE_BRIDGE_INLINE_ENABLED", "true").lower() == "true"

    # Trinity settings
    TRINITY_ENABLED: bool = os.getenv("IDE_BRIDGE_TRINITY_ENABLED", "true").lower() == "true"
    JARVIS_PRIME_URL: str = os.getenv("JARVIS_PRIME_URL", "http://localhost:8011")
    REACTOR_CORE_URL: str = os.getenv("REACTOR_CORE_URL", "http://localhost:8012")

    # Performance
    LRU_CACHE_SIZE: int = int(os.getenv("IDE_BRIDGE_LRU_SIZE", "100"))
    EVENT_QUEUE_SIZE: int = int(os.getenv("IDE_BRIDGE_EVENT_QUEUE_SIZE", "1000"))


# =============================================================================
# Data Classes
# =============================================================================

class IDEEventType(Enum):
    """Types of IDE events."""
    FILE_OPENED = "file_opened"
    FILE_CLOSED = "file_closed"
    FILE_CHANGED = "file_changed"
    FILE_SAVED = "file_saved"
    CURSOR_MOVED = "cursor_moved"
    SELECTION_CHANGED = "selection_changed"
    DIAGNOSTIC_CHANGED = "diagnostic_changed"
    TERMINAL_OUTPUT = "terminal_output"
    GIT_CHANGED = "git_changed"
    EXTENSION_COMMAND = "extension_command"


class DiagnosticSeverity(Enum):
    """Diagnostic severity levels (LSP-compatible)."""
    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4


@dataclass
class CursorPosition:
    """Cursor position in a file."""
    line: int
    character: int

    def to_dict(self) -> Dict[str, int]:
        return {"line": self.line, "character": self.character}


@dataclass
class TextRange:
    """Range of text in a file."""
    start: CursorPosition
    end: CursorPosition

    def to_dict(self) -> Dict[str, Any]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}


@dataclass
class Diagnostic:
    """IDE diagnostic (error, warning, etc.)."""
    range: TextRange
    message: str
    severity: DiagnosticSeverity
    source: str = ""
    code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "range": self.range.to_dict(),
            "message": self.message,
            "severity": self.severity.value,
            "source": self.source,
            "code": self.code,
        }


@dataclass
class FileContext:
    """Context for a single file."""
    uri: str
    path: str
    content: str
    language_id: str
    version: int
    is_active: bool = False
    is_dirty: bool = False
    cursor: Optional[CursorPosition] = None
    selection: Optional[TextRange] = None
    diagnostics: List[Diagnostic] = field(default_factory=list)
    last_accessed: float = field(default_factory=time.time)

    @property
    def content_hash(self) -> str:
        return hashlib.md5(self.content.encode()).hexdigest()[:16]

    @property
    def lines(self) -> List[str]:
        return self.content.split("\n")

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def byte_count(self) -> int:
        return len(self.content.encode())

    def get_line(self, line_num: int) -> Optional[str]:
        if 0 <= line_num < len(self.lines):
            return self.lines[line_num]
        return None

    def get_context_around_cursor(self, lines_before: int = 10, lines_after: int = 5) -> str:
        """Get code context around cursor position."""
        if not self.cursor:
            return ""

        start = max(0, self.cursor.line - lines_before)
        end = min(len(self.lines), self.cursor.line + lines_after + 1)
        return "\n".join(self.lines[start:end])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uri": self.uri,
            "path": self.path,
            "language_id": self.language_id,
            "version": self.version,
            "is_active": self.is_active,
            "is_dirty": self.is_dirty,
            "cursor": self.cursor.to_dict() if self.cursor else None,
            "selection": self.selection.to_dict() if self.selection else None,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "line_count": self.line_count,
            "byte_count": self.byte_count,
            "content_hash": self.content_hash,
        }


@dataclass
class IDEContext:
    """Complete IDE context snapshot."""
    open_files: Dict[str, FileContext] = field(default_factory=dict)
    active_file: Optional[str] = None
    workspace_folders: List[str] = field(default_factory=list)
    git_branch: Optional[str] = None
    git_status: Dict[str, str] = field(default_factory=dict)
    terminal_output: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def total_bytes(self) -> int:
        return sum(f.byte_count for f in self.open_files.values())

    @property
    def error_count(self) -> int:
        return sum(
            len([d for d in f.diagnostics if d.severity == DiagnosticSeverity.ERROR])
            for f in self.open_files.values()
        )

    @property
    def warning_count(self) -> int:
        return sum(
            len([d for d in f.diagnostics if d.severity == DiagnosticSeverity.WARNING])
            for f in self.open_files.values()
        )

    def get_active_context(self) -> Optional[FileContext]:
        if self.active_file and self.active_file in self.open_files:
            return self.open_files[self.active_file]
        return None

    def get_files_with_errors(self) -> List[FileContext]:
        return [
            f for f in self.open_files.values()
            if any(d.severity == DiagnosticSeverity.ERROR for d in f.diagnostics)
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "open_files": {k: v.to_dict() for k, v in self.open_files.items()},
            "active_file": self.active_file,
            "workspace_folders": self.workspace_folders,
            "git_branch": self.git_branch,
            "git_status": self.git_status,
            "terminal_output": self.terminal_output[-50:],  # Last 50 lines
            "timestamp": self.timestamp,
            "total_bytes": self.total_bytes,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
        }


@dataclass
class IDEEvent:
    """An event from the IDE."""
    type: IDEEventType
    uri: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""


# =============================================================================
# LRU Cache with TTL
# =============================================================================

class TTLLRUCache(Generic[T]):
    """
    LRU Cache with TTL (Time-To-Live) eviction.

    Features:
    - O(1) get/put operations
    - Automatic TTL-based eviction
    - Size-based eviction when full
    - Thread-safe with asyncio locks
    """

    def __init__(self, max_size: int, ttl_seconds: float):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, Tuple[T, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[T]:
        async with self._lock:
            if key not in self._cache:
                return None

            value, timestamp = self._cache[key]

            # Check TTL
            if time.time() - timestamp > self._ttl:
                del self._cache[key]
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            return value

    async def put(self, key: str, value: T) -> None:
        async with self._lock:
            # Remove if exists
            if key in self._cache:
                del self._cache[key]

            # Evict oldest if full
            while len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)

            # Add new item
            self._cache[key] = (value, time.time())

    async def remove(self, key: str) -> bool:
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()

    async def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        async with self._lock:
            current_time = time.time()
            expired = [
                k for k, (_, ts) in self._cache.items()
                if current_time - ts > self._ttl
            ]
            for k in expired:
                del self._cache[k]
            return len(expired)

    @property
    def size(self) -> int:
        return len(self._cache)


# =============================================================================
# Bloom Filter for Fast Path Checking
# =============================================================================

class BloomFilter:
    """
    Probabilistic data structure for fast set membership testing.

    Used to quickly check if a file path might be relevant without
    full string comparison.
    """

    def __init__(self, expected_items: int = 1000, false_positive_rate: float = 0.01):
        import math

        # Calculate optimal size and hash count
        self._size = int(-expected_items * math.log(false_positive_rate) / (math.log(2) ** 2))
        self._hash_count = int(self._size / expected_items * math.log(2))
        self._bits = [False] * self._size

    def _hashes(self, item: str) -> List[int]:
        """Generate hash indices for an item."""
        hashes = []
        for i in range(self._hash_count):
            h = hashlib.md5(f"{item}:{i}".encode()).hexdigest()
            hashes.append(int(h, 16) % self._size)
        return hashes

    def add(self, item: str) -> None:
        for h in self._hashes(item):
            self._bits[h] = True

    def might_contain(self, item: str) -> bool:
        return all(self._bits[h] for h in self._hashes(item))

    def clear(self) -> None:
        self._bits = [False] * self._size


# =============================================================================
# Trie for Path Matching
# =============================================================================

class PathTrie:
    """
    Trie (prefix tree) for efficient path matching.

    Used to find files by prefix quickly.
    """

    def __init__(self):
        self._root: Dict[str, Any] = {}

    def insert(self, path: str, value: Any = True) -> None:
        """Insert a path into the trie."""
        node = self._root
        parts = path.split("/")

        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]

        node["__value__"] = value

    def search(self, path: str) -> Optional[Any]:
        """Search for an exact path."""
        node = self._root
        parts = path.split("/")

        for part in parts:
            if part not in node:
                return None
            node = node[part]

        return node.get("__value__")

    def find_by_prefix(self, prefix: str) -> List[str]:
        """Find all paths matching a prefix."""
        node = self._root
        parts = prefix.split("/") if prefix else []

        for part in parts:
            if part not in node:
                return []
            node = node[part]

        # Collect all paths under this node
        results = []
        self._collect_paths(node, prefix, results)
        return results

    def _collect_paths(self, node: Dict, prefix: str, results: List[str]) -> None:
        if "__value__" in node:
            results.append(prefix)

        for key, child in node.items():
            if key != "__value__":
                self._collect_paths(child, f"{prefix}/{key}", results)

    def remove(self, path: str) -> bool:
        """Remove a path from the trie."""
        # Simple implementation - just mark as not having value
        node = self._root
        parts = path.split("/")

        for part in parts:
            if part not in node:
                return False
            node = node[part]

        if "__value__" in node:
            del node["__value__"]
            return True
        return False


# =============================================================================
# Async Event Queue with Priority
# =============================================================================

class PriorityEventQueue:
    """
    Async priority queue for IDE events.

    Features:
    - Priority-based ordering
    - Debouncing for rapid events
    - Bounded size with overflow handling
    - Async iteration support
    """

    def __init__(self, max_size: int = 1000, debounce_ms: int = 50):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=max_size)
        self._debounce_ms = debounce_ms
        self._last_event_times: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def put(self, event: IDEEvent, priority: int = 5) -> bool:
        """
        Put an event in the queue.

        Returns False if debounced or queue full.
        """
        if self._closed:
            return False

        # Debounce check
        event_key = f"{event.type.value}:{event.uri or ''}"
        current_time = time.time() * 1000

        async with self._lock:
            last_time = self._last_event_times.get(event_key, 0)
            if current_time - last_time < self._debounce_ms:
                return False
            self._last_event_times[event_key] = current_time

        try:
            # Use negative priority so higher priority = lower number = first out
            self._queue.put_nowait((-priority, event.timestamp, event))
            return True
        except asyncio.QueueFull:
            logger.warning("[IDEBridge] Event queue full, dropping event")
            return False

    async def get(self) -> IDEEvent:
        """Get next event from queue."""
        _, _, event = await self._queue.get()
        return event

    async def get_nowait(self) -> Optional[IDEEvent]:
        """Get next event without waiting."""
        try:
            _, _, event = self._queue.get_nowait()
            return event
        except asyncio.QueueEmpty:
            return None

    async def __aiter__(self) -> AsyncIterator[IDEEvent]:
        """Async iterator for events."""
        while not self._closed:
            try:
                event = await asyncio.wait_for(self.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue

    def close(self) -> None:
        self._closed = True

    @property
    def size(self) -> int:
        return self._queue.qsize()


# =============================================================================
# Context Compressor
# =============================================================================

class ContextCompressor:
    """
    Intelligent context compression for large codebases.

    Strategies:
    - Semantic chunking (keep imports, class defs, function signatures)
    - Relevance scoring (based on cursor position, diagnostics)
    - Token-based truncation
    """

    # Patterns to always keep
    KEEP_PATTERNS = [
        r"^import\s+",
        r"^from\s+\S+\s+import",
        r"^class\s+\w+",
        r"^def\s+\w+",
        r"^async\s+def\s+\w+",
        r"^\s+def\s+\w+",  # Methods
        r"^\s+async\s+def\s+\w+",
        r"^@\w+",  # Decorators
    ]

    def __init__(self, max_tokens: int = 8000):
        self._max_tokens = max_tokens
        self._keep_patterns = [re.compile(p) for p in self.KEEP_PATTERNS]

    def compress(
        self,
        context: IDEContext,
        focus_file: Optional[str] = None,
        focus_line: Optional[int] = None,
    ) -> str:
        """
        Compress IDE context to fit within token limit.

        Priority:
        1. Active file around cursor
        2. Files with errors
        3. Recently accessed files
        4. Other open files (summarized)
        """
        parts = []
        remaining_tokens = self._max_tokens

        # 1. Active file context
        if focus_file and focus_file in context.open_files:
            active = context.open_files[focus_file]
            active_context = self._compress_file(
                active, focus_line, max_tokens=remaining_tokens // 2
            )
            parts.append(f"# Active file: {active.path}\n{active_context}")
            remaining_tokens -= self._estimate_tokens(active_context)

        # 2. Files with errors
        error_files = context.get_files_with_errors()
        for ef in error_files[:3]:  # Max 3 error files
            if ef.path == focus_file:
                continue
            error_context = self._compress_errors(ef, max_tokens=remaining_tokens // 4)
            parts.append(f"\n# Errors in: {ef.path}\n{error_context}")
            remaining_tokens -= self._estimate_tokens(error_context)

        # 3. Other files (just structure)
        for path, fc in sorted(
            context.open_files.items(),
            key=lambda x: x[1].last_accessed,
            reverse=True
        )[:5]:
            if fc.path == focus_file or fc in error_files:
                continue
            if remaining_tokens < 500:
                break
            structure = self._extract_structure(fc)
            parts.append(f"\n# Structure: {fc.path}\n{structure}")
            remaining_tokens -= self._estimate_tokens(structure)

        return "\n".join(parts)

    def _compress_file(
        self,
        fc: FileContext,
        focus_line: Optional[int],
        max_tokens: int
    ) -> str:
        """Compress a single file, keeping context around focus line."""
        lines = fc.lines

        if not focus_line:
            focus_line = fc.cursor.line if fc.cursor else len(lines) // 2

        # Keep lines around focus
        window_size = min(50, max_tokens // 10)
        start = max(0, focus_line - window_size)
        end = min(len(lines), focus_line + window_size)

        result_lines = []

        # Add imports at the start
        for i, line in enumerate(lines[:min(50, start)]):
            if any(p.match(line) for p in self._keep_patterns):
                result_lines.append(f"{i + 1}: {line}")

        if start > 0:
            result_lines.append(f"... (lines {50}-{start} omitted)")

        # Add focus window with line numbers
        for i in range(start, end):
            marker = " >>> " if i == focus_line else "     "
            result_lines.append(f"{i + 1}:{marker}{lines[i]}")

        if end < len(lines):
            result_lines.append(f"... (lines {end}-{len(lines)} omitted)")

        return "\n".join(result_lines)

    def _compress_errors(self, fc: FileContext, max_tokens: int) -> str:
        """Compress file to show error context."""
        result = []
        lines = fc.lines

        for diag in fc.diagnostics[:5]:  # Max 5 diagnostics
            line_num = diag.range.start.line
            severity = "ERROR" if diag.severity == DiagnosticSeverity.ERROR else "WARNING"

            # Get context around error
            start = max(0, line_num - 2)
            end = min(len(lines), line_num + 3)

            result.append(f"[{severity}] Line {line_num + 1}: {diag.message}")
            for i in range(start, end):
                marker = " >>> " if i == line_num else "     "
                result.append(f"  {i + 1}:{marker}{lines[i]}")

        return "\n".join(result)

    def _extract_structure(self, fc: FileContext) -> str:
        """Extract just the structure (class/function definitions)."""
        structure = []
        for i, line in enumerate(fc.lines):
            if any(p.match(line) for p in self._keep_patterns):
                structure.append(f"{i + 1}: {line}")

        return "\n".join(structure[:30])  # Max 30 lines

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate (1 token ≈ 4 characters)."""
        return len(text) // 4


# =============================================================================
# IDE Bridge Core
# =============================================================================

class IDEBridge:
    """
    Central bridge between IDE and JARVIS Coding Council.

    Responsibilities:
    - Track IDE context (files, cursor, errors)
    - Route events to appropriate handlers
    - Coordinate with Anthropic engine for suggestions
    - Sync state with Trinity components
    """

    def __init__(self):
        # Context storage
        self._context = IDEContext()
        self._context_lock = asyncio.Lock()

        # Caching
        self._file_cache = TTLLRUCache[FileContext](
            max_size=IDEBridgeConfig.LRU_CACHE_SIZE,
            ttl_seconds=IDEBridgeConfig.CONTEXT_TTL_SECONDS,
        )
        self._suggestion_cache = TTLLRUCache[str](
            max_size=100,
            ttl_seconds=60.0,
        )

        # Fast lookups
        self._path_bloom = BloomFilter(expected_items=1000)
        self._path_trie = PathTrie()

        # Event handling
        self._event_queue = PriorityEventQueue(
            max_size=IDEBridgeConfig.EVENT_QUEUE_SIZE,
            debounce_ms=IDEBridgeConfig.SUGGESTION_DEBOUNCE_MS,
        )
        self._event_handlers: Dict[IDEEventType, List[Callable]] = {}

        # Compression
        self._compressor = ContextCompressor()

        # Engine reference (lazy loaded)
        self._anthropic_engine: Optional[Any] = None

        # Session tracking
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._session_lock = asyncio.Lock()

        # Background tasks
        self._background_tasks: Set[asyncio.Task] = set()
        self._running = False

    async def initialize(self) -> bool:
        """Initialize the IDE bridge."""
        try:
            # Load Anthropic engine
            from ..adapters.anthropic_engine import get_anthropic_engine
            self._anthropic_engine = await get_anthropic_engine()

            # Start event processor
            self._running = True
            task = asyncio.create_task(self._process_events())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

            # Start cache cleanup
            task = asyncio.create_task(self._periodic_cleanup())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

            logger.info("[IDEBridge] Initialized successfully")
            return True

        except Exception as e:
            logger.error(f"[IDEBridge] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the IDE bridge."""
        self._running = False
        self._event_queue.close()

        # Cancel background tasks
        for task in self._background_tasks:
            task.cancel()

        await asyncio.gather(*self._background_tasks, return_exceptions=True)

        # Clear caches
        await self._file_cache.clear()
        await self._suggestion_cache.clear()

        logger.info("[IDEBridge] Shutdown complete")

    # -------------------------------------------------------------------------
    # Context Management
    # -------------------------------------------------------------------------

    async def update_file(self, file_context: FileContext) -> None:
        """Update context for a file."""
        async with self._context_lock:
            self._context.open_files[file_context.uri] = file_context
            self._context.timestamp = time.time()

        # Update indexes
        await self._file_cache.put(file_context.uri, file_context)
        self._path_bloom.add(file_context.path)
        self._path_trie.insert(file_context.path, file_context.uri)

        logger.debug(f"[IDEBridge] Updated file: {file_context.path}")

    async def remove_file(self, uri: str) -> None:
        """Remove a file from context."""
        async with self._context_lock:
            if uri in self._context.open_files:
                path = self._context.open_files[uri].path
                del self._context.open_files[uri]
                self._path_trie.remove(path)

        await self._file_cache.remove(uri)
        logger.debug(f"[IDEBridge] Removed file: {uri}")

    async def set_active_file(self, uri: str) -> None:
        """Set the active (focused) file."""
        async with self._context_lock:
            # Update old active
            if self._context.active_file and self._context.active_file in self._context.open_files:
                self._context.open_files[self._context.active_file].is_active = False

            # Set new active
            self._context.active_file = uri
            if uri in self._context.open_files:
                self._context.open_files[uri].is_active = True
                self._context.open_files[uri].last_accessed = time.time()

    async def update_cursor(self, uri: str, position: CursorPosition) -> None:
        """Update cursor position for a file."""
        async with self._context_lock:
            if uri in self._context.open_files:
                self._context.open_files[uri].cursor = position

    async def update_diagnostics(self, uri: str, diagnostics: List[Diagnostic]) -> None:
        """Update diagnostics for a file."""
        async with self._context_lock:
            if uri in self._context.open_files:
                self._context.open_files[uri].diagnostics = diagnostics

    async def get_context(self) -> IDEContext:
        """Get current IDE context snapshot."""
        async with self._context_lock:
            return self._context

    async def get_compressed_context(
        self,
        focus_file: Optional[str] = None,
        focus_line: Optional[int] = None,
    ) -> str:
        """Get compressed context suitable for LLM input."""
        context = await self.get_context()
        return self._compressor.compress(context, focus_file, focus_line)

    # -------------------------------------------------------------------------
    # Event Handling
    # -------------------------------------------------------------------------

    def register_handler(
        self,
        event_type: IDEEventType,
        handler: Callable[[IDEEvent], Coroutine]
    ) -> None:
        """Register an event handler."""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    async def emit_event(self, event: IDEEvent) -> bool:
        """Emit an IDE event."""
        # Determine priority based on event type
        priority_map = {
            IDEEventType.DIAGNOSTIC_CHANGED: 9,
            IDEEventType.CURSOR_MOVED: 3,
            IDEEventType.FILE_CHANGED: 5,
            IDEEventType.FILE_SAVED: 7,
            IDEEventType.FILE_OPENED: 6,
            IDEEventType.FILE_CLOSED: 4,
        }
        priority = priority_map.get(event.type, 5)
        return await self._event_queue.put(event, priority)

    async def _process_events(self) -> None:
        """Background task to process events."""
        async for event in self._event_queue:
            try:
                handlers = self._event_handlers.get(event.type, [])
                if handlers:
                    await asyncio.gather(
                        *(h(event) for h in handlers),
                        return_exceptions=True
                    )
            except Exception as e:
                logger.error(f"[IDEBridge] Event processing error: {e}")

    async def _periodic_cleanup(self) -> None:
        """Periodic cleanup of expired cache entries."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Every minute
                expired = await self._file_cache.cleanup_expired()
                if expired > 0:
                    logger.debug(f"[IDEBridge] Cleaned up {expired} expired cache entries")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[IDEBridge] Cleanup error: {e}")

    # -------------------------------------------------------------------------
    # Inline Suggestions
    # -------------------------------------------------------------------------

    async def get_inline_suggestion(
        self,
        uri: str,
        line: int,
        character: int,
        trigger_kind: str = "automatic",
    ) -> Optional[str]:
        """
        Get inline code suggestion at cursor position.

        Args:
            uri: File URI
            line: Cursor line
            character: Cursor character
            trigger_kind: How suggestion was triggered

        Returns:
            Suggestion text or None
        """
        if not IDEBridgeConfig.INLINE_SUGGESTION_ENABLED:
            return None

        if not self._anthropic_engine:
            return None

        # Check cache first
        cache_key = f"{uri}:{line}:{character}"
        cached = await self._suggestion_cache.get(cache_key)
        if cached:
            return cached

        try:
            # Get file context
            context = await self.get_context()
            if uri not in context.open_files:
                return None

            file_context = context.open_files[uri]

            # Get compressed context
            compressed = await self.get_compressed_context(uri, line)

            # Build prompt for inline suggestion
            current_line = file_context.get_line(line) or ""
            prefix = current_line[:character]

            # Request suggestion from engine
            from ..adapters.anthropic_engine import ClaudeClient
            client = ClaudeClient()

            system = """You are an inline code completion assistant.
Given the code context and current cursor position, provide a SHORT completion.
Only output the code to insert, nothing else. No explanations, no markdown."""

            user_message = f"""Context:
{compressed}

Current line: {current_line}
Cursor is after: "{prefix}"

Complete the code:"""

            response, _ = await client.complete(
                system=system,
                messages=[{"role": "user", "content": user_message}],
                max_tokens=IDEBridgeConfig.MAX_SUGGESTION_TOKENS,
                temperature=0.2,
            )

            suggestion = response.strip()

            # Cache the suggestion
            await self._suggestion_cache.put(cache_key, suggestion)

            return suggestion

        except Exception as e:
            logger.error(f"[IDEBridge] Suggestion error: {e}")
            return None

    # -------------------------------------------------------------------------
    # Session Management
    # -------------------------------------------------------------------------

    async def create_session(self, session_id: str, client_info: Dict[str, Any]) -> bool:
        """Create a new IDE session."""
        async with self._session_lock:
            if len(self._sessions) >= IDEBridgeConfig.MAX_CONNECTIONS:
                logger.warning("[IDEBridge] Max connections reached")
                return False

            self._sessions[session_id] = {
                "client_info": client_info,
                "created_at": time.time(),
                "last_activity": time.time(),
            }
            logger.info(f"[IDEBridge] Session created: {session_id}")
            return True

    async def end_session(self, session_id: str) -> None:
        """End an IDE session."""
        async with self._session_lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                logger.info(f"[IDEBridge] Session ended: {session_id}")

    async def update_session_activity(self, session_id: str) -> None:
        """Update session last activity time."""
        async with self._session_lock:
            if session_id in self._sessions:
                self._sessions[session_id]["last_activity"] = time.time()

    # -------------------------------------------------------------------------
    # Trinity Integration
    # -------------------------------------------------------------------------

    async def sync_with_trinity(self) -> Dict[str, Any]:
        """Sync IDE context with Trinity components."""
        if not IDEBridgeConfig.TRINITY_ENABLED:
            return {"synced": False, "reason": "trinity_disabled"}

        context = await self.get_context()

        # Prepare sync payload
        payload = {
            "source": "ide_bridge",
            "timestamp": time.time(),
            "context": {
                "active_file": context.active_file,
                "open_files_count": len(context.open_files),
                "error_count": context.error_count,
                "warning_count": context.warning_count,
                "git_branch": context.git_branch,
            },
        }

        results = {"synced": True, "jprime": None, "reactor": None}

        # Sync with J-Prime
        if self._anthropic_engine:
            try:
                await self._anthropic_engine.trinity.notify_evolution_started(
                    task_id=f"ide_sync_{int(time.time())}",
                    description="IDE context sync",
                    target_files=list(context.open_files.keys())[:10],
                )
                results["jprime"] = "synced"
            except Exception as e:
                results["jprime"] = str(e)

        return results

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    async def get_status(self) -> Dict[str, Any]:
        """Get bridge status."""
        context = await self.get_context()
        return {
            "running": self._running,
            "sessions": len(self._sessions),
            "open_files": len(context.open_files),
            "active_file": context.active_file,
            "error_count": context.error_count,
            "warning_count": context.warning_count,
            "cache_size": self._file_cache.size,
            "event_queue_size": self._event_queue.size,
            "anthropic_engine": self._anthropic_engine is not None,
        }


# =============================================================================
# Global Instance
# =============================================================================

_ide_bridge: Optional[IDEBridge] = None
_ide_bridge_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_ide_bridge() -> IDEBridge:
    """Get or create the global IDE bridge instance."""
    global _ide_bridge

    if _ide_bridge is None:
        async with _ide_bridge_lock:
            if _ide_bridge is None:
                _ide_bridge = IDEBridge()
                await _ide_bridge.initialize()

    return _ide_bridge


async def initialize_ide_bridge() -> IDEBridge:
    """Initialize and return the IDE bridge."""
    return await get_ide_bridge()
