"""FileSystemEventBridge — publishes file system events to TrinityEventBus.

Owns a single FileWatchGuard watching the project root. On each real file
change (debounced, checksum-verified), publishes to topic ``fs.changed.*``
so intake sensors can react in sub-second time instead of polling.

Boundary Principle (Manifesto §3 / §5):
  Deterministic: File watching, debounce, checksum, topic routing.
  Agentic: What to *do* with the change (sensor-level decision).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HEARTBEAT_EVERY_N = int(os.environ.get("JARVIS_FS_BRIDGE_HEARTBEAT_EVERY", "100"))


class FileSystemEventBridge:
    """Bridges file system events from FileWatchGuard to TrinityEventBus.

    Parameters
    ----------
    project_root:
        Directory to watch recursively.
    event_bus:
        TrinityEventBus instance for publishing events.
    watch_config:
        Optional FileWatchConfig override. Defaults are tuned for
        source code monitoring (*.py, *.json, debounce 0.3s).
    """

    def __init__(
        self,
        project_root: Path,
        event_bus: Any,
        watch_config: Any = None,
    ) -> None:
        self._project_root = project_root.resolve()
        self._event_bus = event_bus
        self._watch_config = watch_config
        self._guard: Optional[Any] = None
        self._events_published: int = 0

    async def start(self) -> None:
        """Start the FileWatchGuard and begin publishing events."""
        from backend.core.resilience.file_watch_guard import (
            FileWatchGuard,
            FileWatchConfig,
        )

        config = self._watch_config or FileWatchConfig(
            patterns=["*.py", "*.json", "*.yaml", "*.yml"],
            ignore_patterns=[
                "__pycache__/*", ".git/*", "*.pyc", "node_modules/*",
                "venv/*", ".venv/*", "*.egg-info/*", ".ouroboros/*",
                ".worktrees/*", "*.swp", "*.tmp", "*~",
            ],
            recursive=True,
            debounce_seconds=0.3,
            verify_checksum=True,
            dedup_ttl_seconds=2.0,
        )

        self._guard = FileWatchGuard(
            watch_dir=self._project_root,
            on_event=self._on_file_event,
            config=config,
        )
        ok = await self._guard.start()
        if ok:
            logger.info(
                "[FSEventBridge] Watching %s (patterns=%s)",
                self._project_root, config.patterns,
            )
        else:
            logger.warning(
                "[FSEventBridge] FileWatchGuard failed to start for %s",
                self._project_root,
            )

    async def stop(self) -> None:
        """Stop the FileWatchGuard."""
        if self._guard is not None:
            await self._guard.stop()
            logger.info(
                "[FSEventBridge] Stopped (published %d events)",
                self._events_published,
            )

    async def _on_file_event(self, event: Any) -> None:
        """Translate a FileEvent into a TrinityEventBus publication.

        Logs the FIRST published event at INFO so battle test logs carry a
        positive signal that the watchdog → bridge → bus chain is alive.
        Subsequent events log only at DEBUG to avoid spam, with a periodic
        heartbeat every ``_HEARTBEAT_EVERY_N`` events. The "did the chain
        ever fire" question that bt-2026-04-12-005521 could not answer
        from logs alone is now a single grep away.
        """
        try:
            topic = f"fs.changed.{event.event_type.value}"

            # Compute relative path safely
            try:
                rel_path = str(event.path.relative_to(self._project_root))
            except ValueError:
                rel_path = str(event.path)

            extension = event.path.suffix
            is_test = (
                rel_path.startswith("tests/")
                or event.path.name.startswith("test_")
                or event.path.name.endswith("_test.py")
            )
            is_config = (
                extension in (".json", ".yaml", ".yml")
                and ".jarvis" in rel_path
            )

            await self._event_bus.publish_raw(
                topic=topic,
                data={
                    "path": str(event.path),
                    "relative_path": rel_path,
                    "extension": extension,
                    "checksum": event.checksum,
                    "is_test_file": is_test,
                    "is_config_file": is_config,
                    "is_directory": event.is_directory,
                    "timestamp": event.timestamp,
                },
                persist=False,  # High-volume, no need to WAL file events
            )
            self._events_published += 1

            if self._events_published == 1:
                logger.info(
                    "[FSEventBridge] First fs.changed event published: "
                    "topic=%s path=%s — chain is live",
                    topic, rel_path,
                )
            elif self._events_published % _HEARTBEAT_EVERY_N == 0:
                logger.info(
                    "[FSEventBridge] Heartbeat: %d events published "
                    "(latest topic=%s path=%s)",
                    self._events_published, topic, rel_path,
                )
        except Exception:
            logger.debug("[FSEventBridge] Failed to publish event", exc_info=True)

    def get_metrics(self) -> Dict[str, Any]:
        """Return bridge metrics for observability."""
        guard_metrics = {}
        if self._guard is not None and hasattr(self._guard, "get_metrics"):
            guard_metrics = self._guard.get_metrics()
        return {
            "events_published": self._events_published,
            "guard_healthy": self._guard.is_healthy if self._guard else False,
            **guard_metrics,
        }
