"""
Scheduled Trigger Sensor
=========================

Fires IntentEnvelopes based on cron expressions from a YAML config file.

Config format (~/.jarvis/ouroboros/schedules.yaml)::

    schedules:
      - name: security_audit
        cron: "0 2 * * 0"
        goal: "Scan for security vulnerabilities in authentication code"
        target_files:
          - "backend/core/auth.py"
        repo: jarvis
        source: ai_miner
        urgency: normal
        requires_human_ack: true
        enabled: true

      - name: dependency_check
        cron: "0 8 * * 1"
        goal: "Check for outdated dependencies and suggest updates"
        target_files:
          - "pyproject.toml"
        repo: jarvis
        requires_human_ack: true

      - name: test_coverage_sweep
        cron: "0 3 * * *"
        goal: "Find uncovered code paths and generate tests"
        target_files:
          - "backend/"
          - "tests/"
        repo: jarvis
        requires_human_ack: false

Environment variables:
  JARVIS_SCHEDULE_CONFIG -- path to schedules YAML
      (default: ~/.jarvis/ouroboros/schedules.yaml)
  JARVIS_SCHEDULE_CHECK_INTERVAL_S -- seconds between cron checks
      (default: 60)

Dependency: ``croniter`` (pure Python). If not installed, sensor logs a
warning and stays inactive.

Note: ``source`` in each schedule entry MUST be a value accepted by
IntentEnvelope validation (backlog | test_failure | voice_human | ai_miner).
Defaults to ``ai_miner`` if omitted.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ScheduledSensor")

_DEFAULT_CONFIG = Path.home() / ".jarvis" / "ouroboros" / "schedules.yaml"
_DEFAULT_CHECK_INTERVAL = 60.0

# Envelope defaults — must pass IntentEnvelope validation.
_DEFAULT_SOURCE = "ai_miner"
_DEFAULT_URGENCY = "normal"


@dataclass
class ScheduleEntry:
    """A single scheduled trigger parsed from YAML config."""

    name: str
    cron: str
    goal: str
    target_files: Tuple[str, ...]
    repo: str = "jarvis"
    source: str = _DEFAULT_SOURCE
    urgency: str = _DEFAULT_URGENCY
    requires_human_ack: bool = True
    enabled: bool = True
    confidence: float = 0.8
    last_fired: Optional[datetime] = field(default=None, repr=False)


class ScheduledTriggerSensor:
    """Fires IntentEnvelopes on cron schedules loaded from a YAML file.

    Parameters
    ----------
    router:
        ``UnifiedIntakeRouter`` (or any object with an async ``ingest()``
        method that accepts an ``IntentEnvelope``).
    config_path:
        Filesystem path to the schedules YAML file. Falls back to
        ``JARVIS_SCHEDULE_CONFIG`` env var, then the built-in default.
    check_interval_s:
        How often (seconds) to evaluate whether any schedule should fire.
    """

    def __init__(
        self,
        router: Any,
        config_path: Optional[Path] = None,
        check_interval_s: Optional[float] = None,
    ) -> None:
        self._router = router
        self._config_path = config_path or Path(
            os.getenv("JARVIS_SCHEDULE_CONFIG", str(_DEFAULT_CONFIG))
        )
        self._check_interval = check_interval_s or float(
            os.getenv(
                "JARVIS_SCHEDULE_CHECK_INTERVAL_S", str(_DEFAULT_CHECK_INTERVAL)
            )
        )
        self._schedules: List[ScheduleEntry] = []
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._croniter_available = False
        self._fires_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load config, verify croniter, and launch the background check loop."""
        try:
            import croniter as _cr  # noqa: F401

            self._croniter_available = True
        except ImportError:
            logger.warning(
                "croniter not installed -- ScheduledTriggerSensor disabled. "
                "Install with: pip install croniter"
            )
            return

        self._schedules = self._load_config()
        if not self._schedules:
            logger.info(
                "ScheduledTriggerSensor: no enabled schedules at %s",
                self._config_path,
            )
            return

        self._running = True
        self._check_task = asyncio.create_task(
            self._check_loop(), name="scheduled_sensor_loop"
        )
        logger.info(
            "ScheduledTriggerSensor started with %d schedule(s)",
            len(self._schedules),
        )

    async def stop(self) -> None:
        """Cancel the background loop and clean up."""
        self._running = False
        if self._check_task is not None:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            self._check_task = None
        logger.info(
            "ScheduledTriggerSensor stopped (fired=%d)", self._fires_count
        )

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self) -> List[ScheduleEntry]:
        """Parse the YAML config file into ``ScheduleEntry`` objects."""
        if not self._config_path.exists():
            logger.debug(
                "ScheduledTriggerSensor: config not found at %s",
                self._config_path,
            )
            return []
        try:
            import yaml

            with open(self._config_path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}

            entries: List[ScheduleEntry] = []
            for item in data.get("schedules", []):
                if not isinstance(item, dict):
                    continue
                raw_files = item.get("target_files") or []
                # Guarantee non-empty target_files (IntentEnvelope requirement).
                target_files = tuple(raw_files) if raw_files else (".",)
                entry = ScheduleEntry(
                    name=item["name"],
                    cron=item["cron"],
                    goal=item["goal"],
                    target_files=target_files,
                    repo=item.get("repo", "jarvis"),
                    source=item.get("source", _DEFAULT_SOURCE),
                    urgency=item.get("urgency", _DEFAULT_URGENCY),
                    requires_human_ack=item.get("requires_human_ack", True),
                    enabled=item.get("enabled", True),
                    confidence=float(item.get("confidence", 0.8)),
                )
                if entry.enabled:
                    entries.append(entry)
            return entries
        except Exception as exc:
            logger.error("Failed to load schedule config: %s", exc)
            return []

    def reload_config(self) -> int:
        """Hot-reload the config file. Returns the count of enabled schedules."""
        self._schedules = self._load_config()
        return len(self._schedules)

    # ------------------------------------------------------------------
    # Check loop
    # ------------------------------------------------------------------

    async def _check_loop(self) -> None:
        """Periodically evaluate all schedules and fire those that are due."""
        while self._running:
            try:
                now = datetime.now()
                for schedule in self._schedules:
                    if self._should_fire(schedule, now):
                        await self._fire(schedule, now)
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ScheduledTriggerSensor check error: %s", exc)
                await asyncio.sleep(self._check_interval)

    # ------------------------------------------------------------------
    # Cron evaluation
    # ------------------------------------------------------------------

    def _should_fire(self, schedule: ScheduleEntry, now: datetime) -> bool:
        """Return ``True`` if *schedule* is due according to its cron expr."""
        if not schedule.enabled:
            return False
        try:
            from croniter import croniter

            if schedule.last_fired is not None:
                cron = croniter(schedule.cron, schedule.last_fired)
                next_fire = cron.get_next(datetime)
                return now >= next_fire
            else:
                # First evaluation -- fire if a cron tick fell within the
                # last check interval.
                start = now - timedelta(seconds=self._check_interval)
                cron = croniter(schedule.cron, start)
                next_fire = cron.get_next(datetime)
                return next_fire <= now
        except Exception as exc:
            logger.warning(
                "Invalid cron expression for schedule %r: %s",
                schedule.name,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Envelope creation + submission
    # ------------------------------------------------------------------

    async def _fire(self, schedule: ScheduleEntry, now: datetime) -> None:
        """Build an IntentEnvelope for *schedule* and submit to the router."""
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )

            envelope = make_envelope(
                source=schedule.source,
                description=schedule.goal,
                target_files=schedule.target_files,
                repo=schedule.repo,
                confidence=schedule.confidence,
                urgency=schedule.urgency,
                evidence={
                    "trigger": "scheduled",
                    "schedule_name": schedule.name,
                    "cron": schedule.cron,
                    "signature": f"scheduled:{schedule.name}",
                },
                requires_human_ack=schedule.requires_human_ack,
            )
            status = await self._router.ingest(envelope)
            schedule.last_fired = now
            self._fires_count += 1
            logger.info(
                "Scheduled trigger fired: %s (status=%s)",
                schedule.name,
                status,
            )
        except Exception as exc:
            logger.error(
                "Failed to fire schedule %r: %s", schedule.name, exc
            )

    # ------------------------------------------------------------------
    # Health / introspection
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return a health snapshot suitable for supervisor dashboards."""
        return {
            "running": self._running,
            "croniter_available": self._croniter_available,
            "schedule_count": len(self._schedules),
            "fires_count": self._fires_count,
            "config_path": str(self._config_path),
            "schedules": [
                {
                    "name": s.name,
                    "cron": s.cron,
                    "enabled": s.enabled,
                    "last_fired": (
                        s.last_fired.isoformat() if s.last_fired else None
                    ),
                }
                for s in self._schedules
            ],
        }
