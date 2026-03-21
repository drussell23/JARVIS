"""MacOSHostObserver — passive host environment daemon using native kqueue.

Lightweight, non-blocking daemon that detects environmental shifts on the
host machine via kernel-level change notifications.  Feeds events into the
TelemetryBus so the CuriosityEngine can recalculate Shannon Entropy and
discover Ignorance Gaps.

Architecture:
    kqueue (kernel)
        │  NOTE_WRITE / NOTE_EXTEND / NOTE_RENAME
        ▼
    _KqueueDetector (daemon thread, blocking control())
        │  loop.call_soon_threadsafe(queue.put_nowait)
        ▼
    asyncio.Queue (thread-safe bridge)
        │
    _consumer_loop (async task)
        │  classify → emit TelemetryEnvelope
        ▼
    TelemetryBus ─→ TopologyMap ─→ CuriosityEngine.select_target()
                                         │
                                    Shannon Entropy H(domain)
                                    proves Ignorance Gap exists

Design constraints:
    - ZERO external dependencies (uses stdlib select.kqueue on macOS)
    - Falls back to lightweight polling on non-macOS platforms
    - Never imports Quartz/pyobjc (unsafe with CoreAudio threads)
    - Daemon thread: dies with the process, no orphans
    - Queue bounded at 64: oldest dropped on overflow
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENABLED = os.environ.get("JARVIS_HOST_OBSERVER_ENABLED", "true").lower() in (
    "true", "1", "yes",
)
_POLL_INTERVAL_S = float(os.environ.get("JARVIS_HOST_OBSERVER_INTERVAL_S", "30.0"))
_MAX_QUEUE = 64

HOST_CHANGE_SCHEMA = "host.environment_change@1.0.0"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class EnvironmentChange(str, Enum):
    """Categories of host environment changes."""
    APP_INSTALLED = "app_installed"
    APP_REMOVED = "app_removed"
    FILE_DOWNLOADED = "file_downloaded"
    PACKAGE_INSTALLED = "package_installed"
    PACKAGE_REMOVED = "package_removed"
    CONFIG_CHANGED = "config_changed"


@dataclass(frozen=True)
class HostEvent:
    """A single detected environmental shift."""
    change_type: EnvironmentChange
    path: str
    domain_hint: str
    timestamp: float
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Directory snapshot — the core diffing engine
# ---------------------------------------------------------------------------

@dataclass
class _DirectorySnapshot:
    """Lightweight stat-based snapshot of a directory's contents."""
    entries: Dict[str, float]  # name → mtime

    @classmethod
    def take(cls, directory: str) -> _DirectorySnapshot:
        """Scan directory once. Returns empty snapshot if dir doesn't exist."""
        entries: Dict[str, float] = {}
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        entries[entry.name] = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        pass
        except (OSError, PermissionError):
            pass
        return cls(entries=entries)

    def diff(self, newer: _DirectorySnapshot) -> Tuple[Set[str], Set[str], Set[str]]:
        """Compare this snapshot against a newer one.
        Returns (added, removed, modified) name sets.
        """
        old_keys = set(self.entries)
        new_keys = set(newer.entries)
        added = new_keys - old_keys
        removed = old_keys - new_keys
        modified = {
            k for k in old_keys & new_keys
            if self.entries[k] != newer.entries[k]
        }
        return added, removed, modified


# ---------------------------------------------------------------------------
# Watch target configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _WatchTarget:
    """A directory to monitor and how to classify its changes."""
    path: str
    add_type: EnvironmentChange
    remove_type: EnvironmentChange
    domain_hint: str


def _build_watch_targets() -> List[_WatchTarget]:
    """Build the list of directories to watch, resolved at runtime."""
    targets = [
        _WatchTarget(
            path="/Applications",
            add_type=EnvironmentChange.APP_INSTALLED,
            remove_type=EnvironmentChange.APP_REMOVED,
            domain_hint="neural_mesh",
        ),
        _WatchTarget(
            path=str(Path.home() / "Downloads"),
            add_type=EnvironmentChange.FILE_DOWNLOADED,
            remove_type=EnvironmentChange.FILE_DOWNLOADED,
            domain_hint="data_io",
        ),
    ]

    # Discover site-packages for the running interpreter
    for sp in sys.path:
        if "site-packages" in sp and os.path.isdir(sp):
            targets.append(_WatchTarget(
                path=sp,
                add_type=EnvironmentChange.PACKAGE_INSTALLED,
                remove_type=EnvironmentChange.PACKAGE_REMOVED,
                domain_hint="exploration",
            ))
            break

    # Config files directory
    jarvis_config = str(Path.home() / ".jarvis")
    if os.path.isdir(jarvis_config):
        targets.append(_WatchTarget(
            path=jarvis_config,
            add_type=EnvironmentChange.CONFIG_CHANGED,
            remove_type=EnvironmentChange.CONFIG_CHANGED,
            domain_hint="infrastructure",
        ))

    return targets


# ---------------------------------------------------------------------------
# Change detector protocol + implementations
# ---------------------------------------------------------------------------

class _ChangeDetector:
    """Abstract detector. Subclasses implement blocking wait for changes."""

    def setup(self, directories: List[str]) -> None:  # noqa: ARG002
        raise NotImplementedError

    def wait_for_changes(self, timeout: float) -> List[str]:  # noqa: ARG002
        """Block up to *timeout* seconds. Return list of changed directory paths."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class _KqueueDetector(_ChangeDetector):
    """Native macOS kqueue-based change detection. Near-zero CPU when idle."""

    def __init__(self) -> None:
        import select
        self._kq = select.kqueue()
        self._fd_to_path: Dict[int, str] = {}
        self._select = select

    def setup(self, directories: List[str]) -> None:
        for dir_path in directories:
            if not os.path.isdir(dir_path):
                continue
            try:
                fd = os.open(dir_path, os.O_RDONLY)
                self._fd_to_path[fd] = dir_path
                ev = self._select.kevent(
                    fd,
                    filter=self._select.KQ_FILTER_VNODE,
                    flags=self._select.KQ_EV_ADD | self._select.KQ_EV_CLEAR,
                    fflags=(
                        self._select.KQ_NOTE_WRITE
                        | self._select.KQ_NOTE_EXTEND
                        | self._select.KQ_NOTE_RENAME
                        | self._select.KQ_NOTE_DELETE
                        | self._select.KQ_NOTE_ATTRIB
                    ),
                )
                self._kq.control([ev], 0, 0)
            except OSError as exc:
                logger.debug("[HostObserver] kqueue: cannot watch %s: %s", dir_path, exc)

    def wait_for_changes(self, timeout: float) -> List[str]:
        try:
            events = self._kq.control(None, len(self._fd_to_path) or 1, timeout)
            return [
                self._fd_to_path[e.ident]
                for e in events
                if e.ident in self._fd_to_path
            ]
        except OSError:
            return []

    def close(self) -> None:
        for fd in list(self._fd_to_path):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fd_to_path.clear()
        try:
            self._kq.close()
        except OSError:
            pass


class _PollingDetector(_ChangeDetector):
    """Fallback polling detector for non-macOS or when kqueue unavailable."""

    def __init__(self, interval: float = _POLL_INTERVAL_S) -> None:
        self._interval = interval
        self._dirs: List[str] = []
        self._last_mtimes: Dict[str, float] = {}

    def setup(self, directories: List[str]) -> None:
        self._dirs = [d for d in directories if os.path.isdir(d)]
        for d in self._dirs:
            try:
                self._last_mtimes[d] = os.stat(d).st_mtime
            except OSError:
                self._last_mtimes[d] = 0.0

    def wait_for_changes(self, timeout: float) -> List[str]:
        time.sleep(min(timeout, self._interval))
        changed = []
        for d in self._dirs:
            try:
                current = os.stat(d).st_mtime
                if current != self._last_mtimes.get(d, 0.0):
                    changed.append(d)
                    self._last_mtimes[d] = current
            except OSError:
                pass
        return changed

    def close(self) -> None:
        self._dirs.clear()


def _create_detector() -> _ChangeDetector:
    """Auto-select the best detector for this platform."""
    if platform.system() == "Darwin":
        try:
            import select
            if hasattr(select, "kqueue"):
                return _KqueueDetector()
        except ImportError:
            pass
    return _PollingDetector()


# ---------------------------------------------------------------------------
# MacOSHostObserver
# ---------------------------------------------------------------------------

class MacOSHostObserver:
    """Passive host environment daemon.

    Detects environmental shifts via kernel-level change notifications
    (kqueue on macOS, polling fallback elsewhere).  Emits TelemetryEnvelope
    events that feed TopologyMap and trigger CuriosityEngine's Shannon
    Entropy recalculation.

    Usage::

        observer = MacOSHostObserver(telemetry_bus, topology)
        await observer.start()
        ...
        await observer.stop()
    """

    def __init__(
        self,
        telemetry_bus: Any = None,
        topology: Any = None,
        curiosity_engine: Any = None,
        enabled: bool = _ENABLED,
        poll_interval: float = _POLL_INTERVAL_S,
        detector: Optional[_ChangeDetector] = None,
    ) -> None:
        self._bus = telemetry_bus
        self._topology = topology
        self._engine = curiosity_engine
        self._enabled = enabled
        self._poll_interval = poll_interval
        self._detector = detector or _create_detector()

        self._queue: asyncio.Queue[HostEvent] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._watch_targets: List[_WatchTarget] = []
        self._snapshots: Dict[str, _DirectorySnapshot] = {}
        self._events_emitted: int = 0
        self._domains_updated: Set[str] = set()

        # Hooks for external subscribers
        self._on_change_hooks: List[Callable[[HostEvent], Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def events_emitted(self) -> int:
        return self._events_emitted

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the observer daemon thread and async consumer."""
        if not self._enabled:
            logger.info("[HostObserver] Disabled via env")
            return

        self._watch_targets = _build_watch_targets()
        dirs = [t.path for t in self._watch_targets]

        # Take initial snapshots
        for target in self._watch_targets:
            self._snapshots[target.path] = _DirectorySnapshot.take(target.path)

        # Setup detector
        self._detector.setup(dirs)

        # Start detector thread
        self._stop_event.clear()
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._detector_thread,
            args=(loop,),
            name="host_observer_kqueue",
            daemon=True,
        )
        self._thread.start()

        # Start async consumer
        self._task = asyncio.create_task(
            self._consumer_loop(), name="host_observer_consumer"
        )
        logger.info(
            "[HostObserver] Started: watching %d directories via %s",
            len(dirs),
            type(self._detector).__name__,
        )

    async def stop(self) -> None:
        """Stop the observer cleanly."""
        self._stop_event.set()

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

        self._detector.close()
        logger.info("[HostObserver] Stopped (emitted %d events)", self._events_emitted)

    # ------------------------------------------------------------------
    # Detector thread (blocking, runs in daemon thread)
    # ------------------------------------------------------------------

    def _detector_thread(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking loop that waits for kqueue/poll notifications."""
        while not self._stop_event.is_set():
            try:
                changed_dirs = self._detector.wait_for_changes(
                    timeout=self._poll_interval
                )
                for dir_path in changed_dirs:
                    events = self._diff_directory(dir_path)
                    for event in events:
                        try:
                            loop.call_soon_threadsafe(
                                self._queue.put_nowait, event
                            )
                        except (asyncio.QueueFull, RuntimeError):
                            pass
            except Exception as exc:
                logger.debug("[HostObserver] Detector thread error: %s", exc)
                if not self._stop_event.is_set():
                    time.sleep(1.0)

    def _diff_directory(self, dir_path: str) -> List[HostEvent]:
        """Compare current state against snapshot, emit HostEvents."""
        target = None
        for t in self._watch_targets:
            if t.path == dir_path:
                target = t
                break
        if target is None:
            return []

        old_snap = self._snapshots.get(dir_path, _DirectorySnapshot(entries={}))
        new_snap = _DirectorySnapshot.take(dir_path)
        self._snapshots[dir_path] = new_snap

        added, removed, modified = old_snap.diff(new_snap)
        events: List[HostEvent] = []
        now = time.time()

        for name in added:
            events.append(HostEvent(
                change_type=target.add_type,
                path=os.path.join(dir_path, name),
                domain_hint=target.domain_hint,
                timestamp=now,
                details={"name": name, "action": "added"},
            ))

        for name in removed:
            events.append(HostEvent(
                change_type=target.remove_type,
                path=os.path.join(dir_path, name),
                domain_hint=target.domain_hint,
                timestamp=now,
                details={"name": name, "action": "removed"},
            ))

        for name in modified:
            events.append(HostEvent(
                change_type=target.add_type,
                path=os.path.join(dir_path, name),
                domain_hint=target.domain_hint,
                timestamp=now,
                details={"name": name, "action": "modified"},
            ))

        return events

    # ------------------------------------------------------------------
    # Async consumer loop
    # ------------------------------------------------------------------

    async def _consumer_loop(self) -> None:
        """Drain the event queue, classify changes, emit telemetry."""
        while True:
            try:
                try:
                    event = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                self._emit_telemetry(event)
                self._update_topology(event)
                self._events_emitted += 1

                # Fire hooks
                for hook in self._on_change_hooks:
                    try:
                        result = hook(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[HostObserver] Consumer error: %s", exc)
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------
    # TelemetryBus emission
    # ------------------------------------------------------------------

    def _emit_telemetry(self, event: HostEvent) -> None:
        """Emit a TelemetryEnvelope for this host change."""
        if self._bus is None:
            return
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope
            envelope = TelemetryEnvelope.create(
                event_schema=HOST_CHANGE_SCHEMA,
                source="macos_host_observer",
                trace_id="host_observation",
                span_id=f"change_{event.change_type.value}",
                partition_key="lifecycle",
                payload={
                    "change_type": event.change_type.value,
                    "path": event.path,
                    "domain_hint": event.domain_hint,
                    "details": event.details,
                },
            )
            self._bus.emit(envelope)
        except Exception as exc:
            logger.debug("[HostObserver] Telemetry emit failed: %s", exc)

    # ------------------------------------------------------------------
    # TopologyMap integration — propagate to CuriosityEngine
    # ------------------------------------------------------------------

    def _update_topology(self, event: HostEvent) -> None:
        """Update the TopologyMap based on the detected change.

        When a new application or package is detected, we mark the
        corresponding domain as having potential new capabilities.
        This increases Shannon Entropy for that domain, which the
        CuriosityEngine will detect on its next scoring cycle.

        Propagation chain:
            HostEvent → TopologyMap.register(node) → domain_coverage drops
            → entropy_over_domain(domain) rises → CuriosityEngine scores
            higher UCB for that domain → ExplorationSentinel spawned.
        """
        if self._topology is None:
            return

        domain = event.domain_hint
        self._domains_updated.add(domain)

        # Register a placeholder capability node for the detected change
        # so CuriosityEngine sees an inactive node and increases entropy.
        try:
            from backend.core.topology.topology_map import CapabilityNode

            # Derive a capability name from the change
            name = event.details.get("name", os.path.basename(event.path))
            cap_name = f"discovered_{domain}_{name}".lower().replace(" ", "_").replace(".", "_")

            # Only register if not already known
            if cap_name not in self._topology.nodes:
                node = CapabilityNode(
                    name=cap_name,
                    domain=domain,
                    repo_owner="jarvis",
                    active=False,
                    coverage_score=0.0,
                    exploration_attempts=0,
                )
                self._topology.register(node)
                logger.info(
                    "[HostObserver] Registered capability '%s' in domain '%s' "
                    "(H=%.3f → CuriosityEngine will detect ignorance gap)",
                    cap_name,
                    domain,
                    self._topology.entropy_over_domain(domain),
                )
        except Exception as exc:
            logger.debug("[HostObserver] Topology update failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_hook(self, callback: Callable[[HostEvent], Any]) -> None:
        """Register a callback invoked on every detected change."""
        self._on_change_hooks.append(callback)

    def health(self) -> Dict[str, Any]:
        """Return observer health snapshot."""
        return {
            "enabled": self._enabled,
            "running": self._task is not None and not self._task.done(),
            "detector": type(self._detector).__name__,
            "watched_dirs": len(self._watch_targets),
            "events_emitted": self._events_emitted,
            "domains_updated": sorted(self._domains_updated),
            "queue_depth": self._queue.qsize(),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[MacOSHostObserver] = None


def get_host_observer(**kwargs: Any) -> MacOSHostObserver:
    """Get or create the singleton MacOSHostObserver."""
    global _instance
    if _instance is None:
        _instance = MacOSHostObserver(**kwargs)
    return _instance
