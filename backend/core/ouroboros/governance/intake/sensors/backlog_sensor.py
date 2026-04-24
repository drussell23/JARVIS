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
        "status": "pending" | "in_progress" | "completed",
        "urgency_hint": "critical" | "high" | "normal" | "low"  (F2 Slice 1, optional)
    }

Priority → urgency mapping:
    5 → "high", 4 → "high", 3 → "normal", 1-2 → "low"

F2 Slice 1 — optional per-entry ``urgency_hint`` lets individual backlog
entries override the priority-map default (and the F3 session-wide env
override) when ``JARVIS_BACKLOG_URGENCY_HINT_ENABLED=true``. Absent /
invalid values fall back to priority-map. Precedence, most-specific wins:
per-entry hint > F3 env override > priority-map default. See
``memory/project_followup_f2_backlog_urgency_hint_schema.md`` for the
full arc; ``routing_hint`` is Slice 2 and is NOT read here.
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
        "JARVIS_BACKLOG_FS_EVENTS_ENABLED", "true",
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

# ---------------------------------------------------------------------------
# F3 (Wave 3 (6) Slice 5a side-arc, 2026-04-23): default-urgency override
# ---------------------------------------------------------------------------
#
# Graduation cadences for Wave 2 (5) / Wave 3 (6) that require multi-file
# post-GENERATE work (fan-out, Iron Gate live evidence) hit a structural
# bottleneck: BacklogSensor-emitted ops carry ``source="backlog"`` which
# :class:`UrgencyRouter` maps to BACKGROUND whenever urgency != "critical"
# (source-based BG classification fires regardless of priority→urgency).
# BACKGROUND is sealed from Claude cascade (``project_bg_spec_sealed.md``),
# so when DW has topology issues, backlog ops die upstream of the phase
# pipeline's `pctx.generation` production and no downstream reachability
# markers (including Wave 3 (6)'s ``[ParallelDispatch]``) can fire.
#
# F3 is the narrowest fix: a session-scoped env knob that overrides the
# priority→urgency mapping for BacklogSensor emissions. Default unset →
# behavior byte-identical to pre-F3. Set to ``"critical"`` → ops route
# IMMEDIATE (Claude direct). Set to ``"high"`` / ``"normal"`` / ``"low"``
# → priority mapping ignored but urgency still flows through the rest of
# classification (source=backlog may still land BG via the source-based
# branch; ``"critical"`` is the only escape via the Priority-1 IMMEDIATE
# gate).
#
# Scope boundaries (binding per operator F3 contract, 2026-04-23):
# - Only affects this sensor's emissions. No changes to UrgencyRouter or
#   the intake router's dispatch semantics. Those are F1 (non-blocking
#   follow-up — see project_followup_f1_intake_governor_enforcement.md).
# - No schema change to backlog.json entries. Per-entry urgency_hint is
#   F2 (non-blocking follow-up — project_followup_f2_backlog_urgency_hint_schema.md).
# - One INFO log per override-armed scan cycle so the ledger can prove
#   when the knob was active.
#
# This knob is intended for graduation / battle-test harness use ONLY.
# Long-term production intake should rely on the enforcing SensorGovernor
# from F1.
_VALID_URGENCIES = frozenset({"critical", "high", "normal", "low"})


def _default_urgency_override() -> Optional[str]:
    """Return the urgency override if set to a recognized value, else None.

    Reads ``JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY`` at call time. Invalid
    or unset values return ``None`` — the sensor then falls back to the
    priority→urgency mapping. Never raises.
    """
    raw = os.environ.get("JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "").strip().lower()
    if not raw:
        return None
    if raw in _VALID_URGENCIES:
        return raw
    return None


# ---------------------------------------------------------------------------
# F2 Slice 1 — per-entry urgency_hint consumption
# ---------------------------------------------------------------------------
#
# Default-off flag gating per-entry ``urgency_hint`` reads on backlog.json
# entries. When on + entry carries a valid hint, that hint wins over both
# the F3 session-wide env override AND the priority-map default. When off
# OR hint absent/invalid, the sensor behaves byte-identically to pre-F2.
# Slice 1 stamps the envelope with the hint value only; ``routing_hint``
# consumption lives in Slice 2 (see F2 scope doc).
def _urgency_hint_enabled() -> bool:
    """Re-read ``JARVIS_BACKLOG_URGENCY_HINT_ENABLED`` at call-time."""
    return os.environ.get(
        "JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "false",
    ).strip().lower() in ("true", "1", "yes")


def _validate_urgency_hint(raw: Any) -> Optional[str]:
    """Normalize + validate a raw urgency_hint value.

    Accepts any string matching (case-insensitive) one of ``_VALID_URGENCIES``.
    Non-string, empty, or unknown values return ``None`` so the caller can
    fall back to the priority-map default. Never raises.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    if cleaned in _VALID_URGENCIES:
        return cleaned
    return None


@dataclass
class BacklogTask:
    task_id: str
    description: str
    target_files: List[str]
    priority: int
    repo: str
    status: str = "pending"
    # F2 Slice 1 — optional per-entry hint. None = not set / invalid;
    # caller falls back to priority-map default.
    urgency_hint: Optional[str] = None

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
        # F3: re-read the override per scan so operator env changes take
        # effect without a restart. When set, log ONCE per scan_once
        # invocation that produced an envelope — keeps telemetry concise
        # (not per-task) while still §8-auditable per graduation directive.
        _urgency_override = _default_urgency_override()
        _override_logged_this_scan = False
        # F2 Slice 1: re-read the master flag per scan (same runtime-
        # responsiveness contract as F3). Default-off: hints are parsed
        # into BacklogTask but not consumed for envelope urgency.
        _hint_flag_on = _urgency_hint_enabled()
        _hint_applied_this_scan = False
        _hint_invalid_this_scan = False
        for item in tasks_raw:
            # F2 Slice 1: validate + normalize the per-entry hint at
            # construction time. Invalid values get surfaced as one
            # WARNING per scan (below) for §8 auditability.
            _raw_hint = item.get("urgency_hint")
            _validated_hint = _validate_urgency_hint(_raw_hint)
            if _raw_hint is not None and _validated_hint is None:
                _hint_invalid_this_scan = True
            task = BacklogTask(
                task_id=item.get("task_id", ""),
                description=item.get("description", ""),
                target_files=list(item.get("target_files", [])),
                priority=int(item.get("priority", 3)),
                repo=item.get("repo", "jarvis"),
                status=item.get("status", "pending"),
                urgency_hint=_validated_hint,
            )
            if task.status != "pending":
                continue
            if task.task_id in self._seen_task_ids:
                continue
            if not task.target_files:
                continue

            # Precedence (most-specific wins):
            #   F2 per-entry hint > F3 session env override > priority-map
            # When master flag is off, per-entry hint is parsed but NOT
            # consumed — falls through to F3/priority, byte-identical to
            # pre-F2 behavior for default-off sessions.
            if _hint_flag_on and task.urgency_hint is not None:
                effective_urgency = task.urgency_hint
                _hint_applied_this_scan = True
            elif _urgency_override is not None:
                effective_urgency = _urgency_override
            else:
                effective_urgency = task.urgency
            if _urgency_override is not None and not _override_logged_this_scan:
                logger.info(
                    "[BacklogSensor] JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY "
                    "override active: urgency=%s (applied to all emissions "
                    "this scan)",
                    _urgency_override,
                )
                _override_logged_this_scan = True

            envelope = make_envelope(
                source="backlog",
                description=task.description,
                target_files=tuple(task.target_files),
                repo=task.repo,
                confidence=0.7 + (task.priority - 1) * 0.05,
                urgency=effective_urgency,
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

        # F2 Slice 1 telemetry — one INFO per scan when at least one
        # per-entry hint was consumed; one WARNING per scan when any
        # raw hint failed validation (so operators can diagnose silent
        # fallbacks without scanning every entry by hand). Ledger-parseable.
        if _hint_applied_this_scan:
            logger.info(
                "[BacklogSensor] JARVIS_BACKLOG_URGENCY_HINT_ENABLED active: "
                "per-entry urgency_hint consumed for one or more emissions "
                "this scan (precedence: per-entry > F3 env > priority-map)"
            )
        if _hint_invalid_this_scan:
            logger.warning(
                "[BacklogSensor] one or more backlog.json entries had an "
                "invalid urgency_hint (accepted values: %s); affected "
                "entries fell back to priority-map / F3 override",
                sorted(_VALID_URGENCIES),
            )

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
