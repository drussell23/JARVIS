"""LifecycleVoiceNarrator -- real-time spoken narration of supervisor state.

Hooks into TelemetryBus and supervisor zone transitions to produce
intelligent, human-like voice narration of everything JARVIS does.
Runs entirely in background tasks -- never blocks the boot sequence
or the main event loop.

Architecture:
    TelemetryBus ──subscribe("*")──> _on_envelope()
                                          │
                                     _classify()
                                          │
                                     _enqueue()
                                          │
                              ┌────────────────────────┐
                              │  _narrator_loop (task) │
                              │  batches + dedup + say │
                              └────────────────────────┘
                                          │
                                      safe_say()  (non-blocking)

Design constraints:
    - ZERO imports from unified_supervisor.py (decoupled)
    - All voice via safe_say() (proven path, gate + dedup)
    - Debounce: max 1 narration per 4 seconds (configurable)
    - Batch: rapid lifecycle transitions are collapsed into one sentence
    - Queue depth: 32 max, oldest dropped on overflow (never OOM)
    - Personalized: time-of-day greetings, owner name from env
    - Intelligent: different cadence for startup vs runtime vs recovery
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEBOUNCE_S = float(os.environ.get("JARVIS_NARRATOR_DEBOUNCE_S", "4.0"))
_MAX_QUEUE = int(os.environ.get("JARVIS_NARRATOR_MAX_QUEUE", "32"))
_ENABLED = os.environ.get("JARVIS_LIFECYCLE_NARRATOR_ENABLED", "true").lower() in (
    "true", "1", "yes",
)
_OWNER = os.environ.get("JARVIS_OWNER_NAME", "Sir")


class NarrationPriority(Enum):
    """Priority tiers -- higher numeric value = higher priority."""
    LOW = auto()       # routine health pings
    NORMAL = auto()    # zone transitions, agent spawns
    HIGH = auto()      # recovery events, failures
    CRITICAL = auto()  # security alerts, full system down


@dataclass(frozen=True)
class NarrationItem:
    """A single item waiting to be spoken."""
    text: str
    priority: NarrationPriority
    timestamp: float = field(default_factory=time.monotonic)
    category: str = ""  # for dedup: "lifecycle", "fault", "recovery", etc.


# ---------------------------------------------------------------------------
# Message templates -- human-like, not robotic
# ---------------------------------------------------------------------------

_BOOT_ZONE_MESSAGES: Dict[str, List[str]] = {
    "preflight": [
        "Running preflight checks.",
        "Preflight sequence initiated.",
    ],
    "backend": [
        "Backend systems loading.",
        "Core services coming online.",
    ],
    "integration": [
        "Integration components connecting.",
        "Wiring subsystem bridges.",
    ],
    "trinity": [
        "Trinity pipeline activating. Mind, Body, Soul.",
        "Bringing the Trinity ecosystem online.",
    ],
    "neural_mesh_agents": [
        "Neural mesh agents initializing.",
        "Spawning agent collective.",
    ],
    "agi_os": [
        "Intelligence layer activating.",
        "A.G.I. operating system online.",
    ],
    "governance": [
        "Governance pipeline starting. Ouroboros is awake.",
        "Self-programming governance loop activating.",
    ],
    "proactive_drive": [
        "Proactive drive engaged. Curiosity engine online.",
        "Autonomous exploration system ready.",
    ],
}

_LIFECYCLE_MESSAGES: Dict[str, List[str]] = {
    "PROBING": [
        "Checking J-Prime health.",
    ],
    "READY": [
        "J-Prime is online and ready for inference.",
        "J-Prime connected. Full intelligence available.",
    ],
    "DEGRADED": [
        "J-Prime is degraded. Falling back to cloud.",
        "Local inference degraded. Routing through cloud A.P.I.",
    ],
    "DEAD": [
        "J-Prime is offline. Cloud fallback active.",
        "Lost connection to J-Prime. Switching to backup.",
    ],
}

_FAULT_MESSAGES: List[str] = [
    "Fault detected. {detail}. Initiating recovery.",
    "Issue identified: {detail}. Auto-recovery in progress.",
    "{detail}. I'm handling it.",
]

_RECOVERY_MESSAGES: List[str] = [
    "Recovery successful. Systems nominal.",
    "Issue resolved. Back to full capacity.",
    "Recovered. All clear.",
]

_STARTUP_COMPLETE: List[str] = [
    "All systems operational, {name}. Ready for your command.",
    "Boot complete. {name}, I'm at your service.",
    "Systems fully online, {name}. What would you like to do?",
]


def _time_greeting(name: str) -> str:
    """Generate a time-of-day aware greeting."""
    from datetime import datetime
    hour = datetime.now().hour
    if 4 <= hour < 7:
        greetings = [
            f"Up early, {name}. Initiating systems.",
            f"Good pre-dawn, {name}. Booting up.",
        ]
    elif 7 <= hour < 12:
        greetings = [
            f"Good morning, {name}. Starting up.",
            f"Morning, {name}. Let's get to work.",
        ]
    elif 12 <= hour < 17:
        greetings = [
            f"Good afternoon, {name}. Coming online.",
            f"Afternoon, {name}. Systems activating.",
        ]
    elif 17 <= hour < 21:
        greetings = [
            f"Good evening, {name}. Ready when you are.",
            f"Evening, {name}. Powering up.",
        ]
    else:
        greetings = [
            f"Working late, {name}? Starting up.",
            f"Late session, {name}. I'm here.",
        ]
    return random.choice(greetings)


# ---------------------------------------------------------------------------
# LifecycleVoiceNarrator
# ---------------------------------------------------------------------------

class LifecycleVoiceNarrator:
    """Non-blocking voice narrator for supervisor lifecycle events.

    Subscribes to TelemetryBus and converts state transitions into
    intelligent spoken narration via safe_say().

    Usage:
        narrator = LifecycleVoiceNarrator()
        await narrator.start()  # subscribes to bus, starts loop
        ...
        await narrator.stop()
    """

    def __init__(
        self,
        debounce_s: float = _DEBOUNCE_S,
        max_queue: int = _MAX_QUEUE,
        owner_name: str = _OWNER,
        enabled: bool = _ENABLED,
    ) -> None:
        self._debounce_s = debounce_s
        self._max_queue = max_queue
        self._owner = owner_name
        self._enabled = enabled

        self._queue: asyncio.Queue[NarrationItem] = asyncio.Queue(maxsize=max_queue)
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._last_spoke: float = 0.0
        self._last_category: str = ""
        self._recent_texts: Deque[str] = deque(maxlen=10)
        self._boot_narrated: bool = False
        self._startup_complete_narrated: bool = False

        # External hooks: list of async callables(text) for dashboard, logging, etc.
        self._on_narrate_hooks: List[Callable[[str], Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background narrator loop and subscribe to TelemetryBus."""
        if not self._enabled:
            logger.info("[LifecycleNarrator] Disabled via env")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._narrator_loop(), name="lifecycle_narrator")

        # Subscribe to TelemetryBus
        try:
            from backend.core.telemetry_contract import get_telemetry_bus
            bus = get_telemetry_bus()
            bus.subscribe("*", self._on_envelope)
            logger.info("[LifecycleNarrator] Subscribed to TelemetryBus")
        except Exception as exc:
            logger.warning("[LifecycleNarrator] TelemetryBus subscribe failed: %s", exc)

        # Narrate boot greeting
        if not self._boot_narrated:
            self._boot_narrated = True
            greeting = _time_greeting(self._owner)
            self.enqueue(greeting, NarrationPriority.HIGH, category="greeting")

    async def stop(self) -> None:
        """Stop the narrator loop gracefully."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        logger.info("[LifecycleNarrator] Stopped")

    def enqueue(
        self,
        text: str,
        priority: NarrationPriority = NarrationPriority.NORMAL,
        category: str = "",
    ) -> None:
        """Enqueue a narration item (non-blocking, drops on overflow)."""
        if not self._enabled or not text:
            return

        # Dedup: skip if we said this exact text recently
        text_stripped = text.strip()
        if text_stripped in self._recent_texts:
            return

        item = NarrationItem(
            text=text_stripped,
            priority=priority,
            category=category,
        )

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Drop lowest priority or oldest
            logger.debug("[LifecycleNarrator] Queue full, dropping: %s", text[:40])

    def narrate_zone(self, zone_name: str) -> None:
        """Narrate a boot zone transition."""
        templates = _BOOT_ZONE_MESSAGES.get(zone_name)
        if templates:
            self.enqueue(random.choice(templates), NarrationPriority.NORMAL, category=f"zone:{zone_name}")

    def narrate_startup_complete(self, duration_s: float) -> None:
        """Narrate that startup is fully complete."""
        if self._startup_complete_narrated:
            return
        self._startup_complete_narrated = True
        msg = random.choice(_STARTUP_COMPLETE).format(name=self._owner)
        if duration_s > 0:
            msg += f" Boot took {duration_s:.0f} seconds."
        self.enqueue(msg, NarrationPriority.HIGH, category="startup_complete")

    def add_hook(self, callback: Callable[[str], Any]) -> None:
        """Register a callback invoked with every narrated text."""
        self._on_narrate_hooks.append(callback)

    # ------------------------------------------------------------------
    # TelemetryBus handler
    # ------------------------------------------------------------------

    async def _on_envelope(self, envelope: Any) -> None:
        """Handle a TelemetryEnvelope from the bus.

        v305.0: Stripped down to user-relevant events only.
        Removed: routine lifecycle transitions (PROBING/DEGRADED/DEAD cycle
        spammed every health check), proactive drive chatter, agent graph
        state, and recovery attempts. These caused constant false-positive
        announcements. Only fault.raised and task completion are narrated now.
        Startup-complete narration is handled separately via narrate_startup_complete().
        """
        try:
            schema = envelope.event_schema
            payload = envelope.payload

            # Only narrate genuine faults (not routine state churn)
            if schema.startswith("fault.raised"):
                fault_class = payload.get("fault_class", "unknown fault")
                msg = random.choice(_FAULT_MESSAGES).format(detail=fault_class)
                self.enqueue(msg, NarrationPriority.HIGH, category="fault")

            # Task completion narration (fed by RuntimeTaskOrchestrator)
            elif schema.startswith("task.completed"):
                summary = payload.get("summary", "")
                if summary:
                    self.enqueue(summary, NarrationPriority.HIGH, category="task_complete")

        except Exception as exc:
            logger.debug("[LifecycleNarrator] Envelope handler error: %s", exc)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _narrator_loop(self) -> None:
        """Background loop that drains the queue and speaks via safe_say."""
        logger.info("[LifecycleNarrator] Background loop started")

        while not self._stop_event.is_set():
            try:
                # Wait for next item with timeout (allows clean shutdown)
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                # Debounce: skip if same category spoken too recently
                now = time.monotonic()
                if (
                    item.category == self._last_category
                    and item.priority.value < NarrationPriority.HIGH.value
                    and (now - self._last_spoke) < self._debounce_s
                ):
                    continue

                # Batch: drain queue for any HIGHER priority items
                batch = [item]
                while not self._queue.empty():
                    try:
                        extra = self._queue.get_nowait()
                        batch.append(extra)
                    except asyncio.QueueEmpty:
                        break

                # Sort by priority (highest first), take the best one
                batch.sort(key=lambda x: x.priority.value, reverse=True)
                best = batch[0]

                # Speak (non-blocking via safe_say with wait=False)
                await self._speak(best.text)
                self._last_spoke = time.monotonic()
                self._last_category = best.category
                self._recent_texts.append(best.text)

                # Fire hooks
                for hook in self._on_narrate_hooks:
                    try:
                        result = hook(best.text)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[LifecycleNarrator] Loop error: %s", exc)
                await asyncio.sleep(1.0)

        logger.info("[LifecycleNarrator] Background loop exited")

    async def _speak(self, text: str) -> None:
        """Speak text via safe_say (non-blocking, fire-and-forget)."""
        try:
            from backend.core.supervisor.unified_voice_orchestrator import safe_say
            await safe_say(
                text,
                wait=False,
                source="lifecycle_narrator",
            )
        except ImportError:
            logger.debug("[LifecycleNarrator] safe_say not available")
        except Exception as exc:
            logger.debug("[LifecycleNarrator] Speech failed: %s", exc)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return narrator health snapshot."""
        return {
            "enabled": self._enabled,
            "running": self._task is not None and not self._task.done(),
            "queue_depth": self._queue.qsize(),
            "narrations_spoken": len(self._recent_texts),
            "last_category": self._last_category,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[LifecycleVoiceNarrator] = None


def get_lifecycle_narrator(**kwargs: Any) -> LifecycleVoiceNarrator:
    """Get or create the singleton LifecycleVoiceNarrator."""
    global _instance
    if _instance is None:
        _instance = LifecycleVoiceNarrator(**kwargs)
    return _instance
