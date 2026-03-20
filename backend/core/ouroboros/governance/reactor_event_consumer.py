"""
Reactor Event Consumer
======================

Bidirectional event bridge between JARVIS and Reactor-Core.

JARVIS -> Reactor: EXPERIENCE_GENERATED events via CrossRepoEventBus (already works)
Reactor -> JARVIS: Events read from a shared reactor-inbox directory

Directory convention:
  ~/.jarvis/ouroboros/events/          -- JARVIS outbox (CrossRepoEventBus writes)
  ~/.jarvis/ouroboros/reactor-inbox/   -- Reactor writes here, this consumer reads
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.cross_repo import CrossRepoEventBus

logger = logging.getLogger("Ouroboros.ReactorEventConsumer")

# Default inbox directory
_DEFAULT_INBOX = Path.home() / ".jarvis" / "ouroboros" / "reactor-inbox"


class ReactorEventConsumer:
    """Polls a shared directory for events emitted by Reactor-Core.

    Reactor-Core writes JSON event files to reactor-inbox/pending/.
    This consumer reads them, dispatches to registered handlers,
    and moves processed events to reactor-inbox/processed/.

    Parameters
    ----------
    event_bus:
        The JARVIS-side CrossRepoEventBus for re-emitting events.
    inbox_dir:
        Path to the reactor inbox directory.
    poll_interval_s:
        How often to poll for new events (seconds).
    """

    def __init__(
        self,
        event_bus: CrossRepoEventBus,
        inbox_dir: Path = _DEFAULT_INBOX,
        poll_interval_s: float = 5.0,
    ):
        self._bus = event_bus
        self._inbox = inbox_dir
        self._poll_interval = poll_interval_s
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._events_processed = 0
        self._events_failed = 0

    async def start(self) -> None:
        """Start polling the reactor inbox."""
        self._inbox.mkdir(parents=True, exist_ok=True)
        (self._inbox / "pending").mkdir(exist_ok=True)
        (self._inbox / "processed").mkdir(exist_ok=True)
        (self._inbox / "failed").mkdir(exist_ok=True)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("ReactorEventConsumer started, watching %s", self._inbox)

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info(
            "ReactorEventConsumer stopped (processed=%d, failed=%d)",
            self._events_processed,
            self._events_failed,
        )

    async def _poll_loop(self) -> None:
        """Main poll loop -- reads pending events from reactor inbox."""
        while self._running:
            try:
                pending_dir = self._inbox / "pending"
                for event_file in sorted(pending_dir.glob("*.json")):
                    await self._process_event(event_file)
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Reactor inbox poll error: %s", e)
                await asyncio.sleep(10.0)

    async def _process_event(self, event_file: Path) -> None:
        """Process a single event file from Reactor's outbox."""
        try:
            data = json.loads(await asyncio.to_thread(event_file.read_text))

            # Re-emit into JARVIS's event bus so registered handlers fire
            from backend.core.ouroboros.cross_repo import CrossRepoEvent

            event = CrossRepoEvent.from_dict(data)

            await self._bus.emit(event)
            self._events_processed += 1

            # Move to processed
            dest = self._inbox / "processed" / event_file.name
            await asyncio.to_thread(event_file.rename, dest)
            logger.info(
                "Processed reactor event: %s (%s)", event.id, event.type.value
            )

        except Exception as e:
            logger.error(
                "Error processing reactor event %s: %s", event_file.name, e
            )
            self._events_failed += 1
            # Move to failed
            try:
                dest = self._inbox / "failed" / event_file.name
                await asyncio.to_thread(event_file.rename, dest)
            except Exception:
                pass

    def health(self) -> Dict[str, Any]:
        """Return health status."""
        return {
            "running": self._running,
            "inbox_dir": str(self._inbox),
            "events_processed": self._events_processed,
            "events_failed": self._events_failed,
        }
