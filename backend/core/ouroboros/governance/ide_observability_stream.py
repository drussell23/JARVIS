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
    Sequence,
    Tuple,
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

# Upgrade 1 Bounded Epistemic Loop (PRD §31.2) Slice 4 vocabulary.
# Single event covering all 7 BudgetOutcome branches — payload
# carries outcome string + reason + per-op snapshot. Operators
# subscribe to one event and route on payload.outcome (matches
# the posture_changed / governor_throttle_applied pattern).
EVENT_TYPE_BUDGET_ACTION_TAKEN = "budget_action_taken"

# §37 Slice 5 (PRD §37.7 Tier 1 #1, 2026-05-05) — approaching-
# budget warning. Cost-band-crossing detector emits this event
# only on band TRANSITIONS (chatter-suppression structural via
# `cost_warning_observer.CostWarningObserver`). Payload carries
# stream_key + from_band + to_band + fraction + spent_usd +
# budget_usd. Operators consume via `/listen filter
# type=cost_band_crossed` (Slice 2 territory).
EVENT_TYPE_COST_BAND_CROSSED = "cost_band_crossed"

# §37 Slice 6 (PRD §37.7 Tier 1 #3, 2026-05-05) — PlanGenerator
# output. Emitted at PLAN-phase completion with full schema-plan.1
# JSON payload for operator consumption via `/show_plan`. Operators
# read via Slice 2 broker history (`/listen filter
# type=plan_generated`) OR the dedicated `/show_plan` verb that
# renders structured fields (approach / ordered_changes /
# risk_factors / test_strategy / complexity).
EVENT_TYPE_PLAN_GENERATED = "plan_generated"

# §37 Slice 8 (PRD §37.7 Tier 1 #6, 2026-05-05) — pre-trip
# circuit-breaker warning. Composes Slice 5's band-crossing
# discipline applied to ``CircuitBreaker._failure_count`` vs
# ``_failure_threshold``. Operators see the band ladder
# (NOTICE / WARN / CRITICAL / BREACH) BEFORE the breaker
# trips OPEN, instead of only seeing the trip event after
# the fact. Payload carries breaker_id (component identifier)
# + from_band + to_band + failure_count + threshold + ratio.
# Operators consume via `/listen filter
# type=circuit_breaker_approaching`.
EVENT_TYPE_CIRCUIT_BREAKER_APPROACHING = (
    "circuit_breaker_approaching"
)

# §37 Tier 2 #13 Slice 1 — per-tool confidence band crossing. Fires
# only on band TRANSITIONS (chatter-suppressed; mirrors Slice 5 +
# Slice 8 discipline). Payload: stream_key + op_id + tool_name +
# from_band + to_band + confidence + sample_size. Master-flag-gated
# at the producer site (JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED).
# Operators consume via `/listen filter type=tool_confidence_band_crossed`.
EVENT_TYPE_MULTI_PRIOR_DISPATCH = "multi_prior_dispatch"
"""Move 6.5 Slice 4 — fired by ``MultiPriorDispatchObserver``
on action-recommendation transitions OR whenever a dispatch
verdict carries cancelled_count > 0 / error_count > 0
(operator binding 2026-05-07 requires cancelled rolls to be
ledger-observable, not silent). Chatter-suppressed otherwise."""


EVENT_TYPE_EXECUTION_GRAPH_PROGRESS = (
    "execution_graph_progress"
)
"""Phase 3 A2 — fired by ``ExecutionGraphProgressBridge``
when the canonical ``ExecutionGraphProgressTracker`` emits a
``GraphEvent``. Chatter-suppressed by default to graph-level
events (GRAPH_SUBMITTED/STARTED/COMPLETED/FAILED/CANCELLED)
+ terminal unit-level events
(UNIT_COMPLETED/FAILED/CANCELLED). Operator binding
2026-05-07: 'read-only projection; no authority on APPLY.'
The bridge MUST NOT mutate tracker state."""


EVENT_TYPE_AUTONOMY_COMMAND_BUS = (
    "autonomy_command_bus"
)
"""Phase 3 A3 — fired by ``AutonomyCommandBusBridge`` when
``CommandBus.snapshot_all()`` aggregate metrics deltas
across all live bus instances. Chatter-suppressed: SSE only
when total_dispatched / rejected_dedup /
rejected_backpressure / by_command_type counts changed vs
last poll. Operator binding 2026-05-07:
'rate-limited, CORS/loopback same as existing observability
slices.'"""


EVENT_TYPE_FEEDBACK_ENGINE_SIGNAL = (
    "feedback_engine_signal"
)
"""Phase B4 (PRD §3.6.x, 2026-05-10) — fired by
``feedback_engine_sse_producer`` on AutonomyFeedbackEngine state
transitions. Closed 3-kind taxonomy (rollback_threshold_crossed
/ model_promoted / curriculum_batch_emitted) — string-typed in
the payload so external consumers (VS Code extension,
dashboards, audit replay) can dispatch on the kind without
needing the producer module. Chatter-suppressed at the
canonical engine site (multiples-of-threshold for rollbacks;
empty batches silent for curriculum). Producer-bridge §33.2.
Master flag ``JARVIS_FEEDBACK_ENGINE_SSE_PRODUCER_ENABLED``
default-false until Phase 9 cadence."""


EVENT_TYPE_FLAG_GRADUATED = "flag_graduated"
"""§38.11-B (PRD v2.65→v2.66, 2026-05-07) — fired by
``session_continuity.GraduationTicker`` when a flag transitions
to READY in :class:`UnifiedGraduationVerdict` (eligible for
default-true flip). Payload carries the flag name + clean
session count + transition details. Composes canonical
``unified_graduation_dashboard.aggregate_dashboard()``."""


EVENT_TYPE_INTERVENTION_BANNER_RAISED = (
    "intervention_banner_raised"
)
"""§38.11-C (PRD v2.66→v2.67, 2026-05-08) — fired by
``anticipation_surface.AnticipationSurface.record_banner``
when an autonomy sensor enqueues a proactive op intervening
in operator's flow. Payload carries the
:class:`InterventionBannerEvent.to_dict()` projection
(banner_kind / signal_source / summary / op_id /
risk_tier_label). Composes canonical NarrativeChannel INTENT
prose for human-readable rationale; module never produces
parallel prose."""


EVENT_TYPE_COHERENCE_REPORTED = "coherence_reported"
"""§3.6.2 Vector #5 closure (PRD v2.79→v2.80, 2026-05-09) —
fired by ``cross_session_harness.report_coherence`` after
each multi-session coherence aggregation. Payload carries
boundary_count + per-boundary drift records (4 axes ×
DriftLevel). Composes the 4 canonical cross-session
memory substrates: UserPreferenceStore + AdaptationLedger
+ SemanticIndex + LastSessionSummary."""


EVENT_TYPE_PHASE_ORCHESTRA_CUE = "phase_orchestra_cue"
"""§39 Tier-7 #20 (PRD v2.75→v2.76, 2026-05-09) — fired by
``phase_orchestra.emit_cue`` on every phase transition.
Payload carries phase_name + phase_index + note + intensity
+ op_id. Composes canonical pipeline_progress 11-phase
tuple. Downstream audio consumers (TUI, IDE, Karen voice)
play the actual sound; substrate is producer-only."""


EVENT_TYPE_ARCHITECTURE_SNAPSHOT = "architecture_snapshot"
"""§39 Tier-5 #5 (PRD v2.74→v2.75, 2026-05-09) — fired by
``architecture_viz.aggregate_architecture_snapshot``. Payload
carries 8-zone activity counts. Composes canonical
activity_radar."""


EVENT_TYPE_CONFIDENCE_AURA_RENDERED = (
    "confidence_aura_rendered"
)
"""§39 Tier-5 #15 — fired by ``confidence_aura.aggregate_aura``.
Bounded payload (by_tier summary + token count, not raw
text). Composes canonical ConfidenceTrace +
margin_top1_top2."""


EVENT_TYPE_ATTENTION_MIRROR_UPDATED = (
    "attention_mirror_updated"
)
"""§39 Tier-5 #16 — fired by
``attention_mirror.aggregate_attention``. Payload bounded
to primary_focus + item count + window. Composes canonical
SSE broker recent_history + narrative_channel."""


EVENT_TYPE_PORTRAIT_RENDERED = "portrait_rendered"
"""§39 Tier-5 #17 — fired by
``procedural_portrait.aggregate_portrait``. Payload carries
mode + mood + posture labels + deterministic seed. Composes
canonical polish_bundle + posture_palette."""


EVENT_TYPE_SESSION_STORY_RENDERED = "session_story_rendered"
"""§39 Tier-4 #10 (PRD v2.73→v2.74, 2026-05-09) — fired by
``session_story.aggregate_session_story`` for each session
narrative. Payload carries session_id + duration_human +
cost_human + stop_reason + beats[]. Composes canonical
LastSessionSummary."""


EVENT_TYPE_MEMORY_CRYSTALLIZATION_AGGREGATED = (
    "memory_crystallization_aggregated"
)
"""§39 Tier-4 #18 (PRD v2.73→v2.74, 2026-05-09) — fired by
``memory_crystallization.aggregate_crystal_timeline`` after
each insights.jsonl read. Payload bounded to layer summary
+ by_age counts (NOT raw crystal bodies). Composes canonical
MemoryInsight schema (4 categories) via on-disk
.jarvis/ouroboros/consciousness/insights.jsonl."""


EVENT_TYPE_TRAJECTORY_PREDICTED = "trajectory_predicted"
"""§39 Tier-3 #4 (PRD v2.72→v2.73, 2026-05-08) — fired by
``op_trajectory_predictor.predict_trajectory`` after each
prediction. Payload carries op_id + confidence + median/p90
durations + ETA. Composes canonical OpBlockBuffer history
— no parallel duration ledger."""


EVENT_TYPE_COMMAND_PREVIEW_RENDERED = (
    "command_preview_rendered"
)
"""§39 Tier-3 #19 (PRD v2.72→v2.73, 2026-05-08) — fired by
``risk_command_preview.preview_command`` for hypothetical
pre-submission classification. Payload carries predicted
route + risk-floor + verdict + cost/duration estimates.
Composes canonical UrgencyRouter classifier + risk_tier_floor
+ sensor_governor — no parallel route logic."""


EVENT_TYPE_DASHBOARD_RENDERED = "dashboard_rendered"
"""§39 Tier-2 #1 (PRD v2.71→v2.72, 2026-05-08) — fired by
``organism_dashboard.aggregate_dashboard`` after each fresh
multi-pane composition. Payload carries layout +
pane-name list + per-pane size summary (NOT the full
rendered text — bounded payload). Composes ALL 8
canonical pane render-surfaces."""


EVENT_TYPE_PHASE_FLOW_UPDATED = "phase_flow_updated"
"""§39 Tier-1 #14 (PRD v2.70→v2.71, 2026-05-08) — fired by
``phase_flow_ribbon.aggregate_phase_flow`` after each fresh
ribbon snapshot composition. Payload carries the
:class:`PhaseFlowSnapshot.to_dict()` projection (cells +
active_phase_name + by_density + window_s). Composes
canonical ``pipeline_progress.forward_flow_phases`` for
the 11-phase tuple — no parallel phase ordering."""


EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED = (
    "capability_constellation_updated"
)
"""§38.11-F (PRD v2.69→v2.70, 2026-05-08) — fired by
``capability_constellation.aggregate_constellation`` after
each fresh snapshot composition. Payload carries the
contracted §38.11.5a row 6 fields per star
(``flag_name`` / ``brightness`` / ``graduation_state`` /
``linked_principles``) + by_brightness + by_category
summary maps. Closes §39 #8 by composition."""


EVENT_TYPE_SECOND_ORDER_DOLL_PROGRESS_UPDATED = (
    "second_order_doll_progress_updated"
)
"""§40 Tier 1 #15 (PRD v2.98+, 2026-05-10) — fired by
``second_order_doll_metric.aggregate_doll_completion`` after
each fresh snapshot composition. Payload carries
``stage_counts`` + ``completion_ratio`` + per-axis
``{category, stage, autonomous_commit_count}`` projection.
RSI-acceleration probe: measures how close O+V is to safely
completing second-order self-modification (cage-modification
under operator authority). Composes canonical flag_registry
+ capability_constellation.principles_for_category +
auto_committer.ov_signature_substring — no parallel state."""


EVENT_TYPE_AUTOBIOGRAPHY_AUDIT_COMPLETED = (
    "autobiography_audit_completed"
)
"""§40 Wave 1 #8 (PRD v2.98+, 2026-05-10) — fired by
``adversarial_autobiography.audit_autobiography`` after every
fresh retrospective audit run. Payload carries ``finding`` +
``commits_audited`` + ``escape_count`` + ``clean_count`` +
``cage_health_ratio`` projection. Closes §3.6.2 Vector #7
(Quine-shape cage bypass) empirically by composing P9.4
corpus + auto_committer.ov_signature_substring + git log
walker against O+V's own commit history."""


EVENT_TYPE_ANTIVENOM_IMMUNIZATION_AUDITED = (
    "antivenom_immunization_audited"
)
"""§40 Wave 2 #7 (PRD v2.98+, 2026-05-10) — fired by
``antivenom_self_immunization.audit_self_immunization`` after
every probe loop. Payload carries ``finding`` + ``probes_run``
+ ``immunized_count`` + ``gap_count`` + ``baseline_miss_count``
+ ``coverage_ratio`` + ``per_kind_gap`` projection. Composes
P9.4 corpus + SemanticGuardian with 5 deterministic mutations
(whitespace_drift / comment_insertion / rename_variable /
string_split_concat / parens_wrap) to surface immunization
gaps where the cage's static-string AST detectors miss
semantically-equivalent variants."""


EVENT_TYPE_VOICE_TRANSCRIPT_RECORDED = (
    "voice_transcript_recorded"
)
"""§40 Wave 3 #17 (PRD v2.98+, 2026-05-10) — fired by
``conversation_bridge.record_voice_transcript`` after a voice
ASR transcript successfully lands in the ConversationBridge
ring buffer. Payload carries bounded operator-visibility fields
(``length_chars``, ``op_id``, ``confidence`` if supplied) — the
raw text NEVER appears in the SSE payload so untrusted voice
content doesn't traverse the observability stream. Surface is
purely a "voice transcript captured at T" beacon; downstream
operator tooling reads the actual content from the bridge's
``snapshot()`` via existing observability surfaces."""


EVENT_TYPE_BELIEF_REVISION_RECORDED = (
    "belief_revision_recorded"
)
"""§40 Wave 4 #9 (PRD v2.99+, 2026-05-10) — fired by
``belief_revision_ledger.record_evidence`` after one evidence
row is durably appended to the §33.4 JSONL ledger at
``.jarvis/belief_revision_ledger.jsonl``. Payload carries the
:class:`EvidenceRecord.to_dict()` projection (``claim_id`` +
``evidence_kind`` + ``source_op_id`` + ``source_session_id`` +
``observed_at_iso`` + ``observed_at_unix`` + ``note`` +
``schema_version``). Load-bearing dependency for Wave 4 #11 /
#10 / #13. Producer hook is best-effort — broker exception
NEVER raises into the calibration path."""


EVENT_TYPE_POSTMORTEM_FUSED = "postmortem_fused"
"""§40 Wave 4 #11 (PRD v2.99+, 2026-05-10) — fired by
``postmortem_fusion.fuse_recent_postmortems`` whenever the
evaluation surfaces a non-NO_PATTERN verdict (FUSED or
EMERGING). Payload carries summary metrics (``verdict`` +
``fused_count`` + ``emerging_count`` + ``postmortems_scanned``
+ ``clusters_examined`` + ``elapsed_s`` + ``schema_version``)
— the per-meta detail lives in the :class:`FusionReport`
artifact returned to the caller. Composes Wave 3 #7's
postmortem_clusterer + Wave 4 #9 belief_revision_ledger.
Producer hook is best-effort — broker exception NEVER raises."""


EVENT_TYPE_SCHELLING_TIE_BROKEN = "schelling_tie_broken"
"""§40 Wave 4 #12 (PRD v2.99+, 2026-05-10) — fired by
``schelling_consensus_prior.break_tie`` only when decision is
TIE_BROKEN (NO_TIE / NO_RECORD / DISABLED outcomes are silent
— operators get pinged only on actionable selections). Payload
carries the tie-break summary (``decision`` + ``consensus_outcome``
+ ``chosen_prior_kind`` + ``chosen_roll_id`` + ``trust_table_size``
+ ``elapsed_s`` + ``schema_version``). Composes Move 6.5
generative_quorum ConsensusVerdict read-only + the §33.4
prior-history ledger at ``.jarvis/schelling_prior_history.jsonl``.
Producer hook is best-effort — broker exception NEVER raises."""


EVENT_TYPE_SLEEP_CONSOLIDATION_PASSED = (
    "sleep_consolidation_passed"
)
"""§40 Wave 4 #10 (PRD v2.99+, 2026-05-10) — fired by
``sleep_consolidation_pass.run_consolidation_pass`` whenever
the evaluation completes with a verdict of CONSOLIDATED or
DREAMING (AWAKE / DISABLED are silent — operators get pinged
only when the substrate actually ran the pattern matcher).
Payload carries summary metrics (``verdict`` + ``idle_seconds``
+ ``idle_threshold_s`` + ``blueprints_examined`` +
``candidate_count`` + ``falsified_belief_count`` +
``fused_meta_count`` + ``elapsed_s`` + ``schema_version``).
Composes Wave 4 #9 belief_revision_ledger + Wave 4 #11
postmortem_fusion + DreamEngine (read-only via injectable
provider). Producer hook is best-effort — broker exception
NEVER raises."""


EVENT_TYPE_MIRROR_SELF_CALIBRATED = "mirror_self_calibrated"
"""§40 Wave 4 #14 (PRD v2.99+, 2026-05-10) — fired by
``mirror_self_test.compute_all_calibrations`` whenever at
least one prediction dimension reaches actionable verdict
(POOR / FAIR / GOOD — UNCALIBRATED is silent). Payload carries
a per-dimension summary (``next_phase`` / ``target_file`` /
``risk_tier`` / ``outcome`` each with their verdict + accuracy
+ sample_count). Substrate records predictions at op-start +
actuals at op-end and computes 4-axis calibration; falsified
predictions optionally bridge into Wave 4 #9 belief ledger.
Producer hook is best-effort — broker exception NEVER raises."""


EVENT_TYPE_ANTI_FRAGILITY_EVALUATED = (
    "anti_fragility_evaluated"
)
"""§40 Wave 4 #13 (PRD v2.99+, 2026-05-10) — fired by
``anti_fragility_budget.evaluate_modules`` whenever at least
one module is STRESSED or EXHAUSTED (HEALTHY-only batches are
silent — operators get pinged only on actionable stress).
Payload carries summary metrics (``module_count`` +
``healthy_count`` + ``stressed_count`` + ``exhausted_count`` +
``elapsed_s`` + ``schema_version``). Substrate composes Wave
4 #9 belief-pressure + Wave 1 #15 doll-fragility into a per-
module stress score → 4-value verdict (HEALTHY / STRESSED /
EXHAUSTED / DISABLED) + budget allowance. Producer hook is
best-effort — broker exception NEVER raises."""


EVENT_TYPE_CROSS_REPO_MIRROR_FOUND = "cross_repo_mirror_found"
"""§40 Wave 5 #20 (PRD v2.99+, 2026-05-11) — fired by
``cross_repo_causal_mirror.scan_mirror_correlations`` only on
MIRROR_FOUND verdict. TRIGGER-GATED — substrate stays inert
unless multi-remote repo OR force flag."""


EVENT_TYPE_COVERAGE_GATE_EVALUATED = (
    "coverage_gate_evaluated"
)
"""§41.4 Phase 1 sixth arc (PRD v3.0+, 2026-05-11) — fired by
``coverage_gate.evaluate_coverage`` after each assessment.
Payload carries verdict + source + overall_pct + thresholds +
matched/missing counts + boundary_crossed + elapsed_s.
Advisory only — substrate does NOT gate APPLY."""


EVENT_TYPE_LONG_HORIZON_MEMORY_RECALLED = (
    "long_horizon_memory_recalled"
)
"""§41.4 Phase 1 seventh arc (PRD v3.0+, 2026-05-11) — fired by
``long_horizon_memory.recall_memory`` after each commit-history
walk. Payload carries verdict + total_commits_scanned +
horizon_span_days + horizon_classification + theme/hot/stale
counts + composed_source_count + elapsed_s. Observational only —
substrate does NOT gate APPLY (advisory cross-session memory)."""


EVENT_TYPE_INFRA_RECOVERY_EVALUATED = (
    "infra_recovery_evaluated"
)
"""§41.4 Phase 1 eighth arc (PRD v3.0+, 2026-05-11) — fired by
``infra_recovery_loop.run_recovery_loop`` after each scan.
Payload carries verdict + auto_reclaim_enabled + check_count +
attempt_count + success_count + by_component/health/action
histograms + elapsed_s. Composes point-source recovery primitives
(posture_health task-death classifier + worktree_manager
reap_orphans) into unified periodic scanner with hard-opt-in
mutation gate per Manifesto §6."""


EVENT_TYPE_MULTI_DAY_DEADLOCK_EVALUATED = (
    "multi_day_deadlock_evaluated"
)
"""§41.4 Phase 1 ninth (final) arc (PRD v3.0+, 2026-05-11) — fired
by ``multi_day_deadlock_detector.detect_deadlocks`` after each
cross-session scan. Payload carries verdict + lookback_days +
sessions_scanned + signal_count + by_kind/severity histograms +
elapsed_s. 4 detector kinds catch patterns single-session detection
misses: REPEAT_STOP_REASON / REPEAT_FAILURE / VERDICT_THRASH /
ZERO_PROGRESS. Observational only — surfaces diagnostic signals;
operator action is NEVER autonomous."""


EVENT_TYPE_MUTATION_TESTING_EVALUATED = (
    "mutation_testing_evaluated"
)
"""§41.4 Phase 1 fifth arc (PRD v3.0+, 2026-05-11) — fired
by ``mutation_testing_harness.evaluate_file`` after each
assessment. Payload carries verdict + source_file +
total_mutants + killed/survived/timeout/error counts +
kill_ratio + boundary_crossed + elapsed_s. Advisory only —
substrate does not gate APPLY."""


EVENT_TYPE_MULTI_STEP_ORCHESTRATED = (
    "multi_step_orchestrated"
)
"""§41.4 Phase 1 fourth arc (PRD v3.0+, 2026-05-11) — fired
by ``multi_step_orchestrator.advance_orchestration`` after
each tick. Payload carries verdict + parent_goal_id +
total/blocked/ready/emitted/done/failed counts +
completion_ratio + elapsed_s + schema_version. Composes
goal_decomposition_planner + canonical envelope factory."""


EVENT_TYPE_ARCHITECTURAL_TASTE_EVALUATED = (
    "architectural_taste_evaluated"
)
"""§41.4 Phase 1 third arc (PRD v3.0+, 2026-05-11) — fired by
``architectural_taste_layer.evaluate_change`` after each
assessment. Payload carries overall_verdict + assessment_count
+ llm_enriched + elapsed_s + schema_version. Advisory only —
substrate does not gate APPLY."""


EVENT_TYPE_GOAL_DECOMPOSED = "goal_decomposed"
"""§41.4 Phase 1 second arc (PRD v3.0+, 2026-05-11) — fired
by ``goal_decomposition_planner.decompose_and_emit`` after
each decomposition pass. Payload carries verdict +
sub_goal_count + dag_depth + emitted_count + elapsed_s +
schema_version. Composes canonical
intake.intent_envelope.make_envelope — no parallel envelope
construction."""


EVENT_TYPE_ROADMAP_PROCESSED = "roadmap_processed"
"""§41.4 Phase 1 (PRD v3.0+, 2026-05-11) — fired by
``roadmap_reader.process_roadmap`` after each roadmap
processing pass. Payload carries verdict (NO_ROADMAP / VALID /
INVALID_SIGNATURE / MALFORMED) + goal_count + emitted_count +
signature_valid + elapsed_s + schema_version. Composes
canonical intake.intent_envelope.make_envelope +
UnifiedIntakeRouter.ingest — no parallel cage."""


EVENT_TYPE_WEB_BROWSING_ACTION = "web_browsing_action"
"""§41.5 Phase 0 (PRD v3.0+, 2026-05-11) — fired by
``web_browser.perform_browsing_action`` after every browsing
action (SEARCH/NAVIGATE/FOLLOW_LINK/EXTRACT_TEXT/EXTRACT_IMAGE
/CITE) regardless of verdict. Payload carries action +
verdict + host + content_bytes + redacted_bytes +
leaked_credential_kinds + backend_used + latency_ms +
op_id. Composes 5 existing surfaces (web_search /
browser_bridge / web_research_service / conversation_bridge /
mcp_output_scanner) + cross_process_jsonl — no parallel HTTP."""


EVENT_TYPE_PROOF_CARRIER_BUILT = "proof_carrier_built"
"""§40 Wave 5 #19 (PRD v2.99+, 2026-05-11) — fired by
``proof_carrier_transport.build_proof_carrier`` whenever
verdict is WARN or BLOCK (CLEAN silent). Payload carries
per-candidate evidence summary across Wave 3 #5/#6/#7 sources."""


EVENT_TYPE_COGNITIVE_LOAD_SHED_TRIGGERED = (
    "cognitive_load_shed_triggered"
)
"""§40 Wave 5 #21 (PRD v2.99+, 2026-05-11) — fired by
``cognitive_load_shedding.evaluate_cognitive_load`` whenever
verdict is ELEVATED or OVERLOADED. Payload carries load_score,
shed_kind, and stressed/exhausted counts. Composes Wave 4 #13
+ Wave 5 #18. Advisory only — consumer-side throttling stays
out of scope."""


EVENT_TYPE_PREDICTIVE_POSTMORTEM_FORECASTED = (
    "predictive_postmortem_forecasted"
)
"""§40 Wave 5 #18 (PRD v2.99+, 2026-05-11) — fired by
``predictive_postmortem.forecast_postmortem_risk`` whenever
verdict is MODERATE / HIGH / CRITICAL (LOW silent). Payload
carries forecast_score + per-component scores + dominant_factor
+ schema_version. Composes Wave 4 #9 belief drift + #11 meta
recurrence + #14 calibration decay."""


EVENT_TYPE_META_PRIOR_LEARNED = "meta_prior_learned"
"""§40 Wave 5 #22 (PRD v2.99+, 2026-05-11) — fired by
``meta_prior_learning.compute_meta_distribution`` whenever at
least one prior reaches DOMINANT or EMERGING verdict. Payload
carries per-verdict counts + elapsed_s + schema_version.
Composes Wave 4 #12 Schelling history ledger. Producer hook
is best-effort — broker exception NEVER raises."""


EVENT_TYPE_COMPOSITIONAL_CURIOSITY_EVALUATED = (
    "compositional_curiosity_evaluated"
)
"""§40 Wave 1 #16 (PRD v2.99+, 2026-05-11) — fired by
``compositional_curiosity.identify_curious_pairs`` whenever the
evaluation surfaces EMERGING or ACTIONABLE verdict
(NO_CANDIDATES / DISABLED silent — operators pinged only on
actionable signal). Payload carries summary metrics
(``verdict`` + ``pairs_examined`` + ``candidate_count`` +
``elapsed_s`` + ``schema_version``). Substrate composes
FlagRegistry inventory + Wave 1 #15 doll snapshot + per-
substrate import graph via ast.parse → per-Category pair
novelty score. Last non-experimental §40 arc — closes Wave 1.
Producer hook is best-effort — broker exception NEVER raises."""


EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED = (
    "proactive_proposal_emitted"
)
"""§38.11-E (PRD v2.68→v2.69, 2026-05-08) — fired by
``proactive_proposal_surface.ProactiveProposalLedger.record``
when one of the 4 canonical producers (curiosity / capability
gap / opportunity / architecture) emits a new proposal.
Payload carries the :class:`ProactiveProposal.to_dict()`
projection (proposal_id / kind / signal_source / summary /
rationale / priority_hint / decision). Closes §39 #11
"Capability gap proactive proposals" by composition."""


EVENT_TYPE_DREAM_EMITTED = "dream_emitted"
"""§38.11-D (PRD v2.67→v2.68, 2026-05-08) — fired by
``introspective_voice.emit_dream_prose`` when DreamEngine's
speculative-blueprint prose commits as a
:data:`NarrativeKind.DREAM` frame in the canonical
NarrativeChannel. Payload carries op_id / phase / ref /
char_count. Composes the (now 7-value) canonical
:class:`NarrativeKind` taxonomy — DREAM is the kind this
slice extends the taxonomy with."""


EVENT_TYPE_PREFETCH_SCHEDULED = "prefetch_scheduled"
"""§38.11-C (PRD v2.66→v2.67, 2026-05-08) — fired by
``anticipation_surface.AnticipationSurface.record_prefetch``
when the PLAN phase / Venom tool loop schedules a tool call
about to fire BEFORE GENERATE produces a patch. Payload
carries the :class:`PrefetchEvent.to_dict()` projection
(op_id / prefetch_kind / tool_name / arg_summary). Five
prefetch kinds: read_file / search_code / get_callers /
glob_files / other."""


EVENT_TYPE_THINKING_PROGRESS_TICK = "thinking_progress_tick"
"""§37 Phase 2 (PRD §37 v2.54→v2.55, 2026-05-07) — fired by
``ThinkingProgressObserver`` when the active-thinking
aggregator detects an effort-band OR verb-phrase crossing.
Chatter-suppressed structurally: identical re-update is
silent (no SSE fires). Payload carries the
:class:`ThinkingProgressSnapshot.to_dict()` projection
(verb_phrase / elapsed_s / tokens_input / tokens_output /
effort_band / is_active). Composes canonical
``narrative_channel`` + ``stream_renderer``."""


EVENT_TYPE_TOOL_CONFIDENCE_BAND_CROSSED = (
    "tool_confidence_band_crossed"
)

# §31 U2 Slice 3 (PRD §31.3 empirical wiring, 2026-05-05) —
# causal-decision advisory transitions. The chatter-suppressed
# observer composes :func:`causality_consumer.compute_op_causal_features`
# and emits this event ONLY on advice TRANSITIONS — same-band
# observations are silent (mirrors the cost_band_crossed +
# circuit_breaker_approaching discipline). Payload carries
# session_id + record_id + from_advice + to_advice +
# ancestor_count + sibling_count + recurrence_score. Operators
# consume via `/listen filter type=causal_advisory_emitted`.
EVENT_TYPE_CAUSAL_ADVISORY_EMITTED = "causal_advisory_emitted"

# M9 CuriosityGradient (PRD §30.5.1) Slice 4 vocabulary.
# Single event covering all CuriosityScore transitions — payload
# carries cluster_id + magnitude + dominant_source + decay_reason
# + transition_kind ("threshold_crossed" / "decay_applied" /
# "operator_reset"). Operators subscribe once and route on
# payload.transition_kind. Same pattern as posture_changed /
# budget_action_taken — no per-transition event type explosion.
EVENT_TYPE_CURIOSITY_CHANGED = "curiosity_changed"

# TrajectoryAuditor un-stranding (PRD §24.10.2 / §1 long-horizon
# semantic stability gap closure 2026-05-04) — single event for
# warning + critical drift verdicts. Payload carries verdict +
# signals tuple + snapshot_hash + ts_unix + reason
# ("boot" / "periodic"). Stable + growing transitions stay
# silent (chatter suppression by construction).
EVENT_TYPE_TRAJECTORY_DRIFT_DETECTED = "trajectory_drift_detected"

# Upgrade 2 DecisionRecord Causality Graph (PRD §31.3) Slice 4
# vocabulary. Single event covering all 4 actionable
# ReplayDriftKind values (NONE is silent — chatter suppression).
# Payload carries session_id + record_index + drift_kind
# (string from closed enum) + record_id + expected_hash +
# actual_hash + detail (bounded) + ts_unix. Operators subscribe
# once and route on payload.drift_kind.
EVENT_TYPE_DECISION_DRIFT_DETECTED = "decision_drift_detected"

# M10 ArchitectureProposer (PRD §32.4) Slice 5 vocabulary.
# Fired by lifecycle.advance() at every phase transition into
# AWAITING_APPROVAL / PUSH_FAILED / FAILED / DECIDED_SKIP /
# REJECTED / EXPIRED / GRADUATED. Single event — operators
# route on payload.terminal_phase. NOT fired at intermediate
# phases (VALIDATING / COMMITTING / etc.) — chatter suppression.
EVENT_TYPE_M10_PROPOSAL_EMITTED = "m10_proposal_emitted"

# Move 7 — Cross-op Semantic Budget (PRD §29.4) Slice 3 vocabulary.
# Fired by `CrossOpSemanticBudgetObserver` at verdict-ladder
# TRANSITIONS only (chatter-suppressed; same-verdict ticks are
# silent). Payload carries verdict + prev_verdict + integrated_drift
# + threshold + approaching_band + centroids_seen + ts_unix.
# Operators subscribe once and route on payload.verdict.
EVENT_TYPE_SEMANTIC_BUDGET_CHANGED = "semantic_budget_changed"

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

# Gap #4 Slice 4 — IDE-native diff review stream vocabulary.
# Fired by ReviewCoordinator at every transition into a terminal
# ReviewState (or at PENDING when the branch is freshly created).
# Operators subscribe once and route on payload.state. The VS Code
# extension's openPendingReview command consumes ``review_branch_created``
# to surface a notification with a "Review in IDE" button (Slice 5).
EVENT_TYPE_REVIEW_BRANCH_CREATED = "review_branch_created"
EVENT_TYPE_REVIEW_BRANCH_ACCEPTED = "review_branch_accepted"
EVENT_TYPE_REVIEW_BRANCH_REJECTED = "review_branch_rejected"
EVENT_TYPE_REVIEW_BRANCH_EXPIRED = "review_branch_expired"
EVENT_TYPE_SESSION_BOOKMARKED = "session_bookmarked"
EVENT_TYPE_SESSION_UNBOOKMARKED = "session_unbookmarked"
EVENT_TYPE_SESSION_PINNED = "session_pinned"
EVENT_TYPE_SESSION_UNPINNED = "session_unpinned"

# PlanFalsificationDetector Slice 5 — structural plan-step falsification
# verdict (advisory). Fired by the orchestrator bridge after every
# bridge_to_replan() call so observability sees both REPLAN_TRIGGERED
# (preempts legacy DynamicRePlanner) and the silent paths (no
# falsification, insufficient evidence, disabled, failed). Read-only;
# no authority surface.
EVENT_TYPE_PLAN_FALSIFICATION_VERDICT = "plan_falsification_verdict"

# SkillRegistry-AutonomousReach Slice 5 — observer fire decision.
# SkillObserver publishes one frame per (skill, signal) evaluation
# so operators see both FIRED + every skip reason (decision /
# rate_limit_exhausted / dedup_hit / invoker_raised). Read-only;
# no authority surface.
EVENT_TYPE_SKILL_INVOKED = "skill_invoked"

# ClusterIntelligence-CrossSession Slice 5 — DomainMap entry persisted
# from a successful cluster_coverage exploration. Fired by the cascade
# observer's record path. Read-only; no authority surface. Operators
# subscribe to track which clusters O+V is building cross-session
# memory for + how the file-set + exploration_count evolve over time.
EVENT_TYPE_DOMAIN_MAP_UPDATED = "domain_map_updated"

# AutoCommitterIgnoreGuard Slice 3 — gitignore breach blocked. Fired
# by AutoCommitter when Layer 1 (pre-stage) refuses ignored paths OR
# Layer 2 (post-stage validator) catches a breach that slipped past
# Layer 1 and aborts the commit. Read-only; no authority surface.
# Operators subscribe to monitor the AutoCommitter sovereignty
# boundary live.
EVENT_TYPE_AUTO_COMMITTER_IGNORED_BLOCKED = (
    "auto_committer_ignored_blocked"
)

# ClusterIntelligence-CrossSession empirical-closure addendum —
# semantic embedder degraded from fastembed (primary) to the pure-
# stdlib hashing fallback. Fired once per process, when _AdaptiveEmbedder
# detects fastembed cannot serve embeddings (offline / sandbox / model
# download failure / runtime error). Operators subscribe to know that
# the SemanticIndex is running on the lower-quality fallback and may
# want to repair the fastembed install.
EVENT_TYPE_SEMANTIC_EMBEDDER_FALLBACK = "semantic_embedder_fallback"

# MissionInferrer Slice C — fired by GoalInferenceEngine.build() on
# cache miss (when signal extraction + clustering actually re-runs).
# Carries the lightweight projection of the new InferenceResult so
# operators see hypotheses change in real time without polling the
# /observability/goal-inference GET. Read-only; no authority surface.
EVENT_TYPE_GOAL_INFERENCE_BUILT = "goal_inference_built"

# Production Oracle (Tier 2 #6) Slice D — fired on every observer tick
# with the lightweight aggregate verdict + adapter counts. Read-only;
# advisory; never mutates Iron Gate / risk / route. Operators
# subscribe to track production-health drift in real time without
# polling the GET endpoint.
EVENT_TYPE_PRODUCTION_ORACLE_SIGNAL = "production_oracle_signal_observed"

# Tier 2 #6 follow-up Arc 1 (2026-05-03) — fired by the orchestrator
# VERIFY hook when auto_action_router proposes a non-NO_ACTION
# AdvisoryAction. Read-only; advisory; the proposal is logged + may
# be reviewed via /auto-action REPL but is NEVER auto-applied while
# JARVIS_AUTO_ACTION_ENFORCE=false (the only state the arc ships in).
EVENT_TYPE_AUTO_ACTION_PROPOSAL = "auto_action_proposal"

# Stream-validator constants (2026-05-03) — these were defined as
# raw string literals in their respective publisher modules and got
# silently dropped by the stream broker because the validator's
# frozenset didn't know them. Lifting them to constants HERE keeps
# the publisher modules canonical (no duplication of the literal)
# while making the stream module the single source of truth for
# "valid event types". Cross-module publishers (Move 4 InvariantDrift
# Auditor, Coherence Auditor, auto_action_router emit phase) reference
# these constants now.
EVENT_TYPE_INVARIANT_DRIFT_DETECTED = "invariant_drift_detected"
EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED = "behavioral_drift_detected"
EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED = "auto_action_proposal_emitted"


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

# Priority #5 Slice 4 — Continuous Invariant Gradient Watcher
# observability. Two event types fire from gradient_observer:
#   * REPORT_RECORDED — per-report SSE: one event per recorded
#     GradientReport (after compute_gradient_outcome). Payload:
#     {outcome, total_samples, breach_count, drift_count,
#     readings_count, tightening, cluster_kind, schema_version}.
#   * BASELINE_UPDATED — per-aggregation SSE: fires when periodic
#     observer recomputes the gradient-effectiveness baseline and
#     the CIGWEffectivenessOutcome changed (or every Nth pass for
#     liveness). Payload: {outcome, total_reports, actionable_count,
#     stable_count, drifting_count, breached_count, total_breaches,
#     stable_rate, drift_rate, breach_rate, baseline_quality,
#     tightening, schema_version}.
# Both are PURE OBSERVABILITY — no authority surface. Cost-contract
# preserved by AST-pinned construction (observer reads cached
# artifacts only).
EVENT_TYPE_CIGW_REPORT_RECORDED = "cigw_report_recorded"
EVENT_TYPE_CIGW_BASELINE_UPDATED = "cigw_baseline_updated"

# ----------------------------------------------------------------------
# Upgrade 3 Slice 5 — Failure-Mode Memory at first-attempt GENERATE
# (PRD §31.4). One event per matched-and-injected recurrence: fires
# from strategic_direction's render method when the retriever returns
# >=1 match and the section is composed into the prompt. Authority-
# free observability (the model already saw the section before the
# event fires; SSE is for operator visibility, not control flow).
# ----------------------------------------------------------------------
EVENT_TYPE_FAILURE_MODE_RECALLED_AT_GENERATE = (
    "failure_mode_recalled_at_generate"
)

# ----------------------------------------------------------------------
# M11 Slice 4 — ActionOutcomeMemory recall at first-attempt GENERATE
# (PRD §30.5.3). Symmetric positive-evidence pair to the Upgrade 3
# event above. Fires from strategic_direction's render method when
# the retriever returns >=1 match and the section is composed into
# the prompt. Authority-free observability — the model already saw
# the section before the event fires; SSE is for operator visibility,
# not control flow.
# ----------------------------------------------------------------------
EVENT_TYPE_ACTION_OUTCOME_RECALLED_AT_GENERATE = (
    "action_outcome_recalled_at_generate"
)

# ----------------------------------------------------------------------
# Slice 0 (P3/P4 graduation observability) — fires from
# strategic_direction's render methods when the advisory dev-memory /
# rust-map section is non-empty AND injected into the GENERATE prompt.
# Counts-only payload (no titles / summaries / URIs — operator
# memory/ may be sensitive). Authority-free: the model already saw
# the section; SSE is operator visibility, not control flow. Makes
# graduation grep-provable from session debug.log + the existing
# /observability stream without dumping the prompt body.
# ----------------------------------------------------------------------
EVENT_TYPE_STRATEGIC_DEV_MEMORY_INJECTED = (
    "strategic_dev_memory_injected"
)
EVENT_TYPE_STRATEGIC_RUST_MAP_INJECTED = (
    "strategic_rust_map_injected"
)

# ----------------------------------------------------------------------
# Deep Observability Gap #2 Slice 4 — Confidence-policy write surface.
# ----------------------------------------------------------------------
#
# IDE-driven operator proposals to tighten the ConfidenceMonitor
# threshold knobs. Every event correlates to one AdaptationProposal
# in the AdaptationLedger; the proposal_id is the op_id field.
#
#   * PROPOSED — fires when ide_policy_router accepts a POST and
#     AdaptationLedger.propose returns OK.
#   * APPROVED — fires on operator approval (ledger.approve).
#   * REJECTED — fires on operator rejection (ledger.reject).
#   * APPLIED  — fires after the YAML writer materializes the
#     approved proposal into .jarvis/adapted_confidence_thresholds.yaml
#     (Slice 4 emits PROPOSED / APPROVED / REJECTED; Slice 5 wires
#     APPLIED to the YAML writer hook).
EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED = "confidence_policy_proposed"
EVENT_TYPE_CONFIDENCE_POLICY_APPROVED = "confidence_policy_approved"
EVENT_TYPE_CONFIDENCE_POLICY_REJECTED = "confidence_policy_rejected"
EVENT_TYPE_CONFIDENCE_POLICY_APPLIED = "confidence_policy_applied"

# ----------------------------------------------------------------------
# Deep Observability Gap #3 Slice 3 — L3 worktree topology stream.
# ----------------------------------------------------------------------
#
# IDE-driven view of the SubagentScheduler's in-memory DAG. Two
# event types translated 1:1 from the autonomy EventEmitter:
#
#   * TOPOLOGY_UPDATED — fires on every graph-level state change
#     (CREATED → RUNNING → COMPLETED / FAILED / CANCELLED). Payload:
#     {graph_id, phase, ready_units, running_units, completed_units,
#      failed_units, cancelled_units, last_error}. The ``op_id``
#     field of the SSE frame carries the agent's op_id so IDE
#     consumers can correlate with task tree.
#
#   * UNIT_STATE_CHANGED — fires when a single work unit transitions
#     state (PENDING → RUNNING, RUNNING → COMPLETED/FAILED/CANCELLED).
#     Payload: {graph_id, unit_id, repo, status, barrier_id,
#      owned_paths, (optional) failure_class, error, runtime_ms,
#      causal_parent_id}. The frame's op_id carries the agent op_id.
#
# Bridge implementation lives in
# ``verification.worktree_topology_sse_bridge`` — a pure
# translator (autonomy → broker), zero modifications to the
# scheduler. Default-off behind
# ``JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED``.
EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED = "worktree_topology_updated"
EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED = "worktree_unit_state_changed"

# Q4 Priority #2 Slice 4 — closure-loop autonomous-tightening proposal
# emission. Fires when the bridge submits a PROPOSED-status row to
# AdaptationLedger.propose; carries advisory_id + parameter_name +
# proposal_id + record_fingerprint so consumers can correlate to
# /observability/closure-loop/{history,pending} GETs and to
# /adapt show <proposal_id>.
EVENT_TYPE_CLOSURE_LOOP_PROPOSAL_EMITTED = "closure_loop_proposal_emitted"

# TerminationHookRegistry Slice 4 — fires when the registry
# dispatches a phase. Carries the phase + cause + outcome
# histogram + per-hook records so IDE consumers can render a
# "session terminating" notification with what hooks fired
# (the partial-summary-writer adapter's outcome is the most
# operationally relevant signal). Most consumers will see this
# event at session end (PRE_SHUTDOWN_EVENT_SET dispatch fires
# from signal handlers / wall-clock watchdog).
EVENT_TYPE_TERMINATION_HOOK_DISPATCHED = "termination_hook_dispatched"

# AdmissionGate Slice 3 — fires when CandidateGenerator
# evaluates the admission gate and the decision is a SHED outcome
# (SHED_BUDGET_INSUFFICIENT or SHED_QUEUE_DEEP). Carries the full
# AdmissionRecord projection so IDE consumers can render
# "saturation event" notifications + correlate to the structural
# `pre_admission_shed` exhaustion at the orchestrator level.
EVENT_TYPE_ADMISSION_DECISION_EMITTED = "admission_decision_emitted"
# CodebaseCharacterDigest Slice 3 — emitted when ProactiveExploration
# Sensor surfaces an under-touched semantic cluster as an IntentEnvelope.
# Lets IDE/observability consumers correlate the cluster-coverage bias
# firing to the resulting GENERATE op (via subsequent task lifecycle
# events sharing the same op_id once intake assigns one).
EVENT_TYPE_CODEBASE_CHARACTER_INJECTED = "codebase_character_injected"

# §37 Tier 1 #2 (PRD §37.7, 2026-05-09 v2.84) — PostureObserver
# task-death detection. Fires when posture_health classifies the
# observer as DEGRADED_HUNG / DEGRADED_FAILING / TASK_DEAD.
# Debounced cross-process via posture_health._maybe_publish_*.
# Operators consume via `/listen filter type=
# posture_observer_degraded` OR `/posture health` REPL.
EVENT_TYPE_POSTURE_OBSERVER_DEGRADED = "posture_observer_degraded"

# Venom V2 Slice 3 (PRD v2.91, 2026-05-10) — fires every time
# permission_decision_archive.maybe_record_decision archives a
# Venom-V2 per-tool permission decision. Payload carries the
# canonical AggregatePermissionDecision projection (decision /
# tool_name / detail / deny_callbacks / ask_callbacks /
# total_callbacks) plus the archive ref (``p-N``) so IDE
# consumers can correlate the SSE event to a /expand p-N
# retrieval. Best-effort: master-flag-gated at the archive's
# producer (JARVIS_PERMISSION_ARCHIVE_ENABLED) — when off, the
# record() short-circuits before publish. Stream-side gate
# (JARVIS_IDE_STREAM_ENABLED) still applies via publish_task_event.
EVENT_TYPE_PERMISSION_DECISION_RECORDED = (
    "permission_decision_recorded"
)

# §41.3 #26 Phase 2 Slice 3 — fast-path Q&A artifact-record beacon.
# Fires once per parked QAArtifact (every q-N ring insertion).
# Closes the observability-triad parity gap: every other artifact-
# ring substrate (tool_render, diff_archive, op_block_buffer,
# narrative_channel, permission_decision_archive) already publishes
# an SSE event on record; Q&A was the last ring without one.
# Best-effort: master-flag-gated at the substrate producer
# (JARVIS_FAST_PATH_QA_ENABLED) — when off, ask_question short-
# circuits at the master gate so the producer hook never fires.
# Stream-side gate (JARVIS_IDE_STREAM_ENABLED) still applies via
# publish_task_event. Payload is QAArtifact.to_dict() (already
# bounded — question[:1024], answer projected as char-count only
# to avoid surfacing operator content over the IDE stream).
EVENT_TYPE_QA_RECORDED = "qa_recorded"

# §32.4/§40.1 Slice 3 — M10 cadence runner phase-transition
# beacon. Fires when sweep_pending_for_merge or
# expire_stale_pending transitions a proposal-store row
# (AWAITING_APPROVAL → GRADUATED on PR merge, AWAITING_APPROVAL
# → EXPIRED on timeout, etc.). Operator-initiated only (Slice 3);
# autonomous orchestrator wiring is deferred per Layer-changing-
# event discipline. Master-flag gated at producer side
# (JARVIS_M10_ARCH_PROPOSER_ENABLED + JARVIS_M10_CADENCE_ENABLED).
EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED = "m10_proposal_phase_changed"

# Treefinement Phase 4 — branch + layer lifecycle events. Producers
# (in repair_tree_archive.maybe_archive_tree_result + the runner)
# fire best-effort; broker exception NEVER raises into the runner
# path. Stream-side gate (JARVIS_IDE_STREAM_ENABLED) applies via
# publish_task_event. Substrate-side gate is the in-memory ring
# master flag (JARVIS_L2_TREE_ARCHIVE_ENABLED) — when off, the
# producer-bridge short-circuits before publish.
EVENT_TYPE_REPAIR_BRANCH_PROMOTED = "repair_branch_promoted"
EVENT_TYPE_REPAIR_BRANCH_PRUNED = "repair_branch_pruned"
EVENT_TYPE_REPAIR_LAYER_COMPLETED = "repair_layer_completed"
EVENT_TYPE_REPAIR_TREE_WON = "repair_tree_won"

# --- Operation FSM lifecycle (B.2.0.5 — distinct from task_* TaskBoard) ---
#
# The orchestrator's operation-FSM terminals (COMPLETE / CANCELLED /
# POSTMORTEM) are architecturally distinct from the TaskBoard
# ``task_completed`` / ``task_cancelled`` events, which scope to the
# tool-call board concept owned by ``task_tool.py``. IDE clients,
# the SWE-Bench-Pro evaluator (PRD §40.7.9 Phase B.2.2), and any
# future operation-watching consumer subscribe to this event to
# learn when an op reached a terminal state.
#
# Payload schema (closed; documented in PRD §40.7.10-b205):
#   * op_id:                str       — OperationContext.op_id
#   * phase:                str       — OperationPhase value
#                                       (one of COMPLETE / CANCELLED /
#                                       POSTMORTEM, or the intermediate
#                                       phase the op died in)
#   * state:                str       — OperationState value (one of
#                                       applied / rolled_back / failed
#                                       / blocked)
#   * terminal_reason_code: str       — ctx.terminal_reason_code (may be "")
#   * phase_entered_at:     str       — ISO8601 from ctx.phase_entered_at
#   * timestamp:            str       — ISO8601 wall-clock at publish
EVENT_TYPE_OPERATION_TERMINAL = "operation_terminal"

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
    EVENT_TYPE_CIGW_REPORT_RECORDED,             # Priority #5 Slice 4
    EVENT_TYPE_CIGW_BASELINE_UPDATED,            # Priority #5 Slice 4
    EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED,        # Gap #2 Slice 4
    EVENT_TYPE_CONFIDENCE_POLICY_APPROVED,        # Gap #2 Slice 4
    EVENT_TYPE_CONFIDENCE_POLICY_REJECTED,        # Gap #2 Slice 4
    EVENT_TYPE_CONFIDENCE_POLICY_APPLIED,         # Gap #2 Slice 4
    EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED,         # Gap #3 Slice 3
    EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED,       # Gap #3 Slice 3
    EVENT_TYPE_CLOSURE_LOOP_PROPOSAL_EMITTED,     # Q4 P#2 Slice 4
    EVENT_TYPE_TERMINATION_HOOK_DISPATCHED,       # TermHook Slice 4
    EVENT_TYPE_ADMISSION_DECISION_EMITTED,        # AdmissionGate Slice 3
    EVENT_TYPE_CODEBASE_CHARACTER_INJECTED,       # CodebaseCharDigest Slice 3
    EVENT_TYPE_POSTURE_OBSERVER_DEGRADED,         # §37 Tier 1 #2 (v2.84)
    EVENT_TYPE_AUTO_ACTION_PROPOSAL,              # auto_action_router (Move 3)
    EVENT_TYPE_PRODUCTION_ORACLE_SIGNAL,          # Production Oracle (Tier 2 #6)
    EVENT_TYPE_SEMANTIC_EMBEDDER_FALLBACK,        # SemanticIndex stdlib fallback
    EVENT_TYPE_GOAL_INFERENCE_BUILT,              # MissionInferrer (Tier 2 #5)
    EVENT_TYPE_INVARIANT_DRIFT_DETECTED,          # Move 4 InvariantDriftAuditor
    EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED,         # Coherence Auditor (Priority #1)
    EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED,      # auto_action_router emit phase
    EVENT_TYPE_FAILURE_MODE_RECALLED_AT_GENERATE,  # Upgrade 3 Slice 5 (PRD §31.4)
    EVENT_TYPE_ACTION_OUTCOME_RECALLED_AT_GENERATE,  # M11 Slice 4 (PRD §30.5.3)
    EVENT_TYPE_STRATEGIC_DEV_MEMORY_INJECTED,      # Slice 0 (P3 graduation obs)
    EVENT_TYPE_STRATEGIC_RUST_MAP_INJECTED,        # Slice 0 (P4 graduation obs)
    EVENT_TYPE_COST_BAND_CROSSED,                # §37 Slice 5 (PRD §37.7 Tier 1 #1)
    EVENT_TYPE_PLAN_GENERATED,                   # §37 Slice 6 (PRD §37.7 Tier 1 #3)
    EVENT_TYPE_CIRCUIT_BREAKER_APPROACHING,      # §37 Slice 8 (PRD §37.7 Tier 1 #6)
    EVENT_TYPE_CAUSAL_ADVISORY_EMITTED,          # §31 U2 Slice 3 (PRD §31.3 empirical wiring)
    EVENT_TYPE_TOOL_CONFIDENCE_BAND_CROSSED,     # §37 Tier 2 #13 Slice 1 (per-tool confidence)
    EVENT_TYPE_MULTI_PRIOR_DISPATCH,             # Move 6.5 Slice 4 (multi-prior dispatch observer)
    EVENT_TYPE_EXECUTION_GRAPH_PROGRESS,         # Phase 3 A2 (read-only projection of canonical tracker)
    EVENT_TYPE_AUTONOMY_COMMAND_BUS,             # Phase 3 A3 (read-only polling of CommandBus.snapshot_all)
    EVENT_TYPE_THINKING_PROGRESS_TICK,           # §37 Phase 2 (active-thinking aggregator — chatter-suppressed band/verb crossings)
    EVENT_TYPE_FEEDBACK_ENGINE_SIGNAL,           # Phase B4 (AutonomyFeedbackEngine producer-bridge)
    EVENT_TYPE_FLAG_GRADUATED,                   # §38.11-B (graduation ticker — flag READY transitions)
    EVENT_TYPE_INTERVENTION_BANNER_RAISED,       # §38.11-C (proactive intervention banner)
    EVENT_TYPE_PREFETCH_SCHEDULED,               # §38.11-C (anticipatory pre-fetch indicator)
    EVENT_TYPE_DREAM_EMITTED,                    # §38.11-D (DreamEngine DREAM-kind narrative frame committed)
    EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED,       # §38.11-E (proactive proposal surface ledger entry)
    EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED, # §38.11-F (capability constellation snapshot refresh)
    EVENT_TYPE_PHASE_FLOW_UPDATED,               # §39 Tier-1 #14 (phase-flow ribbon snapshot)
    EVENT_TYPE_DASHBOARD_RENDERED,               # §39 Tier-2 #1 (organism dashboard multi-pane snapshot)
    EVENT_TYPE_TRAJECTORY_PREDICTED,             # §39 Tier-3 #4 (op trajectory prediction)
    EVENT_TYPE_COMMAND_PREVIEW_RENDERED,         # §39 Tier-3 #19 (pre-submission risk preview)
    EVENT_TYPE_SESSION_STORY_RENDERED,           # §39 Tier-4 #10 (operator's-eye session story)
    EVENT_TYPE_MEMORY_CRYSTALLIZATION_AGGREGATED, # §39 Tier-4 #18 (memory crystallization timeline)
    EVENT_TYPE_ARCHITECTURE_SNAPSHOT,            # §39 Tier-5 #5 (8-zone organism viz)
    EVENT_TYPE_CONFIDENCE_AURA_RENDERED,         # §39 Tier-5 #15 (per-token confidence aura)
    EVENT_TYPE_ATTENTION_MIRROR_UPDATED,         # §39 Tier-5 #16 (attention focus snapshot)
    EVENT_TYPE_PORTRAIT_RENDERED,                # §39 Tier-5 #17 (procedural ASCII portrait)
    EVENT_TYPE_PHASE_ORCHESTRA_CUE,              # §39 Tier-7 #20 (phase orchestra audio cue)
    EVENT_TYPE_COHERENCE_REPORTED,               # §3.6.2 Vector #5 (cross-session coherence harness)
    EVENT_TYPE_PERMISSION_DECISION_RECORDED,     # Venom V2 Slice 3 (PRD v2.91, permission_decision_archive)
    EVENT_TYPE_SECOND_ORDER_DOLL_PROGRESS_UPDATED, # §40 Tier 1 #15 (PRD v2.98+, second-order doll completion metric)
    EVENT_TYPE_AUTOBIOGRAPHY_AUDIT_COMPLETED,    # §40 Wave 1 #8 (PRD v2.98+, adversarial autobiography retrospective audit)
    EVENT_TYPE_ANTIVENOM_IMMUNIZATION_AUDITED,   # §40 Wave 2 #7 (PRD v2.98+, antivenom self-immunization probe loop)
    EVENT_TYPE_VOICE_TRANSCRIPT_RECORDED,        # §40 Wave 3 #17 (PRD v2.98+, voice transcript bounded beacon)
    EVENT_TYPE_BELIEF_REVISION_RECORDED,         # §40 Wave 4 #9 (PRD v2.99+, belief revision ledger evidence row)
    EVENT_TYPE_POSTMORTEM_FUSED,                 # §40 Wave 4 #11 (PRD v2.99+, postmortem fusion meta-postmortem)
    EVENT_TYPE_SCHELLING_TIE_BROKEN,             # §40 Wave 4 #12 (PRD v2.99+, schelling-point tie-break)
    EVENT_TYPE_SLEEP_CONSOLIDATION_PASSED,       # §40 Wave 4 #10 (PRD v2.99+, sleep consolidation pass)
    EVENT_TYPE_MIRROR_SELF_CALIBRATED,           # §40 Wave 4 #14 (PRD v2.99+, mirror-self calibration)
    EVENT_TYPE_ANTI_FRAGILITY_EVALUATED,         # §40 Wave 4 #13 (PRD v2.99+, anti-fragility budget)
    EVENT_TYPE_COMPOSITIONAL_CURIOSITY_EVALUATED, # §40 Wave 1 #16 (PRD v2.99+, compositional curiosity)
    EVENT_TYPE_META_PRIOR_LEARNED,               # §40 Wave 5 #22 (PRD v2.99+, meta-prior learning)
    EVENT_TYPE_PREDICTIVE_POSTMORTEM_FORECASTED, # §40 Wave 5 #18 (PRD v2.99+, predictive postmortem)
    EVENT_TYPE_COGNITIVE_LOAD_SHED_TRIGGERED,    # §40 Wave 5 #21 (PRD v2.99+, cognitive load shedding)
    EVENT_TYPE_PROOF_CARRIER_BUILT,              # §40 Wave 5 #19 (PRD v2.99+, proof carrier)
    EVENT_TYPE_CROSS_REPO_MIRROR_FOUND,          # §40 Wave 5 #20 (PRD v2.99+, cross-repo mirror)
    EVENT_TYPE_WEB_BROWSING_ACTION,              # §41.5 Phase 0 (PRD v3.0+, web_browser composer)
    EVENT_TYPE_ROADMAP_PROCESSED,                # §41.4 Phase 1 (PRD v3.0+, roadmap_reader)
    EVENT_TYPE_GOAL_DECOMPOSED,                  # §41.4 Phase 1 second arc (PRD v3.0+, goal_decomposition_planner)
    EVENT_TYPE_ARCHITECTURAL_TASTE_EVALUATED,    # §41.4 Phase 1 third arc (PRD v3.0+, architectural_taste_layer)
    EVENT_TYPE_MULTI_STEP_ORCHESTRATED,          # §41.4 Phase 1 fourth arc (PRD v3.0+, multi_step_orchestrator)
    EVENT_TYPE_MUTATION_TESTING_EVALUATED,       # §41.4 Phase 1 fifth arc (PRD v3.0+, mutation_testing_harness)
    EVENT_TYPE_COVERAGE_GATE_EVALUATED,          # §41.4 Phase 1 sixth arc (PRD v3.0+, coverage_gate)
    EVENT_TYPE_LONG_HORIZON_MEMORY_RECALLED,     # §41.4 Phase 1 seventh arc (PRD v3.0+, long_horizon_memory)
    EVENT_TYPE_INFRA_RECOVERY_EVALUATED,         # §41.4 Phase 1 eighth arc (PRD v3.0+, infra_recovery_loop)
    EVENT_TYPE_MULTI_DAY_DEADLOCK_EVALUATED,     # §41.4 Phase 1 ninth (final) arc (PRD v3.0+, multi_day_deadlock_detector)
    EVENT_TYPE_QA_RECORDED,                      # §41.3 #26 Phase 2 Slice 3 (PRD v3.x, fast_path_qa)
    EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED,        # §32.4/§40.1 Slice 3 (M10 cadence runner)
    EVENT_TYPE_REPAIR_BRANCH_PROMOTED,           # Treefinement Phase 4 (repair_tree_archive)
    EVENT_TYPE_REPAIR_BRANCH_PRUNED,             # Treefinement Phase 4 (repair_tree_archive)
    EVENT_TYPE_REPAIR_LAYER_COMPLETED,           # Treefinement Phase 4 (repair_tree_archive)
    EVENT_TYPE_REPAIR_TREE_WON,                  # Treefinement Phase 4 (repair_tree_archive)
    EVENT_TYPE_OPERATION_TERMINAL,               # B.2.0.5 (v3.7 — operation FSM terminal event;
                                                  # distinct from task_* TaskBoard events; consumed by
                                                  # SWE-Bench-Pro Phase B.2.2 evaluator + IDE clients
                                                  # tracking full-op lifecycle, not just tool-call boards)
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

    # --- read-side helpers for operator surfaces ------------------------
    # PRD §37 Slice 2 (2026-05-05) — ``/listen`` REPL composes these.
    # All return fresh lists/tuples (caller mutations don't leak back
    # into broker state). Lock-protected snapshot reads.

    def recent_history(
        self,
        *,
        limit: int = 20,
        event_type: Optional[str] = None,
        op_id: Optional[str] = None,
    ) -> List["StreamEvent"]:
        """Return the most-recent ``limit`` events from the bounded
        history ring, optionally filtered by ``event_type`` and/or
        ``op_id``. Returns events in chronological order (oldest
        first within the requested window).

        Defensive: caller mutations of the returned list do not
        leak into broker state. Lock-protected snapshot. NEVER
        raises.

        Used by the ``/listen`` operator surface to render event
        history for forensic + state-reconstruction needs.
        """
        try:
            limit_clamped = max(1, min(int(limit), self._history_maxlen))
        except (TypeError, ValueError):
            limit_clamped = 20
        with self._lock:
            # Snapshot the deque to a list under the lock so no
            # concurrent publish modifies it during iteration.
            snapshot = list(self._history)
        # Apply filters in chronological order (deque order).
        filtered: List["StreamEvent"] = []
        for ev in snapshot:
            if event_type is not None and ev.event_type != event_type:
                continue
            if op_id is not None and ev.op_id != op_id:
                continue
            filtered.append(ev)
        # Take the last `limit_clamped` entries (most-recent within
        # the filter; chronological ordering preserved).
        return filtered[-limit_clamped:]

    def distinct_event_types(self) -> List[str]:
        """Return the alphabetically-sorted list of distinct event
        types currently present in history. Cheap; uses set
        comprehension. NEVER raises."""
        with self._lock:
            return sorted({ev.event_type for ev in self._history})

    def distinct_op_ids(self, *, limit: int = 50) -> List[str]:
        """Return up to ``limit`` distinct op_ids most-recent-first
        within history. Caps at ``limit`` to prevent operator
        wall-of-text rendering when the ring is full."""
        try:
            limit_clamped = max(1, int(limit))
        except (TypeError, ValueError):
            limit_clamped = 50
        with self._lock:
            seen: Dict[str, None] = {}
            for ev in reversed(self._history):
                if ev.op_id and ev.op_id not in seen:
                    seen[ev.op_id] = None
                    if len(seen) >= limit_clamped:
                        break
        return list(seen.keys())

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


# ---------------------------------------------------------------------------
# Operation-FSM lifecycle publish (B.2.0.5 — distinct from TaskBoard)
# ---------------------------------------------------------------------------
#
# This is the canonical bridge from orchestrator FSM terminal transitions
# to the SSE broker. Composed at ``orchestrator._record_ledger`` AFTER a
# successful ``ledger.append()`` (idempotency rides on the ledger's existing
# (op_id, state) dedup key — append returns False on duplicate, the
# publish call is gated by that True return). Never raises into the
# caller; never blocks the ledger append.
#
# Closed taxonomies (AST-pinned by spine):
#   * TERMINAL_OPERATION_STATES — the four ledger states that
#     correspond to a "the op is done one way or another" terminal:
#     applied / rolled_back / failed / blocked. Intermediate state
#     records (sandboxing, gating, applying, validating, ...) do NOT
#     publish.
#
# Master switch ``JARVIS_OP_LIFECYCLE_SSE_ENABLED`` (§33.1 default-FALSE).
# When OFF, the publish is a no-op and the broker sees zero
# operation_terminal events — byte-identical to pre-B.2.0.5 behavior.

OP_LIFECYCLE_SSE_ENABLED_ENV_VAR: str = "JARVIS_OP_LIFECYCLE_SSE_ENABLED"

# The four ledger states that flag "this operation has reached a
# terminal outcome". Closed taxonomy — AST-pinned. Mirrors
# ``backend.core.ouroboros.governance.ledger.OperationState`` values
# verbatim; not imported to keep this module substrate-independent
# (intake → governance.ledger is a deeper dep than this observability
# module should hold).
TERMINAL_OPERATION_STATES: frozenset = frozenset({
    "applied",
    "rolled_back",
    "failed",
    "blocked",
})


def op_lifecycle_sse_enabled() -> bool:
    """Master flag query (§33.1 default-FALSE)."""
    raw = os.environ.get(OP_LIFECYCLE_SSE_ENABLED_ENV_VAR, "")
    return raw.strip().lower() in ("true", "1", "yes", "on")


def publish_operation_terminal(ctx: Any, state: Any) -> Optional[str]:
    """Publish an ``operation_terminal`` event for a terminal op transition.

    Called by ``orchestrator._record_ledger`` AFTER a successful
    ``ledger.append()`` returned True. Idempotency rides on the ledger's
    existing (op_id, state) dedup key. Best-effort + bounded payload +
    NEVER raises — operator binding for B.2.0.5: ``never raise into
    _record_ledger``.

    Parameters
    ----------
    ctx:
        :class:`OperationContext` (or duck-typed equivalent with
        ``op_id``, ``phase``, ``phase_entered_at``,
        ``terminal_reason_code``). The Any type keeps this module
        independent of op_context to avoid an
        observability → state-machine dep cycle.
    state:
        :class:`OperationState` (or any object exposing ``.value`` as
        a string). When ``.value`` is not in
        :data:`TERMINAL_OPERATION_STATES`, this is a no-op and the
        caller's intermediate-state recording proceeds without an
        SSE side effect.

    Returns
    -------
    Optional[str]
        Event id on successful publish; ``None`` when the master flag
        is off, the state is not terminal, the stream is disabled, or
        any internal failure occurred.
    """
    if not op_lifecycle_sse_enabled():
        return None
    try:
        state_value = getattr(state, "value", None)
        if not isinstance(state_value, str):
            return None
        if state_value not in TERMINAL_OPERATION_STATES:
            return None
        op_id = getattr(ctx, "op_id", None)
        if not isinstance(op_id, str) or not op_id:
            return None
        phase_obj = getattr(ctx, "phase", None)
        phase_value = getattr(phase_obj, "name", None)
        if not isinstance(phase_value, str):
            phase_value = ""
        terminal_reason_code = getattr(ctx, "terminal_reason_code", "") or ""
        phase_entered_at = getattr(ctx, "phase_entered_at", None)
        try:
            phase_entered_iso = (
                phase_entered_at.isoformat()
                if phase_entered_at is not None else ""
            )
        except Exception:  # noqa: BLE001
            phase_entered_iso = ""
        payload: Dict[str, Any] = {
            "op_id": op_id,
            "phase": phase_value,
            "state": state_value,
            "terminal_reason_code": str(terminal_reason_code)[:256],
            "phase_entered_at": phase_entered_iso,
            "timestamp": _iso_now(),
        }
        return publish_task_event(
            EVENT_TYPE_OPERATION_TERMINAL, op_id, payload,
        )
    except Exception:  # noqa: BLE001 — strict fail-silent per operator binding
        logger.debug(
            "[Stream] publish_operation_terminal exception",
            exc_info=True,
        )
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


def publish_plan_falsification_verdict(
    *,
    op_id: str,
    outcome: str,
    falsified_step_index: Optional[int] = None,
    falsifying_evidence_kinds: Tuple[str, ...] = (),
    contradicting_detail: str = "",
    total_hypotheses: int = 0,
    total_evidence: int = 0,
    monotonic_tightening_verdict: str = "",
    prompt_injected: bool = False,
) -> Optional[str]:
    """Best-effort publisher for plan_falsification_verdict frames.

    Returns the event_id on success, None when stream is disabled
    / broker missing / publish raised. Never raises. Fired by the
    orchestrator bridge on every bridge_to_replan() call so
    operators see both REPLAN_TRIGGERED (preempts the legacy
    DynamicRePlanner regex backstop) and silent paths
    (NO_FALSIFICATION / INSUFFICIENT_EVIDENCE / DISABLED / FAILED).

    Read-only payload — no authority surface."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_PLAN_FALSIFICATION_VERDICT, op_id,
            {
                "outcome": str(outcome),
                "falsified_step_index": falsified_step_index,
                "falsifying_evidence_kinds": list(
                    falsifying_evidence_kinds
                ),
                "contradicting_detail": str(
                    contradicting_detail or "",
                )[:500],
                "total_hypotheses": int(total_hypotheses),
                "total_evidence": int(total_evidence),
                "monotonic_tightening_verdict": str(
                    monotonic_tightening_verdict or "",
                ),
                "prompt_injected": bool(prompt_injected),
            },
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug(
            "[Stream] publish_plan_falsification_verdict exception",
            exc_info=True,
        )
        return None


def publish_skill_invocation(
    *,
    qualified_name: str,
    triggered_by_kind: str,
    triggered_by_signal: str,
    outcome: str,
    spec_index: Optional[int] = None,
    fired: bool = False,
    skip_reason: str = "",
    invocation_ok: Optional[bool] = None,
    invocation_duration_ms: Optional[float] = None,
    decided_at_monotonic: float = 0.0,
) -> Optional[str]:
    """Best-effort publisher for skill_invoked frames.

    Returns the event_id on success, None when stream is disabled
    / broker missing / publish raised. NEVER raises. Fired by
    SkillObserver on every fire-or-skip evaluation so operators
    see the full lifecycle (FIRED + every skip reason from the
    closed-4 SKIP_REASON vocabulary -- decision /
    rate_limit_exhausted / dedup_hit / invoker_raised).

    Read-only payload -- no authority surface."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_SKILL_INVOKED, qualified_name or "unknown",
            {
                "qualified_name": str(qualified_name),
                "triggered_by_kind": str(triggered_by_kind),
                "triggered_by_signal": str(
                    triggered_by_signal or "",
                )[:200],
                "outcome": str(outcome),
                "spec_index": spec_index,
                "fired": bool(fired),
                "skip_reason": str(skip_reason or "")[:200],
                "invocation_ok": invocation_ok,
                "invocation_duration_ms": invocation_duration_ms,
                "decided_at_monotonic": float(
                    decided_at_monotonic or 0.0,
                ),
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.debug(
            "[Stream] publish_skill_invocation exception",
            exc_info=True,
        )
        return None


def publish_domain_map_update(
    *,
    centroid_hash8: str,
    cluster_id: int = -1,
    theme_label: str = "",
    discovered_files_count: int = 0,
    architectural_role: str = "",
    confidence: float = 0.0,
    exploration_count: int = 0,
    populated_by_op_id: str = "",
) -> Optional[str]:
    """Best-effort publisher for ``domain_map_updated`` frames.

    Returns the event_id on success, None when stream is disabled
    / broker missing / publish raised. NEVER raises. Fired by
    the cluster-exploration cascade observer's record path so
    operators see which clusters O+V is building cross-session
    memory for.

    Read-only payload -- no authority surface. Carries counts
    not file lists (the full file-set lives in the DomainMap
    JSON entry on disk; this is the lightweight notification)."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_DOMAIN_MAP_UPDATED,
            centroid_hash8 or "unknown",
            {
                "centroid_hash8": str(centroid_hash8),
                "cluster_id": int(cluster_id),
                "theme_label": str(theme_label or "")[:200],
                "discovered_files_count": int(discovered_files_count),
                "architectural_role": str(
                    architectural_role or "",
                )[:500],
                "confidence": float(confidence),
                "exploration_count": int(exploration_count),
                "populated_by_op_id": str(
                    populated_by_op_id or "",
                ),
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.debug(
            "[Stream] publish_domain_map_update exception",
            exc_info=True,
        )
        return None


def publish_auto_action_proposal(
    *,
    op_id: str,
    action_type: str,
    reason_code: str,
    target_op_family: str = "",
    proposed_risk_tier: str = "",
    evidence: str = "",
) -> Optional[str]:
    """Best-effort publisher for ``auto_action_proposal`` frames.
    Returns the event_id on success, None when stream disabled /
    broker missing / publish raised. NEVER raises. Fired by the
    orchestrator VERIFY hook when a non-NO_ACTION proposal lands."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_AUTO_ACTION_PROPOSAL,
            (op_id or "unknown")[:80],
            {
                "op_id": str(op_id or "")[:80],
                "action_type": str(action_type or "")[:60],
                "reason_code": str(reason_code or "")[:80],
                "target_op_family": str(target_op_family or "")[:80],
                "proposed_risk_tier": (
                    str(proposed_risk_tier or "")[:40]
                ),
                "evidence": str(evidence or "")[:300],
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.debug(
            "[Stream] publish_auto_action_proposal exception",
            exc_info=True,
        )
        return None


def publish_production_oracle_signal(
    *,
    aggregate_verdict: str,
    signal_count: int = 0,
    adapters_queried: int = 0,
    adapters_failed: int = 0,
    tick_duration_ms: int = 0,
    posture: str = "",
) -> Optional[str]:
    """Best-effort publisher for ``production_oracle_signal_observed``
    frames. Returns the event_id on success, None when the stream is
    disabled / broker missing / publish raised. NEVER raises.

    Read-only payload. Carries the aggregate verdict + counts only
    (not raw signal payloads -- those are visible via the GET route's
    history projection)."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_PRODUCTION_ORACLE_SIGNAL,
            (aggregate_verdict or "unknown")[:40],
            {
                "aggregate_verdict": str(aggregate_verdict or "")[:40],
                "signal_count": int(signal_count),
                "adapters_queried": int(adapters_queried),
                "adapters_failed": int(adapters_failed),
                "tick_duration_ms": int(tick_duration_ms),
                "posture": str(posture or "")[:40],
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.debug(
            "[Stream] publish_production_oracle_signal exception",
            exc_info=True,
        )
        return None


def publish_goal_inference_built(
    *,
    built_at: float,
    build_ms: int,
    total_samples: int,
    hypotheses_count: int,
    top_theme: str = "",
    top_confidence: float = 0.0,
    sources_contributing: int = 0,
    build_reason: str = "",
) -> Optional[str]:
    """Best-effort publisher for ``goal_inference_built`` frames.

    Returns the event_id on success, None when stream is disabled /
    broker missing / publish raised. NEVER raises. Fired by
    ``GoalInferenceEngine.build()`` only on cache miss (the actual
    rebuild path), not on every cache-hit ``build()`` invocation.

    Read-only payload -- no authority surface. Carries the lightweight
    projection (counts + top theme) so operators can subscribe and
    correlate inferred-direction shifts to behavior changes without
    polling the GET endpoint or reading raw evidence."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_GOAL_INFERENCE_BUILT,
            (top_theme or "no-theme")[:100],
            {
                "built_at": float(built_at),
                "build_ms": int(build_ms),
                "total_samples": int(total_samples),
                "hypotheses_count": int(hypotheses_count),
                "top_theme": str(top_theme or "")[:200],
                "top_confidence": float(top_confidence),
                "sources_contributing": int(sources_contributing),
                "build_reason": str(build_reason or "")[:50],
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.debug(
            "[Stream] publish_goal_inference_built exception",
            exc_info=True,
        )
        return None


def publish_semantic_embedder_fallback(
    *,
    primary_model: str,
    fallback_model: str,
    fallback_dim: int = 0,
) -> Optional[str]:
    """Best-effort publisher for ``semantic_embedder_fallback`` frames.

    Returns the event_id on success, None when stream is disabled /
    broker missing / publish raised. NEVER raises. Fired once per
    process by ``_AdaptiveEmbedder`` when fastembed first fails and
    the embedder permanently swaps to the stdlib fallback.

    Read-only payload -- no authority surface. Carries model names +
    dimensionality only; no corpus content, no embeddings, no tokens
    (the cluster substrate's content lives in ``IndexStats`` projected
    via the ``GET /observability/codebase-character`` route)."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_SEMANTIC_EMBEDDER_FALLBACK,
            (primary_model or "unknown")[:200],
            {
                "primary_model": str(primary_model or "")[:200],
                "fallback_model": str(fallback_model or "")[:200],
                "fallback_dim": int(fallback_dim),
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.debug(
            "[Stream] publish_semantic_embedder_fallback exception",
            exc_info=True,
        )
        return None


def publish_auto_committer_ignored_blocked(
    *,
    op_id: str,
    layer: str,
    blocked_paths: Tuple[str, ...] = (),
    skipped_count: int = 0,
    aborted: bool = False,
) -> Optional[str]:
    """Best-effort publisher for ``auto_committer_ignored_blocked``
    frames. Fired by AutoCommitter when Layer 1 (pre-stage)
    refuses ignored paths OR Layer 2 (post-stage validator)
    catches a breach + aborts the commit.

    ``layer`` ∈ ``{"layer1_prestage", "layer2_validator"}``.
    ``aborted`` is True only for Layer 2 catches (Layer 1 is
    silent skip + commit may still succeed for clean inputs).

    Read-only payload -- no authority surface. NEVER raises."""
    if not stream_enabled():
        return None
    try:
        return get_default_broker().publish(
            EVENT_TYPE_AUTO_COMMITTER_IGNORED_BLOCKED,
            op_id or "unknown",
            {
                "op_id": str(op_id),
                "layer": str(layer),
                "blocked_paths": list(blocked_paths)[:32],
                "skipped_count": int(skipped_count),
                "aborted": bool(aborted),
            },
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.debug(
            "[Stream] publish_auto_committer_ignored_blocked "
            "exception", exc_info=True,
        )
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


# ---------------------------------------------------------------------------
# Upgrade 1 Bounded Epistemic Loop (PRD §31.2) Slice 4 publisher
# ---------------------------------------------------------------------------


def publish_semantic_budget_event(
    *,
    verdict: str,
    prev_verdict: str,
    integrated_drift: float,
    threshold: float,
    approaching_band: float,
    centroids_seen: int,
    ts_unix: float,
) -> Optional[str]:
    """Best-effort publisher for ``semantic_budget_changed``
    frames (PRD §29.4 Move 7 Slice 3, 2026-05-05). Fired by
    :class:`CrossOpSemanticBudgetObserver` at verdict-ladder
    transitions only — chatter-suppressed (same-verdict ticks
    are silent).

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {
            "verdict": verdict or "",
            "prev_verdict": prev_verdict or "",
            "integrated_drift": float(integrated_drift),
            "threshold": float(threshold),
            "approaching_band": float(approaching_band),
            "centroids_seen": int(centroids_seen),
            "ts_unix": float(ts_unix),
        }
        return get_default_broker().publish(
            EVENT_TYPE_SEMANTIC_BUDGET_CHANGED,
            "semantic_budget",
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] semantic_budget_changed publish "
            "exception", exc_info=True,
        )
        return None


def publish_m10_proposal_event(
    *,
    proposal_id: str,
    kind: str,
    terminal_phase: str,
    pr_url: str = "",
    pr_branch: str = "",
    failure_reason: str = "",
    cost_usd: float = 0.0,
    ts_unix: float,
) -> Optional[str]:
    """Best-effort publisher for ``m10_proposal_emitted``
    frames (PRD §32.4 Slice 5). Fired at every terminal-or-
    awaiting phase transition by
    :class:`ProposalLifecycleOrchestrator.advance`.

    Single event covers all terminal+awaiting phases; operators
    route on ``payload.terminal_phase``. Chatter-suppressed
    intermediate phases (VALIDATING / COMMITTING / PUSHING)
    do NOT publish.

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {
            "proposal_id": proposal_id or "",
            "kind": kind or "",
            "terminal_phase": terminal_phase or "",
            "pr_url": pr_url or "",
            "pr_branch": pr_branch or "",
            "failure_reason": failure_reason or "",
            "cost_usd": float(cost_usd),
            "ts_unix": float(ts_unix),
        }
        return get_default_broker().publish(
            EVENT_TYPE_M10_PROPOSAL_EMITTED,
            proposal_id or "m10_proposal",
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] m10_proposal_emitted publish exception",
            exc_info=True,
        )
        return None


def publish_decision_drift_event(
    *,
    session_id: str,
    record_index: int,
    drift_kind: str,
    record_id: str,
    expected: str,
    actual: str,
    detail: str,
    ts_unix: float,
) -> Optional[str]:
    """Best-effort publisher for ``decision_drift_detected``
    frames. Fired by :func:`replay_session_consistency` per
    detected drift entry (PRD §31.3 Slice 4).

    Single event for all 4 actionable :class:`ReplayDriftKind`
    values — operators subscribe once and route on
    ``payload.drift_kind``. Pattern matches
    :func:`publish_trajectory_drift_event` /
    :func:`publish_curiosity_event` /
    :func:`publish_budget_action_event`.

    Bounded payload — ``expected`` / ``actual`` / ``detail``
    are pre-truncated by :meth:`ReplayDriftReport.to_dict` to
    keep SSE frames small (256-char cap per field). NEVER
    raises; returns the published event id or None on master-
    flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {
            "session_id": session_id or "",
            "record_index": int(record_index),
            "drift_kind": drift_kind,
            "record_id": record_id or "",
            "expected": expected or "",
            "actual": actual or "",
            "detail": detail or "",
            "ts_unix": float(ts_unix),
        }
        return get_default_broker().publish(
            EVENT_TYPE_DECISION_DRIFT_DETECTED,
            session_id or "decision_drift",
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] decision drift event publish exception",
            exc_info=True,
        )
        return None


def publish_trajectory_drift_event(
    *,
    verdict: str,
    signals: Tuple[Dict[str, Any], ...],
    snapshot_hash: str,
    ts_unix: float,
    reason: str,
) -> Optional[str]:
    """Best-effort publisher for ``trajectory_drift_detected``
    frames. Fired by :class:`TrajectoryAuditorObserver` on
    warning / critical drift verdicts.

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {
            "verdict": verdict,
            "signals": list(signals or ()),
            "snapshot_hash": snapshot_hash,
            "ts_unix": float(ts_unix),
            "reason": reason,
            "signal_count": len(signals or ()),
        }
        return get_default_broker().publish(
            EVENT_TYPE_TRAJECTORY_DRIFT_DETECTED,
            "trajectory_auditor",
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] trajectory drift event publish exception",
            exc_info=True,
        )
        return None


def publish_autonomy_command_bus_event(
    *,
    instance_count: int,
    total_qsize: int,
    total_dispatched: int,
    total_rejected_dedup: int,
    total_rejected_backpressure: int,
    by_command_type: Optional[Dict[str, int]] = None,
    delta: Optional[Dict[str, int]] = None,
    ts_unix: float,
) -> Optional[str]:
    """Best-effort publisher for ``autonomy_command_bus``
    frames (Phase 3 A3, 2026-05-07). Fired by
    ``AutonomyCommandBusBridge`` ONLY when polled metrics
    show a delta vs last poll (chatter-suppressed otherwise).

    Operator binding 2026-05-07: 'rate-limited, CORS/loopback
    same as existing observability slices.' The bridge polls
    canonical ``CommandBus.snapshot_all()``; this publisher
    is the single SSE projection surface — direct
    ``broker.publish`` forbidden in the bridge.

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        body: Dict[str, Any] = {
            "instance_count": int(instance_count),
            "total_qsize": int(total_qsize),
            "total_dispatched": int(total_dispatched),
            "total_rejected_dedup": int(
                total_rejected_dedup,
            ),
            "total_rejected_backpressure": int(
                total_rejected_backpressure,
            ),
            "by_command_type": dict(
                by_command_type or {},
            ),
            "delta": dict(delta or {}),
            "ts_unix": float(ts_unix),
        }
        return get_default_broker().publish(
            EVENT_TYPE_AUTONOMY_COMMAND_BUS,
            "autonomy_command_bus",
            body,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] autonomy_command_bus publish "
            "exception", exc_info=True,
        )
        return None


def publish_feedback_engine_signal_event(
    *,
    transition_kind: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Best-effort publisher for ``feedback_engine_signal``
    frames (Phase B4, 2026-05-10). Fired by
    ``feedback_engine_sse_producer`` on three closed transition
    kinds: ``rollback_threshold_crossed`` /
    ``model_promoted`` / ``curriculum_batch_emitted``.

    The producer is the single SSE projection surface for
    AutonomyFeedbackEngine state — direct broker.publish from
    the engine itself is forbidden (authority asymmetry).
    Chatter-suppression is enforced at the engine site (no
    same-state re-emission).

    NEVER raises; returns the published event id or ``None`` on
    master-flag-off / publish failure / empty transition_kind."""
    if not stream_enabled():
        return None
    try:
        kind = str(transition_kind or "").strip()
        if not kind:
            return None
        body: Dict[str, Any] = {
            "transition_kind": kind,
            "payload": dict(payload or {}),
        }
        return get_default_broker().publish(
            EVENT_TYPE_FEEDBACK_ENGINE_SIGNAL,
            "feedback_engine_signal",
            body,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] feedback_engine_signal publish "
            "exception", exc_info=True,
        )
        return None


def publish_execution_graph_progress_event(
    *,
    kind: str,
    graph_id: str,
    op_id: str,
    unit_id: str,
    ts_ns: int,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Best-effort publisher for ``execution_graph_progress``
    frames (Phase 3 A2, 2026-05-07). Fired by
    ``ExecutionGraphProgressBridge`` on canonical tracker
    events that pass the chatter-suppression filter
    (graph-level always; unit-level terminal-only by default).

    Operator binding 2026-05-07: 'read-only projection; no
    authority on APPLY.' The bridge MUST NOT mutate tracker
    state — this publisher is the canonical SSE surface for
    operator-facing visibility.

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        body: Dict[str, Any] = {
            "kind": kind or "",
            "graph_id": graph_id or "",
            "op_id": op_id or "",
            "unit_id": unit_id or "",
            "ts_ns": int(ts_ns),
            "payload": dict(payload or {}),
        }
        return get_default_broker().publish(
            EVENT_TYPE_EXECUTION_GRAPH_PROGRESS,
            "execution_graph_progress",
            body,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] execution_graph_progress publish "
            "exception", exc_info=True,
        )
        return None


def publish_multi_prior_dispatch_event(
    *,
    op_id: str,
    decision: str,
    action_recommendation: str,
    prev_action_recommendation: str,
    consensus_outcome: str,
    completed_count: int,
    cancelled_count: int,
    timeout_count: int,
    error_count: int,
    cost_total_usd: float,
    wall_clock_s: float,
    ts_unix: float,
) -> Optional[str]:
    """Best-effort publisher for ``multi_prior_dispatch``
    frames (Move 6.5 Slice 4, 2026-05-07). Fired by
    ``MultiPriorDispatchObserver`` on action-recommendation
    transitions OR whenever a dispatch verdict carries
    ``cancelled_count > 0`` / ``error_count > 0`` (operator
    binding 2026-05-07: cancelled rolls MUST be ledger-
    observable, not silent). Same-action ticks with no
    cancellations / errors are chatter-suppressed.

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {
            "op_id": op_id or "",
            "decision": decision or "",
            "action_recommendation": (
                action_recommendation or ""
            ),
            "prev_action_recommendation": (
                prev_action_recommendation or ""
            ),
            "consensus_outcome": consensus_outcome or "",
            "completed_count": int(completed_count),
            "cancelled_count": int(cancelled_count),
            "timeout_count": int(timeout_count),
            "error_count": int(error_count),
            "cost_total_usd": float(cost_total_usd),
            "wall_clock_s": float(wall_clock_s),
            "ts_unix": float(ts_unix),
        }
        return get_default_broker().publish(
            EVENT_TYPE_MULTI_PRIOR_DISPATCH,
            "multi_prior_dispatch",
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] multi_prior_dispatch publish exception",
            exc_info=True,
        )
        return None


def publish_curiosity_event(
    *,
    cluster_id: str,
    transition_kind: str,
    magnitude: float,
    confidence: float,
    dominant_source: str,
    decay_reason: str,
    samples_count: int,
    extra_telemetry: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Best-effort publisher for ``curiosity_changed`` frames.

    Single event for all CuriosityScore transitions —
    operators subscribe once and route on
    ``payload.transition_kind``:

      * ``threshold_crossed`` — score magnitude crossed a
        consumer-relevant boundary (e.g., 0.5 — neutral pivot)
      * ``decay_applied`` — STALE_FOCUS / RECURRENCE_LOOP
        auto-decay engaged
      * ``operator_reset`` — ``/curiosity reset <id>``
        operator-explicit decay
      * ``samples_milestone`` — cluster crossed
        :func:`curiosity_min_samples` and exited cold-start

    Pattern matches :func:`publish_posture_event` /
    :func:`publish_budget_action_event` — single event, payload
    routes.

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {
            "cluster_id": cluster_id or "",
            "transition_kind": transition_kind,
            "magnitude": float(magnitude),
            "confidence": float(confidence),
            "dominant_source": dominant_source,
            "decay_reason": decay_reason,
            "samples_count": int(samples_count),
        }
        if extra_telemetry:
            payload["telemetry"] = dict(extra_telemetry)
        return get_default_broker().publish(
            EVENT_TYPE_CURIOSITY_CHANGED,
            cluster_id or "_global",
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] curiosity event publish exception",
            exc_info=True,
        )
        return None


def publish_budget_action_event(
    *,
    outcome: str,
    reason: str,
    op_id: str,
    budget_snapshot: Optional[Dict[str, Any]] = None,
    new_risk_tier: Optional[str] = None,
    extra_telemetry: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Best-effort publisher for ``budget_action_taken`` frames.

    Single event for all 7 BudgetOutcome branches — operators
    subscribe once and route on ``payload.outcome``. Pattern
    matches :func:`publish_governor_throttle_event` /
    :func:`publish_posture_event`.

    NEVER raises; returns the published event id or None on
    master-flag-off / publish failure."""
    if not stream_enabled():
        return None
    try:
        payload: Dict[str, Any] = {
            "outcome": outcome,
            "reason": reason,
            "op_id": op_id or "",
        }
        if budget_snapshot is not None:
            payload["budget"] = budget_snapshot
        if new_risk_tier is not None:
            payload["new_risk_tier"] = new_risk_tier
        if extra_telemetry:
            payload["telemetry"] = dict(extra_telemetry)
        return get_default_broker().publish(
            EVENT_TYPE_BUDGET_ACTION_TAKEN,
            op_id or "epistemic_budget",
            payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] budget action publish exception",
            exc_info=True,
        )
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


# ---------------------------------------------------------------------------
# Gap #4 Slice 4 — review-branch lifecycle SSE publisher
# ---------------------------------------------------------------------------


_REVIEW_STATE_TO_EVENT_TYPE: Mapping[str, str] = {
    "pending": EVENT_TYPE_REVIEW_BRANCH_CREATED,
    "accepted": EVENT_TYPE_REVIEW_BRANCH_ACCEPTED,
    "rejected": EVENT_TYPE_REVIEW_BRANCH_REJECTED,
    "expired": EVENT_TYPE_REVIEW_BRANCH_EXPIRED,
}


def publish_review_branch_event(
    state: str,
    op_id: str,
    *,
    branch_name: Optional[str] = None,
    archive_ref: Optional[str] = None,
    file_paths: Optional[Sequence[str]] = None,
    risk_tier: Optional[str] = None,
    base_sha: Optional[str] = None,
    tip_sha: Optional[str] = None,
    error: str = "",
) -> Optional[str]:
    """Best-effort publisher for ``review_branch_*`` SSE frames.

    Maps ReviewState string → event type via :data:`_REVIEW_STATE_TO_EVENT_TYPE`.
    Returns the event_id on success or ``None`` if the stream is
    disabled / state is unknown / publish raised. NEVER raises.

    Slice 5's VS Code extension subscribes to all four event types
    and surfaces:
      * ``review_branch_created`` → notification with "Review in IDE" button
      * ``review_branch_accepted`` → status bar tick + auto-dismiss
      * ``review_branch_rejected`` → status bar warning + auto-dismiss
      * ``review_branch_expired`` → status bar warning + 10s sticky
    """
    if not stream_enabled():
        return None
    event_type = _REVIEW_STATE_TO_EVENT_TYPE.get(state)
    if event_type is None:
        return None
    try:
        payload: Dict[str, Any] = {
            "state": state,
            "op_id": op_id,
        }
        if branch_name:
            payload["branch_name"] = branch_name
        if archive_ref:
            payload["archive_ref"] = archive_ref
        if file_paths:
            payload["file_paths"] = list(file_paths)[:32]
        if risk_tier:
            payload["risk_tier"] = risk_tier
        if base_sha:
            payload["base_sha"] = base_sha
        if tip_sha:
            payload["tip_sha"] = tip_sha
        if error:
            payload["error"] = error[:200]
        return get_default_broker().publish(event_type, op_id, payload)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[Stream] publish_review_branch_event exception",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration for B.2.0.5
    operation-FSM lifecycle SSE master flag. Returns count successfully
    registered. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=OP_LIFECYCLE_SSE_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "B.2.0.5 master switch (§33.1 default-FALSE): when ON, "
                "the orchestrator emits an ``operation_terminal`` SSE "
                "event after every successful terminal ledger.append() "
                "(states: applied / rolled_back / failed / blocked). "
                "Distinct from task_* TaskBoard events — this fans out "
                "the orchestrator FSM's terminal state, consumed by the "
                "SWE-Bench-Pro Phase B.2.2 evaluator, IDE extensions "
                "tracking full-op lifecycle, and any future operation-"
                "watching consumer. Best-effort + bounded payload + "
                "never raises into _record_ledger."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "ide_observability_stream.py"
            ),
            example="false",
            since="v3.7 Phase 2 Phase B.2.0.5 (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Stream] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count
