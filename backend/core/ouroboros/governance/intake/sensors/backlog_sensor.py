"""
BacklogSensor (Sensor A) — Polls task backlog store for pending work.

Backlog file: ``{project_root}/.jarvis/backlog.json``  (default)
Schema per entry:
    {
        "task_id": str,
        "description": str,
        "target_files": [str, ...],
        "priority": int 1-5,
        "repo": str,
        "status": "pending" | "in_progress" | "completed"
    }

Priority → urgency mapping:
    5 → "high", 4 → "high", 3 → "normal", 1-2 → "low"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope, IntentEnvelope

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gap #4 migration: FS-event-primary mode
# ---------------------------------------------------------------------------
#
# When ``JARVIS_BACKLOG_FS_EVENTS_ENABLED=true``, TrinityEventBus
# ``fs.changed.*`` events become the primary trigger: a write to
# ``.jarvis/backlog.json`` → instant rescan at pub/sub latency, not a
# 60s poll cycle. Poll demotes to ``JARVIS_BACKLOG_FALLBACK_INTERVAL_S``
# (default 3600s = 1h) as a safety net for dropped events.
#
# Shadow pattern: flag defaults OFF so current 60s-poll behavior is
# preserved until a behavioral graduation arc flips it. backlog.json is
# a single operator-edited file, so no storm-guard is needed — the path
# filter on "backlog.json" suffix is tight enough that a bulk mutation
# cannot hit this handler more than once per write.
def fs_events_enabled() -> bool:
    """Re-read ``JARVIS_BACKLOG_FS_EVENTS_ENABLED`` at call-time."""
    return os.environ.get(
        "JARVIS_BACKLOG_FS_EVENTS_ENABLED", "false",
    ).lower() in ("true", "1", "yes")


_BACKLOG_FALLBACK_INTERVAL_S: float = float(
    os.environ.get("JARVIS_BACKLOG_FALLBACK_INTERVAL_S", "3600")
)


_PRIORITY_URGENCY: Dict[int, str] = {
    5: "high",
    4: "high",
    3: "normal",
    2: "low",
    1: "low",
}


@dataclass
class BacklogTask:
    task_id: str
    description: str
    target_files: List[str]
    priority: int
    repo: str
    status: str = "pending"

    @property
    def urgency(self) -> str:
        return _PRIORITY_URGENCY.get(self.priority, "normal")


class BacklogSensor:
    """Polls a JSON backlog file and produces IntentEnvelopes for pending tasks.

    Parameters
    ----------
    backlog_path:
        Path to backlog JSON file.
    repo_root:
        Repository root (used for relative path normalization).
    router:
        UnifiedIntakeRouter to call ``ingest()`` on.
    poll_interval_s:
        Seconds between scans.
    """

    def __init__(
        self,
        backlog_path: Path,
        repo_root: Path,
        router: Any,
        poll_interval_s: float = 60.0,
    ) -> None:
        self._backlog_path = backlog_path
        self._repo_root = repo_root
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._seen_task_ids: set[str] = set()
        # --- Gap #4 FS-event state (captured once so a runtime env flip
        # does not retroactively demote the poll loop; matches every
        # earlier sensor migration) ----------------------------------
        self._fs_events_mode: bool = fs_events_enabled()
        self._fs_events_handled: int = 0
        self._fs_events_ignored: int = 0

    async def scan_once(self) -> List[IntentEnvelope]:
        """Run one scan. Returns list of envelopes produced and ingested."""
        if not self._backlog_path.exists():
            return []

        try:
            raw = self._backlog_path.read_text(encoding="utf-8")
            tasks_raw: List[Dict] = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("BacklogSensor: failed to read backlog: %s", exc)
            return []

        produced: List[IntentEnvelope] = []
        for item in tasks_raw:
            task = BacklogTask(
                task_id=item.get("task_id", ""),
                description=item.get("description", ""),
                target_files=list(item.get("target_files", [])),
                priority=int(item.get("priority", 3)),
                repo=item.get("repo", "jarvis"),
                status=item.get("status", "pending"),
            )
            if task.status != "pending":
                continue
            if task.task_id in self._seen_task_ids:
                continue
            if not task.target_files:
                continue

            envelope = make_envelope(
                source="backlog",
                description=task.description,
                target_files=tuple(task.target_files),
                repo=task.repo,
                confidence=0.7 + (task.priority - 1) * 0.05,
                urgency=task.urgency,
                evidence={"task_id": task.task_id, "signature": task.task_id},
                requires_human_ack=False,
            )
            try:
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    self._seen_task_ids.add(task.task_id)
                    produced.append(envelope)
                    logger.info("BacklogSensor: enqueued task_id=%s", task.task_id)
            except Exception:
                logger.exception("BacklogSensor: ingest failed for task_id=%s", task.task_id)

        return produced

    async def start(self) -> None:
        """Start background polling loop."""
        self._running = True
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="backlog_sensor_poll",
        )
        effective = (
            _BACKLOG_FALLBACK_INTERVAL_S
            if self._fs_events_mode
            else self._poll_interval_s
        )
        mode = (
            "fs-events-primary (backlog.json change → scan_once; poll=fallback)"
            if self._fs_events_mode
            else "poll-primary"
        )
        logger.info(
            "[BacklogSensor] Started poll_interval=%ds mode=%s backlog_path=%s",
            int(effective), mode, self._backlog_path,
        )

    async def stop(self) -> None:
        self._running = False
        task = self._poll_task
        self._poll_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # Event-driven path (Manifesto §3: zero polling, pure reflex)
    # ------------------------------------------------------------------

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Subscribe to file-system events via ``TrinityEventBus``.

        Gated by ``JARVIS_BACKLOG_FS_EVENTS_ENABLED`` (default OFF). When
        the flag is off this method is a logged no-op so legacy 60s-poll
        behavior is preserved exactly. Caller contract matches every
        other gap-#4 sensor: ``IntakeLayerService`` unconditionally calls
        ``subscribe_to_bus`` on every sensor that exposes it; the flag
        check lives here so one sensor's decision doesn't require
        special-casing at the call site.

        Subscription failures are caught locally — the intake layer must
        never regress just because TrinityEventBus rejected a
        subscription.
        """
        if not self._fs_events_mode:
            logger.debug(
                "[BacklogSensor] FS-event subscription skipped "
                "(JARVIS_BACKLOG_FS_EVENTS_ENABLED=false). "
                "Poll-primary mode active — no gap #4 resolution.",
            )
            return

        try:
            await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        except Exception as exc:
            logger.warning(
                "[BacklogSensor] FS-event subscription failed: %s "
                "(poll-fallback at %ds continues)",
                exc, int(_BACKLOG_FALLBACK_INTERVAL_S),
            )
            return

        logger.info(
            "[BacklogSensor] subscribed to fs.changed.* — "
            "FS events now PRIMARY (poll demoted to %ds fallback)",
            int(_BACKLOG_FALLBACK_INTERVAL_S),
        )

    async def _on_fs_event(self, event: Any) -> None:
        """React to file change — rescan if backlog.json was modified.

        The filter on ``backlog.json`` suffix is tight enough that bulk
        mutations elsewhere in the tree can never reach scan_once — no
        storm-guard is required. Non-matching events bump the
        ``_fs_events_ignored`` counter; matching events bump
        ``_fs_events_handled`` and log the explicit FS-event origin so
        operators can distinguish it from the fallback poll.
        """
        try:
            payload = event.payload
        except AttributeError:
            self._fs_events_ignored += 1
            return
        rel_path = payload.get("relative_path", "") if payload else ""
        if not rel_path.endswith("backlog.json"):
            self._fs_events_ignored += 1
            return
        self._fs_events_handled += 1
        logger.info(
            "[BacklogSensor] scan trigger=fs_event path=%s topic=%s",
            rel_path, getattr(event, "topic", "<unknown>"),
        )
        try:
            await self.scan_once()
        except Exception:
            logger.debug(
                "[BacklogSensor] event-driven scan error", exc_info=True,
            )

    # ------------------------------------------------------------------
    # Poll fallback (safety net when event spine is unavailable)
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                logger.debug(
                    "[BacklogSensor] scan trigger=%s",
                    "fallback_poll" if self._fs_events_mode else "poll",
                )
                await self.scan_once()
            except Exception:
                logger.exception("BacklogSensor: poll error")
            effective_interval = (
                _BACKLOG_FALLBACK_INTERVAL_S
                if self._fs_events_mode
                else self._poll_interval_s
            )
            try:
                await asyncio.sleep(effective_interval)
            except asyncio.CancelledError:
                break
