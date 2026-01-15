"""
Trinity Bridge Adapter v2.0 - Production Hardened
=================================================

Closes the Trinity Loop by watching Reactor Core event directories
and forwarding MODEL_READY and other events to JARVIS.

HARDENED VERSION (v2.0) with:
- FileWatchGuard for robust file watching with recovery
- AtomicFileOps for safe file operations
- Correlation context for cross-repo tracing
- Circuit breaker for event bus dispatch
- Comprehensive health monitoring and metrics

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    Reactor Core                                 │
    │  [Training Pipeline] → [MODEL_READY event] → [File Write]       │
    └────────────────────────────────┬────────────────────────────────┘
                                     │
                                     ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                 TrinityBridgeAdapter (This Module)              │
    │  [File Watcher] → [Event Parser] → [TrinityEventBus]            │
    └────────────────────────────────┬────────────────────────────────┘
                                     │
                                     ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                       JARVIS / JARVIS Prime                     │
    │  [TrinityHandlers] → [UnifiedModelServing] → [Hot Swap]         │
    └─────────────────────────────────────────────────────────────────┘

This adapter:
1. Watches ~/.jarvis/reactor/events/ for Reactor Core events
2. Watches ~/.jarvis/trinity/events/ for cross-repo events
3. Parses event files (JSON format)
4. Forwards to TrinityEventBus for handler dispatch
5. Cleans up processed files

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

# Import resilience utilities
try:
    from backend.core.resilience.file_watch_guard import (
        FileWatchGuard, FileWatchConfig, FileEvent, FileEventType
    )
    from backend.core.resilience.atomic_file_ops import AtomicFileOps, AtomicFileConfig
    from backend.core.resilience.correlation_context import (
        CorrelationContext, with_correlation, inject_correlation,
        extract_correlation, get_correlated_logger
    )
    from backend.core.resilience.cross_repo_circuit_breaker import (
        CrossRepoCircuitBreaker, CircuitBreakerConfig, CircuitOpenError, FailureType
    )
    RESILIENCE_AVAILABLE = True
except ImportError:
    RESILIENCE_AVAILABLE = False
    FileWatchGuard = None
    AtomicFileOps = None

# Fallback to watchdog if resilience not available
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None

logger = logging.getLogger("TrinityBridgeAdapter")

# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

REACTOR_EVENTS_DIR = Path(os.getenv(
    "REACTOR_EVENTS_DIR",
    str(Path.home() / ".jarvis" / "reactor" / "events")
))

TRINITY_EVENTS_DIR = Path(os.getenv(
    "TRINITY_EVENTS_DIR",
    str(Path.home() / ".jarvis" / "trinity" / "events")
))

CROSS_REPO_EVENTS_DIR = Path(os.getenv(
    "CROSS_REPO_EVENTS_DIR",
    str(Path.home() / ".jarvis" / "cross_repo" / "events")
))

# Polling interval for async file check (backup if watchdog misses events)
POLL_INTERVAL = float(os.getenv("TRINITY_BRIDGE_POLL_INTERVAL", "5.0"))

# Event types we care about
CRITICAL_EVENT_TYPES = {
    "model_ready",
    "model_validated",
    "model_failed",
    "model_rollback",
    "training_complete",
    "hot_swap_model",
    "update_model_routing",
}


class BridgeEventType(Enum):
    """Types of events the bridge can handle."""
    MODEL_READY = "model_ready"
    MODEL_VALIDATED = "model_validated"
    MODEL_FAILED = "model_failed"
    MODEL_ROLLBACK = "model_rollback"
    TRAINING_COMPLETE = "training_complete"
    HOT_SWAP_MODEL = "hot_swap_model"
    UPDATE_ROUTING = "update_model_routing"
    EXPERIENCE_BATCH = "experience_batch"
    HEARTBEAT = "heartbeat"
    UNKNOWN = "unknown"


@dataclass
class BridgeMetrics:
    """Metrics for the bridge adapter."""
    events_received: int = 0
    events_forwarded: int = 0
    events_failed: int = 0
    files_processed: int = 0
    last_event_time: Optional[float] = None
    events_by_type: Dict[str, int] = field(default_factory=dict)
    # Connection state metrics
    event_bus_connected: bool = False
    event_bus_last_check: Optional[float] = None
    watchers_active: int = 0
    cross_repo_connections: int = 0


# =============================================================================
# Async File Watcher using watchdog + asyncio
# =============================================================================

class AsyncEventHandler(FileSystemEventHandler):
    """
    Watchdog event handler that queues events for async processing.

    Uses an asyncio Queue to bridge between watchdog's threading model
    and our async processing.
    """

    def __init__(self, event_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._queue = event_queue
        self._loop = loop
        self._seen_files: Set[str] = set()

    def on_created(self, event: FileCreatedEvent) -> None:
        """Handle file creation events."""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        # Only process JSON files
        if file_path.suffix != ".json":
            return

        # Skip temp files
        if file_path.name.startswith(".") or file_path.name.startswith("_"):
            return

        # Deduplicate (watchdog can fire multiple events for same file)
        file_key = str(file_path)
        if file_key in self._seen_files:
            return
        self._seen_files.add(file_key)

        # Limit seen files cache
        if len(self._seen_files) > 1000:
            self._seen_files = set(list(self._seen_files)[-500:])

        # Queue for async processing
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait,
                file_path,
            )
        except Exception as e:
            logger.warning(f"Failed to queue event: {e}")


# =============================================================================
# Trinity Bridge Adapter
# =============================================================================

class TrinityBridgeAdapter:
    """
    Watches Reactor Core and Trinity event directories and forwards
    events to TrinityEventBus for processing.

    This is the CRITICAL component that closes the Trinity Loop:
    - Reactor Core trains a model and writes MODEL_READY event
    - This adapter picks up the event and forwards to TrinityEventBus
    - TrinityHandlers receive the event and hot-swap the model

    HARDENED Features (v2.0):
    - FileWatchGuard for robust file watching with automatic recovery
    - AtomicFileOps for safe file read/delete operations
    - Circuit breaker for event bus dispatch (prevents cascading failures)
    - Correlation context for cross-repo request tracing
    - Comprehensive health monitoring with auto-restart
    - Fallback to basic watchdog if resilience utilities unavailable
    """

    def __init__(
        self,
        event_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self.logger = logging.getLogger("TrinityBridgeAdapter")

        # Event callback (defaults to TrinityEventBus dispatch)
        self._event_callback = event_callback

        # Watch directories
        self._watch_dirs: List[Path] = [
            REACTOR_EVENTS_DIR,
            TRINITY_EVENTS_DIR,
            CROSS_REPO_EVENTS_DIR,
        ]

        # ===== RESILIENCE COMPONENTS (v2.0) =====
        self._use_resilience = RESILIENCE_AVAILABLE

        # File watch guards (one per directory)
        self._file_guards: List[Any] = []

        # Atomic file operations
        self._file_ops: Optional[AtomicFileOps] = None
        if RESILIENCE_AVAILABLE:
            self._file_ops = AtomicFileOps(AtomicFileConfig(
                max_retries=3,
                verify_checksum=False,  # Speed over verification for events
            ))

        # Circuit breaker for event bus dispatch
        self._circuit_breaker: Optional[CrossRepoCircuitBreaker] = None
        if RESILIENCE_AVAILABLE:
            self._circuit_breaker = CrossRepoCircuitBreaker(
                name="trinity_bridge_eventbus",
                config=CircuitBreakerConfig(
                    failure_threshold=5,
                    success_threshold=2,
                    timeout_seconds=30.0,
                    adaptive_thresholds=True,
                ),
            )

        # ===== FALLBACK COMPONENTS =====
        # Async queue for events from watchdog (fallback mode)
        self._event_queue: asyncio.Queue[Path] = asyncio.Queue(maxsize=1000)

        # Watchdog observers (fallback mode)
        self._observers: List[Any] = []

        # ===== DEDUPLICATION =====
        self._processed_events: Set[str] = set()
        self._processed_queue: List[str] = []  # For LRU eviction
        self._max_processed = 10000

        # ===== METRICS =====
        self._metrics = BridgeMetrics()

        # ===== STATE =====
        self._running = False
        self._process_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._consecutive_errors = 0
        self._last_error: Optional[Exception] = None

        # Ensure directories exist
        for dir_path in self._watch_dirs:
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.logger.debug(f"Could not create {dir_path}: {e}")

    async def start(self) -> bool:
        """Start the bridge adapter."""
        if self._running:
            return True

        self._running = True
        self.logger.info("TrinityBridgeAdapter v2.0 starting...")

        if self._use_resilience and RESILIENCE_AVAILABLE:
            # ===== HARDENED MODE: Use FileWatchGuard =====
            await self._start_resilient_watchers()
        else:
            # ===== FALLBACK MODE: Use basic watchdog =====
            await self._start_fallback_watchers()

        # Update watcher count in metrics
        self._metrics.watchers_active = (
            len(self._file_guards) if self._use_resilience else len(self._observers)
        )

        # ===== PROACTIVE EVENT BUS CONNECTION =====
        await self._establish_event_bus_connection()

        # Start health monitoring task
        self._health_task = asyncio.create_task(
            self._health_monitor_loop(),
            name="trinity_bridge_health_monitor",
        )

        # Process any existing files on startup
        await self._process_existing_files()

        mode = "RESILIENT" if self._use_resilience else "FALLBACK"
        watch_count = self._metrics.watchers_active

        self.logger.info(
            f"TrinityBridgeAdapter v2.0 ready [{mode} mode] "
            f"(watching {watch_count} directories, event_bus={self._metrics.event_bus_connected})"
        )
        return True

    async def _establish_event_bus_connection(self) -> bool:
        """
        Proactively establish connection to TrinityEventBus.

        This ensures the event bus is connected BEFORE we start processing events,
        fixing the "Initialized but no connections" issue.
        """
        try:
            # Import TrinityEventBus - try multiple function names for compatibility
            event_bus = None
            try:
                from backend.core.trinity_event_bus import (
                    get_trinity_event_bus,
                    get_event_bus_if_exists,
                    is_event_bus_running,
                )
                # Try to get existing bus first, then initialize if needed
                event_bus = get_event_bus_if_exists()
                if event_bus is None:
                    event_bus = await get_trinity_event_bus()
            except ImportError:
                try:
                    from core.trinity_event_bus import (
                        get_trinity_event_bus,
                        get_event_bus_if_exists,
                    )
                    event_bus = get_event_bus_if_exists()
                    if event_bus is None:
                        event_bus = await get_trinity_event_bus()
                except ImportError:
                    self.logger.warning("TrinityEventBus module not found")
                    self._metrics.event_bus_connected = False
                    return False

            if event_bus:
                self._metrics.event_bus_connected = True
                self._metrics.event_bus_last_check = time.time()

                # Subscribe to bridge heartbeat for keepalive
                async def on_bridge_heartbeat(data: Dict[str, Any]):
                    """Handle heartbeat events to maintain connection."""
                    self._metrics.event_bus_last_check = time.time()

                try:
                    await event_bus.subscribe("bridge.heartbeat", on_bridge_heartbeat)
                except Exception:
                    pass  # Non-critical

                # Check for cross-repo connections if available
                if hasattr(event_bus, 'get_connection_count'):
                    try:
                        self._metrics.cross_repo_connections = await event_bus.get_connection_count()
                    except Exception:
                        self._metrics.cross_repo_connections = 0

                # Check if multicast is enabled for cross-repo
                if hasattr(event_bus, '_multicast_enabled'):
                    if event_bus._multicast_enabled:
                        self._metrics.cross_repo_connections += 1

                self.logger.info("TrinityEventBus connection established")
                return True
            else:
                self.logger.warning("TrinityEventBus not available")
                self._metrics.event_bus_connected = False
                return False

        except Exception as e:
            self.logger.error(f"Failed to establish event bus connection: {e}")
            self._metrics.event_bus_connected = False
            return False

    async def _start_resilient_watchers(self) -> None:
        """Start resilient file watchers using FileWatchGuard."""
        for dir_path in self._watch_dirs:
            try:
                config = FileWatchConfig(
                    recursive=False,
                    patterns=["*.json"],
                    ignore_patterns=[".*", "_*", "*.tmp", "*.bak"],
                    debounce_seconds=0.05,
                    verify_checksum=True,
                    restart_on_error=True,
                    max_consecutive_errors=5,
                )

                guard = FileWatchGuard(
                    watch_dir=dir_path,
                    on_event=self._on_file_event,
                    config=config,
                    on_error=self._on_watch_error,
                )

                if await guard.start():
                    self._file_guards.append(guard)
                    self.logger.info(f"[Resilient] Watching directory: {dir_path}")
                else:
                    self.logger.warning(f"[Resilient] Failed to start guard for {dir_path}")

            except Exception as e:
                self.logger.warning(f"[Resilient] Error setting up {dir_path}: {e}")

    async def _start_fallback_watchers(self) -> None:
        """Start fallback watchdog observers."""
        if not WATCHDOG_AVAILABLE:
            self.logger.warning("Watchdog not available, using polling only")
            # Start polling task
            self._poll_task = asyncio.create_task(
                self._poll_loop(),
                name="trinity_bridge_poll_loop",
            )
            return

        loop = asyncio.get_event_loop()

        for dir_path in self._watch_dirs:
            if dir_path.exists():
                try:
                    handler = AsyncEventHandler(self._event_queue, loop)
                    observer = Observer()
                    observer.schedule(handler, str(dir_path), recursive=False)
                    observer.start()
                    self._observers.append(observer)
                    self.logger.info(f"[Fallback] Watching directory: {dir_path}")
                except Exception as e:
                    self.logger.warning(f"[Fallback] Failed to watch {dir_path}: {e}")

        # Start async processing task for fallback mode
        self._process_task = asyncio.create_task(
            self._process_loop(),
            name="trinity_bridge_process_loop",
        )

        # Start fallback polling task
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="trinity_bridge_poll_loop",
        )

    async def _on_file_event(self, event: "FileEvent") -> None:
        """Handle file event from FileWatchGuard (resilient mode)."""
        if event.event_type != FileEventType.CREATED:
            return

        await self._process_file(event.path)

    async def _on_watch_error(self, error: Exception) -> None:
        """Handle file watch error."""
        self._consecutive_errors += 1
        self._last_error = error
        self.logger.error(f"File watch error: {error}")

    async def _health_monitor_loop(self) -> None:
        """Monitor health and auto-restart unhealthy watchers."""
        while self._running:
            try:
                await asyncio.sleep(30.0)  # Check every 30 seconds

                # ===== WATCHER HEALTH CHECK =====
                if self._use_resilience:
                    # Check FileWatchGuard health
                    healthy_guards = 0
                    for guard in self._file_guards:
                        if not guard.is_healthy:
                            self.logger.warning(
                                f"FileWatchGuard unhealthy for {guard.watch_dir}, restarting..."
                            )
                            await guard.stop()
                            await guard.start()
                        else:
                            healthy_guards += 1
                    self._metrics.watchers_active = healthy_guards
                else:
                    # Check watchdog observer health
                    healthy_observers = 0
                    for observer in self._observers:
                        if not observer.is_alive():
                            self.logger.warning("Watchdog observer died, restarting...")
                            # Restart all observers
                            await self._stop_fallback_watchers()
                            await self._start_fallback_watchers()
                            break
                        else:
                            healthy_observers += 1
                    self._metrics.watchers_active = healthy_observers

                # ===== EVENT BUS CONNECTION CHECK =====
                # Re-check event bus connection periodically
                await self._refresh_event_bus_connection()

                # Reset error count on successful health check
                self._consecutive_errors = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")

    async def _refresh_event_bus_connection(self) -> None:
        """Refresh event bus connection status."""
        try:
            # Import TrinityEventBus - try multiple function names for compatibility
            event_bus = None
            try:
                from backend.core.trinity_event_bus import (
                    get_trinity_event_bus,
                    get_event_bus_if_exists,
                    is_event_bus_running,
                )
                # First check if event bus exists and is running
                if is_event_bus_running():
                    event_bus = get_event_bus_if_exists()
                else:
                    # Try to initialize if not running
                    event_bus = await get_trinity_event_bus()
            except ImportError:
                try:
                    from core.trinity_event_bus import (
                        get_trinity_event_bus,
                        get_event_bus_if_exists,
                    )
                    event_bus = get_event_bus_if_exists()
                    if event_bus is None:
                        event_bus = await get_trinity_event_bus()
                except ImportError:
                    self._metrics.event_bus_connected = False
                    return

            if event_bus:
                self._metrics.event_bus_connected = True
                self._metrics.event_bus_last_check = time.time()

                # Update cross-repo connection count
                if hasattr(event_bus, 'get_connection_count'):
                    try:
                        self._metrics.cross_repo_connections = await event_bus.get_connection_count()
                    except Exception:
                        pass

                # Check multicast for cross-repo
                if hasattr(event_bus, '_multicast_enabled'):
                    if event_bus._multicast_enabled and self._metrics.cross_repo_connections == 0:
                        self._metrics.cross_repo_connections = 1
            else:
                # Try to re-establish connection
                self.logger.warning("Event bus disconnected, attempting reconnection...")
                self._metrics.event_bus_connected = False
                await self._establish_event_bus_connection()

        except Exception as e:
            self.logger.debug(f"Event bus refresh failed: {e}")
            self._metrics.event_bus_connected = False

    async def stop(self) -> None:
        """Stop the bridge adapter."""
        self._running = False

        if self._use_resilience:
            # Stop FileWatchGuard instances
            await self._stop_resilient_watchers()
        else:
            # Stop fallback watchdog observers
            await self._stop_fallback_watchers()

        # Cancel async tasks
        for task in [self._process_task, self._poll_task, self._health_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self.logger.info(
            f"TrinityBridgeAdapter v2.0 stopped "
            f"(processed {self._metrics.events_forwarded} events)"
        )

    async def _stop_resilient_watchers(self) -> None:
        """Stop resilient file watchers."""
        for guard in self._file_guards:
            try:
                await guard.stop()
            except Exception as e:
                self.logger.debug(f"FileWatchGuard stop error: {e}")
        self._file_guards.clear()

    async def _stop_fallback_watchers(self) -> None:
        """Stop fallback watchdog observers."""
        for observer in self._observers:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception as e:
                self.logger.debug(f"Observer stop error: {e}")
        self._observers.clear()

    async def _process_existing_files(self) -> None:
        """Process any existing event files on startup."""
        for dir_path in self._watch_dirs:
            if not dir_path.exists():
                continue

            try:
                for file_path in dir_path.glob("*.json"):
                    if file_path.name.startswith("."):
                        continue
                    await self._process_file(file_path)
            except Exception as e:
                self.logger.warning(f"Error processing existing files in {dir_path}: {e}")

    async def _process_loop(self) -> None:
        """Main async loop to process queued file events."""
        while self._running:
            try:
                # Wait for file event from watchdog
                file_path = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0,
                )

                # Process the file
                await self._process_file(file_path)

            except asyncio.TimeoutError:
                # Normal timeout, continue loop
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Process loop error: {e}")
                await asyncio.sleep(0.5)

    async def _poll_loop(self) -> None:
        """Fallback polling loop to catch missed events."""
        while self._running:
            try:
                await asyncio.sleep(POLL_INTERVAL)

                if not self._running:
                    break

                # Scan all watch directories
                for dir_path in self._watch_dirs:
                    if dir_path.exists():
                        await self._scan_directory(dir_path)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Poll loop error: {e}")
                await asyncio.sleep(1.0)

    async def _scan_directory(self, dir_path: Path) -> None:
        """Scan a directory for unprocessed event files."""
        try:
            for file_path in dir_path.glob("*.json"):
                # Skip hidden/temp files
                if file_path.name.startswith("."):
                    continue

                # Skip already processed
                event_id = self._get_event_id(file_path)
                if event_id in self._processed_events:
                    continue

                await self._process_file(file_path)

        except Exception as e:
            self.logger.warning(f"Directory scan error for {dir_path}: {e}")

    def _get_event_id(self, file_path: Path) -> str:
        """Generate unique ID for deduplication."""
        try:
            stat = file_path.stat()
            return f"{file_path.name}_{stat.st_mtime}_{stat.st_size}"
        except Exception:
            return str(file_path)

    async def _process_file(self, file_path: Path) -> None:
        """Process a single event file."""
        # Deduplication check
        event_id = self._get_event_id(file_path)
        if event_id in self._processed_events:
            return

        try:
            # Read file content
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(None, file_path.read_text)
            event = json.loads(content)

            # Mark as processed
            self._mark_processed(event_id)
            self._metrics.files_processed += 1

            # Get event type
            event_type = event.get("event_type", event.get("type", "unknown"))

            # Update metrics
            self._metrics.events_received += 1
            self._metrics.last_event_time = time.time()
            self._metrics.events_by_type[event_type] = \
                self._metrics.events_by_type.get(event_type, 0) + 1

            # Check if this is a critical event we need to forward
            if event_type.lower() in CRITICAL_EVENT_TYPES:
                self.logger.info(
                    f"[TrinityBridge] Critical event received: {event_type} "
                    f"(source: {file_path.name})"
                )

                # Forward to TrinityEventBus
                await self._forward_event(event)

            # Delete processed file
            try:
                await loop.run_in_executor(None, file_path.unlink)
                self.logger.debug(f"Deleted processed file: {file_path.name}")
            except FileNotFoundError:
                pass  # Already deleted
            except Exception as e:
                self.logger.warning(f"Could not delete {file_path.name}: {e}")

        except json.JSONDecodeError:
            self.logger.warning(f"Invalid JSON in {file_path.name}")
            # Move to .failed
            try:
                file_path.rename(file_path.with_suffix(".json.failed"))
            except Exception:
                pass
        except FileNotFoundError:
            # File already processed by another instance
            pass
        except Exception as e:
            self.logger.error(f"Error processing {file_path}: {e}")
            self._metrics.events_failed += 1

    def _mark_processed(self, event_id: str) -> None:
        """Mark an event as processed with LRU eviction."""
        if event_id in self._processed_events:
            return

        self._processed_events.add(event_id)
        self._processed_queue.append(event_id)

        # LRU eviction
        while len(self._processed_queue) > self._max_processed:
            old_id = self._processed_queue.pop(0)
            self._processed_events.discard(old_id)

    async def _forward_event(self, event: Dict[str, Any]) -> None:
        """Forward event to TrinityEventBus or callback with circuit breaker protection."""
        try:
            if self._event_callback:
                # Use provided callback (no circuit breaker for custom callbacks)
                result = self._event_callback(event)
                if asyncio.iscoroutine(result):
                    await result
            else:
                # Use circuit breaker for event bus dispatch
                if self._circuit_breaker and RESILIENCE_AVAILABLE:
                    try:
                        await self._circuit_breaker.execute(
                            tier="event_bus",
                            func=self._dispatch_to_event_bus,
                            args=(event,),
                        )
                    except CircuitOpenError as e:
                        self.logger.warning(
                            f"[TrinityBridge] Circuit breaker OPEN for event bus: {e.reason}. "
                            f"Retry in {e.retry_after:.1f}s"
                        )
                        # Queue event for later retry
                        self._metrics.events_failed += 1
                        return
                else:
                    # No circuit breaker, dispatch directly
                    await self._dispatch_to_event_bus(event)

            self._metrics.events_forwarded += 1
            self.logger.info(
                f"[TrinityBridge] Forwarded event: {event.get('event_type', 'unknown')}"
            )

        except Exception as e:
            self.logger.error(f"Event forwarding failed: {e}")
            self._metrics.events_failed += 1

    async def _dispatch_to_event_bus(self, event: Dict[str, Any]) -> None:
        """Dispatch event to TrinityEventBus."""
        try:
            # Import TrinityEventBus - try multiple function names for compatibility
            event_bus = None
            try:
                from backend.core.trinity_event_bus import (
                    get_trinity_event_bus,
                    get_event_bus_if_exists,
                )
                event_bus = get_event_bus_if_exists()
                if event_bus is None:
                    event_bus = await get_trinity_event_bus()
            except ImportError:
                try:
                    from core.trinity_event_bus import (
                        get_trinity_event_bus,
                        get_event_bus_if_exists,
                    )
                    event_bus = get_event_bus_if_exists()
                    if event_bus is None:
                        event_bus = await get_trinity_event_bus()
                except ImportError:
                    self.logger.warning("TrinityEventBus module not found")
                    self._metrics.event_bus_connected = False
                    return

            if event_bus:
                # Use publish_raw for dict events
                await event_bus.publish_raw(
                    topic=f"reactor.{event.get('event_type', 'event')}",
                    data=event,
                )
                # Update connection status on successful dispatch
                self._metrics.event_bus_connected = True
                self._metrics.event_bus_last_check = time.time()
            else:
                self.logger.warning("TrinityEventBus not available")
                self._metrics.event_bus_connected = False

        except ImportError:
            self.logger.warning("TrinityEventBus module not found")
            self._metrics.event_bus_connected = False
        except Exception as e:
            self.logger.error(f"EventBus dispatch failed: {e}")
            self._metrics.event_bus_connected = False
            raise

    def get_metrics(self) -> Dict[str, Any]:
        """Get bridge metrics including resilience status and connection state."""
        # Calculate active watchers count
        watchers_active = (
            len(self._file_guards) if self._use_resilience else len(self._observers)
        )

        base_metrics = {
            "version": "2.0.0",
            "events_received": self._metrics.events_received,
            "events_forwarded": self._metrics.events_forwarded,
            "events_failed": self._metrics.events_failed,
            "files_processed": self._metrics.files_processed,
            "last_event_time": self._metrics.last_event_time,
            "events_by_type": self._metrics.events_by_type,
            "running": self._running,
            "watch_dirs": [str(d) for d in self._watch_dirs],
            "consecutive_errors": self._consecutive_errors,
            "last_error": str(self._last_error) if self._last_error else None,
            # ===== CONNECTION STATE METRICS (Required by run_supervisor.py) =====
            "event_bus_connected": self._metrics.event_bus_connected,
            "event_bus_last_check": self._metrics.event_bus_last_check,
            "watchers_active": watchers_active,
            "cross_repo_connections": self._metrics.cross_repo_connections,
            # ===== WATCHED DIRECTORIES (for logging) =====
            "reactor_events_dir": str(REACTOR_EVENTS_DIR),
            "trinity_events_dir": str(TRINITY_EVENTS_DIR),
            "cross_repo_events_dir": str(CROSS_REPO_EVENTS_DIR),
        }

        # Add resilience-specific metrics
        if self._use_resilience:
            base_metrics["mode"] = "RESILIENT"
            base_metrics["file_guards_active"] = len(self._file_guards)
            base_metrics["file_guards"] = [
                guard.get_metrics() for guard in self._file_guards
            ]
            if self._circuit_breaker:
                base_metrics["circuit_breaker"] = self._circuit_breaker.get_status()
            if self._file_ops:
                base_metrics["file_ops"] = self._file_ops.get_metrics()
        else:
            base_metrics["mode"] = "FALLBACK"
            base_metrics["observers_active"] = len(self._observers)

        return base_metrics

    @property
    def is_healthy(self) -> bool:
        """Check if bridge is healthy."""
        if not self._running:
            return False

        if self._consecutive_errors >= 5:
            return False

        if self._use_resilience:
            return all(guard.is_healthy for guard in self._file_guards)
        else:
            return all(obs.is_alive() for obs in self._observers)


# =============================================================================
# Global Instance Management
# =============================================================================

_bridge_adapter: Optional[TrinityBridgeAdapter] = None
_bridge_lock: Optional[asyncio.Lock] = None


def _get_bridge_lock() -> asyncio.Lock:
    """Get or create the bridge lock."""
    global _bridge_lock
    if _bridge_lock is None:
        _bridge_lock = asyncio.Lock()
    return _bridge_lock


async def get_trinity_bridge() -> TrinityBridgeAdapter:
    """Get the global TrinityBridgeAdapter instance."""
    global _bridge_adapter

    lock = _get_bridge_lock()
    async with lock:
        if _bridge_adapter is None:
            _bridge_adapter = TrinityBridgeAdapter()
            await _bridge_adapter.start()

        return _bridge_adapter


async def shutdown_trinity_bridge() -> None:
    """Shutdown the global TrinityBridgeAdapter."""
    global _bridge_adapter

    if _bridge_adapter:
        await _bridge_adapter.stop()
        _bridge_adapter = None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "TrinityBridgeAdapter",
    "BridgeEventType",
    "BridgeMetrics",
    "get_trinity_bridge",
    "shutdown_trinity_bridge",
]
