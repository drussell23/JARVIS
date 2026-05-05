"""TrajectoryAuditor un-stranding observer (PRD §24.10.2 / §1
"Long-horizon semantic stability" gap closure, 2026-05-04).

The :class:`TrajectoryAuditor` shipped in 2026-04-XX with a
complete audit pipeline (codebase walk → snapshot → rolling
baseline → 4-metric drift detection → JSONL persistence) but
**no producer ever invoked it**. This observer is the
producer — it fires on boot + periodic tick + after each
auto-commit, runs ``audit()``, persists snapshots, publishes
``EVENT_TYPE_TRAJECTORY_DRIFT_DETECTED`` SSE on warning/critical
verdicts.

Architectural locks (operator mandate):

  * **Pure substrate, zero LLM cost** — the underlying
    :class:`TrajectoryAuditor` is stdlib-only (ast + os +
    pathlib + json + hashlib). Cost contract structurally
    preserved.
  * **Boot-time snapshot is non-blocking** — fires async via
    ``asyncio.create_task`` so the governed-loop boot path is
    not blocked on a full codebase walk.
  * **Periodic-tick cadence is env-tunable** — default 6h
    (``JARVIS_TRAJECTORY_OBSERVER_INTERVAL_S``). Codebase walk
    is expensive (~1-3s on JARVIS-scale repos); a 6h cadence
    bounds the overhead at ~24 walks/day.
  * **SSE chatter suppression** — only ``warning`` and
    ``critical`` verdicts publish; ``stable`` and ``growing``
    transitions stay silent. Operators see the JSONL when
    they want detail.
  * **NEVER raises** out of any public method — defensive
    everywhere. The auditor itself NEVER raises; the observer
    inherits that contract.
  * **Authority asymmetry** (AST-pinned at graduation) —
    observer MUST NOT import orchestrator / iron_gate /
    providers / urgency_router / candidate_generator /
    tool_executor / strategic_direction. Pure observer over
    the underlying auditor's read-only audit path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + env knobs
# ---------------------------------------------------------------------------


def trajectory_observer_enabled() -> bool:
    """``JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED`` —
    graduation-controlled. The underlying auditor's master flag
    (``JARVIS_TRAJECTORY_AUDITOR_ENABLED``) gates the audit
    pipeline; this observer flag gates the *invocation* of that
    pipeline.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit ``0`` / ``false`` / ``no`` / ``off`` = off.
    Re-read on every call so flips hot-revert."""
    raw = os.environ.get(
        "JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # Graduated default 2026-05-04
    return raw not in ("0", "false", "no", "off")


def _interval_s() -> float:
    """``JARVIS_TRAJECTORY_OBSERVER_INTERVAL_S`` — periodic tick
    cadence. Default 21600 (6h); clamped [60, 86400] (1m–24h)."""
    raw = os.environ.get(
        "JARVIS_TRAJECTORY_OBSERVER_INTERVAL_S", "",
    ).strip()
    if not raw:
        return 21600.0
    try:
        v = float(raw)
        if v < 60.0:
            return 60.0
        if v > 86400.0:
            return 86400.0
        return v
    except (TypeError, ValueError):
        return 21600.0


def _project_root() -> Path:
    """``JARVIS_PROJECT_ROOT`` — repo root for codebase walks.
    Defaults to cwd."""
    raw = os.environ.get("JARVIS_PROJECT_ROOT", "").strip()
    return Path(raw) if raw else Path(".")


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class TrajectoryAuditorObserver:
    """Async observer that drives the stranded
    :class:`TrajectoryAuditor`. Boot snapshot + periodic tick;
    publishes SSE on warning/critical drift verdicts.

    Lifecycle: caller awaits :meth:`start` once; then
    :meth:`stop` on shutdown. Both are idempotent + NEVER
    raise."""

    def __init__(
        self, project_root: Optional[Path] = None,
    ) -> None:
        self._project_root = project_root or _project_root()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._last_audit_at_unix: float = 0.0
        self._auditor = None  # Lazy-constructed in run()

    async def start(self) -> bool:
        """Start the periodic loop. Idempotent. Returns True if
        the loop is now running, False on master-off / any
        error."""
        try:
            if not trajectory_observer_enabled():
                return False
            if self._task is not None and not self._task.done():
                return True
            self._stopping = False
            self._task = asyncio.create_task(
                self._loop(),
                name="trajectory_auditor_observer",
            )
            return True
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[TrajectoryObserver] start raised", exc_info=True,
            )
            return False

    async def stop(self) -> None:
        """Stop the loop. Idempotent; NEVER raises."""
        self._stopping = True
        try:
            if self._task is not None and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 — defensive
            pass
        self._task = None

    async def _loop(self) -> None:
        """Boot snapshot + periodic-tick loop. NEVER raises."""
        try:
            # Boot-time snapshot (non-blocking from caller's
            # perspective via the create_task wrapping above).
            await self._tick(reason="boot")
            interval = _interval_s()
            while not self._stopping:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return
                if self._stopping:
                    return
                await self._tick(reason="periodic")
                # Re-read interval on each loop so env changes
                # hot-revert without restart
                interval = _interval_s()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[TrajectoryObserver] _loop raised",
                exc_info=True,
            )

    def _resolved_auditor(self):
        """Lazy auditor construction — defer the import + walk
        until the first tick fires."""
        if self._auditor is not None:
            return self._auditor
        try:
            from backend.core.ouroboros.governance.observability.trajectory_auditor import (  # noqa: E501
                get_default_trajectory_auditor,
            )
            self._auditor = get_default_trajectory_auditor(
                project_root=self._project_root,
            )
            return self._auditor
        except Exception:  # noqa: BLE001 — defensive
            return None

    async def _tick(self, *, reason: str) -> None:
        """One audit cycle: snapshot → record → audit → SSE.
        Awaits :func:`asyncio.to_thread` for the blocking
        codebase walk so the event loop stays responsive.
        NEVER raises."""
        if not trajectory_observer_enabled():
            return
        try:
            # Underlying auditor ALSO has a master flag
            # (JARVIS_TRAJECTORY_AUDITOR_ENABLED). When that's
            # off, audit() still works but a clean operator
            # disablement should be respected — exit early.
            from backend.core.ouroboros.governance.observability.trajectory_auditor import (  # noqa: E501
                is_trajectory_enabled,
            )
            if not is_trajectory_enabled():
                return
        except Exception:  # noqa: BLE001 — defensive
            return
        auditor = self._resolved_auditor()
        if auditor is None:
            return
        try:
            # Codebase walk + drift check (blocking — to_thread)
            report = await asyncio.to_thread(auditor.audit)
            # Persist snapshot — best-effort
            try:
                auditor.record_snapshot(report.current)
            except Exception:  # noqa: BLE001 — defensive
                pass
            self._last_audit_at_unix = time.time()
            verdict = report.verdict
            # SSE only on warning / critical / drifting / alarming —
            # stable + growing stay silent (chatter suppression)
            if verdict in ("drifting", "alarming"):
                self._publish_sse(report, reason=reason)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[TrajectoryObserver] _tick raised",
                exc_info=True,
            )

    def _publish_sse(self, report, *, reason: str) -> None:
        """Best-effort SSE publish on drift detection. NEVER
        raises."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_trajectory_drift_event,
            )
            publish_trajectory_drift_event(
                verdict=report.verdict,
                signals=tuple(
                    {
                        "metric": s.metric,
                        "baseline_value": float(
                            s.baseline_value,
                        ),
                        "current_value": float(s.current_value),
                        "change_pct": float(s.change_pct),
                        "severity": s.severity,
                        "detail": s.detail,
                    }
                    for s in (report.drift_signals or ())
                ),
                snapshot_hash=str(
                    getattr(report.current, "snapshot_hash", ""),
                ),
                ts_unix=float(report.ts_unix),
                reason=reason,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[TrajectoryObserver] SSE publish raised",
                exc_info=True,
            )

    @property
    def last_audit_at_unix(self) -> float:
        return self._last_audit_at_unix


# ---------------------------------------------------------------------------
# Process-singleton
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[TrajectoryAuditorObserver] = None


def get_default_trajectory_observer() -> TrajectoryAuditorObserver:
    global _DEFAULT_OBSERVER  # noqa: PLW0603
    if _DEFAULT_OBSERVER is None:
        _DEFAULT_OBSERVER = TrajectoryAuditorObserver()
    return _DEFAULT_OBSERVER


def reset_default_trajectory_observer_for_tests() -> None:
    global _DEFAULT_OBSERVER  # noqa: PLW0603
    _DEFAULT_OBSERVER = None


__all__ = [
    "TrajectoryAuditorObserver",
    "get_default_trajectory_observer",
    "reset_default_trajectory_observer_for_tests",
    "trajectory_observer_enabled",
]
