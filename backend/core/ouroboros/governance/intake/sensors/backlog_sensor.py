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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope, IntentEnvelope

logger = logging.getLogger(__name__)

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
        self._seen_task_ids: set = set()

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
        asyncio.create_task(self._poll_loop(), name="backlog_sensor_poll")

    def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except Exception:
                logger.exception("BacklogSensor: poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break
