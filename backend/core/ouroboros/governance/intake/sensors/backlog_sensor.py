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
        "urgency_hint": "critical" | "high" | "normal" | "low"    (F2 Slice 1, optional)
        "routing_hint": "immediate" | "standard" | "complex" |
                        "background" | "speculative"              (F2 Slice 2, optional)
    }

Priority → urgency mapping:
    5 → "high", 4 → "high", 3 → "normal", 1-2 → "low"

F2 Slice 1 — optional per-entry ``urgency_hint`` lets individual backlog
entries override the priority-map default (and the F3 session-wide env
override) when ``JARVIS_BACKLOG_URGENCY_HINT_ENABLED=true``. Absent /
invalid values fall back to priority-map. Precedence, most-specific wins:
per-entry hint > F3 env override > priority-map default.

F2 Slice 2 — optional per-entry ``routing_hint`` stamps an envelope-level
``routing_override`` that UrgencyRouter honors under the same master flag
``JARVIS_BACKLOG_URGENCY_HINT_ENABLED``. Disambiguated from
UrgencyRouter's existing harness pre-stamp path via the
``provider_route_reason`` prefix ``"envelope_routing_override"``. See
``memory/project_followup_f2_backlog_urgency_hint_schema.md`` for the
full arc.
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


# F2 Slice 2 — allowed routing_hint values (ProviderRoute enum values).
# Duplicated here (vs importing) to keep intake → urgency_router import-
# acyclic: sensors run upstream of routing and must not depend on
# routing internals.
_VALID_ROUTING_HINTS = frozenset({
    "immediate", "standard", "complex", "background", "speculative",
})


def _validate_routing_hint(raw: Any) -> Optional[str]:
    """Normalize + validate a raw routing_hint value.

    Accepts any string matching (case-insensitive) one of
    ``_VALID_ROUTING_HINTS``. Non-string, empty, or unknown values
    return ``None`` so the caller can skip envelope stamping. Never
    raises.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    if cleaned in _VALID_ROUTING_HINTS:
        return cleaned
    return None


# ---------------------------------------------------------------------------
# P1 Slice 3 — auto_proposed second source (SelfGoalFormationEngine ledger)
# ---------------------------------------------------------------------------
#
# When ``JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED=true``, BacklogSensor reads
# the ``self_goal_formation_proposals.jsonl`` audit ledger as a second
# input source. Each proposal becomes one IntentEnvelope with:
#   * source = "auto_proposed"  (distinct from manual "backlog" entries)
#   * evidence = {auto_proposed: True, signature_hash, cluster_member_count, rationale, ...}
#   * requires_human_ack = True  (operator-review-required tier per PRD §9 P1)
#
# Default-off until Slice 5 graduation.

# Cap on proposals emitted per scan — bounds the second source's
# emission rate so a runaway engine cannot flood the intake queue.
_MAX_PROPOSALS_PER_SCAN: int = 5

# Cap on the number of ledger entries we scan per call (most-recent N).
# Mirrors the postmortem-recall MAX_SCAN pattern; keeps the read bounded
# even if the ledger grows beyond expectation.
_MAX_LEDGER_ENTRIES_TO_SCAN: int = 200

# Posture-to-urgency mapping for proposals. EXPLORE proposals come from
# active development cycles → "normal". CONSOLIDATE proposals indicate
# the operator wants to close threads → "high" so they surface ahead of
# routine BG work.
_POSTURE_URGENCY_MAP: Dict[str, str] = {
    "EXPLORE": "normal",
    "CONSOLIDATE": "high",
}


def _auto_proposed_enabled() -> bool:
    """Master flag for the P1 Slice 3 auto_proposed second source.

    Default ``false`` until Slice 5 graduation. When off, BacklogSensor
    behaves byte-for-byte like pre-Slice-3 (manual backlog.json only)."""
    raw = os.environ.get(
        "JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


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
    # F2 Slice 2 — optional per-entry routing hint. None = not set /
    # invalid; caller skips envelope routing_override stamping.
    routing_hint: Optional[str] = None

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
        proposals_ledger_path: Optional[Path] = None,
    ) -> None:
        self._backlog_path = backlog_path
        self._repo_root = repo_root
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._seen_task_ids: set[str] = set()
        # P1 Slice 3 — second-source: SelfGoalFormationEngine proposals
        # JSONL ledger. Default path mirrors the engine's persistence
        # default (.jarvis/self_goal_formation_proposals.jsonl).
        self._proposals_ledger_path: Path = (
            Path(proposals_ledger_path).resolve()
            if proposals_ledger_path is not None
            else (Path(repo_root) / ".jarvis"
                  / "self_goal_formation_proposals.jsonl").resolve()
        )
        # --- Gap #4 FS-event state (captured once so a runtime env flip
        # does not retroactively demote the poll loop; matches every
        # earlier sensor migration) ----------------------------------
        self._fs_events_mode: bool = fs_events_enabled()
        self._fs_events_handled: int = 0
        self._fs_events_ignored: int = 0

    async def scan_once(self) -> List[IntentEnvelope]:
        """Run one scan. Returns list of envelopes produced and ingested
        across BOTH the manual ``backlog.json`` source and (P1 Slice 3,
        when enabled via ``JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED``) the
        ``SelfGoalFormationEngine`` proposals JSONL ledger.

        The two scans are independent — a missing ``backlog.json`` does
        not block the proposals scan, and vice versa — so operators can
        rely on the auto-proposed pipeline even before they create their
        first manual backlog entry."""
        produced: List[IntentEnvelope] = []
        produced.extend(await self._scan_backlog_json())
        if _auto_proposed_enabled():
            produced.extend(await self._scan_proposals_ledger())
        return produced

    async def _scan_backlog_json(self) -> List[IntentEnvelope]:
        """Original ``scan_once`` body — reads ``backlog.json`` (manual
        operator entries). Extracted in P1 Slice 3 so the proposals
        ledger can run as a peer second source."""
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
        # F2 Slice 2 — routing_hint telemetry counters. Same master flag
        # gates consumption (single operator knob for the full F2 arc).
        _routing_applied_this_scan = False
        _routing_invalid_this_scan = False
        for item in tasks_raw:
            # F2 Slice 1: validate + normalize the per-entry hint at
            # construction time. Invalid values get surfaced as one
            # WARNING per scan (below) for §8 auditability.
            _raw_hint = item.get("urgency_hint")
            _validated_hint = _validate_urgency_hint(_raw_hint)
            if _raw_hint is not None and _validated_hint is None:
                _hint_invalid_this_scan = True
            # F2 Slice 2: same pattern for routing_hint.
            _raw_routing = item.get("routing_hint")
            _validated_routing = _validate_routing_hint(_raw_routing)
            if _raw_routing is not None and _validated_routing is None:
                _routing_invalid_this_scan = True
            task = BacklogTask(
                task_id=item.get("task_id", ""),
                description=item.get("description", ""),
                target_files=list(item.get("target_files", [])),
                priority=int(item.get("priority", 3)),
                repo=item.get("repo", "jarvis"),
                status=item.get("status", "pending"),
                urgency_hint=_validated_hint,
                routing_hint=_validated_routing,
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

            # F2 Slice 2: stamp envelope.routing_override when master
            # flag on + entry has valid routing_hint. Flag-off or hint
            # absent/invalid → empty string (pre-F2 byte-identical,
            # envelope carries no override, UrgencyRouter falls through
            # to source-type mapping).
            _effective_routing_override = ""
            if _hint_flag_on and task.routing_hint is not None:
                _effective_routing_override = task.routing_hint
                _routing_applied_this_scan = True
            envelope = make_envelope(
                source="backlog",
                description=task.description,
                target_files=tuple(task.target_files),
                repo=task.repo,
                confidence=0.7 + (task.priority - 1) * 0.05,
                urgency=effective_urgency,
                evidence={"task_id": task.task_id, "signature": task.task_id},
                requires_human_ack=False,
                routing_override=_effective_routing_override,
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
        # F2 Slice 2 — mirror telemetry for routing_hint.
        if _routing_applied_this_scan:
            logger.info(
                "[BacklogSensor] JARVIS_BACKLOG_URGENCY_HINT_ENABLED active: "
                "per-entry routing_hint consumed for one or more emissions "
                "this scan (envelope.routing_override stamped; "
                "UrgencyRouter honors via envelope_routing_override path)"
            )
        if _routing_invalid_this_scan:
            logger.warning(
                "[BacklogSensor] one or more backlog.json entries had an "
                "invalid routing_hint (accepted values: %s); affected "
                "entries emit without routing_override (pre-F2 fallback)",
                sorted(_VALID_ROUTING_HINTS),
            )

        return produced

    async def _scan_proposals_ledger(self) -> List[IntentEnvelope]:
        """P1 Slice 3 — read the SelfGoalFormationEngine JSONL ledger as
        a second input source.

        Per-scan bounds (defense-in-depth on top of engine-side caps):
          * Reads at most ``_MAX_LEDGER_ENTRIES_TO_SCAN`` most-recent rows.
          * Emits at most ``_MAX_PROPOSALS_PER_SCAN`` envelopes.
          * Skips any signature_hash already in ``_seen_task_ids`` so the
            same proposal cannot fan out across multiple scans.

        Best-effort: malformed lines / missing fields / missing files
        return ``[]`` — never raises.
        """
        if not self._proposals_ledger_path.exists():
            return []

        try:
            raw = self._proposals_ledger_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug(
                "[BacklogSensor] auto_proposed: ledger read failed: %s", exc,
            )
            return []

        lines = raw.splitlines()
        # Most-recent N (ledger is append-only newest-last).
        recent = lines[-_MAX_LEDGER_ENTRIES_TO_SCAN:]

        produced: List[IntentEnvelope] = []
        emitted_log_count = 0

        for line in recent:
            if len(produced) >= _MAX_PROPOSALS_PER_SCAN:
                break
            line = line.strip()
            if not line:
                continue
            try:
                draft = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Malformed line — skip silently (defensive: never abort
                # on a bad row).
                continue
            if not isinstance(draft, dict):
                continue

            sig_hash = str(draft.get("signature_hash", "")).strip()
            description = str(draft.get("description", "")).strip()
            target_files_raw = draft.get("target_files", []) or []
            if not (sig_hash and description and target_files_raw):
                continue
            # Dedup against the prefixed task_id form actually stored
            # in _seen_task_ids on successful enqueue (below).
            dedup_key = f"auto-proposed:{sig_hash}"
            if dedup_key in self._seen_task_ids:
                continue

            target_files = tuple(
                str(f) for f in target_files_raw if str(f).strip()
            )
            if not target_files:
                continue

            posture = str(draft.get("posture_at_proposal", "")).strip().upper()
            urgency = _POSTURE_URGENCY_MAP.get(posture, "normal")

            evidence: Dict[str, Any] = {
                # Use signature_hash as both task_id (for sensor dedup) +
                # signature (for downstream dedup keys).
                "task_id": f"auto-proposed:{sig_hash}",
                "signature": sig_hash,
                "auto_proposed": True,
                "signature_hash": sig_hash,
                "cluster_member_count": int(
                    draft.get("cluster_member_count", 0) or 0
                ),
                "rationale": str(draft.get("rationale", ""))[:500],
                "posture_at_proposal": posture,
                "schema_version": str(
                    draft.get("schema_version", "self_goal_formation.1")
                ),
            }

            envelope = make_envelope(
                source="auto_proposed",
                description=description,
                target_files=target_files,
                repo="jarvis",
                confidence=0.6,
                urgency=urgency,
                evidence=evidence,
                # Operator-review-required tier per PRD §9 P1: every
                # auto-proposed entry must hit a human surface before
                # the FSM auto-applies it.
                requires_human_ack=True,
            )

            try:
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    self._seen_task_ids.add(f"auto-proposed:{sig_hash}")
                    produced.append(envelope)
                    if emitted_log_count == 0:
                        logger.info(
                            "[BacklogSensor] auto_proposed: enqueued "
                            "signature=%s description=%r posture=%s urgency=%s",
                            sig_hash, description[:80], posture, urgency,
                        )
                    emitted_log_count += 1
            except Exception:
                logger.exception(
                    "BacklogSensor: auto_proposed ingest failed for "
                    "signature=%s",
                    sig_hash,
                )

        if emitted_log_count > 1:
            logger.info(
                "[BacklogSensor] auto_proposed: %d total proposals enqueued "
                "this scan",
                emitted_log_count,
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
