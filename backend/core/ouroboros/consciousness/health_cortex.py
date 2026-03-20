"""backend/core/ouroboros/consciousness/health_cortex.py

HealthCortex — real-time health aggregation for the Trinity Consciousness layer.

Design:
    - Poll each registered subsystem every ``poll_interval_s`` seconds.
    - Adapt ad-hoc Dict[str, Any] health payloads into typed SubsystemHealth.
    - Detect verdict transitions (HEALTHY -> DEGRADED, etc.) and emit a
      CommProtocol HEARTBEAT only on change, with a 60-second debounce.
    - Persist the HealthTrend ring-buffer to disk every 5 minutes; reload
      on start so trend survives process restarts.
    - All public methods are non-blocking; the poll loop runs as a background
      asyncio Task.

Thread-safety:
    ``_snapshot`` and ``_trend`` are only ever mutated inside the single
    asyncio event loop — no locking needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

try:
    import psutil as _psutil  # optional — graceful fallback if missing
    _PSUTIL_AVAILABLE = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False

from backend.core.ouroboros.consciousness.types import (
    BudgetHealth,
    HealthTrend,
    ResourceHealth,
    SubsystemHealth,
    TrinityHealthSnapshot,
    TrustHealth,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_S: float = 5.0          # per-subsystem call timeout
_FLUSH_INTERVAL_S: float = 300.0      # persist trend every 5 minutes
_TRANSITION_DEBOUNCE_S: float = 60.0  # minimum gap between heartbeat emissions
_ROTATION_BYTES: int = 10 * 1024 * 1024  # 10 MB rotation threshold
_UNKNOWN_STREAK_THRESHOLD: int = 3    # consecutive unknowns -> DEGRADED
_DEFAULT_TREND_PATH = Path.home() / ".jarvis" / "ouroboros" / "consciousness" / "health_trend.jsonl"

# Subsystem names that map to TrinityHealthSnapshot named fields.
_TRINITY_NAMES = ("jarvis", "prime", "reactor")


# ---------------------------------------------------------------------------
# HealthCortex
# ---------------------------------------------------------------------------


class HealthCortex:
    """Aggregate real-time health across all registered subsystems.

    Parameters
    ----------
    subsystems:
        Mapping of ``name -> callable_or_object``.  Each value must be either:
        - An object with a ``.health()`` method (sync or async), or
        - A plain callable ``() -> Dict[str, Any]`` (sync or async).
    comm:
        CommProtocol instance used to emit HEARTBEAT messages on transitions.
        Any object with ``async emit_heartbeat(op_id, phase, progress_pct)``
        is accepted; pass ``None`` to disable emission (useful in tests).
    poll_interval_s:
        Seconds between successive poll rounds.
    trend_path:
        Override the default persistence path (primarily for tests).
    """

    def __init__(
        self,
        subsystems: Dict[str, Any],
        comm: Any,
        poll_interval_s: float = 10.0,
        trend_path: Optional[Path] = None,
    ) -> None:
        self._subsystems: Dict[str, Callable[[], Any]] = {
            name: self._resolve_callable(obj)
            for name, obj in subsystems.items()
        }
        self._comm = comm
        self._poll_interval_s = poll_interval_s
        self._trend_path: Path = trend_path or _DEFAULT_TREND_PATH

        # Mutable state — only touched inside the event loop
        self._trend: HealthTrend = HealthTrend()
        self._snapshot: Optional[TrinityHealthSnapshot] = None
        self._previous_verdict: Optional[str] = None
        # Initialise far enough in the past so the first transition always
        # passes the debounce check regardless of process uptime.  Using
        # -debounce - 1 guarantees: time.monotonic() - _last_heartbeat_s
        # is always > _TRANSITION_DEBOUNCE_S at first emission.
        self._last_heartbeat_s: float = -_TRANSITION_DEBOUNCE_S - 1.0

        # Per-subsystem consecutive-unknown counters
        self._unknown_streak: Dict[str, int] = {n: 0 for n in self._subsystems}

        self._poll_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._flush_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted trend from disk then launch the background poll loop."""
        await self._load_trend()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="health_cortex_poll")
        self._flush_task = asyncio.create_task(self._flush_loop(), name="health_cortex_flush")
        logger.info("[HealthCortex] Started (interval=%.1fs)", self._poll_interval_s)

    async def stop(self) -> None:
        """Cancel the poll loop and flush the trend to disk."""
        for task in (self._poll_task, self._flush_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._poll_task = None
        self._flush_task = None
        await self._flush_trend()
        logger.info("[HealthCortex] Stopped.")

    # ------------------------------------------------------------------
    # Public non-blocking accessors
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Optional[TrinityHealthSnapshot]:
        """Return the most recently cached snapshot without blocking.

        Returns ``None`` if no poll has completed yet.
        """
        return self._snapshot

    def get_trend(self, window_minutes: int = 60) -> HealthTrend:
        """Return the live HealthTrend object.

        Callers can query ``trend.get_window(minutes)`` or use ``len(trend)``.
        """
        return self._trend

    # ------------------------------------------------------------------
    # Internal — poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Background task: call ``_poll_once`` every ``poll_interval_s``."""
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[HealthCortex] Unexpected error in poll loop")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise

    async def _flush_loop(self) -> None:
        """Background task: flush trend to disk every _FLUSH_INTERVAL_S."""
        while True:
            try:
                await asyncio.sleep(_FLUSH_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            try:
                await self._flush_trend()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[HealthCortex] Error flushing trend")

    async def _poll_once(self) -> None:
        """Execute a single full poll round across all subsystems."""
        now_utc = datetime.now(timezone.utc).isoformat()

        # Poll all subsystems concurrently
        raw_results: Dict[str, Any] = {}
        tasks = {
            name: asyncio.create_task(self._call_subsystem(name, fn))
            for name, fn in self._subsystems.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            raw_results[name] = result

        # Adapt raw dicts -> SubsystemHealth
        subsystem_healths: Dict[str, SubsystemHealth] = {}
        for name, raw in raw_results.items():
            health = self._adapt_health(name, raw, now_utc)
            subsystem_healths[name] = health
            # Update unknown streak counter
            if health.status == "unknown":
                self._unknown_streak[name] = self._unknown_streak.get(name, 0) + 1
            else:
                self._unknown_streak[name] = 0

        # Build the three trinity subsystem slots
        jarvis_h = subsystem_healths.get("jarvis") or self._unknown_subsystem("jarvis", now_utc)
        prime_h = subsystem_healths.get("prime") or self._unknown_subsystem("prime", now_utc)
        reactor_h = subsystem_healths.get("reactor") or self._unknown_subsystem("reactor", now_utc)

        # Resources
        resources = self._poll_resources()
        budget = self._poll_budget()
        trust = self._poll_trust()

        # Compute overall verdict and score
        all_healths = list(subsystem_healths.values())
        verdict = self._compute_verdict(all_healths)
        score = self._compute_score(all_healths)

        snapshot = TrinityHealthSnapshot(
            timestamp_utc=now_utc,
            overall_verdict=verdict,
            overall_score=score,
            jarvis=jarvis_h,
            prime=prime_h,
            reactor=reactor_h,
            resources=resources,
            budget=budget,
            trust=trust,
        )

        self._snapshot = snapshot
        self._trend.add(snapshot)

        # Detect state transition and maybe emit heartbeat
        await self._maybe_emit_transition(verdict)
        self._previous_verdict = verdict

    async def _call_subsystem(self, name: str, fn: Callable[[], Any]) -> Any:
        """Call *fn* with a per-subsystem timeout.  Returns raw dict or exception."""
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=_POLL_TIMEOUT_S)
            elif asyncio.isfuture(result):
                result = await asyncio.wait_for(result, timeout=_POLL_TIMEOUT_S)
            # Sync call — no timeout wrapping needed (assume fast)
            return result
        except asyncio.TimeoutError:
            logger.warning("[HealthCortex] Subsystem %r timed out", name)
            return TimeoutError(f"{name} timed out after {_POLL_TIMEOUT_S}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[HealthCortex] Subsystem %r raised: %s", name, exc)
            return exc

    # ------------------------------------------------------------------
    # Adapters
    # ------------------------------------------------------------------

    def _adapt_health(
        self,
        name: str,
        raw: Any,
        now_utc: str,
    ) -> SubsystemHealth:
        """Convert a raw health dict (or exception) into a SubsystemHealth."""
        if isinstance(raw, Exception):
            return SubsystemHealth(
                name=name,
                status="unknown",
                score=0.0,
                details={"error": str(raw)},
                polled_at_utc=now_utc,
            )

        if not isinstance(raw, dict):
            return SubsystemHealth(
                name=name,
                status="unknown",
                score=0.0,
                details={"raw": str(raw)},
                polled_at_utc=now_utc,
            )

        # Determine status/score from well-known keys
        state = raw.get("state", "")
        running = raw.get("running", None)

        if isinstance(state, str):
            state_lower = state.lower()
            if state_lower == "active":
                status, score = "healthy", 1.0
            elif state_lower in ("degraded", "degrading"):
                status, score = "degraded", 0.5
            elif state_lower in ("idle", "paused"):
                # IDLE/PAUSED is a valid non-error state — treat as healthy
                status, score = "healthy", 0.9
            elif state_lower in ("failed", "error", "offline"):
                status, score = "unknown", 0.0
            else:
                # Unknown state string — check running flag as fallback
                if running is True:
                    status, score = "healthy", 1.0
                elif running is False:
                    status, score = "unknown", 0.0
                else:
                    status, score = "degraded", 0.5
        elif running is True:
            status, score = "healthy", 1.0
        elif running is False:
            status, score = "unknown", 0.0
        else:
            status, score = "degraded", 0.5

        return SubsystemHealth(
            name=name,
            status=status,
            score=score,
            details=raw,
            polled_at_utc=now_utc,
        )

    @staticmethod
    def _unknown_subsystem(name: str, now_utc: str) -> SubsystemHealth:
        return SubsystemHealth(
            name=name,
            status="unknown",
            score=0.0,
            details={},
            polled_at_utc=now_utc,
        )

    # ------------------------------------------------------------------
    # Verdict & score
    # ------------------------------------------------------------------

    def _compute_verdict(self, healths: List[SubsystemHealth]) -> str:
        """Compute overall verdict from the list of subsystem health readings.

        Rules (applied in order, first match wins):
        1. If 2+ subsystems have score == 0.0 on the *current* poll -> CRITICAL.
        2. If any subsystem has an unknown streak >= threshold -> DEGRADED.
        3. If any subsystem status is "degraded" or "unknown" -> DEGRADED.
        4. All subsystems healthy -> HEALTHY.

        Note: rule 1 (CRITICAL) requires simultaneous failure of multiple
        subsystems.  A single subsystem that has been unknown for 3+ consecutive
        rounds only satisfies rule 2 (DEGRADED), not rule 1.
        """
        if not healths:
            return "HEALTHY"

        # Rule 1: two or more subsystems simultaneously at zero score
        zero_count = sum(1 for h in healths if h.score == 0.0)
        if zero_count >= 2:
            return "CRITICAL"

        # Rule 2: persistent unknown streak on any single subsystem
        for name, streak in self._unknown_streak.items():
            if streak >= _UNKNOWN_STREAK_THRESHOLD:
                return "DEGRADED"

        # Rule 3: any degraded / unknown status in this round
        for h in healths:
            if h.status in ("degraded", "unknown"):
                return "DEGRADED"

        return "HEALTHY"

    @staticmethod
    def _compute_score(healths: List[SubsystemHealth]) -> float:
        """Weighted average of subsystem scores (equal weight)."""
        if not healths:
            return 1.0
        return sum(h.score for h in healths) / len(healths)

    # ------------------------------------------------------------------
    # Side-channel pollers (resources, budget, trust)
    # ------------------------------------------------------------------

    def _poll_resources(self) -> ResourceHealth:
        """Collect host-level resource metrics via psutil, or return zeros."""
        if not _PSUTIL_AVAILABLE or _psutil is None:
            return ResourceHealth(
                cpu_percent=0.0,
                ram_percent=0.0,
                disk_percent=0.0,
                pressure="NORMAL",
            )
        try:
            cpu = _psutil.cpu_percent(interval=None)
            ram = _psutil.virtual_memory().percent
            disk = _psutil.disk_usage("/").percent
            pressure = self._derive_pressure(cpu, ram, disk)
            return ResourceHealth(
                cpu_percent=cpu,
                ram_percent=ram,
                disk_percent=disk,
                pressure=pressure,
            )
        except Exception:
            logger.debug("[HealthCortex] psutil resource poll failed", exc_info=True)
            return ResourceHealth(
                cpu_percent=0.0,
                ram_percent=0.0,
                disk_percent=0.0,
                pressure="NORMAL",
            )

    @staticmethod
    def _derive_pressure(cpu: float, ram: float, disk: float) -> str:
        max_pct = max(cpu, ram, disk)
        if max_pct >= 95.0:
            return "EMERGENCY"
        if max_pct >= 85.0:
            return "CRITICAL"
        if max_pct >= 70.0:
            return "ELEVATED"
        return "NORMAL"

    @staticmethod
    def _poll_budget() -> BudgetHealth:
        """Read budget from env vars; return zeros when not configured."""
        try:
            daily = float(os.getenv("JARVIS_DAILY_SPEND_USD", "0.0"))
            iteration = float(os.getenv("JARVIS_ITERATION_SPEND_USD", "0.0"))
            remaining = float(os.getenv("JARVIS_REMAINING_USD", "10.0"))
        except (ValueError, TypeError):
            daily, iteration, remaining = 0.0, 0.0, 10.0
        return BudgetHealth(
            daily_spend_usd=daily,
            iteration_spend_usd=iteration,
            remaining_usd=remaining,
        )

    @staticmethod
    def _poll_trust() -> TrustHealth:
        """Read trust tier from env; return governed/0.0 when not configured."""
        tier = os.getenv("JARVIS_GOVERNANCE_MODE", "governed")
        try:
            progress = float(os.getenv("JARVIS_TRUST_GRADUATION_PROGRESS", "0.0"))
        except (ValueError, TypeError):
            progress = 0.0
        return TrustHealth(current_tier=tier, graduation_progress=progress)

    # ------------------------------------------------------------------
    # Transition emission
    # ------------------------------------------------------------------

    async def _maybe_emit_transition(self, current_verdict: str) -> None:
        """Emit a HEARTBEAT CommMessage if the verdict has changed and debounce allows."""
        if self._comm is None:
            return
        if self._previous_verdict is None:
            # First poll — record verdict but don't emit
            return
        if current_verdict == self._previous_verdict:
            return

        now_mono = time.monotonic()
        if now_mono - self._last_heartbeat_s < _TRANSITION_DEBOUNCE_S:
            logger.debug(
                "[HealthCortex] Transition %s->%s suppressed by debounce",
                self._previous_verdict,
                current_verdict,
            )
            return

        self._last_heartbeat_s = now_mono
        try:
            await self._comm.emit_heartbeat(
                op_id="health_cortex",
                phase=f"verdict_transition:{self._previous_verdict}->{current_verdict}",
                progress_pct=0.0,
            )
            logger.info(
                "[HealthCortex] Emitted HEARTBEAT: %s -> %s",
                self._previous_verdict,
                current_verdict,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[HealthCortex] emit_heartbeat failed", exc_info=True)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _flush_trend(self) -> None:
        """Serialize the current trend ring-buffer to JSONL, rotating at 10 MB."""
        path = self._trend_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Rotate if over size limit
            if path.exists() and path.stat().st_size >= _ROTATION_BYTES:
                rotated = path.with_suffix(f".{int(time.time())}.jsonl")
                path.rename(rotated)
                logger.info("[HealthCortex] Rotated trend file -> %s", rotated)

            snapshots = list(self._trend._entries)  # noqa: SLF001
            lines = [_snapshot_to_json(s) for s in snapshots]
            await asyncio.get_event_loop().run_in_executor(
                None, _write_lines, path, lines
            )
            logger.debug("[HealthCortex] Flushed %d snapshots to %s", len(lines), path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[HealthCortex] Failed to flush trend", exc_info=True)

    async def _load_trend(self) -> None:
        """Restore the trend ring-buffer from disk if the file exists."""
        path = self._trend_path
        if not path.exists():
            return
        try:
            lines = await asyncio.get_event_loop().run_in_executor(
                None, _read_lines, path
            )
            loaded = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    snap = _snapshot_from_json(line)
                    if snap is not None:
                        self._trend.add(snap)
                        loaded += 1
                except Exception:
                    logger.debug("[HealthCortex] Skipping corrupt trend line")
            logger.info("[HealthCortex] Loaded %d snapshots from %s", loaded, path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[HealthCortex] Failed to load trend", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_callable(obj: Any) -> Callable[[], Any]:
        """Return a zero-arg callable for *obj*.

        Priority order:
        1. Object has a ``.health`` attribute (method) — use ``obj.health``.
           This covers all real subsystem objects AND MagicMock test doubles
           that have their ``.health`` attribute configured.
        2. Object is a plain callable with no ``.health`` attribute
           (function, lambda, AsyncMock at the top level) — return it directly.
        """
        if hasattr(obj, "health"):
            return obj.health  # type: ignore[return-value]
        if callable(obj):
            return obj
        raise TypeError(
            f"Subsystem object {obj!r} is not callable and has no .health() method"
        )


# ---------------------------------------------------------------------------
# JSONL serialisation helpers (module-level, pure functions)
# ---------------------------------------------------------------------------


def _snapshot_to_json(snap: TrinityHealthSnapshot) -> str:
    """Serialize a TrinityHealthSnapshot to a compact JSON line."""
    record = {
        "timestamp_utc": snap.timestamp_utc,
        "overall_verdict": snap.overall_verdict,
        "overall_score": snap.overall_score,
        "jarvis": _sh_to_dict(snap.jarvis),
        "prime": _sh_to_dict(snap.prime),
        "reactor": _sh_to_dict(snap.reactor),
        "resources": {
            "cpu_percent": snap.resources.cpu_percent,
            "ram_percent": snap.resources.ram_percent,
            "disk_percent": snap.resources.disk_percent,
            "pressure": snap.resources.pressure,
        },
        "budget": {
            "daily_spend_usd": snap.budget.daily_spend_usd,
            "iteration_spend_usd": snap.budget.iteration_spend_usd,
            "remaining_usd": snap.budget.remaining_usd,
        },
        "trust": {
            "current_tier": snap.trust.current_tier,
            "graduation_progress": snap.trust.graduation_progress,
        },
    }
    return json.dumps(record)


def _sh_to_dict(sh: SubsystemHealth) -> Dict[str, Any]:
    return {
        "name": sh.name,
        "status": sh.status,
        "score": sh.score,
        "details": sh.details,
        "polled_at_utc": sh.polled_at_utc,
    }


def _snapshot_from_json(line: str) -> Optional[TrinityHealthSnapshot]:
    """Deserialize a JSONL line back to a TrinityHealthSnapshot.  Returns None on error."""
    try:
        d = json.loads(line)
        return TrinityHealthSnapshot(
            timestamp_utc=d["timestamp_utc"],
            overall_verdict=d["overall_verdict"],
            overall_score=float(d["overall_score"]),
            jarvis=_sh_from_dict(d["jarvis"]),
            prime=_sh_from_dict(d["prime"]),
            reactor=_sh_from_dict(d["reactor"]),
            resources=ResourceHealth(
                cpu_percent=float(d["resources"]["cpu_percent"]),
                ram_percent=float(d["resources"]["ram_percent"]),
                disk_percent=float(d["resources"]["disk_percent"]),
                pressure=d["resources"]["pressure"],
            ),
            budget=BudgetHealth(
                daily_spend_usd=float(d["budget"]["daily_spend_usd"]),
                iteration_spend_usd=float(d["budget"]["iteration_spend_usd"]),
                remaining_usd=float(d["budget"]["remaining_usd"]),
            ),
            trust=TrustHealth(
                current_tier=d["trust"]["current_tier"],
                graduation_progress=float(d["trust"]["graduation_progress"]),
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _sh_from_dict(d: Dict[str, Any]) -> SubsystemHealth:
    return SubsystemHealth(
        name=d["name"],
        status=d["status"],
        score=float(d["score"]),
        details=d.get("details", {}),
        polled_at_utc=d["polled_at_utc"],
    )


def _write_lines(path: Path, lines: List[str]) -> None:
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _read_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()
