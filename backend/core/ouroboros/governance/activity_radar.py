"""Live activity radar (PRD §38 Slice 4, 2026-05-07).

Closes the operator-flagged "real visibility problem" from
§38.2: O+V runs 16 sensors + 5 contexts + 11 phases + autonomy
bridges + cron soaks + cost cage + posture inferrer in
parallel — most invisible to the operator. Activity radar
surfaces a 60-second sliding window of WHAT'S ACTIVE in the
organism, categorized into closed taxonomy buckets.

CC has nothing like this — it's structurally one thread of
execution. Activity radar is a unique-to-O+V differentiator.

## Composes canonical sources (operator binding "no duplication")

  * :class:`governance.ide_observability_stream.StreamEventBroker`
    via ``recent_history(limit=...)`` — the canonical SSE ring
    buffer; returns chronologically-ordered StreamEvent records
    with event_type + timestamp + op_id + payload.
  * :class:`governance.firing_telemetry.FiringTelemetryRegistry`
    via ``snapshot()`` — the canonical per-key counter store;
    returns FireCounterEntry tuples with key + count + first/
    last_seen timestamps. Sensors register here; the radar
    composes this for "fires per N seconds" rates.

NEVER reimplements event history, sensor counters, or
sliding-window aggregation primitives.

## Architectural locks (operator mandate, AST-pinned)

  1. **Master flag default-FALSE** per §33.1.
  2. **Authority asymmetry** — imports stdlib +
     governance.ide_observability_stream + governance.firing_telemetry
     ONLY. NEVER imports orchestrator / iron_gate / policy /
     providers / candidate_generator / change_engine /
     semantic_guardian.
  3. **Closed 5-value category taxonomy** — :class:`ActivityCategory`
     (SENSORS / BRIDGES / GOVERNANCE / GENERATION / OTHER).
     New values require explicit scope-doc + pin update.
  4. **Composes canonical broker** — aggregator MUST
     lazy-import ``StreamEventBroker.recent_history`` (no
     parallel event ring; no direct subscriber loop).
  5. **Composes canonical firing telemetry** — sensor-rate
     surfacing MUST compose ``FiringTelemetryRegistry.snapshot``
     (no parallel counter store).
"""
from __future__ import annotations

import datetime
import enum
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


ACTIVITY_RADAR_SCHEMA_VERSION: str = "activity_radar.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_ACTIVITY_RADAR_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off, :func:`format_activity_radar`
    returns empty string and the ``/radar`` REPL verb reports
    disabled. Operator flips after observing the radar's
    composition."""
    if os.environ.get( "JARVIS_ACTIVITY_RADAR_ENABLED", "", ).strip().lower() in _TRUTHY:
        return True
    # §40 polish pack opt-in — when JARVIS_UX_POLISH_PACK_ENABLED
    # is on AND the operator hasn't explicitly disabled this
    # substrate via its own env flag, the pack predicate
    # activates it. Preserves §33.1 default-FALSE discipline:
    # the canonical _flag(...) / _TRUTHY check above is intact
    # so the substrate's master_default_false AST pin still
    # fires structurally.
    try:
        from backend.core.ouroboros.governance.ux_polish_pack import (
            is_substrate_in_active_pack,
        )
        return is_substrate_in_active_pack('activity_radar')
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Tunable knobs — env-overridable (operator-binding "no hardcoding")
# ---------------------------------------------------------------------------


_DEFAULT_WINDOW_S: float = 60.0
_DEFAULT_HISTORY_LIMIT: int = 500
_DEFAULT_BAR_WIDTH: int = 14
_DEFAULT_TOP_EVENTS_PER_CATEGORY: int = 3


def window_seconds() -> float:
    raw = os.environ.get(
        "JARVIS_ACTIVITY_RADAR_WINDOW_S", "",
    ).strip()
    if not raw:
        return _DEFAULT_WINDOW_S
    try:
        return max(1.0, min(3600.0, float(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_S


def history_limit() -> int:
    raw = os.environ.get(
        "JARVIS_ACTIVITY_RADAR_HISTORY_LIMIT", "",
    ).strip()
    if not raw:
        return _DEFAULT_HISTORY_LIMIT
    try:
        return max(10, min(10000, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_LIMIT


def bar_width() -> int:
    raw = os.environ.get(
        "JARVIS_ACTIVITY_RADAR_BAR_WIDTH", "",
    ).strip()
    if not raw:
        return _DEFAULT_BAR_WIDTH
    try:
        return max(4, min(60, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_BAR_WIDTH


def top_events_per_category() -> int:
    raw = os.environ.get(
        "JARVIS_ACTIVITY_RADAR_TOP_EVENTS", "",
    ).strip()
    if not raw:
        return _DEFAULT_TOP_EVENTS_PER_CATEGORY
    try:
        return max(0, min(10, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_TOP_EVENTS_PER_CATEGORY


# ---------------------------------------------------------------------------
# Closed 5-value activity-category taxonomy (AST-pinned)
# ---------------------------------------------------------------------------


class ActivityCategory(str, enum.Enum):
    """Closed 5-value taxonomy describing what subsystem
    produced the event. Bytes-pinned via AST regression.

      * ``SENSORS`` — autonomous sensor signal sources (16
        sensors per CLAUDE.md). Includes curiosity intents,
        codebase character injections, intent classifications.
      * ``BRIDGES`` — Phase 3 autonomy observability bridges
        (ExecutionMonitor / ExecutionGraphProgress /
        AutonomyCommandBus).
      * ``GOVERNANCE`` — posture / governor / circuit-breaker /
        cost-band / drift / memory-pressure events.
      * ``GENERATION`` — orchestrator pipeline + tool/plan/
        candidate events (task_*, plan_*, multi_prior_*,
        thinking_progress, decision_*).
      * ``OTHER`` — any event_type not in the canonical
        category map. Defensive fallback — keeps radar
        complete without dropping events.
    """

    SENSORS = "sensors"
    BRIDGES = "bridges"
    GOVERNANCE = "governance"
    GENERATION = "generation"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Event-type → category mapping (canonical declarative table)
# ---------------------------------------------------------------------------


_SENSORS_PREFIXES: FrozenSet[str] = frozenset({
    "curiosity_",
    "codebase_character_",
    "intent_",
    "vision_",
    "test_",
    "backlog_",
})

_SENSORS_EXACT: FrozenSet[str] = frozenset({
    "curiosity_intent_emitted",
    "curiosity_question_emitted",
    "codebase_character_injected",
    "skill_invocation",
    "domain_map_update",
    "production_oracle_signal",
    "goal_inference_built",
})

_BRIDGES_EXACT: FrozenSet[str] = frozenset({
    "execution_graph_progress",
    "autonomy_command_bus",
    "task_created",
    "task_started",
    "task_updated",
    "task_completed",
    "task_cancelled",
    "board_closed",
})

_GOVERNANCE_EXACT: FrozenSet[str] = frozenset({
    "posture_changed",
    "behavioral_drift_detected",
    "invariant_drift_detected",
    "cost_band_crossed",
    "circuit_breaker_approaching",
    "governor_throttle_applied",
    "governor_emergency_brake",
    "memory_pressure_changed",
    "memory_fanout_decision",
    "flag_typo_detected",
    "flag_registered",
    "flag_changed",
    "tool_confidence_band_crossed",
    "confidence_drop_detected",
    "review_branch_created",
    "review_branch_accepted",
    "review_branch_rejected",
    "review_branch_expired",
})

_GENERATION_EXACT: FrozenSet[str] = frozenset({
    "plan_pending",
    "plan_approved",
    "plan_rejected",
    "plan_expired",
    "plan_generated",
    "multi_prior_dispatch",
    "thinking_progress_tick",
    "decision_recorded",
    "confidence_observed",
    "decision_drift_detected",
    "metrics_updated",
    "adversarial_findings_emitted",
    "auto_action_proposal",
    "auto_action_proposal_emitted",
    "counterfactual_replay_complete",
    "sbt_tree_complete",
    "cigw_report_recorded",
    "failure_mode_recalled_at_generate",
    "action_outcome_recalled_at_generate",
    "causal_advisory_emitted",
    "m10_proposal_emitted",
    "ledger_entry_added",
    "context_compacted",
    "terminal_postmortem_persisted",
    "dag_fork_detected",
})


def categorize_event_type(event_type: str) -> ActivityCategory:
    """Map an SSE event type → :class:`ActivityCategory`.
    Pure function. NEVER raises.

    Match precedence:
      1. Exact match in SENSORS / BRIDGES / GOVERNANCE /
         GENERATION sets
      2. Sensor prefix match (e.g., ``curiosity_*``)
      3. Defensive fallback → ``OTHER``"""
    if not isinstance(event_type, str) or not event_type:
        return ActivityCategory.OTHER
    et = event_type.strip().lower()
    if et in _SENSORS_EXACT:
        return ActivityCategory.SENSORS
    if et in _BRIDGES_EXACT:
        return ActivityCategory.BRIDGES
    if et in _GOVERNANCE_EXACT:
        return ActivityCategory.GOVERNANCE
    if et in _GENERATION_EXACT:
        return ActivityCategory.GENERATION
    # Prefix fallback for sensor families.
    for prefix in _SENSORS_PREFIXES:
        if et.startswith(prefix):
            return ActivityCategory.SENSORS
    return ActivityCategory.OTHER


# ---------------------------------------------------------------------------
# Versioned snapshot artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategorySummary:
    """Per-category aggregation. Frozen for safe propagation."""

    schema_version: str = ACTIVITY_RADAR_SCHEMA_VERSION
    category: ActivityCategory = ActivityCategory.OTHER
    event_count: int = 0
    distinct_event_types: int = 0
    top_events: Tuple[Tuple[str, int], ...] = field(
        default_factory=tuple,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "category": self.category.value,
            "event_count": int(self.event_count),
            "distinct_event_types": int(
                self.distinct_event_types,
            ),
            "top_events": [
                {"event_type": et, "count": int(n)}
                for et, n in self.top_events
            ],
        }


@dataclass(frozen=True)
class ActivityRadarSnapshot:
    """Full radar snapshot — one per :func:`aggregate_activity`
    call. Frozen + serializable."""

    schema_version: str = ACTIVITY_RADAR_SCHEMA_VERSION
    window_s: float = 0.0
    aggregated_at_unix: float = 0.0
    events_in_window: int = 0
    distinct_event_types: int = 0
    by_category: Tuple[CategorySummary, ...] = field(
        default_factory=tuple,
    )
    sensor_fire_rate_per_min: float = 0.0
    distinct_op_ids_in_window: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "window_s": float(self.window_s),
            "aggregated_at_unix": float(
                self.aggregated_at_unix,
            ),
            "events_in_window": int(self.events_in_window),
            "distinct_event_types": int(
                self.distinct_event_types,
            ),
            "by_category": [
                c.to_dict() for c in self.by_category
            ],
            "sensor_fire_rate_per_min": float(
                self.sensor_fire_rate_per_min,
            ),
            "distinct_op_ids_in_window": int(
                self.distinct_op_ids_in_window,
            ),
        }

    def total_for_category(
        self, cat: ActivityCategory,
    ) -> int:
        for c in self.by_category:
            if c.category == cat:
                return c.event_count
        return 0


# ---------------------------------------------------------------------------
# Aggregation — composes canonical broker + firing telemetry
# ---------------------------------------------------------------------------


_TIMESTAMP_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"T(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.(?P<us>\d+))?Z?$",
)


def _parse_iso8601_unix(ts: str) -> Optional[float]:
    """Parse ISO-8601 UTC timestamp (broker emits this shape)
    to Unix seconds. NEVER raises; returns None on bad input."""
    if not isinstance(ts, str) or not ts:
        return None
    m = _TIMESTAMP_RE.match(ts.strip())
    if not m:
        return None
    try:
        dt = datetime.datetime(
            year=int(m.group("y")),
            month=int(m.group("mo")),
            day=int(m.group("d")),
            hour=int(m.group("h")),
            minute=int(m.group("mi")),
            second=int(m.group("s")),
            microsecond=(
                int(m.group("us")[:6].ljust(6, "0"))
                if m.group("us")
                else 0
            ),
            tzinfo=datetime.timezone.utc,
        )
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def aggregate_activity(
    *,
    window_s_override: Optional[float] = None,
    now_unix: Optional[float] = None,
    history_limit_override: Optional[int] = None,
) -> ActivityRadarSnapshot:
    """Compose canonical broker + firing telemetry → activity
    radar snapshot. Pure read; NEVER raises.

    Filters broker history to events within ``window_s`` of
    ``now_unix`` (default both env-overridable). Categorizes
    each event via the canonical declarative map. Computes
    per-category counts + top-N event types per category.

    Composes ``firing_telemetry.snapshot`` for the sensor-fire
    rate (per-minute) — useful when the broker history doesn't
    reach far enough back, since firing_telemetry is
    process-wide-cumulative."""
    import time as _time
    try:
        win = (
            float(window_s_override)
            if window_s_override is not None
            else window_seconds()
        )
        now = (
            float(now_unix)
            if now_unix is not None
            else _time.time()
        )
        cutoff = now - max(0.0, win)
        limit = (
            int(history_limit_override)
            if history_limit_override is not None
            else history_limit()
        )
    except (TypeError, ValueError):
        return ActivityRadarSnapshot(
            window_s=_DEFAULT_WINDOW_S,
            aggregated_at_unix=_time.time(),
        )

    # Compose canonical broker history.
    events: List[Any] = []
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            events = broker.recent_history(limit=limit) or []
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[activity_radar] broker compose swallowed: %s",
            type(exc).__name__,
        )
        events = []

    # Filter to events within window.
    in_window: List[Any] = []
    distinct_op_ids: set = set()
    for ev in events:
        ts = _parse_iso8601_unix(
            getattr(ev, "timestamp", "") or "",
        )
        if ts is None:
            continue
        if ts < cutoff:
            continue
        in_window.append(ev)
        op_id = getattr(ev, "op_id", "") or ""
        if op_id:
            distinct_op_ids.add(op_id)

    # Categorize + aggregate.
    cat_counts: Dict[ActivityCategory, int] = defaultdict(int)
    cat_event_types: Dict[
        ActivityCategory, Dict[str, int],
    ] = defaultdict(lambda: defaultdict(int))
    distinct_types: set = set()
    for ev in in_window:
        et = getattr(ev, "event_type", "") or ""
        cat = categorize_event_type(et)
        cat_counts[cat] += 1
        cat_event_types[cat][et] += 1
        distinct_types.add(et)

    # Build category summaries in canonical order (matches enum
    # iteration order — bytes-pinned via taxonomy invariant).
    summaries: List[CategorySummary] = []
    top_n = top_events_per_category()
    for cat in ActivityCategory:
        et_counts = cat_event_types.get(cat, {})
        sorted_top = sorted(
            et_counts.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:top_n]
        summaries.append(CategorySummary(
            category=cat,
            event_count=cat_counts.get(cat, 0),
            distinct_event_types=len(et_counts),
            top_events=tuple(sorted_top),
        ))

    # Compose firing telemetry for sensor-fire rate.
    sensor_rate = 0.0
    try:
        from backend.core.ouroboros.governance.firing_telemetry import (  # noqa: E501
            get_default_registry,
        )
        registry = get_default_registry()
        snap = registry.snapshot()
        # Total fires across the session uptime.
        uptime = max(1.0, snap.session_uptime_s)
        sensor_rate = (
            snap.total_increments * 60.0
        ) / uptime
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[activity_radar] firing_telemetry compose "
            "swallowed: %s",
            type(exc).__name__,
        )
        sensor_rate = 0.0

    return ActivityRadarSnapshot(
        window_s=win,
        aggregated_at_unix=now,
        events_in_window=len(in_window),
        distinct_event_types=len(distinct_types),
        by_category=tuple(summaries),
        sensor_fire_rate_per_min=sensor_rate,
        distinct_op_ids_in_window=len(distinct_op_ids),
    )


# ---------------------------------------------------------------------------
# Render — multi-line ASCII radar
# ---------------------------------------------------------------------------


_FILLED_CHAR_DEFAULT: str = "█"
_EMPTY_CHAR_DEFAULT: str = "░"


def _filled_char() -> str:
    return os.environ.get(
        "JARVIS_ACTIVITY_RADAR_FILLED_CHAR", "",
    ) or _FILLED_CHAR_DEFAULT


def _empty_char() -> str:
    return os.environ.get(
        "JARVIS_ACTIVITY_RADAR_EMPTY_CHAR", "",
    ) or _EMPTY_CHAR_DEFAULT


def _render_bar(
    count: int, max_count: int, *, width: int,
) -> str:
    if max_count <= 0 or width <= 0:
        return _empty_char() * max(0, width)
    filled = int(round(width * (count / max_count)))
    filled = max(0, min(width, filled))
    return (
        _filled_char() * filled
        + _empty_char() * (width - filled)
    )


_CATEGORY_LABELS: Dict[ActivityCategory, str] = {
    ActivityCategory.SENSORS: "SENSORS",
    ActivityCategory.BRIDGES: "BRIDGES",
    ActivityCategory.GOVERNANCE: "GOVERNANCE",
    ActivityCategory.GENERATION: "GENERATION",
    ActivityCategory.OTHER: "OTHER",
}


def format_activity_radar(
    snapshot: Optional[ActivityRadarSnapshot] = None,
) -> str:
    """Render the radar as a multi-line ASCII string.

    Output shape:
        ``Activity radar (last 60s · 47 events · 18 ops):``
        ``  SENSORS    ████████████░░  18  curiosity_intent...``
        ``  BRIDGES    ██████░░░░░░░░  9   execution_graph...``
        ``  GOVERNANCE ███░░░░░░░░░░░  4   posture_changed``
        ``  GENERATION ██████████░░░░  15  task_completed...``

    NEVER raises. Returns empty when:
      * Master flag off
      * No events in window
    """
    try:
        if not master_enabled():
            return ""
        snap = (
            snapshot
            if snapshot is not None
            else aggregate_activity()
        )
        if snap.events_in_window <= 0:
            return ""
        # Find max count for bar normalization.
        max_count = max(
            (c.event_count for c in snap.by_category),
            default=0,
        )
        if max_count <= 0:
            return ""
        bw = bar_width()
        win_int = int(snap.window_s)
        header = (
            f"Activity radar (last {win_int}s · "
            f"{snap.events_in_window} events · "
            f"{snap.distinct_op_ids_in_window} ops):"
        )
        lines = [header]
        # Stable label width based on canonical categories.
        label_width = max(
            len(label) for label in _CATEGORY_LABELS.values()
        )
        for c in snap.by_category:
            if c.event_count <= 0:
                continue
            label = _CATEGORY_LABELS.get(
                c.category, c.category.value.upper(),
            )
            bar = _render_bar(
                c.event_count, max_count, width=bw,
            )
            top_str = ""
            if c.top_events:
                top_names = [
                    name for name, _ in c.top_events
                ]
                top_str = ", ".join(top_names[:3])
                if len(top_str) > 50:
                    top_str = top_str[:47] + "..."
            lines.append(
                f"  {label:<{label_width}} {bar} "
                f"{c.event_count:>4}  {top_str}"
            )
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[activity_radar] format_activity_radar "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_ACTIVITY_RADAR_ENABLED
         stays default-FALSE per §33.1.
      2. ``authority_asymmetry`` — substrate purity.
      3. ``category_taxonomy_5_values`` — closed-enum integrity.
      4. ``composes_canonical_broker`` — aggregator MUST
         lazy-import ``recent_history`` from
         ``ide_observability_stream`` (no parallel event ring).
      5. ``composes_canonical_firing_telemetry`` — sensor-rate
         path MUST lazy-import ``get_default_registry`` from
         ``firing_telemetry`` (no parallel counter store).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/activity_radar.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                # §40 polish-pack composition: walk only the
                # top-level body + unconditional containers (Try)
                # so `if env_check: return True` is correctly
                # recognized as gated. Naive `"return True" in src`
                # would fire on the conditional path too.
                def _has_unconditional_return_true(stmts):
                    for stmt in stmts:
                        if (
                            isinstance(stmt, ast.Return)
                            and isinstance(stmt.value, ast.Constant)
                            and stmt.value.value is True
                        ):
                            return True
                        if isinstance(stmt, ast.Try):
                            if _has_unconditional_return_true(
                                stmt.body,
                            ):
                                return True
                            if _has_unconditional_return_true(
                                stmt.finalbody,
                            ):
                                return True
                    return False

                if _has_unconditional_return_true(node.body):
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_ACTIVITY_RADAR_ENABLED" not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_ACTIVITY_RADAR_ENABLED"
                    )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"activity_radar MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_category_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "SENSORS", "BRIDGES", "GOVERNANCE",
            "GENERATION", "OTHER",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "ActivityCategory":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"ActivityCategory missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"ActivityCategory has extras "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_composes_broker(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "ide_observability_stream" not in source:
            violations.append(
                "activity_radar MUST compose "
                "ide_observability_stream (no parallel "
                "event ring)"
            )
        if "recent_history" not in source:
            violations.append(
                "aggregator MUST use canonical "
                "recent_history accessor"
            )
        return tuple(violations)

    def _validate_composes_firing_telemetry(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "firing_telemetry" not in source:
            violations.append(
                "activity_radar MUST compose "
                "firing_telemetry (no parallel counter "
                "store)"
            )
        if "get_default_registry" not in source:
            violations.append(
                "sensor-rate path MUST use canonical "
                "get_default_registry accessor"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "activity_radar_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_ACTIVITY_RADAR_ENABLED "
                "stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "activity_radar_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Radar MUST stay pure substrate composing "
                "ide_observability_stream + firing_telemetry "
                "+ stdlib ONLY. NEVER imports orchestrator / "
                "iron_gate / policy / providers / "
                "candidate_generator / change_engine / "
                "semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "activity_radar_category_taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "ActivityCategory is a 5-value closed "
                "taxonomy (SENSORS / BRIDGES / GOVERNANCE / "
                "GENERATION / OTHER). New values require "
                "explicit scope-doc + pin update."
            ),
            validate=_validate_category_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "activity_radar_composes_canonical_broker"
            ),
            target_file=target,
            description=(
                "Aggregator MUST compose canonical "
                "ide_observability_stream.recent_history. "
                "No parallel event ring; no direct subscriber "
                "loop."
            ),
            validate=_validate_composes_broker,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "activity_radar_composes_canonical_firing_"
                "telemetry"
            ),
            target_file=target,
            description=(
                "Sensor-rate path MUST compose canonical "
                "firing_telemetry.get_default_registry. No "
                "parallel counter store."
            ),
            validate=_validate_composes_firing_telemetry,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_ACTIVITY_RADAR_ENABLED",
            "bool",
            "false",
            (
                "Master flag for the live activity radar "
                "(§38 Slice 4). Default-FALSE per §33.1."
            ),
        ),
        (
            "JARVIS_ACTIVITY_RADAR_WINDOW_S",
            "float",
            str(_DEFAULT_WINDOW_S),
            "Sliding window seconds (default 60).",
        ),
        (
            "JARVIS_ACTIVITY_RADAR_HISTORY_LIMIT",
            "int",
            str(_DEFAULT_HISTORY_LIMIT),
            "Max events pulled from broker history.",
        ),
        (
            "JARVIS_ACTIVITY_RADAR_BAR_WIDTH",
            "int",
            str(_DEFAULT_BAR_WIDTH),
            "Render bar width (chars).",
        ),
        (
            "JARVIS_ACTIVITY_RADAR_TOP_EVENTS",
            "int",
            str(_DEFAULT_TOP_EVENTS_PER_CATEGORY),
            "Top-N event types shown per category.",
        ),
        (
            "JARVIS_ACTIVITY_RADAR_FILLED_CHAR",
            "str",
            _FILLED_CHAR_DEFAULT,
            "Filled-position bar glyph (default █).",
        ),
        (
            "JARVIS_ACTIVITY_RADAR_EMPTY_CHAR",
            "str",
            _EMPTY_CHAR_DEFAULT,
            "Empty-position bar glyph (default ░).",
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="ux",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "activity_radar.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return n
    return n


__all__ = [
    "ACTIVITY_RADAR_SCHEMA_VERSION",
    "ActivityCategory",
    "ActivityRadarSnapshot",
    "CategorySummary",
    "aggregate_activity",
    "categorize_event_type",
    "format_activity_radar",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "window_seconds",
]
