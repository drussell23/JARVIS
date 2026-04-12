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
from typing import Any, Callable, Dict, List, Optional, Set

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
        self._event_queue: asyncio.Queue[FileEvent] = (
            BoundedAsyncQueue(maxsize=500, policy=OverflowPolicy.WARN_AND_BLOCK, name="file_watch_events")
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

        self._observer.schedule(
            handler,
            str(self.watch_dir),
            recursive=self.config.recursive,
        )
        self._observer.start()
        logger.info(f"[FileWatchGuard] Observer backend: {backend_name}")

    async def _stop_watchdog(self) -> None:
        """Stop the watchdog observer."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

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

            # v2.1: Thread-safe call into the main event loop
            # put_nowait is safe to call from another thread via call_soon_threadsafe
            self._main_loop.call_soon_threadsafe(
                self._event_queue.put_nowait, event
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
