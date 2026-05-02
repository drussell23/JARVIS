"""IDE observability stream — Gap #6 Slice 2.

Server-Sent Events (SSE) channel exposing live agent state to
operator-side IDE extensions. Mounts alongside the Slice 1 GET
endpoints on :class:`EventChannelServer`; same host, same loopback
discipline, same CORS allowlist, same deny-by-default env gate.

## Why SSE (not WebSocket)

- **Unidirectional.** Server → client only. No bidirectional channel
  = no covert command surface. Authority invariant (Manifesto §1) is
  enforced by the transport, not by discipline alone.
- **Standard HTTP GET.** No protocol upgrade; works through proxies,
  flows through the same aiohttp app, plays nicely with
  ``Last-Event-ID`` reconnection semantics that browsers / VS Code
  ``EventSource`` already implement.
- **Text frames with explicit ``event:`` / ``id:`` / ``data:``
  headers** — natural fit for structured JSON payloads.

## Authority posture (locked by authorization)

- **Read-only.** The stream transport is unidirectional — clients
  cannot push anything back through it. Observability answers *"what
  is the loop doing"*, never *"what should the loop do"*.
- **Deny-by-default.** ``JARVIS_IDE_STREAM_ENABLED`` defaults
  ``false``; disabled returns 403 (port scanners see no signal).
- **Loopback-only.** Same :func:`assert_loopback_only` gate from
  Slice 1 — the stream route can only mount when the server binds a
  loopback host.
- **No imports from gate modules.** The same grep-pin as Slice 1:
  this module never imports orchestrator / policy / iron_gate /
  risk_tier / gate modules. A test enforces the invariant.
- **Bounded everything.** Subscriber cap, per-subscriber queue cap,
  history ring-buffer cap, heartbeat cadence — all env-tunable, all
  defaulted to sane values that cannot DoS the agent.
- **Drop-oldest on back-pressure.** A slow IDE client cannot slow
  down event production. Its queue silently discards old events and
  emits a ``stream_lag`` control frame so the client knows to reset
  its view (via the Slice 1 GET endpoints).

## Integration surface

Task-tool handlers (Gap #5 Slice 2) publish transitions via
:func:`publish_task_event`; :func:`close_task_board` publishes
``board_closed``. The hook is best-effort — a failed publish never
crashes the handler or breaks the per-transition INFO audit log
(which remains the authoritative history per Manifesto §8).

## Schema

Every frame is a JSON payload with a stable shape::

    {
      "schema_version": "1.0",
      "event_id":       "<monotonic-seq-hex>",
      "event_type":     "task_created" | "task_started" | ...
                        | "heartbeat" | "stream_lag" | "replay_start"
                        | "replay_end",
      "op_id":          str,
      "timestamp":      str (ISO-8601 UTC),
      "payload":        object
    }

The ``event_id`` is monotonic within a process lifetime; clients may
pass it back via the ``Last-Event-ID`` header on reconnect to replay
any events still in the ring-buffer history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Deque,
    Dict,
    List,
    Mapping,
    Optional,
    TYPE_CHECKING,
)

if TYPE_CHECKING:
    from aiohttp import web


logger = logging.getLogger(__name__)


# --- Schema / version ------------------------------------------------------


STREAM_SCHEMA_VERSION = "1.0"

# Event-type vocabulary — frozen so clients can hard-code an expected set.
EVENT_TYPE_TASK_CREATED = "task_created"
EVENT_TYPE_TASK_STARTED = "task_started"
EVENT_TYPE_TASK_UPDATED = "task_updated"
EVENT_TYPE_TASK_COMPLETED = "task_completed"
EVENT_TYPE_TASK_CANCELLED = "task_cancelled"
EVENT_TYPE_BOARD_CLOSED = "board_closed"
EVENT_TYPE_HEARTBEAT = "heartbeat"
EVENT_TYPE_STREAM_LAG = "stream_lag"
EVENT_TYPE_REPLAY_START = "replay_start"
EVENT_TYPE_REPLAY_END = "replay_end"

# Problem #7 Slice 4 — plan approval stream vocabulary.
EVENT_TYPE_PLAN_PENDING = "plan_pending"
EVENT_TYPE_PLAN_APPROVED = "plan_approved"
EVENT_TYPE_PLAN_REJECTED = "plan_rejected"
EVENT_TYPE_PLAN_EXPIRED = "plan_expired"

# DirectionInferrer Slice 3 — strategic posture stream vocabulary.
# Single event type: the trigger (inference vs override) is carried in
# the payload so clients render from one handler.
EVENT_TYPE_POSTURE_CHANGED = "posture_changed"

# FlagRegistry Slice 3 — flag introspection stream vocabulary.
EVENT_TYPE_FLAG_TYPO_DETECTED = "flag_typo_detected"
EVENT_TYPE_FLAG_REGISTERED = "flag_registered"

# SensorGovernor + MemoryPressureGate (Wave 1 #3 Slice 3) vocabulary.
EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED = "governor_throttle_applied"
EVENT_TYPE_GOVERNOR_EMERGENCY_BRAKE = "governor_emergency_brake"
EVENT_TYPE_MEMORY_PRESSURE_CHANGED = "memory_pressure_changed"

# Priority 1 Slice 4 — confidence-aware execution event vocabulary
# (PRD §26.5.1). Severity-tiered: P1 = breaker fired (above-floor abort),
# P2 = approaching floor (early warning), P3 = sustained low-confidence
# trend across N ops (posture nudge candidate). Slice 4 ships the
# vocabulary + publish helpers; producer wiring lives in DW provider's
# verdict-emission site and is master-flag-gated by
# JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED.
EVENT_TYPE_MODEL_CONFIDENCE_DROP = "model_confidence_drop"
EVENT_TYPE_MODEL_CONFIDENCE_APPROACHING = "model_confidence_approaching"
EVENT_TYPE_MODEL_SUSTAINED_LOW_CONFIDENCE = "model_sustained_low_confidence"
# Confidence-aware route advisor (Slice 4) — ADVISORY ONLY.
# Cost contract preservation: this event NEVER signals BG/SPEC →
# STANDARD/COMPLEX/IMMEDIATE escalation. The advisor's AST-pinned
# guard + §26.6 runtime CostContractViolation enforce structurally.
EVENT_TYPE_ROUTE_PROPOSAL = "route_proposal"

# Slice 5 Arc B — L3 fan-out decision (allow / clamp / disabled / probe_fail).
# Fires on every gate consultation from subagent_scheduler, not just clamps,
# so operator has full §8 trail. Scheduler call rate is bounded.
EVENT_TYPE_MEMORY_FANOUT_DECISION = "memory_fanout_decision"

# Inline Permission Slice 4 — per-tool-call prompt + grant stream vocab.
EVENT_TYPE_INLINE_PROMPT_PENDING = "inline_prompt_pending"
EVENT_TYPE_INLINE_PROMPT_ALLOWED = "inline_prompt_allowed"
EVENT_TYPE_INLINE_PROMPT_DENIED = "inline_prompt_denied"
EVENT_TYPE_INLINE_PROMPT_EXPIRED = "inline_prompt_expired"
EVENT_TYPE_INLINE_PROMPT_PAUSED = "inline_prompt_paused"
EVENT_TYPE_INLINE_GRANT_CREATED = "inline_grant_created"
EVENT_TYPE_INLINE_GRANT_REVOKED = "inline_grant_revoked"

# Context Preservation arc Slice 4 — ledger / pin / manifest vocab.
EVENT_TYPE_LEDGER_ENTRY_ADDED = "ledger_entry_added"
EVENT_TYPE_CONTEXT_COMPACTED = "context_compacted"
EVENT_TYPE_CONTEXT_PINNED = "context_pinned"
EVENT_TYPE_CONTEXT_UNPINNED = "context_unpinned"
EVENT_TYPE_CONTEXT_PIN_EXPIRED = "context_pin_expired"

# Session Browser extension arc Slice 3 — session history stream vocab.
# Fired by session_stream_bridge.py bridging SessionIndex / BookmarkStore
# listeners onto the broker. Pure observability — no authority surface.
EVENT_TYPE_SESSION_ADDED = "session_added"
EVENT_TYPE_SESSION_RESCAN = "session_rescan"
EVENT_TYPE_SESSION_BOOKMARKED = "session_bookmarked"
EVENT_TYPE_SESSION_UNBOOKMARKED = "session_unbookmarked"
EVENT_TYPE_SESSION_PINNED = "session_pinned"
EVENT_TYPE_SESSION_UNPINNED = "session_unpinned"

# W3(7) Slice 6 — cancel-origin SSE event (additive). Payload schema per
# scope doc §6.3: ``{"event": "cancel_origin_emitted", "data":
# {"cancel_id": str, "op_id": str, "origin": str, "phase": str}}``.
# Full record (with reason, monotonic timestamp, bounded_deadline_s,
# tasks_cancelled list once Slice 5+ populates it) lives at the
# ``/observability/cancels/<cancel_id>`` GET endpoint.
EVENT_TYPE_CANCEL_ORIGIN_EMITTED = "cancel_origin_emitted"

# W2(4) Slice 3 — curiosity question SSE event (additive). Payload schema
# per scope doc §6: ``{"event": "curiosity_question_emitted", "data":
# {"question_id": str, "op_id": str, "posture": str, "result": str,
# "question_text": str (<=80 chars)}}``. Full record (with cost burn,
# monotonic timestamp, full question text) lives at the
# ``/observability/curiosity/<question_id>`` GET endpoint.
EVENT_TYPE_CURIOSITY_QUESTION_EMITTED = "curiosity_question_emitted"

# Phase 4 P4 Slice 4 — convergence metrics suite (PRD §9 P4). Payload:
# ``{"session_id": str, "schema_version": int, "trend": str,
# "composite_score_session_mean": float | None}``. Operators get a
# live ping when a new MetricsSnapshot lands; the full record (all 7
# metrics + sparkline-ready per-op composite list) lives at
# ``/observability/metrics`` GET.
EVENT_TYPE_METRICS_UPDATED = "metrics_updated"

# Phase 5 P5 Slice 4 — adversarial reviewer (PRD §9 P5). Payload:
# ``{"op_id": str, "schema_version": int, "filtered_findings_count":
# int, "high": int, "med": int, "low": int, "skip_reason": str,
# "cost_usd": float}``. Operators get a live ping when a new
# AdversarialReview lands; the full record (findings list with
# descriptions + mitigation_hint + file_reference) lives at
# ``/observability/adversarial/{op_id}`` GET.
EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED = "adversarial_findings_emitted"

# Phase 8 surface wiring Slice 2 — Temporal Observability stream
# vocabulary. Five event types bridge the 5 Phase 8 substrate
# modules (decision_trace_ledger / latent_confidence_ring /
# latency_slo_detector / flag_change_emitter) onto the existing
# StreamEventBroker. Bridge functions live in
# ``observability/sse_bridge.py``; producers (orchestrator code +
# classifiers + periodic monitors) call those bridges after the
# substrate's record/check methods. Best-effort, never raise. All
# 5 are gated by ``JARVIS_PHASE8_SSE_BRIDGE_ENABLED`` (default
# false until graduation) AND per-event sub-flags.
EVENT_TYPE_DECISION_RECORDED = "decision_recorded"
EVENT_TYPE_CONFIDENCE_OBSERVED = "confidence_observed"
EVENT_TYPE_CONFIDENCE_DROP_DETECTED = "confidence_drop_detected"
EVENT_TYPE_SLO_BREACHED = "slo_breached"
EVENT_TYPE_FLAG_CHANGED = "flag_changed"

# Priority D Slice D1 — Postmortem ledger discoverability. Fired by
# Option E's _fire_terminal_postmortem after a successful Merkle
# DAG ledger write. Payload is summary-only; full record at
# ``/observability/postmortems/{op_id}`` GET.
EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED = "terminal_postmortem_persisted"

# Priority 2 Slice 4 — Causality DAG fork detection. Fired by
# dag_navigation.publish_dag_fork_event when a counterfactual
# branch is detected during DAG construction or navigation.
# Payload: {record_id, counterfactual_id, session_id, wall_ts}.
EVENT_TYPE_DAG_FORK_DETECTED = "dag_fork_detected"

# Priority #3 Slice 4 — Counterfactual Replay observability. Two
# event types fire from counterfactual_replay_observer:
#   * COMPLETE — per-verdict SSE: one event per recorded replay
#     (after engine produces a ReplayVerdict). Payload: {session_id,
#     swap_phase, swap_kind, outcome, verdict, recurrence_evidence,
#     tightening, cluster_kind, schema_version}.
#   * BASELINE_UPDATED — per-aggregation SSE: fires when the
#     periodic observer recomputes the recurrence-reduction-pct
#     baseline and the ComparisonOutcome changed (or every Nth
#     pass for liveness). Payload: {outcome, total_replays,
#     actionable_count, recurrence_reduction_pct, regression_rate,
#     postmortems_prevented, baseline_quality, tightening,
#     schema_version}.
# Both are PURE OBSERVABILITY — no authority surface. Cost-contract
# preserved by construction (observer reads cached artifacts only).
EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE = (
    "counterfactual_replay_complete"
)
EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED = (
    "counterfactual_baseline_updated"
)

# Priority #4 Slice 4 — Speculative Branch Tree observability. Two
# event types fire from speculative_branch_observer:
#   * COMPLETE — per-tree SSE: one event per recorded SBT run (after
#     run_speculative_tree produces a TreeVerdictResult). Payload:
#     {decision_id, ambiguity_kind, outcome, branch_count,
#     winning_fingerprint, aggregate_confidence, tightening,
#     cluster_kind, schema_version}.
#   * BASELINE_UPDATED — per-aggregation SSE: fires when the periodic
#     observer recomputes the ambiguity-resolution-rate baseline and
#     the EffectivenessOutcome changed (or every Nth pass for
#     liveness). Payload: {outcome, total_trees, actionable_count,
#     converged_count, ambiguity_resolution_rate, escalation_rate,
#     truncated_failed_rate, baseline_quality, tightening,
#     schema_version}.
# Both are PURE OBSERVABILITY — no authority surface. Cost-contract
# preserved by AST-pinned construction (observer reads cached
# artifacts only).
EVENT_TYPE_SBT_TREE_COMPLETE = "sbt_tree_complete"
EVENT_TYPE_SBT_BASELINE_UPDATED = "sbt_baseline_updated"

_VALID_EVENT_TYPES = frozenset({
    EVENT_TYPE_TASK_CREATED,
    EVENT_TYPE_TASK_STARTED,
    EVENT_TYPE_TASK_UPDATED,
    EVENT_TYPE_TASK_COMPLETED,
    EVENT_TYPE_TASK_CANCELLED,
    EVENT_TYPE_BOARD_CLOSED,
    EVENT_TYPE_HEARTBEAT,
    EVENT_TYPE_STREAM_LAG,
    EVENT_TYPE_REPLAY_START,
    EVENT_TYPE_REPLAY_END,
    EVENT_TYPE_PLAN_PENDING,
    EVENT_TYPE_PLAN_APPROVED,
    EVENT_TYPE_PLAN_REJECTED,
    EVENT_TYPE_PLAN_EXPIRED,
    EVENT_TYPE_INLINE_PROMPT_PENDING,
    EVENT_TYPE_INLINE_PROMPT_ALLOWED,
    EVENT_TYPE_INLINE_PROMPT_DENIED,
    EVENT_TYPE_INLINE_PROMPT_EXPIRED,
    EVENT_TYPE_INLINE_PROMPT_PAUSED,
    EVENT_TYPE_INLINE_GRANT_CREATED,
    EVENT_TYPE_INLINE_GRANT_REVOKED,
    EVENT_TYPE_LEDGER_ENTRY_ADDED,
    EVENT_TYPE_CONTEXT_COMPACTED,
    EVENT_TYPE_CONTEXT_PINNED,
    EVENT_TYPE_CONTEXT_UNPINNED,
    EVENT_TYPE_CONTEXT_PIN_EXPIRED,
    EVENT_TYPE_SESSION_ADDED,
    EVENT_TYPE_SESSION_RESCAN,
    EVENT_TYPE_SESSION_BOOKMARKED,
    EVENT_TYPE_SESSION_UNBOOKMARKED,
    EVENT_TYPE_SESSION_PINNED,
    EVENT_TYPE_SESSION_UNPINNED,
    EVENT_TYPE_POSTURE_CHANGED,
    EVENT_TYPE_FLAG_TYPO_DETECTED,
    EVENT_TYPE_FLAG_REGISTERED,
    EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED,
    EVENT_TYPE_GOVERNOR_EMERGENCY_BRAKE,
    EVENT_TYPE_MEMORY_PRESSURE_CHANGED,
    EVENT_TYPE_MEMORY_FANOUT_DECISION,
    EVENT_TYPE_CANCEL_ORIGIN_EMITTED,  # W3(7) Slice 6
    EVENT_TYPE_CURIOSITY_QUESTION_EMITTED,  # W2(4) Slice 3
    EVENT_TYPE_METRICS_UPDATED,  # Phase 4 P4 Slice 4
    EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED,  # Phase 5 P5 Slice 4
    EVENT_TYPE_DECISION_RECORDED,             # Phase 8 Slice 2
    EVENT_TYPE_CONFIDENCE_OBSERVED,           # Phase 8 Slice 2
    EVENT_TYPE_CONFIDENCE_DROP_DETECTED,      # Phase 8 Slice 2
    EVENT_TYPE_SLO_BREACHED,                  # Phase 8 Slice 2
    EVENT_TYPE_FLAG_CHANGED,                  # Phase 8 Slice 2
    EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED,  # Priority D Slice D1
    EVENT_TYPE_DAG_FORK_DETECTED,             # Priority 2 Slice 4
    EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE,   # Priority #3 Slice 4
    EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED,  # Priority #3 Slice 4
    EVENT_TYPE_SBT_TREE_COMPLETE,                # Priority #4 Slice 4
    EVENT_TYPE_SBT_BASELINE_UPDATED,             # Priority #4 Slice 4
})


# --- Env knobs -------------------------------------------------------------


def stream_enabled() -> bool:
    """Master switch.

    Default: **``true``** (graduated 2026-04-20 via Gap #6 Slice 4
    alongside Slice 1 flag flip; Slice 2 ships the SSE surface itself,
    Slice 3 the VS Code client, Slice 4 the graduation + Cursor-compat
    confirmation). Explicit ``"false"`` reverts to the Slice 2 deny-
    by-default posture — the structural caps (subscriber cap, queue
    cap, history cap, heartbeat cadence, subscribe-rate limiter) and
    authority-invariant grep pin all remain in force regardless of
    this flag. When the flag is explicitly ``"false"``, the stream
    route returns 403 so port scanners see no signal.
    """
    return os.environ.get(
        "JARVIS_IDE_STREAM_ENABLED", "true",
    ).strip().lower() == "true"


def _max_subscribers() -> int:
    """Concurrent SSE connection cap. Default 8 — one or two IDE
    windows per operator is the expected load; 8 leaves generous
    slack without unbounded connection growth."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_IDE_STREAM_MAX_SUBSCRIBERS", "8",
        )))
    except (TypeError, ValueError):
        return 8


def _queue_maxsize() -> int:
    """Per-subscriber queue cap. Default 64 — a slow client can buffer
    ~64 events before drop-oldest kicks in and a ``stream_lag``
    control frame is emitted."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_IDE_STREAM_QUEUE_MAXSIZE", "64",
        )))
    except (TypeError, ValueError):
        return 64


def _history_maxlen() -> int:
    """Ring-buffer size for ``Last-Event-ID`` replay. Default 512 —
    covers ~5 minutes of typical event rate on a busy op."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_IDE_STREAM_HISTORY_MAXLEN", "512",
        )))
    except (TypeError, ValueError):
        return 512


def _heartbeat_seconds() -> float:
    """Heartbeat cadence in seconds. Default 15 — tuned to sit well
    below typical HTTP idle timeouts. 0 disables heartbeats (useful
    in tests)."""
    try:
        return max(0.0, float(os.environ.get(
            "JARVIS_IDE_STREAM_HEARTBEAT_S", "15",
        )))
    except (TypeError, ValueError):
        return 15.0


# --- Event dataclass -------------------------------------------------------


@dataclass(frozen=True)
class StreamEvent:
    """One event on the wire.

    Immutable, JSON-serializable via :meth:`to_dict`. Produces SSE
    frame bytes via :meth:`to_sse_frame`.
    """

    event_id: str
    event_type: str
    op_id: str
    timestamp: str  # ISO-8601 UTC
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = STREAM_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "op_id": self.op_id,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    def to_sse_frame(self) -> bytes:
        """SSE wire encoding. See:
        https://html.spec.whatwg.org/multipage/server-sent-events.html

        Format::

            id: <event_id>
            event: <event_type>
            data: <json>
            <blank line>
        """
        data_json = json.dumps(self.to_dict(), ensure_ascii=False)
        # Escape any embedded newlines per spec (split across multiple
        # data: lines). json.dumps won't emit raw newlines but be
        # defensive about payload strings that contain them.
        data_lines = data_json.replace("\r\n", "\n").split("\n")
        lines = ["id: " + self.event_id,
                 "event: " + self.event_type]
        for line in data_lines:
            lines.append("data: " + line)
        # Trailing blank line terminates the event.
        return ("\n".join(lines) + "\n\n").encode("utf-8")


# --- Helpers ----------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --- Subscriber -------------------------------------------------------------


@dataclass
class _Subscriber:
    """Internal — one connected SSE client.

    Owns a bounded asyncio.Queue of pending events. The broker only
    holds a reference while the client is connected; on disconnect,
    :meth:`close` drops the reference and any in-flight events.
    """

    sub_id: int
    op_id_filter: Optional[str]
    queue: "asyncio.Queue[StreamEvent]"
    loop: asyncio.AbstractEventLoop
    maxsize: int
    drop_count: int = 0
    created_mono: float = field(default_factory=time.monotonic)
    # Edge-case race fix (2026-05-01): per-subscriber degradation
    # tracking so operators can distinguish "one slow client" from
    # "all clients lagging" — the original aggregate dropped_count
    # was blind to per-subscriber health.
    last_drop_at: float = 0.0  # monotonic timestamp of last drop
    _lag_pending: bool = False  # suppress duplicate lag frames
    _closed: bool = False

    def matches(self, event: StreamEvent) -> bool:
        """Does this event pass the op_id filter?

        Control-frame event types always pass (heartbeat / stream_lag
        are per-subscriber metadata and are produced targeted).
        """
        if event.event_type in (
            EVENT_TYPE_HEARTBEAT, EVENT_TYPE_STREAM_LAG,
            EVENT_TYPE_REPLAY_START, EVENT_TYPE_REPLAY_END,
        ):
            return True
        if self.op_id_filter is None or self.op_id_filter == "":
            return True
        return event.op_id == self.op_id_filter


# --- Broker -----------------------------------------------------------------


class StreamEventBroker:
    """Thread-safe in-process publish/subscribe with bounded history.

    One broker instance per process (module-level singleton via
    :func:`get_default_broker`). Tests reset the singleton via
    :func:`reset_default_broker`.

    Publish is sync + non-blocking — safe to call from tool handlers,
    close hooks, or anywhere without awaiting. Subscribers are
    coroutine-based async iterators, used exclusively by the SSE
    handler.
    """

    def __init__(
        self,
        *,
        history_maxlen: Optional[int] = None,
        max_subscribers: Optional[int] = None,
        queue_maxsize: Optional[int] = None,
    ) -> None:
        self._history_maxlen = history_maxlen or _history_maxlen()
        self._max_subscribers = max_subscribers or _max_subscribers()
        self._default_queue_maxsize = queue_maxsize or _queue_maxsize()
        # History is append-only; deque with maxlen does eviction
        # automatically when capacity is reached.
        self._history: Deque[StreamEvent] = deque(maxlen=self._history_maxlen)
        self._subscribers: Dict[int, _Subscriber] = {}
        self._next_sub_id: int = 0
        self._next_event_seq: int = 0
        self._lock = threading.Lock()
        self._published_count: int = 0
        self._dropped_count: int = 0

    # --- introspection (test + /observability/health future) --------------

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def history_size(self) -> int:
        with self._lock:
            return len(self._history)

    @property
    def published_count(self) -> int:
        return self._published_count  # informational; race-tolerant

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    # --- per-subscriber health (edge-case race fix 2026-05-01) -----

    def subscriber_health(self) -> List[Dict[str, Any]]:
        """Per-subscriber health snapshot.

        Edge-case race fix (2026-05-01): the original broker only
        exposed an aggregate ``dropped_count``. Operators could not
        distinguish "one slow client" from "all clients lagging".

        Returns a list of dicts, one per active subscriber::

            {
                "sub_id": int,
                "op_filter": str | "*",
                "drop_count": int,
                "last_drop_ago_s": float | None,
                "queue_depth": int,
                "queue_maxsize": int,
                "status": "healthy" | "lagging" | "wedged",
                "connected_s": float,
            }

        Classification heuristic:
          - ``healthy``: no drops, or no drops in the last 60s
          - ``lagging``: drops occurring but subscriber still draining
          - ``wedged``: queue is full AND last drop was < 5s ago
        """
        now = time.monotonic()
        with self._lock:
            subs = list(self._subscribers.values())
        result: List[Dict[str, Any]] = []
        for sub in subs:
            if sub._closed:
                continue
            drop_count = sub.drop_count
            last_drop_at = sub.last_drop_at
            queue_depth = sub.queue.qsize()
            queue_max = sub.maxsize
            connected_s = now - sub.created_mono

            if last_drop_at > 0:
                last_drop_ago = now - last_drop_at
            else:
                last_drop_ago = None

            # Classification
            if drop_count == 0 or (
                last_drop_ago is not None and last_drop_ago > 60.0
            ):
                status = "healthy"
            elif (
                queue_depth >= queue_max
                and last_drop_ago is not None
                and last_drop_ago < 5.0
            ):
                status = "wedged"
            else:
                status = "lagging"

            result.append({
                "sub_id": sub.sub_id,
                "op_filter": sub.op_id_filter or "*",
                "drop_count": drop_count,
                "last_drop_ago_s": (
                    round(last_drop_ago, 1) if last_drop_ago is not None
                    else None
                ),
                "queue_depth": queue_depth,
                "queue_maxsize": queue_max,
                "status": status,
                "connected_s": round(connected_s, 1),
            })
        return result

    # --- publish -----------------------------------------------------------

    def publish(
        self,
        event_type: str,
        op_id: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Optional[str]:
        """Emit one event. Returns the assigned ``event_id``, or None
        if the event_type is invalid (drop silently — the caller is
        typically a best-effort hook in tool handlers).

        Never raises. Never blocks. Safe to call even when no
        subscribers are connected (event lands in history only).
        """
        if event_type not in _VALID_EVENT_TYPES:
            logger.debug(
                "[Stream] publish rejected unknown event_type=%r", event_type,
            )
            return None
        if not isinstance(op_id, str):
            op_id = str(op_id or "")

        with self._lock:
            self._next_event_seq += 1
            seq = self._next_event_seq
            event_id = format(seq, "012x")
            event = StreamEvent(
                event_id=event_id,
                event_type=event_type,
                op_id=op_id,
                timestamp=_iso_now(),
                payload=dict(payload or {}),
            )
            # Ring-buffer append — deque.maxlen handles eviction.
            self._history.append(event)
            self._published_count += 1
            # Snapshot subscribers under lock; fan-out happens below.
            targets = list(self._subscribers.values())

        # Fan-out OUTSIDE the lock so put_nowait + call_soon_threadsafe
        # can't deadlock against an async consumer.
        for sub in targets:
            if sub._closed:
                continue
            if not sub.matches(event):
                continue
            self._deliver(sub, event)

        return event_id

    def _deliver(self, sub: _Subscriber, event: StreamEvent) -> None:
        """Best-effort enqueue on a subscriber's queue.

        On queue-full: drop the event, mark the subscriber lagging,
        and schedule a ``stream_lag`` control frame. The subscriber
        sees the lag frame and can reset its view via the REST
        endpoints.

        Edge-case race fix (2026-05-01): now sets ``sub.last_drop_at``
        and emits a per-subscriber WARNING log on first drop so
        operators can grep for individual slow clients.
        """
        try:
            # asyncio.Queue.put_nowait raises asyncio.QueueFull when
            # the queue is at maxsize. Wrap in try/except — we're
            # intentionally dropping oldest via the lag signal.
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_count += 1
            sub.drop_count += 1
            sub.last_drop_at = time.monotonic()
            if not sub._lag_pending:
                sub._lag_pending = True
                # Per-subscriber degradation log — first drop for
                # this subscriber since last ack. Enables operators
                # to grep for individual slow clients.
                logger.warning(
                    "[Stream] subscriber_lagging sub=%d "
                    "op_filter=%r drops=%d queue_depth=%d/%d",
                    sub.sub_id,
                    sub.op_id_filter or "*",
                    sub.drop_count,
                    sub.queue.qsize(),
                    sub.maxsize,
                )
                # Attempt to inject a lag frame. If THAT also fails,
                # the subscriber is thoroughly wedged and we just
                # drop silently — the disconnect path will clean up.
                lag_event = StreamEvent(
                    event_id=event.event_id + ":lag",
                    event_type=EVENT_TYPE_STREAM_LAG,
                    op_id=event.op_id,
                    timestamp=_iso_now(),
                    payload={
                        "dropped_since_last_ack": sub.drop_count,
                        "first_missed_event_id": event.event_id,
                        "subscriber_id": sub.sub_id,
                    },
                )
                try:
                    sub.queue.put_nowait(lag_event)
                except asyncio.QueueFull:
                    logger.debug(
                        "[Stream] lag frame also dropped sub=%d", sub.sub_id,
                    )
        except Exception:  # noqa: BLE001 — defensive, must not raise
            logger.debug(
                "[Stream] deliver exception sub=%d", sub.sub_id,
                exc_info=True,
            )

    # --- subscribe ---------------------------------------------------------

    def subscribe(
        self,
        op_id_filter: Optional[str] = None,
        last_event_id: Optional[str] = None,
    ) -> "Optional[_Subscriber]":
        """Register a new subscriber.

        Returns ``None`` if the subscriber cap is exceeded. Callers
        must pass the returned subscriber to :meth:`stream_iter` and
        release it via :meth:`unsubscribe` in a ``finally`` block.
        """
        with self._lock:
            if len(self._subscribers) >= self._max_subscribers:
                return None
            self._next_sub_id += 1
            sub_id = self._next_sub_id
            loop = asyncio.get_event_loop()
            queue: "asyncio.Queue[StreamEvent]" = asyncio.Queue(
                maxsize=self._default_queue_maxsize,
            )
            sub = _Subscriber(
                sub_id=sub_id,
                op_id_filter=op_id_filter or None,
                queue=queue,
                loop=loop,
                maxsize=self._default_queue_maxsize,
            )
            self._subscribers[sub_id] = sub
        logger.info(
            "[Stream] subscriber_connected sub=%d op_filter=%r total=%d",
            sub_id, op_id_filter or "*", len(self._subscribers),
        )
        # Seed replay if Last-Event-ID provided. Under lock-free read —
        # history is effectively append-only for this check.
        self._seed_replay(sub, last_event_id)
        return sub

    def _seed_replay(
        self, sub: _Subscriber, last_event_id: Optional[str],
    ) -> None:
        """Inject a replay_start marker, the events since
        last_event_id (filtered), and a replay_end marker.

        If last_event_id is unknown (evicted from history or never
        seen), replay begins from the oldest event still in history —
        the client sees a lag-style gap which the replay_start
        ``missed_from_id`` field makes visible.
        """
        if not last_event_id:
            return
        with self._lock:
            hist = list(self._history)
        # Linear scan — history is bounded by history_maxlen.
        start_idx = 0
        known = False
        for i, ev in enumerate(hist):
            if ev.event_id == last_event_id:
                known = True
                start_idx = i + 1
                break
        tail = hist[start_idx:] if known else hist
        # Filter by op_id and by event type.
        replay_events = [ev for ev in tail if sub.matches(ev)]
        start_marker = StreamEvent(
            event_id="replay:" + (last_event_id or "0"),
            event_type=EVENT_TYPE_REPLAY_START,
            op_id=sub.op_id_filter or "",
            timestamp=_iso_now(),
            payload={
                "last_event_id": last_event_id,
                "known": known,
                "replay_count": len(replay_events),
            },
        )
        end_marker = StreamEvent(
            event_id="replay:end:" + (last_event_id or "0"),
            event_type=EVENT_TYPE_REPLAY_END,
            op_id=sub.op_id_filter or "",
            timestamp=_iso_now(),
            payload={"replayed": len(replay_events)},
        )
        for ev in [start_marker, *replay_events, end_marker]:
            self._deliver(sub, ev)

    async def stream_iter(
        self, sub: _Subscriber, heartbeat_s: Optional[float] = None,
    ) -> AsyncIterator[StreamEvent]:
        """Async iterator yielding events for one subscriber.

        Emits a :data:`EVENT_TYPE_HEARTBEAT` frame every
        ``heartbeat_s`` seconds when the queue is idle, so dead
        connections surface promptly to the handler's write path.
        ``heartbeat_s=0`` disables heartbeats (tests).
        """
        hb = _heartbeat_seconds() if heartbeat_s is None else heartbeat_s
        try:
            while not sub._closed:
                try:
                    if hb > 0:
                        event = await asyncio.wait_for(
                            sub.queue.get(), timeout=hb,
                        )
                    else:
                        event = await sub.queue.get()
                    # Clear lag-pending flag when the queue drains past
                    # the lag frame itself.
                    if event.event_type == EVENT_TYPE_STREAM_LAG:
                        sub._lag_pending = False
                    yield event
                except asyncio.TimeoutError:
                    yield StreamEvent(
                        event_id="hb:" + format(int(time.monotonic() * 1000), "x"),
                        event_type=EVENT_TYPE_HEARTBEAT,
                        op_id=sub.op_id_filter or "",
                        timestamp=_iso_now(),
                        payload={"subscriber_count": self.subscriber_count},
                    )
        except asyncio.CancelledError:
            raise
        finally:
            self.unsubscribe(sub)

    def unsubscribe(self, sub: "_Subscriber") -> None:
        """Remove a subscriber and release its queue. Idempotent."""
        if sub._closed:
            return
        sub._closed = True
        with self._lock:
            self._subscribers.pop(sub.sub_id, None)
            total = len(self._subscribers)
        logger.info(
            "[Stream] subscriber_disconnected sub=%d drops=%d total=%d",
            sub.sub_id, sub.drop_count, total,
        )

    # --- test helpers ------------------------------------------------------

    def reset(self) -> None:
        """Drop every subscriber + clear history. Test-only — prod
        code never calls this."""
        with self._lock:
            for sub in list(self._subscribers.values()):
                sub._closed = True
            self._subscribers.clear()
            self._history.clear()
            self._next_event_seq = 0
            self._next_sub_id = 0
            self._published_count = 0
            self._dropped_count = 0


# --- Module singleton ------------------------------------------------------


_default_broker: Optional[StreamEventBroker] = None
_default_broker_lock = threading.Lock()


def get_default_broker() -> StreamEventBroker:
    global _default_broker
    with _default_broker_lock:
        if _default_broker is None:
            _default_broker = StreamEventBroker()
        return _default_broker


def reset_default_broker() -> None:
    """Test helper — reset the singleton."""
    global _default_broker
    with _default_broker_lock:
        if _default_broker is not None:
            _default_broker.reset()
        _default_broker = None


def publish_task_event(
    event_type: str,
    op_id: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Public hook for task_tool handlers. Best-effort, never raises.

    Returns the event_id on successful publish, None on any failure
    (stream disabled, invalid event_type, or broker exception).
    """
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(event_type, op_id, payload)
    except Exception:  # noqa: BLE001 — best-effort hook
        logger.debug("[Stream] publish_task_event exception", exc_info=True)
        return None


# --- Stream route handler ---------------------------------------------------


# Same op_id discipline as Slice 1.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


class IDEStreamRouter:
    """Mounts ``GET /observability/stream`` on an aiohttp app.

    Usage::

        from backend.core.ouroboros.governance.ide_observability_stream import (
            IDEStreamRouter, stream_enabled,
        )
        if stream_enabled():
            IDEStreamRouter().register_routes(app)

    Rate-tracker is separate from the Slice 1 router's — different
    trust boundary (a stream is a long-lived connection; Slice 1
    routes are short polls).
    """

    def __init__(
        self,
        broker: Optional[StreamEventBroker] = None,
    ) -> None:
        self._broker = broker
        self._rate_tracker: Dict[str, List[float]] = {}

    def _get_broker(self) -> StreamEventBroker:
        return self._broker or get_default_broker()

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get("/observability/stream", self._handle_stream)

    def _client_key(self, request: "web.Request") -> str:
        peer = getattr(request, "remote", "") or "unknown"
        return str(peer)

    def _check_subscribe_rate(self, client_key: str) -> bool:
        """Subscribe-rate limiter. Lower cap than the Slice 1 polls —
        expect ≤1 (re)connect per minute per client under normal
        operation; 10/min gives burst headroom for flaky network
        without allowing an open-close storm."""
        try:
            limit = max(1, int(os.environ.get(
                "JARVIS_IDE_STREAM_RATE_LIMIT_PER_MIN", "10",
            )))
        except (TypeError, ValueError):
            limit = 10
        now = time.monotonic()
        window_start = now - 60.0
        hist = self._rate_tracker.setdefault(client_key, [])
        while hist and hist[0] < window_start:
            hist.pop(0)
        if len(hist) >= limit:
            return False
        hist.append(now)
        return True

    def _cors_headers(self, request: "web.Request") -> Dict[str, str]:
        # Reuse Slice 1's allowlist so both surfaces share a CORS story.
        from backend.core.ouroboros.governance.ide_observability import (
            _cors_origin_patterns,
        )
        origin = request.headers.get("Origin", "") or ""
        if not origin:
            return {}
        for pattern in _cors_origin_patterns():
            try:
                if re.match(pattern, origin):
                    return {
                        "Access-Control-Allow-Origin": origin,
                        "Vary": "Origin",
                        "Access-Control-Allow-Methods": "GET, OPTIONS",
                    }
            except re.error:
                continue
        return {}

    async def _handle_stream(self, request: "web.Request") -> Any:
        """The SSE handler.

        Emits:
          - 403 when ``JARVIS_IDE_STREAM_ENABLED`` is not true
          - 400 when the ``op_id`` query is malformed
          - 429 when the subscribe-rate cap is exceeded
          - 503 when the subscriber cap is exceeded
          - streaming ``text/event-stream`` otherwise
        """
        from aiohttp import web

        if not stream_enabled():
            return web.json_response(
                {"schema_version": STREAM_SCHEMA_VERSION,
                 "error": True, "reason_code": "ide_stream.disabled"},
                status=403, headers={"Cache-Control": "no-store"},
            )

        # Parse + validate ?op_id=... (optional).
        op_id_filter = request.query.get("op_id", "").strip() or None
        if op_id_filter is not None and not _OP_ID_RE.match(op_id_filter):
            return web.json_response(
                {"schema_version": STREAM_SCHEMA_VERSION,
                 "error": True, "reason_code": "ide_stream.malformed_op_id"},
                status=400, headers={"Cache-Control": "no-store"},
            )

        client_key = self._client_key(request)
        if not self._check_subscribe_rate(client_key):
            return web.json_response(
                {"schema_version": STREAM_SCHEMA_VERSION,
                 "error": True, "reason_code": "ide_stream.rate_limited"},
                status=429, headers={"Cache-Control": "no-store"},
            )

        broker = self._get_broker()
        last_event_id = request.headers.get("Last-Event-ID", "").strip() or None
        sub = broker.subscribe(
            op_id_filter=op_id_filter, last_event_id=last_event_id,
        )
        if sub is None:
            return web.json_response(
                {"schema_version": STREAM_SCHEMA_VERSION,
                 "error": True, "reason_code": "ide_stream.capacity"},
                status=503, headers={
                    "Cache-Control": "no-store", "Retry-After": "30",
                },
            )

        # Successful subscribe — switch to streaming response.
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                # Disable proxy buffering (nginx-ism, harmless elsewhere).
                "X-Accel-Buffering": "no",
                # Reject connection caching — each reconnect gets a
                # fresh subscribe path.
                "Connection": "keep-alive",
            },
        )
        for k, v in self._cors_headers(request).items():
            resp.headers[k] = v
        await resp.prepare(request)

        try:
            # Optional initial comment line — some SSE clients drop the
            # first empty read; a comment frame kicks the parser.
            await resp.write(b": ok\n\n")
            async for event in broker.stream_iter(sub):
                try:
                    await resp.write(event.to_sse_frame())
                except (ConnectionResetError, asyncio.CancelledError):
                    raise
                except Exception:  # noqa: BLE001 — client write path
                    logger.debug(
                        "[Stream] write exception sub=%d", sub.sub_id,
                        exc_info=True,
                    )
                    break
        except asyncio.CancelledError:
            # Client disconnected — stream_iter's finally will call
            # unsubscribe(). Re-raise so aiohttp can complete the
            # response lifecycle.
            raise
        except ConnectionResetError:
            pass
        finally:
            broker.unsubscribe(sub)

        return resp


# ---------------------------------------------------------------------------
# PlanApproval → broker bridge (problem #7 Slice 4)
# ---------------------------------------------------------------------------


def bridge_plan_approval_to_broker(
    controller: Optional[Any] = None,
    broker: Optional[StreamEventBroker] = None,
) -> Callable[[], None]:
    """Wire the PlanApprovalController's transition hook to the
    SSE StreamEventBroker.

    Every plan_pending / plan_approved / plan_rejected / plan_expired
    transition becomes a typed SSE frame with the projection as the
    payload. Works with both the default controller/broker singletons
    and explicit instances (tests inject their own).

    Returns an unsubscribe callable. Idempotent: calling again with
    the same pair returns a fresh subscription without disturbing
    older ones — callers that need exactly-one subscription should
    track their own unsubscribe.

    Authority invariant: this is a read-only adapter. The controller's
    state is the source of truth; the broker never mutates it. The
    bridge runs purely in the push direction (controller → broker).
    """
    if controller is None:
        # Late import to avoid a cycle at module-load: plan_approval
        # doesn't import this module, and this module doesn't import
        # plan_approval at top level.
        from backend.core.ouroboros.governance.plan_approval import (
            get_default_controller as _get_default_controller,
        )
        controller = _get_default_controller()
    if broker is None:
        broker = get_default_broker()

    def _publish(payload: Dict[str, Any]) -> None:
        """Translate a controller transition into a broker publish."""
        event_type = payload.get("event_type")
        projection = payload.get("projection") or {}
        op_id = projection.get("op_id") or ""
        # Whitelist: only plan_* event types pass through. If the
        # controller ever fires a new event type, this bridge stays
        # silent on it rather than emitting malformed frames.
        if event_type not in (
            EVENT_TYPE_PLAN_PENDING,
            EVENT_TYPE_PLAN_APPROVED,
            EVENT_TYPE_PLAN_REJECTED,
            EVENT_TYPE_PLAN_EXPIRED,
        ):
            return
        # The plan payload can be large (full schema plan.1). Strip
        # to a summary projection to keep each SSE frame bounded.
        summary = {
            "state": projection.get("state"),
            "created_ts": projection.get("created_ts"),
            "expires_ts": projection.get("expires_ts"),
            "reviewer": projection.get("reviewer"),
            "reason": projection.get("reason"),
        }
        # IDE clients fetch the full plan via
        # GET /observability/plans/{op_id} — the SSE frame only
        # needs enough metadata to prompt the fetch.
        broker.publish(event_type, op_id, summary)

    return controller.on_transition(_publish)


# ---------------------------------------------------------------------------
# Posture → broker bridge (DirectionInferrer Slice 3)
# ---------------------------------------------------------------------------


def publish_posture_event(
    trigger: str,
    reading: Optional[Any] = None,
    previous: Optional[Any] = None,
    *,
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Best-effort publisher for ``posture_changed`` SSE frames.

    ``trigger`` ∈ {``"inference"``, ``"override_set"``,
    ``"override_cleared"``, ``"override_expired"``}. Returns the
    event_id on success, None when the stream is disabled / broker
    missing / publish raised. Never raises into the observer hot path.

    Since posture is a per-organism property (no op_id), we key the
    event by the trigger + posture value so ``?op_id=posture`` filters
    cleanly (the broker keys off op_id position 2 of the frame — we
    use the constant string ``"posture"`` so the filter vocabulary
    stays stable).
    """
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {"trigger": trigger}
        if reading is not None:
            try:
                payload["posture"] = reading.posture.value
                payload["confidence"] = reading.confidence
                payload["inferred_at"] = reading.inferred_at
                payload["signal_bundle_hash"] = reading.signal_bundle_hash
            except Exception:  # noqa: BLE001
                pass
        if previous is not None:
            try:
                payload["previous_posture"] = previous.posture.value
            except Exception:  # noqa: BLE001
                pass
        if extra:
            for k, v in extra.items():
                if k not in payload:
                    payload[k] = v
        return get_default_broker().publish(
            EVENT_TYPE_POSTURE_CHANGED, "posture", payload,
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug("[Stream] publish_posture_event exception", exc_info=True)
        return None


def bridge_posture_to_broker(
    observer: Optional[Any] = None,
    broker: Optional[StreamEventBroker] = None,
) -> Callable[[], None]:
    """Wire a PostureObserver's ``on_change`` hook into the SSE broker.

    Every inference-driven posture flip becomes a ``posture_changed``
    SSE frame. Override-driven transitions are published via
    :func:`publish_posture_event` from the REPL / override handler
    rather than through this bridge (two sources, single publisher).

    Returns a no-op unsubscribe callable — the observer's hook is a
    simple callable attached at construction; to detach, replace
    ``observer._on_change`` with ``None``.

    Authority invariant: this is a read-only adapter — the broker
    never mutates the observer. Purely push-direction.
    """
    if observer is None:
        try:
            from backend.core.ouroboros.governance.posture_observer import (
                get_default_observer,
            )
            observer = get_default_observer()
        except Exception:  # noqa: BLE001
            logger.debug("[Stream] bridge_posture_to_broker: no observer", exc_info=True)
            return lambda: None
    resolved_broker = broker or get_default_broker()

    def _publish(new_reading: Any, prev_reading: Any) -> None:
        if not stream_enabled():
            return
        try:
            payload: Dict[str, Any] = {
                "trigger": "inference",
                "posture": new_reading.posture.value,
                "confidence": new_reading.confidence,
                "inferred_at": new_reading.inferred_at,
                "signal_bundle_hash": new_reading.signal_bundle_hash,
            }
            if prev_reading is not None:
                try:
                    payload["previous_posture"] = prev_reading.posture.value
                except Exception:  # noqa: BLE001
                    pass
            resolved_broker.publish(
                EVENT_TYPE_POSTURE_CHANGED, "posture", payload,
            )
        except Exception:  # noqa: BLE001 — never raise into observer
            logger.debug("[Stream] posture bridge publish failed", exc_info=True)

    # Install as the observer's change hook. Preserve any existing hook
    # by chaining — tests that already wired a hook will get both calls.
    try:
        prev_hook = getattr(observer, "_on_change", None)
    except Exception:  # noqa: BLE001
        prev_hook = None

    def _chained(new_reading: Any, prev_reading: Any) -> None:
        _publish(new_reading, prev_reading)
        if prev_hook is not None:
            try:
                prev_hook(new_reading, prev_reading)
            except Exception:  # noqa: BLE001
                logger.debug("[Stream] prev posture hook raised", exc_info=True)

    try:
        observer._on_change = _chained  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        logger.debug("[Stream] cannot install posture hook", exc_info=True)
        return lambda: None

    def _unsubscribe() -> None:
        try:
            observer._on_change = prev_hook  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    return _unsubscribe


# ---------------------------------------------------------------------------
# FlagRegistry → broker bridge (Wave 1 #2 Slice 3)
# ---------------------------------------------------------------------------


def publish_flag_typo_event(
    env_name: str,
    suggestion: str,
    distance: int,
) -> Optional[str]:
    """Best-effort publisher for flag_typo_detected frames.

    Returns the event_id on success, None when stream is disabled /
    broker missing / publish raised. Never raises. Deduplication is
    the caller's responsibility — FlagRegistry.report_typos already
    dedups per-env-var-per-process, so this fires exactly once per
    unique typo per session when wired through the bridge.
    """
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_FLAG_TYPO_DETECTED, "flag_registry",
            {
                "env_name": env_name,
                "closest_match": suggestion,
                "distance": distance,
            },
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug("[Stream] publish_flag_typo_event exception", exc_info=True)
        return None


def publish_flag_registered_event(
    flag_name: str,
    category: str,
    source_file: str,
) -> Optional[str]:
    """Best-effort publisher for flag_registered frames.

    Fires on post-boot registrations so IDE clients can refresh their
    in-memory view without polling GET /observability/flags."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_FLAG_REGISTERED, "flag_registry",
            {
                "name": flag_name,
                "category": category,
                "source_file": source_file,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Stream] publish_flag_registered_event exception", exc_info=True)
        return None


def bridge_flag_registry_to_broker(
    registry: Optional[Any] = None,
) -> Callable[[], None]:
    """Wire a FlagRegistry's post-boot ``register()`` calls into the SSE
    broker.

    Typo detection is surfaced via a separate path: callers invoke
    :func:`publish_flag_typo_event` from ``FlagRegistry.report_typos``'s
    emission loop, or via the GET
    ``/observability/flags/unregistered`` handler on-demand.

    Monkey-patches the registry's instance-level ``register`` method to
    publish a ``flag_registered`` SSE frame for each net-new
    registration (overrides of existing specs don't fire — they're
    re-registrations, not new surface).

    Returns an unsubscribe callable that restores the original method.

    Authority invariant: read-only on registry state (the bridge never
    mutates spec contents or read-tracking). Never raises into the
    register() caller path — wrapper delegates to original before any
    publish attempt, so bridge failures can't block registration.
    """
    if registry is None:
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                ensure_seeded,
            )
            registry = ensure_seeded()
        except Exception:  # noqa: BLE001
            logger.debug("[Stream] flag registry bridge: no registry",
                         exc_info=True)
            return lambda: None

    original_register = registry.register

    def _wrapped_register(spec, *, override: bool = True) -> None:
        already = registry.get_spec(spec.name) is not None
        original_register(spec, override=override)
        if not already:
            try:
                publish_flag_registered_event(
                    spec.name, spec.category.value, spec.source_file,
                )
            except Exception:  # noqa: BLE001
                logger.debug("[Stream] flag_registered publish failed",
                             exc_info=True)

    registry.register = _wrapped_register  # type: ignore[method-assign]

    def _unsubscribe() -> None:
        try:
            registry.register = original_register  # type: ignore[method-assign]
        except Exception:  # noqa: BLE001
            pass

    return _unsubscribe


# ---------------------------------------------------------------------------
# SensorGovernor + MemoryPressureGate bridges (Wave 1 #3 Slice 3)
# ---------------------------------------------------------------------------


def publish_governor_throttle_event(decision: Any) -> Optional[str]:
    """Best-effort publisher for governor_throttle_applied frames."""
    if not stream_enabled():
        return None
    try:
        payload = {
            "sensor_name": decision.sensor_name,
            "urgency": decision.urgency.value,
            "posture": decision.posture,
            "weighted_cap": decision.weighted_cap,
            "current_count": decision.current_count,
            "reason_code": decision.reason_code,
            "emergency_brake": decision.emergency_brake,
        }
        return get_default_broker().publish(
            EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED,
            decision.sensor_name, payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] publish_governor_throttle_event exception",
            exc_info=True,
        )
        return None


def publish_governor_emergency_brake_event(
    activated: bool, cost_burn: float, postmortem_rate: float,
) -> Optional[str]:
    """Best-effort publisher for governor_emergency_brake transitions."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_GOVERNOR_EMERGENCY_BRAKE, "sensor_governor",
            {
                "activated": activated,
                "cost_burn_normalized": cost_burn,
                "postmortem_failure_rate": postmortem_rate,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Stream] brake publish exception", exc_info=True)
        return None


def publish_memory_pressure_event(
    previous_level: str, current_level: str,
    free_pct: float, source: str,
) -> Optional[str]:
    """Best-effort publisher for memory_pressure_changed frames."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_MEMORY_PRESSURE_CHANGED, "memory_pressure_gate",
            {
                "previous_level": previous_level,
                "current_level": current_level,
                "free_pct": free_pct,
                "source": source,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Stream] pressure publish exception", exc_info=True)
        return None


def bridge_governor_to_broker(
    governor: Optional[Any] = None,
) -> Callable[[], None]:
    """Wrap ``governor.request_budget`` to publish throttle + brake SSE.

    Monkey-patches the instance method. Returns an unsubscribe callable."""
    if governor is None:
        try:
            from backend.core.ouroboros.governance.sensor_governor import (
                ensure_seeded,
            )
            governor = ensure_seeded()
        except Exception:  # noqa: BLE001
            return lambda: None

    original_request = governor.request_budget
    brake_state = {"active": False}

    def _wrapped_request(sensor_name, urgency=None):
        from backend.core.ouroboros.governance.sensor_governor import Urgency
        if urgency is None:
            urgency = Urgency.STANDARD
        decision = original_request(sensor_name, urgency)
        if not decision.allowed:
            try:
                publish_governor_throttle_event(decision)
            except Exception:  # noqa: BLE001
                logger.debug("[Stream] throttle publish failed", exc_info=True)
        if decision.emergency_brake != brake_state["active"]:
            brake_state["active"] = decision.emergency_brake
            try:
                cost = 0.0
                pm = 0.0
                try:
                    sb = governor._signal_bundle_fn()
                    if sb:
                        cost = float(sb.get("cost_burn_normalized", 0.0))
                        pm = float(sb.get("postmortem_failure_rate", 0.0))
                except Exception:  # noqa: BLE001
                    pass
                publish_governor_emergency_brake_event(
                    decision.emergency_brake, cost, pm,
                )
            except Exception:  # noqa: BLE001
                logger.debug("[Stream] brake publish failed", exc_info=True)
        return decision

    governor.request_budget = _wrapped_request  # type: ignore[method-assign]

    def _unsubscribe() -> None:
        try:
            governor.request_budget = original_request  # type: ignore[method-assign]
        except Exception:  # noqa: BLE001
            pass

    return _unsubscribe


def bridge_memory_pressure_to_broker(
    gate: Optional[Any] = None,
) -> Callable[[], None]:
    """Wrap ``gate.pressure`` to publish level-transition SSE frames."""
    if gate is None:
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import (
                get_default_gate,
            )
            gate = get_default_gate()
        except Exception:  # noqa: BLE001
            return lambda: None

    original_pressure = gate.pressure
    level_state = {"prev": None}

    def _wrapped_pressure():
        level = original_pressure()
        prev = level_state["prev"]
        if prev is not None and prev is not level:
            try:
                probe = gate.probe()
                publish_memory_pressure_event(
                    prev.value if prev else "unknown",
                    level.value,
                    probe.free_pct if probe else 0.0,
                    probe.source if probe else "unknown",
                )
            except Exception:  # noqa: BLE001
                logger.debug("[Stream] pressure publish failed", exc_info=True)
        level_state["prev"] = level
        return level

    gate.pressure = _wrapped_pressure  # type: ignore[method-assign]

    def _unsubscribe() -> None:
        try:
            gate.pressure = original_pressure  # type: ignore[method-assign]
        except Exception:  # noqa: BLE001
            pass

    return _unsubscribe


def publish_memory_fanout_decision_event(
    graph_id: str,
    disposition: str,
    decision: Any,
) -> Optional[str]:
    """Best-effort publisher for Slice 5 Arc B fanout gate decisions.

    Fires on every gate consultation from subagent_scheduler (not just
    clamps) so operators get a full §8 audit trail. Scheduler call rate
    is bounded by graph-execution cadence.

    ``disposition`` ∈ {``"allow"``, ``"clamp"``, ``"disabled"``,
    ``"probe_fail"``}. ``decision`` is a ``FanoutDecision`` from
    ``MemoryPressureGate.can_fanout()``.
    """
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_MEMORY_FANOUT_DECISION, graph_id,
            {
                "graph_id": graph_id,
                "disposition": disposition,
                "n_requested": decision.n_requested,
                "n_allowed": decision.n_allowed,
                "level": decision.level.value,
                "free_pct": decision.free_pct,
                "reason_code": decision.reason_code,
                "source": decision.source,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] publish_memory_fanout_decision_event exception",
            exc_info=True,
        )
        return None
