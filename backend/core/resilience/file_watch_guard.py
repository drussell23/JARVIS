"""
Robust File Watch Guard with Event Deduplication
================================================

Production-grade file watching for cross-repo file-based RPC.

Features:
    - Event deduplication with LRU cache
    - Graceful recovery from watchdog errors
    - Configurable event batching and debouncing
    - Directory creation handling (watches new subdirs)
    - Checksum-based change detection (avoid false positives)
    - Comprehensive metrics and health status

Author: JARVIS Cross-Repo Resilience
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
import queue as thread_queue
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

# Phase 5A: Bounded queue backpressure
try:
    from backend.core.bounded_queue import BoundedAsyncQueue, OverflowPolicy
except ImportError:
    BoundedAsyncQueue = None

logger = logging.getLogger(__name__)


class FileEventType(Enum):
    """Type of file event."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


@dataclass
class FileEvent:
    """Represents a file system event."""

    event_type: FileEventType
    path: Path
    timestamp: float = field(default_factory=time.time)
    checksum: Optional[str] = None  # For content change detection
    old_path: Optional[Path] = None  # For MOVED events
    size: Optional[int] = None
    is_directory: bool = False

    @property
    def event_id(self) -> str:
        """Generate unique ID for deduplication."""
        return f"{self.event_type.value}:{self.path}:{self.checksum or self.timestamp}"


@dataclass
class FileWatchConfig:
    """Configuration for file watch guard."""

    # Basic settings
    recursive: bool = True
    patterns: List[str] = field(default_factory=lambda: ["*"])  # Glob patterns
    ignore_patterns: List[str] = field(default_factory=lambda: ["*.tmp", "*.swp", "*.bak", "*~"])

    # ---------------- Narrow scheduling (root-of-scan control) ------------
    #
    # ``PollingObserver`` on macOS (the default fallback because native
    # FSEvents has been observed to crash silently in long sessions per
    # bt-2026-04-12-005521) does a full tree snapshot O(N) on every tick.
    # On this repo the root contains ~56K ``.py`` files, ~48K of which live
    # in ``venv/``, ``.venv/``, and ``venv_py39_backup/``. At that scale,
    # PollingObserver can't keep up and delivers zero events.
    #
    # ``exclude_top_level_dirs`` is applied at the SCHEDULING layer: those
    # directories are never passed to ``observer.schedule()``, so the
    # PollingObserver snapshot never walks into them. This fixes the
    # "OS-to-Organism nervous system severed" failure surfaced by the
    # TodoScanner graduation arc 2026-04-20. Env override
    # ``JARVIS_FILE_WATCH_EXCLUDE_DIRS`` accepts a comma-separated list.
    #
    # Slice 12I additions (2026-05-22) — closes the wedge surfaced by the
    # Slice 12G-2 LoopDeadman in bt-2026-05-22-223333:
    #   * ``.jarvis``  — runtime state directory; SWE-Bench-Pro
    #                    worktrees (56K-file element-web clones)
    #                    live under .jarvis/swe_bench_pro/worktrees/...
    #                    and were turning PollingObserver into a
    #                    ~100-thread dirsnapshot.walk storm.
    #   * ``.claude``  — Claude Code session state; transient.
    # ``.ouroboros`` was already present.
    exclude_top_level_dirs: frozenset = field(default_factory=lambda: frozenset({
        "venv", ".venv", "venv_py39_backup",
        "node_modules", ".git", ".worktrees",
        "__pycache__", ".pytest_cache", ".mypy_cache",
        "build", "dist", ".ouroboros",
        ".jarvis", ".claude",  # Slice 12I
    }))

    # Slice 12I — defense-in-depth path-pattern exclusion. Even if
    # ``exclude_top_level_dirs`` is overridden via
    # ``JARVIS_FILE_WATCH_EXCLUDE_DIRS`` and drops ``.jarvis``, the
    # SWE-Bench-Pro worktree root MUST stay unwatched: a single
    # element-web clone there is ~56K files and turns the
    # PollingObserver fallback into a CPU storm. These paths are
    # repo-relative (matched via ``Path.parts`` startswith) so a
    # symlinked worktree under a different name still hits the
    # exclusion as long as the canonical repo-relative path matches.
    # Env override:
    # ``JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS`` (comma-separated).
    exclude_path_patterns: frozenset = field(default_factory=lambda: frozenset({
        ".jarvis/swe_bench_pro/worktrees",
        ".jarvis/swe_bench_pro/repo_cache",
        ".jarvis/smoke-logs",
        ".ouroboros/sessions",
    }))

    # Slice 12I — warning threshold. If the post-exclusion narrow-
    # scope schedule resolves to more than this many roots, log a
    # WARNING line at start so operators see runaway-watching early.
    # Env override:
    # ``JARVIS_FILE_WATCH_HIGH_COUNT_WARN`` (int).
    high_watch_count_warn: int = 30

    # Slice 12J — HARD schedule budget. The watchdog
    # ``PollingObserver`` fallback spawns one polling thread per
    # ``observer.schedule()`` call, each doing its own
    # ``dirsnapshot.walk`` on every tick. With ~150 schedules the
    # aggregate GIL contention wedges the asyncio loop within ~10s
    # — bt-2026-05-22-232553 captured 32 concurrent
    # ``dirsnapshot.walk`` frames with 99 polling threads parked.
    #
    # Above this cap, ``_resolve_watch_paths`` COALESCES depth-2
    # nested-venv splits back to their parent recursive schedule
    # (operator-binding: "fewer observer schedules beats perfect
    # nested-dir exclusion"). The legacy ``ignore_patterns``
    # post-event filter still drops events from the coalesced
    # subtree, just at a different layer.
    #
    # Pattern-descent groups (the load-bearing Slice 12I fix for
    # ``.jarvis/swe_bench_pro/worktrees``) are PROTECTED from
    # coalescing — collapsing one back to a recursive parent would
    # resurrect the original 56K-file element-web walk.
    #
    # Default matches ``high_watch_count_warn`` so a non-coalescing
    # schedule never crosses the warning line. Env override:
    # ``JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS`` (int). Set to ``0``
    # to disable the cap (legacy unbounded behavior — NOT
    # recommended; restored only as an operator escape hatch).
    max_scheduled_roots: int = 30

    # Debouncing
    debounce_seconds: float = 0.1  # Wait before firing event
    batch_timeout_seconds: float = 0.5  # Max wait for batch

    # Deduplication
    dedup_cache_size: int = 1000  # LRU cache size
    dedup_ttl_seconds: float = 5.0  # Events within TTL are deduplicated

    # Content verification
    verify_checksum: bool = True  # Use checksum to detect real changes
    min_stable_seconds: float = 0.05  # File must be stable for this long

    # Recovery
    restart_on_error: bool = True
    error_backoff_seconds: float = 1.0
    max_consecutive_errors: int = 5

    # Health
    health_check_interval: float = 30.0


@dataclass
class WatchMetrics:
    """Metrics for file watching."""

    events_received: int = 0
    events_processed: int = 0
    events_deduplicated: int = 0
    events_filtered: int = 0
    errors: int = 0
    restarts: int = 0
    last_event_time: Optional[float] = None
    avg_processing_time_ms: float = 0.0
    # Slice 12A — overflow accounting for the loop-thread enqueue
    # wrapper. ``events_dropped_queue_full`` counts every
    # ``asyncio.QueueFull`` caught inside ``_queue_event_on_loop``.
    # ``queue_full_suppressed_logs`` counts overflow events that
    # were NOT logged because the 1s rate-limit window swallowed
    # them. ``last_overflow_at`` is wall-clock ``time.time()`` of
    # the most recent drop. ``last_overflow_log_at`` is
    # ``time.monotonic()`` of the most recent emitted summary
    # (used for rate-limit gating; monotonic is correct here
    # because we measure elapsed time between two log emissions,
    # not absolute timestamps).
    events_dropped_queue_full: int = 0
    queue_full_suppressed_logs: int = 0
    last_overflow_at: Optional[float] = None
    last_overflow_log_at: Optional[float] = None


# Slice 12J — closed taxonomy of how a depth-1 watch_dir entry made it
# into the schedule. Drives the coalescing budget enforcement in
# ``FileWatchGuard._resolve_watch_paths``:
#
#   * SIMPLE_RECURSIVE — entry has no nested excluded children and no
#     pattern descendant; scheduled as a single ``(entry, True)``
#     tuple. Cannot be coalesced (already 1 schedule).
#
#   * NESTED_VENV_SPLIT — entry contains a name-excluded child at
#     depth 2 (e.g. ``backend/venv``). Splits into one non-recursive
#     parent schedule + N recursive grandchild schedules.
#     COALESCABLE: dropping back to a recursive parent re-includes
#     the nested venv in the polling tree (the post-event
#     ``ignore_patterns`` filter still drops the events) but
#     immediately reclaims N-1 polling threads. Operator binding:
#     "fewer observer schedules beats perfect nested-dir exclusion".
#
#   * PATTERN_DESCENT — entry contains an ``exclude_path_patterns``
#     descendant (load-bearing Slice 12I path for
#     ``.jarvis/swe_bench_pro/worktrees``). Splits via the recursive
#     pattern-aware walker. PROTECTED from coalescing: dropping a
#     coalesced parent recursive schedule would re-include the 56K-
#     file element-web worktree and resurrect the original wedge.
#
# The string form is frozen so AST pins can read it.
_SCHEDULE_GROUP_KIND_SIMPLE_RECURSIVE = "simple_recursive"
_SCHEDULE_GROUP_KIND_NESTED_VENV_SPLIT = "nested_venv_split"
_SCHEDULE_GROUP_KIND_PATTERN_DESCENT = "pattern_descent"
_SCHEDULE_GROUP_KIND_COALESCED = "nested_venv_split_coalesced"


class _ScheduleGroup(NamedTuple):
    """Slice 12J — schedule group emitted by ``_resolve_watch_paths``.

    Carries the (parent, entries, kind) triple so the coalescing
    pass can identify which groups are eligible to shrink and which
    are protected. ``entries`` is the flat list of
    ``(path, recursive)`` tuples this group would contribute to
    ``observer.schedule()`` calls. ``parent`` is the depth-1 entry
    that originated the group (the coalesce target when applicable).
    """

    parent: Path
    entries: Tuple[Tuple[Path, bool], ...]
    kind: str


class _ResolvedSchedule(NamedTuple):
    """Slice 12J — structured return of ``_resolve_watch_paths``.

    Preserves the legacy ``(paths, skipped_by_pattern)`` payload
    from Slice 12I via positional unpacking-equivalent fields and
    adds the budget-enforcement telemetry surface so the
    ``_start_watchdog`` boot log can report
    ``candidate_count`` / ``coalesced_count`` to the operator.
    """

    paths: List[Tuple[Path, bool]]
    skipped_by_pattern: int
    candidate_count: int  # Schedules BEFORE budget enforcement.
    coalesced_count: int  # Groups collapsed back to recursive parent.


class GlobalWatchRegistry:
    """
    v16.0: Centralized registry for all file watches across JARVIS.

    Prevents FSEvents "Cannot add watch - it is already scheduled" errors
    by providing a single point of truth for which directories are being watched.

    This registry is shared between:
    - FileWatchGuard
    - ReactorCoreReceiver
    - TrinityBridgeAdapter
    - Any other component that needs file watching
    """

    _instance: Optional["GlobalWatchRegistry"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._watched_paths: Dict[str, Dict[str, Any]] = {}
                    cls._instance._async_lock: Optional[asyncio.Lock] = None
        return cls._instance

    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create async lock (lazy init for event loop compatibility)."""
        if self._async_lock is None:
            try:
                self._async_lock = asyncio.Lock()
            except RuntimeError:
                # No event loop - will be created later
                pass
        return self._async_lock

    def is_watched(self, path: Path) -> bool:
        """Check if a path is already being watched (sync version)."""
        resolved = str(path.resolve())
        with self._lock:
            return resolved in self._watched_paths

    async def is_watched_async(self, path: Path) -> bool:
        """Check if a path is already being watched (async version)."""
        resolved = str(path.resolve())
        lock = self._get_async_lock()
        if lock:
            async with lock:
                return resolved in self._watched_paths
        return self.is_watched(path)

    def register(self, path: Path, owner: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
        """
        Register a watch. Returns True if registered, False if already watched.
        """
        resolved = str(path.resolve())
        with self._lock:
            if resolved in self._watched_paths:
                return False
            self._watched_paths[resolved] = {
                "owner": owner,
                "loop": loop,
                "registered_at": time.time(),
            }
            return True

    async def register_async(self, path: Path, owner: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
        """Async version of register."""
        resolved = str(path.resolve())
        lock = self._get_async_lock()
        if lock:
            async with lock:
                if resolved in self._watched_paths:
                    return False
                self._watched_paths[resolved] = {
                    "owner": owner,
                    "loop": loop,
                    "registered_at": time.time(),
                }
                return True
        return self.register(path, owner, loop)

    def unregister(self, path: Path) -> bool:
        """Unregister a watch. Returns True if was registered."""
        resolved = str(path.resolve())
        with self._lock:
            return self._watched_paths.pop(resolved, None) is not None

    async def unregister_async(self, path: Path) -> bool:
        """Async version of unregister."""
        resolved = str(path.resolve())
        lock = self._get_async_lock()
        if lock:
            async with lock:
                return self._watched_paths.pop(resolved, None) is not None
        return self.unregister(path)

    def get_owner(self, path: Path) -> Optional[str]:
        """Get the owner of a watch."""
        resolved = str(path.resolve())
        with self._lock:
            info = self._watched_paths.get(resolved)
            return info.get("owner") if info else None

    def get_all_watches(self) -> Dict[str, str]:
        """Get all watches as {path: owner}."""
        with self._lock:
            return {k: v.get("owner", "unknown") for k, v in self._watched_paths.items()}


# Global singleton instance
_watch_registry = GlobalWatchRegistry()


def get_global_watch_registry() -> GlobalWatchRegistry:
    """Get the global watch registry singleton."""
    return _watch_registry


class FileWatchGuard:
    """
    Robust file watcher with event deduplication and recovery.

    Wraps watchdog with additional safety measures for production use.

    v2.0: Enhanced cross-thread async communication with proper event loop handling.
          Fixes "There is no current event loop in thread" errors.

    v2.1 (v16.0): Uses GlobalWatchRegistry to prevent duplicate watches across
          all JARVIS components (FileWatchGuard, ReactorCoreReceiver, etc.)

    Usage:
        config = FileWatchConfig(patterns=["*.json"])
        guard = FileWatchGuard(
            watch_dir=Path("~/.jarvis/events"),
            config=config,
            on_event=handle_event,
        )

        await guard.start()
        # ... events flow to handler ...
        await guard.stop()
    """

    # v2.1: Use centralized registry (kept for backward compatibility)
    _global_watched_paths: Dict[str, "FileWatchGuard"] = {}
    _global_lock = threading.Lock()

    def __init__(
        self,
        watch_dir: Path,
        on_event: Callable[[FileEvent], Any],
        config: Optional[FileWatchConfig] = None,
        on_error: Optional[Callable[[Exception], Any]] = None,
    ):
        self.watch_dir = Path(watch_dir).expanduser().resolve()
        self._on_event = on_event
        self._on_error = on_error
        self.config = config or FileWatchConfig()

        self._observer = None
        self._running = False
        # Slice 12A — file_watch_events queue policy.
        #
        # WARN_AND_BLOCK is wrong for this producer: the thread
        # watcher publishes via ``loop.call_soon_threadsafe`` and
        # the loop-side callback is non-blocking by construction,
        # so there's no producer to "block". Under bursty load the
        # prior policy raised ``asyncio.QueueFull`` inside the
        # callback, leaking the exception into asyncio's default
        # handler — which formatted a multi-line traceback per
        # overflow on the loop thread (~16k tracebacks per soak →
        # 100+ s of cumulative loop-block, empirically validated by
        # bt-2026-05-22-074210).
        #
        # DROP_NEWEST is the right semantic for file watching:
        # losing the newest event is safe because downstream
        # debounce + periodic scan recover any drift. The wrapper
        # ``_queue_event_on_loop`` still catches ``QueueFull``
        # defensively (covers the asyncio.Queue fallback path
        # where BoundedAsyncQueue is unavailable + race conditions).
        self._event_queue: asyncio.Queue[FileEvent] = (
            BoundedAsyncQueue(maxsize=500, policy=OverflowPolicy.DROP_NEWEST, name="file_watch_events")
            if BoundedAsyncQueue is not None else asyncio.Queue()
        )
        self._processor_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None

        # v2.0: Store the main event loop for cross-thread communication
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

        # Deduplication
        self._seen_events: OrderedDict[str, float] = OrderedDict()  # LRU cache
        self._pending_events: Dict[str, FileEvent] = {}  # Debounce buffer

        # File content cache for checksum
        self._checksums: Dict[str, str] = {}

        # Error tracking
        self._consecutive_errors = 0
        self._last_error: Optional[Exception] = None

        self.metrics = WatchMetrics()

    async def start(self) -> bool:
        """
        Start file watching.

        v2.1 (v16.0): Uses GlobalWatchRegistry to coordinate with ALL JARVIS
              components that use file watching (ReactorCoreReceiver, etc.)

        Returns:
            True if started successfully
        """
        if self._running:
            return True

        # v2.0: Capture the main event loop for cross-thread communication
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("[FileWatchGuard] Must be called from async context")
            return False

        # Ensure directory exists
        self.watch_dir.mkdir(parents=True, exist_ok=True)

        # v2.1: Use GlobalWatchRegistry to check for duplicate watches across ALL components
        registry = get_global_watch_registry()

        # Check if already watched by ANY component (FileWatchGuard, ReactorCoreReceiver, etc.)
        if await registry.is_watched_async(self.watch_dir):
            existing_owner = registry.get_owner(self.watch_dir)
            logger.warning(
                f"[FileWatchGuard] Path {self.watch_dir} already watched by {existing_owner}. "
                "Using secondary handler mode."
            )

            # v2.1: Also check local registry for FileWatchGuard instances
            path_key = str(self.watch_dir.resolve())
            with FileWatchGuard._global_lock:
                if path_key in FileWatchGuard._global_watched_paths:
                    existing = FileWatchGuard._global_watched_paths[path_key]
                    if existing._running and existing is not self:
                        existing._register_secondary_handler(self._on_event)
                        self._running = True
                        return True

            # If watched by another component (not FileWatchGuard), use polling fallback
            self._running = True
            self._processor_task = asyncio.create_task(self._polling_fallback())
            return True

        # Register with GlobalWatchRegistry FIRST (prevents race conditions)
        registered = await registry.register_async(self.watch_dir, "FileWatchGuard", self._main_loop)
        if not registered:
            # Lost the race - another component registered just now
            logger.info(f"[FileWatchGuard] Path {self.watch_dir} was just registered by another component")
            self._running = True
            self._processor_task = asyncio.create_task(self._polling_fallback())
            return True

        # Also register in local registry for backward compatibility
        path_key = str(self.watch_dir.resolve())
        with FileWatchGuard._global_lock:
            FileWatchGuard._global_watched_paths[path_key] = self

        try:
            await self._start_watchdog()
            self._running = True

            # Start event processor
            self._processor_task = asyncio.create_task(self._process_events())

            # Start health check
            self._health_task = asyncio.create_task(self._health_check_loop())

            logger.info(f"[FileWatchGuard] Started watching {self.watch_dir}")
            return True

        except Exception as e:
            logger.error(f"[FileWatchGuard] Failed to start: {e}")
            self._last_error = e
            self.metrics.errors += 1

            # Unregister on failure from both registries
            await registry.unregister_async(self.watch_dir)
            with FileWatchGuard._global_lock:
                FileWatchGuard._global_watched_paths.pop(path_key, None)

            # v2.1: Fall back to polling on FSEvents errors
            if "already scheduled" in str(e).lower() or "cannot add watch" in str(e).lower():
                logger.info(f"[FileWatchGuard] FSEvents conflict, using polling fallback")
                self._running = True
                self._processor_task = asyncio.create_task(self._polling_fallback())
                return True

            return False

    async def _polling_fallback(self) -> None:
        """v2.1: Polling fallback when file watching is not available."""
        poll_interval = 1.0  # seconds
        logger.info(f"[FileWatchGuard] Using polling fallback for {self.watch_dir}")

        while self._running:
            try:
                # Scan directory for changes
                for pattern in self.config.patterns:
                    if self.config.recursive:
                        files = self.watch_dir.rglob(pattern)
                    else:
                        files = self.watch_dir.glob(pattern)

                    for path in files:
                        if path.is_file():
                            # Check if file is new or modified
                            path_key = str(path)
                            try:
                                mtime = path.stat().st_mtime
                                checksum = hashlib.md5(path.read_bytes()).hexdigest()

                                old_checksum = self._checksums.get(path_key)
                                if old_checksum != checksum:
                                    self._checksums[path_key] = checksum
                                    event = FileEvent(
                                        event_type=FileEventType.MODIFIED if old_checksum else FileEventType.CREATED,
                                        path=path,
                                        checksum=checksum,
                                    )
                                    if self._should_process(event) and not self._is_duplicate(event):
                                        await self._process_single_event(event)

                            except FileNotFoundError:
                                # File was deleted
                                if path_key in self._checksums:
                                    del self._checksums[path_key]

                await asyncio.sleep(poll_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[FileWatchGuard] Polling error: {e}")
                await asyncio.sleep(poll_interval)

    def _register_secondary_handler(self, handler: Callable[[FileEvent], Any]) -> None:
        """
        v2.0: Register a secondary event handler for shared watching.

        When multiple components want to watch the same directory, secondary
        handlers receive events from the primary watcher.
        """
        if not hasattr(self, "_secondary_handlers"):
            self._secondary_handlers: List[Callable[[FileEvent], Any]] = []
        self._secondary_handlers.append(handler)
        logger.debug(f"[FileWatchGuard] Registered secondary handler ({len(self._secondary_handlers)} total)")

    async def stop(self) -> None:
        """
        Stop file watching.

        v2.1: Properly unregisters from both GlobalWatchRegistry and local registry.
        """
        self._running = False

        # v2.1: Unregister from GlobalWatchRegistry
        registry = get_global_watch_registry()
        await registry.unregister_async(self.watch_dir)

        # v2.0: Unregister from local FileWatchGuard registry (backward compatibility)
        path_key = str(self.watch_dir.resolve())
        with FileWatchGuard._global_lock:
            if FileWatchGuard._global_watched_paths.get(path_key) is self:
                del FileWatchGuard._global_watched_paths[path_key]

        # Stop tasks
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Stop watchdog
        await self._stop_watchdog()

        # v2.0: Clear secondary handlers
        if hasattr(self, "_secondary_handlers"):
            self._secondary_handlers.clear()

        # Clear main loop reference
        self._main_loop = None

        logger.info("[FileWatchGuard] Stopped")

    async def _start_watchdog(self) -> None:
        """Start the watchdog observer.

        Backend selection:
          - On macOS we use ``PollingObserver`` by default. The native FSEvents
            backend segfaults inside ``Observer.join()`` on macOS 26 / ARM64 /
            Python 3.9.6 with ``KERN_PROTECTION_FAILURE`` (pointer auth fail),
            and silently dies inside long-running processes. Battle test
            bt-2026-04-12-005521 ran 30+ minutes with zero fs.changed events
            delivered to any sensor — the FSEvents thread had crashed but no
            error surfaced to the parent process. PollingObserver is safe.
          - On other platforms we keep the default Observer (inotify on Linux,
            ReadDirectoryChangesW on Windows).
          - ``JARVIS_FILE_WATCH_BACKEND`` env var overrides: ``polling``,
            ``native``, or ``auto`` (default).
        """
        try:
            from watchdog.observers import Observer
            from watchdog.observers.polling import PollingObserver
            from watchdog.events import FileSystemEventHandler, FileSystemEvent
        except ImportError:
            raise RuntimeError("watchdog package required: pip install watchdog")

        backend_pref = os.environ.get("JARVIS_FILE_WATCH_BACKEND", "auto").lower()
        import platform
        if backend_pref == "polling":
            observer_cls = PollingObserver
            backend_name = "polling (forced)"
        elif backend_pref == "native":
            observer_cls = Observer
            backend_name = "native (forced)"
        else:  # auto
            if platform.system() == "Darwin":
                observer_cls = PollingObserver
                backend_name = "polling (macOS auto)"
            else:
                observer_cls = Observer
                backend_name = "native (auto)"

        # Create handler that bridges to async
        guard = self

        class AsyncEventHandler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent):
                if event.is_directory and event.event_type != "created":
                    return

                try:
                    # Convert to our event type
                    event_type = {
                        "created": FileEventType.CREATED,
                        "modified": FileEventType.MODIFIED,
                        "deleted": FileEventType.DELETED,
                        "moved": FileEventType.MOVED,
                    }.get(event.event_type)

                    if not event_type:
                        return

                    file_event = FileEvent(
                        event_type=event_type,
                        path=Path(event.src_path),
                        is_directory=event.is_directory,
                        old_path=Path(event.dest_path) if hasattr(event, "dest_path") else None,
                    )

                    # Queue for async processing
                    guard._queue_event(file_event)

                except Exception as e:
                    logger.error(f"[FileWatchGuard] Event handler error: {e}")

        self._observer = observer_cls()
        self._backend_name = backend_name
        handler = AsyncEventHandler()

        # --- Narrow scheduling: skip venv / worktree / cache noise ------
        #
        # Instead of scheduling the repo root recursively (which forces
        # PollingObserver to snapshot 56K+ files including venvs), we
        # schedule each top-level subdirectory individually and drop the
        # high-noise ones. See ``FileWatchConfig.exclude_top_level_dirs``.
        # When a depth-1 dir has a nested excluded child (e.g.
        # ``backend/venv``), we schedule its grandchildren recursively
        # AND the dir itself non-recursively so file-level events at the
        # parent's depth (e.g. ``backend/_probe.py``) still fire without
        # dragging the nested venv into the snapshot.
        excluded = self._resolve_excluded_dirs()
        excluded_path_patterns = self._resolve_excluded_path_patterns()
        # Slice 12J — resolve the schedule cap from env first so the
        # operator escape hatch (``=0`` → unbounded) works even with
        # the dataclass-default field set.
        max_scheduled_roots = self._resolve_max_scheduled_roots()
        resolved = self._resolve_watch_paths(
            excluded, excluded_path_patterns,
            max_scheduled_roots=max_scheduled_roots,
        )
        scheduled_paths = resolved.paths
        skipped_by_pattern = resolved.skipped_by_pattern
        candidate_count = resolved.candidate_count
        coalesced_count = resolved.coalesced_count

        scheduled_ok: List[Tuple[Path, bool]] = []
        for path, recursive in scheduled_paths:
            try:
                self._observer.schedule(handler, str(path), recursive=recursive)
                scheduled_ok.append((path, recursive))
            except Exception as exc:
                logger.warning(
                    "[FileWatchGuard] schedule(%s, recursive=%s) failed: %s",
                    path, recursive, exc,
                )

        # Also schedule the root NON-recursively so top-level file changes
        # (e.g. repo-root config files) still fire events without dragging
        # the venv subtrees into the snapshot. Slice 12J: this single
        # extra schedule sits OUTSIDE the budget — per operator binding:
        # "Observer.schedule is never called more than max cap + root
        # nonrecursive if that remains separate."
        try:
            self._observer.schedule(
                handler, str(self.watch_dir), recursive=False,
            )
        except Exception as exc:
            logger.debug(
                "[FileWatchGuard] non-recursive root schedule failed: %s", exc,
            )

        self._scheduled_paths: List[Tuple[Path, bool]] = scheduled_ok

        # Slice 12J — startup telemetry. Operators can read these
        # lines at boot to verify:
        #   * the schedule budget is being enforced (scheduled <= cap)
        #   * which generated roots got excluded by name vs pattern
        #   * whether the watchdog ``PollingObserver`` fallback is
        #     active (the bt-2026-05-22-232553 wedge surface)
        #   * how many nested-venv splits had to be coalesced to fit
        recursive_count = sum(1 for _, r in scheduled_ok if r)
        polling_fallback_active = (
            backend_name.lower().startswith("polling")
            or "polling" in backend_name.lower()
        )
        self._observer.start()
        logger.info(
            "[FileWatchGuard] Observer backend: %s (polling_fallback=%s), "
            "candidate_roots=%d scheduled_roots=%d "
            "max_scheduled_roots=%s coalesced_roots=%d "
            "(recursive=%d, non_recursive=%d, "
            "excluded_top_level=%s, "
            "excluded_path_patterns=%s, "
            "skipped_by_pattern=%d)",
            backend_name,
            polling_fallback_active,
            candidate_count,
            len(scheduled_ok),
            max_scheduled_roots if max_scheduled_roots > 0 else "unbounded",
            coalesced_count,
            recursive_count,
            len(scheduled_ok) - recursive_count,
            sorted(excluded) if excluded else "(none)",
            sorted(excluded_path_patterns) if excluded_path_patterns else "(none)",
            skipped_by_pattern,
        )

        # Slice 12J — schedule_budget_coalesced WARNING. Surface
        # ONCE per boot when the cap actually engaged so operators
        # see the tradeoff in their dashboards. Quiet when the
        # candidate plan was already within budget.
        if coalesced_count > 0:
            logger.warning(
                "[FileWatchGuard] schedule_budget_coalesced "
                "candidate=%d scheduled=%d cap=%d coalesced_groups=%d — "
                "%d nested-venv-split group(s) collapsed to recursive "
                "parent schedule(s) to stay under the polling-thread "
                "budget. Coalesced subtrees still drop events via "
                "ignore_patterns; pattern-descent groups (Slice 12I "
                "SWE worktree exclusions) were preserved.",
                candidate_count, len(scheduled_ok),
                max_scheduled_roots, coalesced_count, coalesced_count,
            )

        # Slice 12I — runaway-watching early warning (preserved).
        # Even with the Slice 12J HARD cap, the CANDIDATE count
        # remains an operator signal: many candidates → many
        # coalescings → coarser-grained event filtering. Threshold
        # is env-overridable via
        # ``JARVIS_FILE_WATCH_HIGH_COUNT_WARN``.
        try:
            high_warn = int(
                os.environ.get(
                    "JARVIS_FILE_WATCH_HIGH_COUNT_WARN",
                    str(self.config.high_watch_count_warn),
                )
            )
        except ValueError:
            high_warn = self.config.high_watch_count_warn
        if candidate_count > high_warn:
            logger.warning(
                "[FileWatchGuard] runaway-watching guard: %d candidate "
                "roots (> %d threshold; scheduled=%d after budget). "
                "Inspect exclude_top_level_dirs / exclude_path_patterns; "
                "the PollingObserver fallback walks every scheduled root.",
                candidate_count, high_warn, len(scheduled_ok),
            )

    async def _stop_watchdog(self) -> None:
        """Stop the watchdog observer."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

    # ------------------------------------------------------------------
    # Narrow-scheduling helpers (fixes PollingObserver-at-scale failure)
    # ------------------------------------------------------------------

    def _resolve_excluded_dirs(self) -> frozenset:
        """Resolve which top-level directory names to exclude.

        Env override ``JARVIS_FILE_WATCH_EXCLUDE_DIRS`` takes precedence
        over the config default. A blank value falls back to config.
        Useful for adding project-specific noise directories (e.g. a
        build cache under a nonstandard name) without changing code.
        """
        env_override = os.environ.get(
            "JARVIS_FILE_WATCH_EXCLUDE_DIRS", "",
        ).strip()
        if env_override:
            return frozenset(
                d.strip() for d in env_override.split(",") if d.strip()
            )
        return self.config.exclude_top_level_dirs

    def _resolve_max_scheduled_roots(self) -> int:
        """Slice 12J — resolve the hard schedule budget.

        Env override ``JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS`` takes
        precedence over the config default. Invalid (non-integer)
        values fall back to the config default rather than raise —
        operators should not be able to brick FileWatchGuard boot
        by mistyping an env value.

        Value ``0`` is the explicit "unbounded" escape hatch. Any
        positive integer is honored verbatim. Negative integers are
        clamped to ``0`` (treated as "unbounded") for the same
        reason a stray ``-30`` shouldn't crash boot.
        """
        raw = os.environ.get(
            "JARVIS_FILE_WATCH_MAX_SCHEDULED_ROOTS", "",
        ).strip()
        if not raw:
            return self.config.max_scheduled_roots
        try:
            parsed = int(raw)
        except ValueError:
            return self.config.max_scheduled_roots
        return max(0, parsed)

    def _resolve_excluded_path_patterns(self) -> frozenset:
        """Slice 12I — resolve repo-relative path patterns to exclude.

        Env override semantics differ from ``_resolve_excluded_dirs``:
        ``JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS`` is treated as an
        ADDITIVE extension to the config defaults, NOT a replacement.
        Operators can extend SWE-Bench-Pro-style transient roots
        without losing the built-in protection for the worktree dir.

        Each pattern is a repo-relative posix path; matching is done
        by ``Path.parts`` prefix in ``_resolve_watch_paths`` so both
        the pattern root and every descendant get excluded.

        Returns the union of config defaults + env additions, with
        each pattern normalized: leading ``./`` stripped, leading
        ``/`` stripped (always treated as repo-relative), trailing
        ``/`` stripped, posix-form. Blank entries silently dropped.
        """
        def _normalize(raw: str) -> str:
            p = raw.strip().replace("\\", "/")
            while p.startswith("./"):
                p = p[2:]
            while p.startswith("/"):
                p = p[1:]
            while p.endswith("/"):
                p = p[:-1]
            return p

        merged: Set[str] = {
            _normalize(p) for p in self.config.exclude_path_patterns
            if _normalize(p)
        }
        env_additions = os.environ.get(
            "JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", "",
        ).strip()
        if env_additions:
            for chunk in env_additions.split(","):
                norm = _normalize(chunk)
                if norm:
                    merged.add(norm)
        return frozenset(merged)

    def _path_matches_pattern(
        self,
        path: Path,
        patterns: frozenset,
    ) -> bool:
        """Slice 12I — repo-relative parts-prefix match.

        Returns True iff ``path`` resolves to ``watch_dir / <pattern>``
        OR is a descendant of any such pattern root. Operates on
        ``Path.parts`` tuples so the comparison is path-component-safe
        (a pattern ``.jarvis/swe`` cannot accidentally match a sibling
        directory ``.jarvis/swe_bench_pro`` because the comparison is
        by tuple-prefix, not string-prefix).

        Patterns are repo-relative; ``path`` may be absolute or
        relative to ``watch_dir`` — both forms handled. If ``path``
        is not under ``watch_dir`` at all, returns False (don't filter
        unrelated paths).
        """
        if not patterns:
            return False
        try:
            rel = path.relative_to(self.watch_dir)
        except ValueError:
            return False
        rel_parts = rel.parts
        if not rel_parts:
            return False
        for pattern in patterns:
            pat_parts = tuple(
                part for part in pattern.split("/") if part
            )
            if not pat_parts:
                continue
            if len(rel_parts) >= len(pat_parts) and \
                    rel_parts[:len(pat_parts)] == pat_parts:
                return True
        return False

    def _resolve_watch_paths(
        self,
        excluded: frozenset,
        excluded_path_patterns: frozenset = frozenset(),
        max_scheduled_roots: Optional[int] = None,
    ) -> "_ResolvedSchedule":
        """Resolve the narrowed set of directories to schedule for watching.

        Returns a list of ``(path, recursive)`` tuples. Callers pass each
        to ``observer.schedule(handler, path, recursive=recursive)``.

        Key insight: excluded directory names (``venv``, ``.venv``, ...)
        can appear at ANY depth, not just the repo root. Specifically,
        this repo has both ``./venv`` and ``./backend/venv`` — the latter
        would be dragged in if we scheduled ``./backend`` recursively,
        undoing the whole point of the narrow-scope fix.

        Algorithm (bounded descent, practical depth only):
          * Depth 1: iterate ``watch_dir.iterdir()``.
          * If a depth-1 child's name is excluded, skip entirely.
          * Else, if that child's immediate children contain ANY excluded
            names, schedule each NON-excluded grandchild individually
            **recursively** AND schedule the descended parent
            **non-recursively** so file-level events at that depth are
            still delivered. This catches nested venvs like
            ``backend/venv`` without creating a blind spot for files
            directly under ``backend/``.
          * Else, schedule the depth-1 dir recursively (normal case).

        Depth ≥ 3 excluded directories (e.g. ``backend/core/venv``) are
        NOT handled here — they remain covered by the post-event
        ``ignore_patterns`` filter. In practice deep nested venvs are
        vanishingly rare in JARVIS-layout repos.

        Slice 12I addition: ``excluded_path_patterns`` is a frozenset
        of repo-relative posix paths; any path (depth-1 OR depth-2)
        whose ``Path.parts`` tuple-prefix matches one of these patterns
        is dropped from the schedule. This is defense-in-depth for
        ``.jarvis/swe_bench_pro/worktrees`` even when ``.jarvis`` is
        explicitly re-included via env override.

        Slice 12J addition: ``max_scheduled_roots`` enforces a HARD
        cap on the total schedule count. If the candidate plan
        exceeds the cap, nested-venv-split groups are coalesced back
        to their parent recursive schedule (largest savings first)
        until the count is within budget. Pattern-descent groups
        (Slice 12I load-bearing) are PROTECTED from coalescing — they
        carry the SWE-Bench-Pro worktree exclusion that the original
        56K-file wedge depended on. Returns a ``_ResolvedSchedule``
        NamedTuple so the boot log can surface candidate /
        scheduled / coalesced telemetry to the operator.

        Missing root → empty schedule (caller's observer.start()
        still succeeds; health loop will notice and recreate).
        """
        if max_scheduled_roots is None:
            max_scheduled_roots = self.config.max_scheduled_roots

        if not self.watch_dir.exists():
            return _ResolvedSchedule(
                paths=[],
                skipped_by_pattern=0,
                candidate_count=0,
                coalesced_count=0,
            )
        try:
            depth1 = sorted(self.watch_dir.iterdir())
        except OSError as exc:
            logger.warning(
                "[FileWatchGuard] iterdir(%s) failed: %s — "
                "falling back to single-root schedule",
                self.watch_dir, exc,
            )
            return _ResolvedSchedule(
                paths=[(self.watch_dir, True)],
                skipped_by_pattern=0,
                candidate_count=1,
                coalesced_count=0,
            )

        # Slice 12I — pre-tokenize patterns into ``Path.parts`` tuples
        # once. Used for ancestor/descendant relationship checks
        # against repo-relative entry paths. Empty tuples (from
        # ``""``) are dropped so they never match every path.
        pattern_parts_list: List[Tuple[str, ...]] = [
            tuple(p for p in pat.split("/") if p)
            for pat in excluded_path_patterns
        ]
        pattern_parts_list = [p for p in pattern_parts_list if p]

        def _matches_pattern(rel_parts: Tuple[str, ...]) -> bool:
            """rel_parts is AT or BELOW any pattern root."""
            for pat in pattern_parts_list:
                if len(rel_parts) >= len(pat) and \
                        rel_parts[:len(pat)] == pat:
                    return True
            return False

        def _has_pattern_descendant(rel_parts: Tuple[str, ...]) -> bool:
            """Some pattern root is STRICTLY UNDER rel_parts. Means
            we can't schedule rel_parts recursively — must descend
            and route around the pattern."""
            for pat in pattern_parts_list:
                if len(rel_parts) < len(pat) and \
                        pat[:len(rel_parts)] == rel_parts:
                    return True
            return False

        # Slice 12I — bounded recursive descent for pattern-aware
        # routing. Used when an entry contains a pattern descendant
        # at depth > 2 (e.g. ``.jarvis/swe_bench_pro/worktrees``).
        # Stops at pattern roots (counter++) and name-excluded dirs
        # (silent). Depth budget caps unbounded recursion; in
        # practice the deepest known pattern (worktrees) is 3
        # components so budget 6 is generous.
        _MAX_PATTERN_DESCENT_DEPTH = 6
        skipped_by_pattern = 0

        def _walk_with_patterns(
            entry: Path,
            depth_budget: int,
            sink: List[Tuple[Path, bool]],
        ) -> int:
            """Returns the number of pattern-root drops it performed
            so the caller can aggregate them into the group telemetry.
            """
            try:
                rel_parts = entry.relative_to(self.watch_dir).parts
            except ValueError:
                return 0
            if _matches_pattern(rel_parts):
                return 1
            if depth_budget <= 0 or \
                    not _has_pattern_descendant(rel_parts):
                # Safe to schedule recursively — no pattern below.
                sink.append((entry, True))
                return 0
            # Has pattern descendants — schedule non-recursively so
            # file-level events at this depth still fire, and walk
            # children individually to route around pattern roots.
            sink.append((entry, False))
            drops = 0
            try:
                children = list(entry.iterdir())
            except (OSError, PermissionError):
                return drops
            for child in sorted(children):
                if not child.is_dir():
                    continue
                if child.name in excluded:
                    continue
                drops += _walk_with_patterns(
                    child, depth_budget - 1, sink,
                )
            return drops

        # Slice 12J — Phase 1: build groups (no budget enforcement
        # yet). Each depth-1 entry produces at most ONE group; the
        # group's ``entries`` list is the flat schedule contribution.
        groups: List[_ScheduleGroup] = []

        for entry in depth1:
            if not entry.is_dir() or entry.name in excluded:
                continue
            try:
                entry_rel = entry.relative_to(self.watch_dir).parts
            except ValueError:
                entry_rel = ()
            # Depth-1 pattern match (e.g. operator added a top-level
            # dir as a pattern). Drop entirely.
            if _matches_pattern(entry_rel):
                skipped_by_pattern += 1
                continue
            # Slice 12I path-pattern descent (PROTECTED group).
            if _has_pattern_descendant(entry_rel):
                sink: List[Tuple[Path, bool]] = []
                drops = _walk_with_patterns(
                    entry, _MAX_PATTERN_DESCENT_DEPTH, sink,
                )
                skipped_by_pattern += drops
                if sink:
                    groups.append(_ScheduleGroup(
                        parent=entry,
                        entries=tuple(sink),
                        kind=_SCHEDULE_GROUP_KIND_PATTERN_DESCENT,
                    ))
                continue
            # Legacy depth-2 peek for nested-venv-style excluded
            # name children (COALESCABLE group).
            try:
                depth2 = list(entry.iterdir())
            except (OSError, PermissionError):
                # Can't peek — safer to include the dir as-is.
                groups.append(_ScheduleGroup(
                    parent=entry,
                    entries=((entry, True),),
                    kind=_SCHEDULE_GROUP_KIND_SIMPLE_RECURSIVE,
                ))
                continue
            has_nested_excluded = any(
                child.is_dir() and child.name in excluded
                for child in depth2
            )
            if has_nested_excluded:
                # Schedule non-excluded grandchildren recursively
                # AND the parent itself non-recursively.
                split_entries: List[Tuple[Path, bool]] = [
                    (entry, False),
                ]
                for grand in sorted(depth2):
                    if grand.is_dir() and grand.name not in excluded:
                        split_entries.append((grand, True))
                groups.append(_ScheduleGroup(
                    parent=entry,
                    entries=tuple(split_entries),
                    kind=_SCHEDULE_GROUP_KIND_NESTED_VENV_SPLIT,
                ))
            else:
                groups.append(_ScheduleGroup(
                    parent=entry,
                    entries=((entry, True),),
                    kind=_SCHEDULE_GROUP_KIND_SIMPLE_RECURSIVE,
                ))

        # Slice 12J — Phase 2: enforce schedule budget by coalescing
        # NESTED_VENV_SPLIT groups (largest savings first). The
        # candidate count is fixed BEFORE coalescing for telemetry;
        # ``current`` tracks the live count as we coalesce.
        candidate_count = sum(len(g.entries) for g in groups)
        coalesced_count = 0
        # ``max_scheduled_roots <= 0`` is the operator escape hatch
        # (legacy unbounded behavior; opt-out of the cap entirely).
        if max_scheduled_roots > 0 and candidate_count > max_scheduled_roots:
            # Sort coalescable groups by descending entry count so we
            # take the biggest savings first. Indices preserved so
            # we can mutate ``groups`` in-place without disturbing
            # ordering of non-coalesced entries.
            coalescable_indices = sorted(
                (
                    i for i, g in enumerate(groups)
                    if g.kind == _SCHEDULE_GROUP_KIND_NESTED_VENV_SPLIT
                ),
                key=lambda i: -len(groups[i].entries),
            )
            current = candidate_count
            for i in coalescable_indices:
                if current <= max_scheduled_roots:
                    break
                old_group = groups[i]
                savings = len(old_group.entries) - 1
                if savings <= 0:
                    continue
                groups[i] = _ScheduleGroup(
                    parent=old_group.parent,
                    entries=((old_group.parent, True),),
                    kind=_SCHEDULE_GROUP_KIND_COALESCED,
                )
                current -= savings
                coalesced_count += 1

        # Slice 12J — Phase 3: flatten groups into the final
        # ``observer.schedule()`` plan, preserving depth-1 order so
        # operator log lines stay alphabetical.
        paths: List[Tuple[Path, bool]] = []
        for g in groups:
            paths.extend(g.entries)

        return _ResolvedSchedule(
            paths=paths,
            skipped_by_pattern=skipped_by_pattern,
            candidate_count=candidate_count,
            coalesced_count=coalesced_count,
        )

    # ---- Slice 12A — loop-thread enqueue wrapper -----------------
    #
    # ``_queue_event_on_loop`` runs on the asyncio event-loop thread
    # (scheduled by ``_queue_event`` via ``call_soon_threadsafe``).
    # It is the SOLE permitted target for that ``call_soon_threadsafe``
    # call; the AST pin in the paired test surface enforces this.
    #
    # Why a wrapper instead of ``self._event_queue.put_nowait``:
    #
    # The producer publishes from a watchdog observer thread under
    # bursty FS load (e.g., OpportunityMiner scanning 760+ .py files
    # in one cycle). When the bounded queue overflows, ``put_nowait``
    # raises ``asyncio.QueueFull`` synchronously. If that callable is
    # the callback handed to ``call_soon_threadsafe``, asyncio catches
    # the raise as an unhandled callback exception and routes it to
    # its default exception handler — which formats the full Python
    # traceback and logs it on the loop thread. Empirically that
    # happened ~16k times in bt-2026-05-22-074210, sustaining
    # 100+ seconds of cumulative loop-block.
    #
    # The wrapper swallows ``QueueFull`` (and any other exception)
    # before asyncio sees it, increments structured metrics, and
    # rate-limits the warning log so the loop is never starved by
    # logging overhead.

    def _queue_event_on_loop(self, event: FileEvent) -> None:
        """Loop-thread enqueue wrapper. Catches ``asyncio.QueueFull``
        + any other exception so the asyncio default exception
        handler is never invoked from this codepath.

        Metrics:
          * ``events_dropped_queue_full`` — total drops.
          * ``queue_full_suppressed_logs`` — drops in the current
            rate-limit window that were NOT logged.
          * ``last_overflow_at`` — wall-clock ``time.time()`` of
            the most recent drop (for ``/health`` consumers).
          * ``last_overflow_log_at`` — monotonic timestamp of the
            most recent emitted summary (rate-limit gate).

        NEVER raises. The behaviour contract: on success, the event
        is enqueued and the function returns. On overflow, the
        event is dropped (downstream debounce + periodic scan
        recover any drift), metrics are bumped, and a summary log
        may be emitted (≤1 per second per FileWatchGuard instance).
        """
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.metrics.events_dropped_queue_full += 1
            self.metrics.last_overflow_at = time.time()
            now_mono = time.monotonic()
            last_log = self.metrics.last_overflow_log_at
            if last_log is None or (now_mono - last_log) >= 1.0:
                logger.warning(
                    "[FileWatchGuard] file_watch_events overflow: "
                    "dropped=%d (suppressed %d in last window)",
                    self.metrics.events_dropped_queue_full,
                    self.metrics.queue_full_suppressed_logs,
                )
                self.metrics.last_overflow_log_at = now_mono
                self.metrics.queue_full_suppressed_logs = 0
            else:
                self.metrics.queue_full_suppressed_logs += 1
        except Exception as exc:  # noqa: BLE001 — never raise on loop
            # Defensive: catch anything else so asyncio's default
            # handler is never invoked from this callback.
            logger.debug(
                "[FileWatchGuard] _queue_event_on_loop unexpected "
                "error (handled): %s: %s",
                type(exc).__name__, exc,
            )

    def _queue_event(self, event: FileEvent) -> None:
        """
        Queue an event for processing (called from watchdog thread).

        v2.1 (v16.0): ROOT CAUSE FIX for "There is no current event loop in thread" error.

        The error occurs because:
        1. Watchdog callbacks run in a background thread (Thread-24, Thread-22, etc.)
        2. asyncio.Queue operations require the event loop
        3. The thread doesn't have an event loop by default

        Fix: Use call_soon_threadsafe with proper None checks and defensive handling.
        Also use a thread-safe fallback queue when async queue isn't available.

        Slice 12A: ``call_soon_threadsafe`` targets
        ``_queue_event_on_loop`` (wrapper), not ``put_nowait``
        directly — so ``asyncio.QueueFull`` never leaks into the
        default exception handler.
        """
        # v2.1: First check if we have a valid main loop reference
        if self._main_loop is None:
            # No main loop captured - this means start() wasn't called properly
            logger.debug("[FileWatchGuard] No main loop captured, event may be lost")
            return

        try:
            # v2.1: Check if loop is still running AND not closed
            if not self._main_loop.is_running():
                logger.debug("[FileWatchGuard] Main event loop not running")
                return

            if self._main_loop.is_closed():
                logger.debug("[FileWatchGuard] Main event loop is closed")
                return

            # v2.1: Thread-safe call into the main event loop.
            #
            # Slice 12A: target is the FileWatchGuard-owned wrapper
            # ``_queue_event_on_loop``, NOT ``self._event_queue.put_nowait``
            # directly. The wrapper runs on the loop thread, catches
            # ``asyncio.QueueFull`` instead of leaking it into asyncio's
            # default exception handler, increments overflow metrics, and
            # emits rate-limited summary logs. This is the producer-side
            # fix for the loop-starvation cascade observed in
            # bt-2026-05-22-074210 (16k+ QueueFull tracebacks formatted
            # on-loop). AST-pinned in the paired test surface — must NOT
            # regress to passing ``put_nowait`` as the callback target.
            self._main_loop.call_soon_threadsafe(
                self._queue_event_on_loop, event
            )

        except RuntimeError as e:
            # v2.1: Handle specific runtime errors
            error_str = str(e).lower()
            if "closed" in error_str:
                logger.debug("[FileWatchGuard] Event loop closed, ignoring event")
            elif "no current event loop" in error_str or "no running event loop" in error_str:
                # This shouldn't happen with our fix, but handle it gracefully
                logger.debug("[FileWatchGuard] Event loop not available in thread (expected for watchdog)")
            else:
                logger.warning(f"[FileWatchGuard] Queue RuntimeError: {e}")

        except Exception as e:
            # v2.1: Catch-all for unexpected errors - log at debug to avoid spam
            logger.debug(f"[FileWatchGuard] Queue error (handled): {type(e).__name__}: {e}")

    async def _process_events(self) -> None:
        """Process events from queue with debouncing and deduplication."""
        batch_deadline = 0.0
        batch: List[FileEvent] = []

        while self._running:
            try:
                # Get event with timeout
                timeout = self.config.batch_timeout_seconds
                if batch:
                    timeout = max(0, batch_deadline - time.time())

                try:
                    event = await asyncio.wait_for(
                        self._event_queue.get(),
                        timeout=timeout,
                    )
                    self.metrics.events_received += 1

                    # Apply filters
                    if not self._should_process(event):
                        self.metrics.events_filtered += 1
                        continue

                    # Check deduplication
                    if self._is_duplicate(event):
                        self.metrics.events_deduplicated += 1
                        continue

                    # Add to batch
                    batch.append(event)
                    if not batch_deadline:
                        batch_deadline = time.time() + self.config.debounce_seconds

                except asyncio.TimeoutError:
                    pass

                # Process batch if ready
                if batch and (
                    time.time() >= batch_deadline
                    or len(batch) >= 10  # Max batch size
                ):
                    await self._process_batch(batch)
                    batch = []
                    batch_deadline = 0.0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FileWatchGuard] Processing error: {e}")
                self.metrics.errors += 1
                self._consecutive_errors += 1
                self._last_error = e

                if self._on_error:
                    try:
                        result = self._on_error(e)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass

                if self._consecutive_errors >= self.config.max_consecutive_errors:
                    await self._handle_error_overflow()

                await asyncio.sleep(self.config.error_backoff_seconds)

    def _should_process(self, event: FileEvent) -> bool:
        """Check if event should be processed based on patterns."""
        path = event.path
        name = path.name

        # Check ignore patterns
        import fnmatch

        for pattern in self.config.ignore_patterns:
            if fnmatch.fnmatch(name, pattern):
                return False

        # Check include patterns
        if self.config.patterns:
            matched = False
            for pattern in self.config.patterns:
                if fnmatch.fnmatch(name, pattern):
                    matched = True
                    break
            if not matched:
                return False

        return True

    def _is_duplicate(self, event: FileEvent) -> bool:
        """Check if event is a duplicate."""
        event_id = event.event_id
        now = time.time()

        # Check if we've seen this event recently
        if event_id in self._seen_events:
            seen_time = self._seen_events[event_id]
            if now - seen_time < self.config.dedup_ttl_seconds:
                return True

        # Update LRU cache
        self._seen_events[event_id] = now

        # Maintain cache size
        while len(self._seen_events) > self.config.dedup_cache_size:
            self._seen_events.popitem(last=False)  # Remove oldest

        return False

    async def _process_batch(self, events: List[FileEvent]) -> None:
        """Process a batch of events."""
        # Consolidate events for same file
        by_path: Dict[str, FileEvent] = {}
        for event in events:
            path_key = str(event.path)

            # Later events override earlier ones
            if event.event_type == FileEventType.DELETED:
                # Delete supersedes all
                by_path[path_key] = event
            elif event.event_type == FileEventType.CREATED:
                # Create only if not already have newer event
                if path_key not in by_path:
                    by_path[path_key] = event
            else:
                # Modified replaces create
                existing = by_path.get(path_key)
                if not existing or existing.event_type != FileEventType.DELETED:
                    by_path[path_key] = event

        # Process each unique event
        for event in by_path.values():
            await self._process_single_event(event)

    async def _process_single_event(self, event: FileEvent) -> None:
        """
        Process a single event.

        v2.0: Also notifies secondary handlers for shared watching.
        """
        start_time = time.time()

        try:
            # For modifications, verify file is stable and content changed
            if event.event_type == FileEventType.MODIFIED:
                if self.config.verify_checksum:
                    if not await self._verify_content_changed(event):
                        return

            # Wait for file to be stable
            if event.event_type in (FileEventType.CREATED, FileEventType.MODIFIED):
                if not event.is_directory:
                    await self._wait_for_stable(event.path)

            # Add file info
            if event.path.exists() and not event.is_directory:
                event.size = event.path.stat().st_size

            # Call primary handler
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result

            # v2.0: Call secondary handlers (for shared watching)
            if hasattr(self, "_secondary_handlers"):
                for handler in self._secondary_handlers:
                    try:
                        handler_result = handler(event)
                        if asyncio.iscoroutine(handler_result):
                            await handler_result
                    except Exception as handler_err:
                        logger.warning(f"[FileWatchGuard] Secondary handler error: {handler_err}")

            self.metrics.events_processed += 1
            self.metrics.last_event_time = time.time()
            self._consecutive_errors = 0

            # Update processing time metric
            processing_ms = (time.time() - start_time) * 1000
            total = (
                self.metrics.avg_processing_time_ms * (self.metrics.events_processed - 1)
                + processing_ms
            )
            self.metrics.avg_processing_time_ms = total / self.metrics.events_processed

        except Exception as e:
            logger.error(f"[FileWatchGuard] Event handler error for {event.path}: {e}")
            self.metrics.errors += 1
            raise

    async def _verify_content_changed(self, event: FileEvent) -> bool:
        """Verify file content actually changed (avoid false positives)."""
        path = event.path
        path_key = str(path)

        if not path.exists():
            return True

        try:
            content = await asyncio.to_thread(path.read_bytes)
            new_checksum = hashlib.md5(content).hexdigest()

            old_checksum = self._checksums.get(path_key)
            self._checksums[path_key] = new_checksum

            if old_checksum and old_checksum == new_checksum:
                # Content didn't change
                return False

            event.checksum = new_checksum
            return True

        except Exception:
            return True  # Assume changed on error

    async def _wait_for_stable(self, path: Path) -> None:
        """Wait for file to stop being written."""
        if not path.exists():
            return

        if self.config.min_stable_seconds <= 0:
            return

        last_size = -1
        stable_start = 0.0

        while True:
            try:
                current_size = path.stat().st_size
                now = time.time()

                if current_size != last_size:
                    last_size = current_size
                    stable_start = now
                elif now - stable_start >= self.config.min_stable_seconds:
                    # File is stable
                    return

            except FileNotFoundError:
                # File was deleted
                return

            await asyncio.sleep(0.01)

            # Timeout after 5 seconds
            if not stable_start or time.time() - stable_start > 5.0:
                return

    async def _handle_error_overflow(self) -> None:
        """Handle too many consecutive errors."""
        logger.warning(
            f"[FileWatchGuard] {self._consecutive_errors} consecutive errors, "
            f"restarting watcher"
        )

        if self.config.restart_on_error:
            self.metrics.restarts += 1
            await self._stop_watchdog()
            await asyncio.sleep(self.config.error_backoff_seconds)

            try:
                await self._start_watchdog()
                self._consecutive_errors = 0
            except Exception as e:
                logger.error(f"[FileWatchGuard] Restart failed: {e}")

    async def _health_check_loop(self) -> None:
        """Background health check."""
        while self._running:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                # Check watchdog is alive
                if self._observer and not self._observer.is_alive():
                    logger.warning("[FileWatchGuard] Observer died, restarting")
                    self.metrics.restarts += 1
                    await self._stop_watchdog()
                    await self._start_watchdog()

                # Check directory exists
                if not self.watch_dir.exists():
                    logger.warning(
                        f"[FileWatchGuard] Watch directory disappeared: {self.watch_dir}"
                    )
                    self.watch_dir.mkdir(parents=True, exist_ok=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FileWatchGuard] Health check error: {e}")

    @property
    def is_healthy(self) -> bool:
        """Check if watcher is healthy."""
        if not self._running:
            return False

        if self._observer and not self._observer.is_alive():
            return False

        if self._consecutive_errors >= self.config.max_consecutive_errors:
            return False

        return True

    def get_metrics(self) -> Dict[str, Any]:
        """Get watcher metrics."""
        return {
            "watch_dir": str(self.watch_dir),
            "is_running": self._running,
            "is_healthy": self.is_healthy,
            "events_received": self.metrics.events_received,
            "events_processed": self.metrics.events_processed,
            "events_deduplicated": self.metrics.events_deduplicated,
            "events_filtered": self.metrics.events_filtered,
            "errors": self.metrics.errors,
            "restarts": self.metrics.restarts,
            "consecutive_errors": self._consecutive_errors,
            "last_event_time": self.metrics.last_event_time,
            "avg_processing_time_ms": round(self.metrics.avg_processing_time_ms, 2),
            "dedup_cache_size": len(self._seen_events),
            "queue_size": self._event_queue.qsize(),
            "last_error": str(self._last_error) if self._last_error else None,
        }

    async def trigger_scan(self) -> int:
        """
        Manually scan directory and emit events for existing files.

        Useful for catching up after restart.

        Returns:
            Number of events emitted
        """
        count = 0

        for pattern in self.config.patterns:
            if self.config.recursive:
                files = self.watch_dir.rglob(pattern)
            else:
                files = self.watch_dir.glob(pattern)

            for path in files:
                if path.is_file():
                    event = FileEvent(
                        event_type=FileEventType.CREATED,
                        path=path,
                        is_directory=False,
                    )
                    if self._should_process(event):
                        self._queue_event(event)
                        count += 1

        logger.info(f"[FileWatchGuard] Triggered scan, found {count} files")
        return count
