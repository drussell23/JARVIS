"""
Scheduled Agent Runner -- Cron-Based Recurring Governance Tasks
================================================================

Provides a standalone scheduling system that fires governance operations
on cron-like schedules without requiring external dependencies (croniter
is NOT required).  Each schedule entry specifies a goal, optional target
files, and a cron expression.

Architecture
------------

.. code-block:: text

    ScheduledAgentRunner
        |
        v
    _check_loop() [every 60s]
        |
        v
    for each due ScheduleEntry:
        CronParser.is_due(entry) -> True
            |
            v
        GLS.submit(ctx, trigger_source="scheduled_agent")

Persistence is via JSON at ``~/.jarvis/ouroboros/schedules.json``.  The
runner self-heals if the file is corrupted or missing.

Boundary Principle
------------------
Deterministic: schedule evaluation (CronParser), persistence, lifecycle.
Agentic: actual goal execution delegated entirely to GLS pipeline.

Environment Variables
---------------------
``JARVIS_SCHEDULED_AGENTS_DIR``
    Directory for schedule persistence (default: ~/.jarvis/ouroboros).
``JARVIS_SCHEDULED_AGENTS_CHECK_INTERVAL_S``
    Seconds between schedule checks (default: 60).
``JARVIS_SCHEDULED_AGENTS_ENABLED``
    Set to ``0`` or ``false`` to disable all scheduled agents at startup.
"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.ScheduledAgents")

_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_SCHEDULED_AGENTS_DIR",
        str(Path.home() / ".jarvis" / "ouroboros"),
    )
)
_PERSISTENCE_FILE = _PERSISTENCE_DIR / "schedules.json"
_CHECK_INTERVAL_S = float(
    os.environ.get("JARVIS_SCHEDULED_AGENTS_CHECK_INTERVAL_S", "60")
)
_ENABLED = os.environ.get("JARVIS_SCHEDULED_AGENTS_ENABLED", "1").lower() not in (
    "0",
    "false",
    "no",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ScheduleEntry:
    """A single scheduled governance task.

    Attributes
    ----------
    schedule_id:
        Unique identifier (UUID hex).
    cron_expr:
        Five-field cron expression: minute hour day-of-month month day-of-week.
        Supports ``*``, ``*/N``, ``N-M``, ``N,M``.
    goal:
        Human-readable goal description for the governance pipeline.
    target_files:
        Tuple of file paths to target.  Empty = let the pipeline auto-detect.
    enabled:
        Whether this schedule is active.
    last_run:
        Wall-clock timestamp (``time.time()``) of last successful fire.
    next_run:
        Computed next fire time (wall-clock).
    run_count:
        Number of times this schedule has fired.
    max_runs:
        Maximum number of fires.  ``None`` = unlimited, ``1`` = one-shot.
    created_at:
        Wall-clock timestamp when the entry was created.
    """

    schedule_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cron_expr: str = "0 * * * *"
    goal: str = ""
    target_files: Tuple[str, ...] = ()
    enabled: bool = True
    last_run: Optional[float] = None
    next_run: Optional[float] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # Normalise target_files to tuple
        if isinstance(self.target_files, list):
            self.target_files = tuple(self.target_files)


# ---------------------------------------------------------------------------
# Cron Parser (no external deps)
# ---------------------------------------------------------------------------


class CronParseError(ValueError):
    """Raised when a cron expression cannot be parsed."""


class CronParser:
    """Minimal cron expression parser.

    Supports the standard 5-field format:
    ``minute hour day-of-month month day-of-week``

    Field syntax:
      - ``*`` — every value
      - ``*/N`` — every N-th value
      - ``N-M`` — range from N to M inclusive
      - ``N,M,...`` — specific values
      - ``N`` — exact value
    """

    _FIELD_RANGES: Tuple[Tuple[int, int], ...] = (
        (0, 59),   # minute
        (0, 23),   # hour
        (1, 31),   # day-of-month
        (1, 12),   # month
        (0, 6),    # day-of-week (0=Sunday ... 6=Saturday)
    )
    _FIELD_NAMES: Tuple[str, ...] = (
        "minute", "hour", "day-of-month", "month", "day-of-week",
    )

    @classmethod
    def parse(cls, expr: str) -> bool:
        """Validate a cron expression.  Returns True if valid."""
        try:
            cls._expand_all(expr)
            return True
        except (CronParseError, ValueError, IndexError):
            return False

    @classmethod
    def _expand_field(cls, token: str, lo: int, hi: int) -> frozenset[int]:
        """Expand a single cron field token into a set of matching integers."""
        values: set[int] = set()
        for part in token.split(","):
            part = part.strip()
            if not part:
                continue
            if "/" in part:
                base, step_s = part.split("/", 1)
                step = int(step_s)
                if step <= 0:
                    raise CronParseError(f"Step must be positive, got {step}")
                if base == "*":
                    start = lo
                elif "-" in base:
                    rng_lo, _ = base.split("-", 1)
                    start = int(rng_lo)
                else:
                    start = int(base)
                values.update(range(start, hi + 1, step))
            elif "-" in part:
                rng_lo_s, rng_hi_s = part.split("-", 1)
                rng_lo, rng_hi = int(rng_lo_s), int(rng_hi_s)
                if rng_lo > rng_hi:
                    raise CronParseError(
                        f"Range start {rng_lo} > end {rng_hi}"
                    )
                values.update(range(rng_lo, rng_hi + 1))
            elif part == "*":
                values.update(range(lo, hi + 1))
            else:
                values.add(int(part))
        # Clamp to valid range
        return frozenset(v for v in values if lo <= v <= hi)

    @classmethod
    def _expand_all(cls, expr: str) -> Tuple[frozenset[int], ...]:
        """Expand all five fields into frozensets of valid values."""
        tokens = expr.strip().split()
        if len(tokens) != 5:
            raise CronParseError(
                f"Expected 5 fields, got {len(tokens)}: {expr!r}"
            )
        return tuple(
            cls._expand_field(tokens[i], lo, hi)
            for i, (lo, hi) in enumerate(cls._FIELD_RANGES)
        )

    @classmethod
    def next_fire_time(cls, expr: str, after: float) -> float:
        """Compute the next fire time (UTC epoch) strictly after ``after``.

        Iterates minute-by-minute starting from ``after + 60`` (rounded
        down to the nearest minute) until a match is found.  Gives up
        after scanning ~400 days to prevent infinite loops.
        """
        fields = cls._expand_all(expr)
        minutes_f, hours_f, doms_f, months_f, dows_f = fields

        # Start from the next full minute after ``after``
        dt = datetime.fromtimestamp(after, tz=timezone.utc)
        dt = dt.replace(second=0, microsecond=0)
        # Advance to the next minute
        dt = dt.replace(
            minute=dt.minute,
            second=0,
            microsecond=0,
        )
        # Step forward one minute to ensure "strictly after"
        start_ts = calendar.timegm(dt.timetuple()) + 60
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)

        # Scan up to ~400 days * 1440 min/day = 576000 iterations
        max_iterations = 576_000
        for _ in range(max_iterations):
            if (
                dt.minute in minutes_f
                and dt.hour in hours_f
                and dt.day in doms_f
                and dt.month in months_f
                and dt.weekday() in _iso_to_cron_dow(dt)
                and _cron_dow_match(dt, dows_f)
            ):
                return calendar.timegm(dt.timetuple())
            # Advance by one minute
            new_ts = calendar.timegm(dt.timetuple()) + 60
            dt = datetime.fromtimestamp(new_ts, tz=timezone.utc)

        raise CronParseError(
            f"Could not find next fire time within 400 days for {expr!r}"
        )

    @classmethod
    def is_due(cls, expr: str, last_run: Optional[float], now: float) -> bool:
        """Check if a cron expression is due for firing.

        Returns True if the schedule should fire at ``now`` given the
        ``last_run`` timestamp (or None if never run).
        """
        try:
            after = last_run if last_run is not None else (now - 86400)
            nxt = cls.next_fire_time(expr, after)
            return nxt <= now
        except CronParseError:
            return False


def _iso_to_cron_dow(dt: datetime) -> frozenset[int]:
    """Convert Python weekday (Mon=0..Sun=6) to cron dow set for matching."""
    # Python: Monday=0 ... Sunday=6
    # Cron:   Sunday=0 ... Saturday=6
    py_dow = dt.weekday()
    cron_dow = (py_dow + 1) % 7
    return frozenset({cron_dow})


def _cron_dow_match(dt: datetime, dows_field: frozenset[int]) -> bool:
    """Check if ``dt``'s day-of-week matches the cron dow field."""
    py_dow = dt.weekday()
    cron_dow = (py_dow + 1) % 7
    return cron_dow in dows_field


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _serialize_entries(entries: List[ScheduleEntry]) -> str:
    """Serialize schedule entries to JSON string."""
    records = []
    for e in entries:
        d = asdict(e)
        # Tuple -> list for JSON
        d["target_files"] = list(d["target_files"])
        records.append(d)
    return json.dumps(records, indent=2)


def _deserialize_entries(raw: str) -> List[ScheduleEntry]:
    """Deserialize schedule entries from JSON string.  Returns empty list on error."""
    try:
        records = json.loads(raw)
        if not isinstance(records, list):
            return []
        entries: List[ScheduleEntry] = []
        for d in records:
            if not isinstance(d, dict):
                continue
            d["target_files"] = tuple(d.get("target_files", ()))
            entries.append(ScheduleEntry(**{
                k: v for k, v in d.items()
                if k in ScheduleEntry.__dataclass_fields__
            }))
        return entries
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Failed to deserialize schedules: %s", exc)
        return []


# ---------------------------------------------------------------------------
# ScheduledAgentRunner
# ---------------------------------------------------------------------------


class ScheduledAgentRunner:
    """Cron-driven governance task scheduler.

    Parameters
    ----------
    gls:
        Reference to the ``GovernedLoopService`` for submitting operations.
        Only ``submit()`` is called, typed as ``Any`` to avoid circular imports.
    persistence_path:
        Filesystem path for schedule persistence.  Defaults to
        ``~/.jarvis/ouroboros/schedules.json``.
    check_interval_s:
        How often (seconds) to evaluate due schedules.
    """

    def __init__(
        self,
        gls: Any,
        persistence_path: Optional[Path] = None,
        check_interval_s: Optional[float] = None,
    ) -> None:
        self._gls = gls
        self._persistence_path = persistence_path or _PERSISTENCE_FILE
        self._check_interval = check_interval_s or _CHECK_INTERVAL_S
        self._entries: List[ScheduleEntry] = []
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._fires_total = 0
        self._last_check: Optional[float] = None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        cron_expr: str,
        goal: str,
        target_files: Sequence[str] = (),
        max_runs: Optional[int] = None,
    ) -> ScheduleEntry:
        """Add a new scheduled task.

        Raises ``CronParseError`` if ``cron_expr`` is invalid.
        """
        if not CronParser.parse(cron_expr):
            raise CronParseError(f"Invalid cron expression: {cron_expr!r}")

        now = time.time()
        entry = ScheduleEntry(
            cron_expr=cron_expr,
            goal=goal,
            target_files=tuple(target_files),
            max_runs=max_runs,
            created_at=now,
        )
        try:
            entry.next_run = CronParser.next_fire_time(cron_expr, now)
        except CronParseError:
            entry.next_run = None

        self._entries.append(entry)
        self._persist()
        logger.info(
            "[ScheduledAgents] Added schedule %s: %s (next=%s)",
            entry.schedule_id[:8],
            goal[:60],
            datetime.fromtimestamp(entry.next_run, tz=timezone.utc).isoformat()
            if entry.next_run else "unknown",
        )
        return entry

    def remove(self, schedule_id: str) -> bool:
        """Remove a schedule by ID.  Returns True if found and removed."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.schedule_id != schedule_id]
        if len(self._entries) < before:
            self._persist()
            logger.info("[ScheduledAgents] Removed schedule %s", schedule_id[:8])
            return True
        return False

    def list_schedules(self) -> List[ScheduleEntry]:
        """Return a shallow copy of all schedule entries."""
        return list(self._entries)

    def enable(self, schedule_id: str) -> bool:
        """Enable a schedule.  Returns True if found."""
        entry = self._find(schedule_id)
        if entry is None:
            return False
        entry.enabled = True
        # Recompute next_run from now
        try:
            entry.next_run = CronParser.next_fire_time(entry.cron_expr, time.time())
        except CronParseError:
            pass
        self._persist()
        return True

    def disable(self, schedule_id: str) -> bool:
        """Disable a schedule.  Returns True if found."""
        entry = self._find(schedule_id)
        if entry is None:
            return False
        entry.enabled = False
        self._persist()
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted schedules and start the background check loop."""
        if not _ENABLED:
            logger.info("[ScheduledAgents] Disabled via environment variable")
            return

        self._load()
        self._running = True
        self._loop_task = asyncio.create_task(
            self._check_loop(), name="scheduled_agents_loop"
        )
        logger.info(
            "[ScheduledAgents] Started with %d schedule(s), check interval %.0fs",
            len(self._entries),
            self._check_interval,
        )

    async def stop(self) -> None:
        """Cancel the background loop and persist state."""
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        self._persist()
        logger.info("[ScheduledAgents] Stopped (%d total fires)", self._fires_total)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return health status for observability."""
        active = [e for e in self._entries if e.enabled]
        next_times = [e.next_run for e in active if e.next_run is not None]
        return {
            "running": self._running,
            "total_schedules": len(self._entries),
            "active_schedules": len(active),
            "fires_total": self._fires_total,
            "last_check": self._last_check,
            "next_fire_time": min(next_times) if next_times else None,
            "check_interval_s": self._check_interval,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find(self, schedule_id: str) -> Optional[ScheduleEntry]:
        return next(
            (e for e in self._entries if e.schedule_id == schedule_id), None
        )

    async def _check_loop(self) -> None:
        """Background loop — checks for due schedules every interval."""
        while self._running:
            try:
                await self._check_and_fire()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[ScheduledAgents] Error in check loop")
            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break

    async def _check_and_fire(self) -> None:
        """Evaluate all enabled schedules and fire those that are due."""
        now = time.time()
        self._last_check = now
        fired = 0

        for entry in self._entries:
            if not entry.enabled:
                continue
            # Honour max_runs
            if entry.max_runs is not None and entry.run_count >= entry.max_runs:
                entry.enabled = False
                continue

            if not CronParser.is_due(entry.cron_expr, entry.last_run, now):
                continue

            # Fire!
            try:
                await self._fire_entry(entry, now)
                fired += 1
            except Exception:
                logger.exception(
                    "[ScheduledAgents] Failed to fire schedule %s",
                    entry.schedule_id[:8],
                )

        if fired:
            self._persist()
            logger.info("[ScheduledAgents] Fired %d schedule(s)", fired)

    async def _fire_entry(self, entry: ScheduleEntry, now: float) -> None:
        """Submit a single schedule entry to the governance pipeline."""
        # Build a minimal OperationContext — import lazily to avoid circular deps
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext(
            goal=entry.goal,
            target_files=list(entry.target_files),
        )

        logger.info(
            "[ScheduledAgents] Firing schedule %s: %s",
            entry.schedule_id[:8],
            entry.goal[:80],
        )

        await self._gls.submit(ctx, trigger_source="scheduled_agent")

        # Update entry state
        entry.last_run = now
        entry.run_count += 1
        try:
            entry.next_run = CronParser.next_fire_time(entry.cron_expr, now)
        except CronParseError:
            entry.next_run = None

        self._fires_total += 1

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write current schedules to disk.  Failures are logged, not raised."""
        try:
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persistence_path.with_suffix(".tmp")
            tmp_path.write_text(_serialize_entries(self._entries), encoding="utf-8")
            tmp_path.replace(self._persistence_path)
        except OSError as exc:
            logger.warning("[ScheduledAgents] Persist failed: %s", exc)

    def _load(self) -> None:
        """Load schedules from disk.  Missing or corrupt files are tolerated."""
        if not self._persistence_path.exists():
            logger.info("[ScheduledAgents] No persisted schedules at %s", self._persistence_path)
            return
        try:
            raw = self._persistence_path.read_text(encoding="utf-8")
            loaded = _deserialize_entries(raw)
            self._entries = loaded
            logger.info(
                "[ScheduledAgents] Loaded %d schedule(s) from %s",
                len(loaded),
                self._persistence_path,
            )
        except OSError as exc:
            logger.warning("[ScheduledAgents] Failed to load schedules: %s", exc)
