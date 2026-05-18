"""Seed registrations for the FlagRegistry — ~50 curated flags.

Each flag is hand-registered with:
  * Correct :class:`FlagType` + default
  * One-sentence description
  * Category from the fixed 8-slot taxonomy
  * Source file where the flag is consumed
  * Example value
  * ``since`` version tag
  * Optional posture relevance for the ``/help posture`` filter

Organizing principle: if a flag is mentioned in ``CLAUDE.md``'s Key
Subsystems section, it belongs here. Flags below that threshold (deep
tuning knobs, experimental not-yet-mentioned) can be added in Slice 5
via a codebase-wide audit script.

Authority invariant: this module imports :class:`Category` /
:class:`FlagType` / :class:`Relevance` / :class:`FlagSpec` from
``flag_registry`` only — zero authority imports. Grep-pinned at Slice 4.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
    Relevance,
)


# Re-used per-posture relevance dicts — kept DRY
_EXPLORE_CRITICAL = {"EXPLORE": Relevance.CRITICAL}
_HARDEN_CRITICAL = {"HARDEN": Relevance.CRITICAL}
_CONSOLIDATE_CRITICAL = {"CONSOLIDATE": Relevance.CRITICAL}
_ALL_POSTURES_CRITICAL = {
    "EXPLORE": Relevance.CRITICAL,
    "CONSOLIDATE": Relevance.CRITICAL,
    "HARDEN": Relevance.CRITICAL,
    "MAINTAIN": Relevance.CRITICAL,
}
_HARDEN_AND_CONSOLIDATE = {
    "HARDEN": Relevance.CRITICAL,
    "CONSOLIDATE": Relevance.RELEVANT,
}


SEED_SPECS: list = [
    # ====================================================================
    # DirectionInferrer / StrategicPosture (Wave 1 #1) — 9 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_DIRECTION_INFERRER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the DirectionInferrer arc. When "
            "false, all four posture surfaces (prompt injection, "
            "/posture REPL, GET /observability/posture, SSE "
            "posture_changed) revert in lockstep."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/direction_inferrer.py",
        example="true",
        since="v1.0",
        posture_relevance=_ALL_POSTURES_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_POSTURE_PROMPT_INJECTION_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the ## Current Strategic Posture section in "
            "CONTEXT_EXPANSION prompt composition. Master must also "
            "be on."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/posture_prompt.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_POSTURE_OBSERVER_INTERVAL_S",
        type=FlagType.INT, default=300,
        description="Seconds between PostureObserver signal-collection cycles.",
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/posture_observer.py",
        example="300",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_POSTURE_HYSTERESIS_WINDOW_S",
        type=FlagType.INT, default=900,
        description=(
            "Minimum seconds between posture flips unless confidence "
            "exceeds the high-confidence bypass threshold."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/posture_observer.py",
        example="900",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS",
        type=FlagType.FLOAT, default=0.75,
        description=(
            "Confidence threshold above which posture flips bypass "
            "the hysteresis window."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/posture_observer.py",
        example="0.75",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_POSTURE_CONFIDENCE_FLOOR",
        type=FlagType.FLOAT, default=0.35,
        description=(
            "Readings below this confidence are demoted to MAINTAIN."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/direction_inferrer.py",
        example="0.35",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_POSTURE_OVERRIDE_MAX_H",
        type=FlagType.INT, default=24,
        description=(
            "Hours maximum any /posture override can be active. Operator "
            "requests beyond this are clamped with a warning."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/posture_observer.py",
        example="24",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_POSTURE_HISTORY_SIZE",
        type=FlagType.INT, default=256,
        description=(
            "Ring-buffer capacity for .jarvis/posture_history.jsonl. "
            "Reader surfaces (/posture history, "
            "GET /observability/posture/history) clamp limit to [1, 256]."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/posture_store.py",
        example="256",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_POSTURE_WEIGHTS_OVERRIDE",
        type=FlagType.JSON, default=None,
        description=(
            "Optional JSON hot-swap of the 12x4 DEFAULT_WEIGHTS table "
            "for A/B testing posture inference. Unknown signals / "
            "postures are silently dropped."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/direction_inferrer.py",
        example='{"feat_ratio":{"EXPLORE":1.0}}',
        since="v1.0",
    ),

    # ====================================================================
    # IDE observability — 5 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_IDE_OBSERVABILITY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for read-only GET /observability/* "
            "endpoints (tasks, plans, sessions, posture, flags)."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/ide_observability.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_IDE_STREAM_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the SSE /observability/stream "
            "surface. Event vocabulary includes posture_changed, "
            "plan_*, task_*, flag_typo_detected, heartbeat, stream_lag."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/ide_observability_stream.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN",
        type=FlagType.INT, default=120,
        description=(
            "Per-client sliding-window rate cap on GET "
            "/observability/* endpoints. Default 120 allows 2/sec "
            "steady polling from one IDE client."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/ide_observability.py",
        example="120",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_IDE_STREAM_RATE_LIMIT_PER_MIN",
        type=FlagType.INT, default=10,
        description=(
            "Per-client cap on SSE subscribe attempts per minute. Lower "
            "than GET cap because subscribes are long-lived connections."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/ide_observability_stream.py",
        example="10",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_IDE_OBSERVABILITY_CORS_ORIGINS",
        type=FlagType.STR,
        default=(
            r"^https?://localhost(:\d+)?$,"
            r"^https?://127\.0\.0\.1(:\d+)?$,"
            r"^vscode-webview://[a-z0-9-]+$"
        ),
        description=(
            "Comma-separated regex allowlist for CORS Origin headers. "
            "No wildcard with credentials."
        ),
        category=Category.INTEGRATION,
        source_file="backend/core/ouroboros/governance/ide_observability.py",
        example="^https?://localhost(:\\d+)?$",
        since="v1.0",
    ),

    # ====================================================================
    # Task board (Gap #5) — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_TOOL_TASK_BOARD_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Enables Venom task_create/task_update/task_complete tools "
            "and the per-op TaskBoard. Graduated 2026-04-20."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/task_tool.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Plan approval (Problem #7) — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_PLAN_APPROVAL_MODE",
        type=FlagType.BOOL, default=False,
        description=(
            "When true, every complex op halts at PLAN phase for human "
            "review. Deliberately default-off — turning it on halts "
            "every op."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/plan_approval.py",
        example="false",
        since="v1.0",
        posture_relevance=_HARDEN_CRITICAL,
    ),

    # ====================================================================
    # L2 repair — 2 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_L2_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Iterative self-repair FSM. Engages when VALIDATE exhausts "
            "retries. Closes the Ouroboros cycle per Manifesto §6."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/repair_engine.py",
        example="true",
        since="v1.0",
        posture_relevance=_HARDEN_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_L2_TIMEBOX_S",
        type=FlagType.INT, default=120,
        description=(
            "Maximum seconds L2 can spend on repair iterations before "
            "giving up and routing to POSTMORTEM."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/repair_engine.py",
        example="120",
        since="v1.0",
    ),

    # ====================================================================
    # Auto-commit — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_AUTO_COMMIT_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Creates git commits with the O+V signature after APPLY "
            "and VERIFY pass. Protected branches rejected by default."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/auto_committer.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Subagents — 3 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_SUBAGENT_DISPATCH_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Phase-1 read-only subagent dispatch (Explore). Graduated "
            "2026-04-18 after 3-session clean arc."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/agentic_subagent.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_GENERAL_LLM_DRIVER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "LLM-driven GENERAL subagent (Phase C Slice 1b). Gated by "
            "two-layer Semantic Firewall + mutation cage."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/agentic_general_subagent.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_REVIEW_SUBAGENT_SHADOW",
        type=FlagType.BOOL, default=True,
        description=(
            "REVIEW subagent shadow mode — observer-only, never gates "
            "authority. Graduated 2026-04-20."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/agentic_review_subagent.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Semantic index — 2 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_SEMANTIC_INFERENCE_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Recency-weighted semantic centroid for intake priority "
            "bias + CONTEXT_EXPANSION injection. Local fastembed."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/semantic_index.py",
        example="false",
        since="v1.0",
        posture_relevance=_EXPLORE_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_SEMANTIC_PROMPT_INJECTION_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for CONTEXT_EXPANSION injection from SemanticIndex. "
            "Active only when master inference flag is on."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/semantic_index.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # BG pool & worker timeouts — 3 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_BG_POOL_SIZE",
        type=FlagType.INT, default=3,
        description=(
            "BackgroundAgentPool worker count. 3 is tuned for the "
            "16-sensor intake fan-in plus PriorityQueue headroom."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/background_agent_pool.py",
        example="3",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S",
        type=FlagType.INT, default=1800,
        description=(
            "Outer wall-clock cap per COMPLEX op in the BG worker pool. "
            "Raised from 360s after Session H/I/J battle-tests."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/background_agent_pool.py",
        example="1800",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S",
        type=FlagType.INT, default=900,
        description=(
            "Outer wall-clock cap per SWE-Bench-Pro op in the BG worker "
            "pool.  Source-aware Stage 1.5 motor budget (operator "
            "binding 2026-05-13) — benchmark eval traffic needs a longer "
            "lease than sensor traffic because the full pipeline "
            "(CLASSIFY → ROUTE → CTX → PLAN → GENERATE-with-LLM → "
            "VALIDATE → APPLY → VERIFY) for even a trivial fixture "
            "exceeds the 360s sensor base.  Max-aggregated with "
            "read_only and complex categories — whichever applicable "
            "ceiling is longest wins."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/background_agent_pool.py",
        example="900",
        since="2026-05-13",
    ),
    FlagSpec(
        name="JARVIS_FALLBACK_MAX_TIMEOUT_COMPLEX_S",
        type=FlagType.INT, default=360,
        description=(
            "Hard cap for Claude fallback inner timeout on COMPLEX ops."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="360",
        since="v1.0",
    ),

    # ====================================================================
    # Stage 1.6 — BG release / op park during GENERATE LLM wait — 3 flags
    # (operator binding 2026-05-13; substrate landed Slice 1; default-FALSE
    # until Slice 2 wiring + Slice 3 graduation soak prove the 3-claim
    # spine — slot freed during stall / no double-dispatch / no lost terminal)
    # ====================================================================
    FlagSpec(
        name="JARVIS_BG_PARK_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Master kill switch for the Stage 1.6 BG park substrate.  "
            "When false (default per §33.1), ParkedOpStore.park() raises "
            "and no caller can admit a park — the substrate is byte-"
            "identical at runtime.  Flips to default-true only after the "
            "Slice 3 graduation soak proves slot release + single-flight "
            "+ terminal preservation under live SWE-Bench-Pro traffic."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/op_park_store.py",
        example="true",
        since="2026-05-13",
    ),
    FlagSpec(
        name="JARVIS_BG_PARK_TTL_S",
        type=FlagType.INT, default=1800,
        description=(
            "Seconds after which a parked op whose resume continuation "
            "never fires is reaped (status=ttl_expired).  Defaults to 30 "
            "min — longer than the longest legitimate provider round-trip "
            "(DW BACKGROUND ~10 min) but short enough that dead resume "
            "continuations don't wedge the store."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/op_park_store.py",
        example="1800",
        since="2026-05-13",
    ),
    FlagSpec(
        name="JARVIS_BG_PARK_STORE_MAX_SIZE",
        type=FlagType.INT, default=64,
        description=(
            "Bounded capacity of the ParkedOpStore registry.  When at "
            "capacity, the oldest non-terminal record is evicted "
            "(status=evicted) so any awaiter unblocks cleanly.  Default "
            "64 covers ~BG_POOL_SIZE × 4 concurrent parks with headroom."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/op_park_store.py",
        example="64",
        since="2026-05-13",
    ),

    # ====================================================================
    # WallClockWatchdog suspension diagnostic (Task #94, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_HARNESS_SUSPENSION_WARN_RATIO",
        type=FlagType.FLOAT, default=0.5,
        description=(
            "Monotonic/wall ratio threshold below which the "
            "WallClockWatchdog emits a SUSPENSION LIKELY warning + "
            "stamps summary.json with suspension_likely=true + "
            "suspension_ratio=<value>.  Closes the v14-rev10 session-"
            "075335 observability gap: a session suspended by macOS "
            "App Nap (process paused while laptop slept) currently "
            "fires stop_reason=wall_clock_cap identical to a clean "
            "full-runtime cap-hit.  Threshold value: monotonic/wall < "
            "0.5 means the process was suspended >50% of its session "
            "wall window — graduation/Bar A claims from such a "
            "session are evidence-invalid unless re-run under "
            "caffeinate.  Pure additive diagnostic — no behavior "
            "change to WHEN the watchdog fires."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/battle_test/harness.py",
        example="0.5",
        since="2026-05-14",
    ),

    # ====================================================================
    # D2 — Per-request httpx budget coherence (Task #95, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_CLAUDE_HTTPX_CONNECT_CAP_S",
        type=FlagType.FLOAT, default=5.0,
        description=(
            "Absolute cap on the per-request httpx connect/write/pool "
            "timeout for Claude messages.stream/create calls.  The "
            "actual connect ceiling at call time is min(this_cap, "
            "outer_attempt_budget_s) — connect can never exceed the "
            "outer asyncio.wait_for budget.  Closes v14-rev12 D2: "
            "before this knob, ClaudeProvider constructed a static "
            "httpx.Timeout(connect=10, read=600 thinking / 120 default) "
            "at _ensure_client() time, so a 10.4s outer-attempt budget "
            "produced a 131s actual call (10s connect + 120s read), "
            "12× over the outer wait_for.  Per operator binding "
            "2026-05-14: derive httpx timeouts from per-request budget; "
            "no magic numbers; cap composes outer budget invariant."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="5.0",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_CLAUDE_STREAM_HARD_KILL_GRACE_S",
        type=FlagType.FLOAT, default=30.0,
        description=(
            "Grace seconds added to the *live* UTC wall remaining "
            "budget when computing the Claude stream hard-kill "
            "``asyncio.wait`` timeout (Task #100, 2026-05-14).  The "
            "wait budget is ``_remaining_utc_budget_s(deadline) + "
            "this_grace`` — never ``stale_timeout_snapshot + 30``, "
            "so backoff retries cannot re-inflate the hard-kill window "
            "after the orchestrator deadline has shrunk (D2 envelope)."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="30.0",
        since="2026-05-14",
    ),

    # ====================================================================
    # Gate-adoption audit — thinking=on residual-gap localization (Task #107, 2026-05-15)
    # ====================================================================
    FlagSpec(
        name="JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Default-FALSE diagnostic gate (distinct from the one-shot "
            "JARVIS_CLAUDE_STREAM_BOUNDARY_LOG_ENABLED).  When true, a "
            "bounded concurrent sampler emits periodic "
            "[ClaudeProvider.stream.boundary.audit] snapshots (not-done "
            "task population + names) DURING the pre-first-raw-event "
            "window of each Claude stream, until the first raw SDK "
            "event arrives.  Purpose: localize the Task #105 SPLIT "
            "thinking=on residual gap (Tier-C 20-158s) by naming the "
            "task family that stays runnable while the quiescence gate "
            "is cleared during the minutes-long thinking-phase no-byte "
            "window.  Self-terminating + hard-capped (≤60 samples, "
            "deadline = min(timeout_s+30, 900s)) so it can never leak; "
            "log-only, never raises.  Measurement only — no behavior "
            "change.  Operator-approved 2026-05-15 (Task #107 charter)."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="false",
        since="2026-05-15",
    ),
    FlagSpec(
        name="JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_INTERVAL_S",
        type=FlagType.FLOAT, default=15.0,
        description=(
            "Seconds between [stream.boundary.audit] samples while "
            "awaiting the first raw SDK event (Task #107).  Default "
            "15s — fine enough to resolve the 20-158s thinking=on gap "
            "into multiple snapshots, coarse enough to add negligible "
            "load.  Invalid / non-positive → 15.0.  Only consulted "
            "when JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_ENABLED=true."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="15.0",
        since="2026-05-15",
    ),

    # ====================================================================
    # Autonomous Quiescence Protocol — Core Isolation (Task #104, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_QUIESCENCE_PROTOCOL_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the Autonomous Quiescence Protocol.  "
            "When true, a global asyncio.Event gate is CLEARED for the "
            "lifetime of every Claude SDK stream (quiescence_core_"
            "active); heavy background loops that await the gate at "
            "their critical-iteration top park at 0% CPU until the "
            "core releases, handing 100% of the event loop to the "
            "network stream consumer.  Refcounted for the BG pool's "
            "concurrent workers.  Closes the residual 94-333s "
            "first_raw_event delay the B1 falsification campaign "
            "(Task #103) proved remained after Oracle was disabled — "
            "deterministic containment, not per-subsystem sleep(0) "
            "whack-a-mole.  Default true per operator binding "
            "2026-05-14 ('lock down the entire matrix, let the core "
            "breathe').  False → both surfaces no-op (byte-identical "
            "legacy)."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/quiescence.py",
        example="true",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_QUIESCENCE_MAX_PAUSE_S",
        type=FlagType.FLOAT, default=420.0,
        description=(
            "Anti-starvation ceiling — maximum seconds a background "
            "loop will park in the quiescence gate before proceeding "
            "(degraded, with a WARN) even if the core still holds it.  "
            "Default 420s — longer than any single GENERATE budget "
            "(~360s thinking + grace) so a healthy core never trips "
            "it, but a wedged GENERATE cannot freeze the organism "
            "forever.  Degrade, never starve.  Invalid / non-positive "
            "values fall back to 420.  Task #104 Quiescence Protocol, "
            "operator binding 2026-05-14."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/quiescence.py",
        example="420.0",
        since="2026-05-14",
    ),

    # ====================================================================
    # Autonomous Event-Loop Governance Substrate (Task #102, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the Autonomous Event-Loop Governance "
            "Substrate.  When true, hot iterations over large "
            "collections (Oracle._scan_for_changes 29k files, future "
            "Advisor / sensor sites) inject asyncio.sleep(0) every N "
            "items via cooperative_yield_every_n_async, AND offload "
            "blocking work (file read + hash compute) via "
            "offload_blocking → asyncio.to_thread.  Composes existing "
            "asyncio primitives — no external dependencies, no "
            "dedicated threading hacks that fracture the async "
            "context.  Closes H11 (event-loop starvation) — the "
            "final-mile cause of Claude stream first_token=NEVER "
            "under harness load (Task #101 diagnostic matrix proved "
            "substrate sound at probe scale; harness fails despite "
            "identical config).  Default true per operator binding "
            "2026-05-14: 'autonomous defense, no brittle hacks.'  "
            "Set false for byte-identical legacy behavior."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/event_loop_governance.py",
        example="true",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_EVENT_LOOP_YIELD_EVERY_N",
        type=FlagType.INT, default=64,
        description=(
            "Cooperative-yield cadence — every N items processed by "
            "cooperative_yield_every_n_async triggers an asyncio."
            "sleep(0) to release the event loop.  Default 64 — "
            "calibrated so Oracle's 29k-file scan triggers ~450 "
            "yields (dozens of scheduling slots/sec for the Claude "
            "SDK stream consumer) while amortizing yield overhead.  "
            "Lower = more responsive event loop; higher = more "
            "throughput.  Invalid / non-positive values fall back "
            "to 64.  Task #102 Event-Loop Governance, operator "
            "binding 2026-05-14."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/event_loop_governance.py",
        example="64",
        since="2026-05-14",
    ),

    # ====================================================================
    # Autonomous Connection Lifecycle Policy (Task #99, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_CLAUDE_IDLE_RECYCLE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for ClaudeProvider's autonomous idle-recycle "
            "policy.  When true, ``_ensure_client`` checks "
            "time-since-last-successful-call before returning the "
            "cached AsyncAnthropic client; if elapsed > "
            "JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S, autonomously "
            "recycles the httpx pool (composes existing "
            "``_recycle_client`` primitive — Task #4 cascade hardening) "
            "before the new call.  Closes the v14-rev16 Tier 2 blocker "
            "where Claude streams returned 0 bytes after ~5 min of "
            "pipeline work — stale TCP keepalives silently torn down "
            "by upstream NAT / LB / firewall.  Default true per "
            "operator binding 2026-05-14: 'survive the long-compute "
            "gaps inherent to O+V.'  Set false for byte-identical "
            "legacy behavior."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="true",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_CLAUDE_IDLE_RECYCLE_THRESHOLD_S",
        type=FlagType.FLOAT, default=120.0,
        description=(
            "Seconds of idle time since last successful Claude API "
            "call after which the next ``_ensure_client`` "
            "autonomously recycles the pool.  Default 120s — covers "
            "typical upstream NAT / load-balancer keepalive timeouts "
            "(60-300s).  Set to 0 for opt-out single-use behavior "
            "(every call after any successful call triggers recycle "
            "— useful for diagnosing pool-related defects).  Negative "
            "/ invalid values fall back to 120s default.  Task #99 "
            "Autonomous Connection Lifecycle Policy, operator "
            "binding 2026-05-14."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="120.0",
        since="2026-05-14",
    ),

    # ====================================================================
    # Universal phase-local sub-budgeting (Task #98, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for universal phase-local sub-budgeting "
            "across pre-GENERATE phases (CLASSIFY, ROUTE, "
            "CONTEXT_EXPANSION, PLAN).  When true, each runner "
            "dispatch is wrapped in asyncio.wait_for with a phase "
            "budget computed from min(op_remaining × fraction, "
            "op_remaining - MIN_GENERATE_RESERVE_S).  Graceful degrade "
            "via PhaseResult(status='skip', "
            "reason='phase_budget_exhausted:...') preserves operation "
            "integrity.  Default true per operator binding 2026-05-14: "
            "'every phase must autonomously calculate its phase-local "
            "deadline and gracefully degrade or interrupt its sub-"
            "components if it threatens the MIN_GENERATE_RESERVE_S "
            "floor.'  Setting false reverts to byte-identical pre-"
            "Task-#98 pass-through dispatch."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/phase_budget.py",
        example="true",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_PHASE_BUDGET_FRACTION_CLASSIFY",
        type=FlagType.FLOAT, default=0.05,
        description=(
            "Fraction of op_remaining that CLASSIFY phase may consume "
            "(default 0.05 — CLASSIFY is fast deterministic; small "
            "slice is defense-in-depth).  Composes with universal "
            "kernel: min(fraction × op_remaining, op_remaining - "
            "MIN_GENERATE_RESERVE_S).  Invalid values fall back to "
            "default.  Task #98 universal phase-budget."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/phase_budget.py",
        example="0.05",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_PHASE_BUDGET_FRACTION_ROUTE",
        type=FlagType.FLOAT, default=0.05,
        description=(
            "Fraction of op_remaining that ROUTE phase may consume "
            "(default 0.05).  Same composition shape as CLASSIFY. "
            "Task #98 universal phase-budget."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/phase_budget.py",
        example="0.05",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_PHASE_BUDGET_FRACTION_CONTEXT_EXPANSION",
        type=FlagType.FLOAT, default=0.20,
        description=(
            "Fraction of op_remaining that CONTEXT_EXPANSION phase "
            "may consume (default 0.20 — medium slice since CTX runs "
            "Claude expansion).  Task #98 universal phase-budget."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/phase_budget.py",
        example="0.20",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_PHASE_BUDGET_FRACTION_PLAN",
        type=FlagType.FLOAT, default=0.30,
        description=(
            "Fraction of op_remaining that PLAN phase may consume "
            "(default 0.30 — largest pre-GENERATE slice).  Canonical "
            "name for the Task #97 PLAN-phase fraction; the legacy "
            "JARVIS_PLAN_PHASE_BUDGET_FRACTION knob still works for "
            "back-compat (resolver reads legacy first).  Task #98 "
            "universal phase-budget."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/phase_budget.py",
        example="0.30",
        since="2026-05-14",
    ),

    # ====================================================================
    # PLAN phase-local sub-budgeting (Task #97, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_PLAN_PHASE_BUDGET_FRACTION",
        type=FlagType.FLOAT, default=0.30,
        description=(
            "Fraction of remaining op budget that PLAN may consume "
            "(default 0.30 — leaves 70% for GENERATE).  Combined with "
            "JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S (absolute floor) "
            "via min(fraction_bound, reserve_bound).  Valid range: "
            "(0.0, 1.0]; invalid values fall back to default.  "
            "Closes v14-rev14 Tier 1 regression where PLAN consumed "
            "194-337s of the op budget, leaving GENERATE with "
            "claude_plan_budget_starved:-45.4s_remaining.  Operator "
            "binding 2026-05-14: strict asynchronous isolation + sub-"
            "phase budgeting — no hardcoding."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/plan_generator.py",
        example="0.30",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S",
        type=FlagType.FLOAT, default=60.0,
        description=(
            "Absolute minimum seconds reserved for GENERATE after PLAN "
            "phase completes (default 60s).  Acts as a hard floor on "
            "the PLAN phase budget: plan_budget = min(op_remaining × "
            "fraction, op_remaining - this_reserve).  GENERATE's "
            "Claude calls need real runway — a 1s reserve is "
            "operationally unusable; this knob enforces a viable "
            "minimum.  Task #97 operator binding 2026-05-14."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/plan_generator.py",
        example="60.0",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_PLAN_PHASE_MIN_BUDGET_S",
        type=FlagType.FLOAT, default=5.0,
        description=(
            "Floor below which PLAN is skipped entirely (default 5s) "
            "— graceful degrade.  If the computed phase-local budget "
            "falls below this floor, PlanGenerator returns "
            "PlanResult.skipped_result(\"plan_phase_skipped:...\") "
            "and the pipeline falls through to GENERATE with the "
            "full op_remaining preserved.  Below 5s, a planning "
            "attempt is doomed and would only burn the GENERATE "
            "budget.  Task #97 operator binding 2026-05-14."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/plan_generator.py",
        example="5.0",
        since="2026-05-14",
    ),

    # ====================================================================
    # H1 falsification — http client mode (Task #96, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_CLAUDE_HTTP_CLIENT_MODE",
        type=FlagType.STR, default="custom",
        description=(
            "Closed 2-value taxonomy {custom, stdlib_default} gating "
            "the ClaudeProvider httpx client construction.  Default "
            "'custom' preserves the production httpx.AsyncClient + "
            "Timeout + Limits configuration byte-identically — no "
            "behavior change without explicit operator measurement.  "
            "'stdlib_default' drops the construction-time http_client "
            "kwarg (uses SDK / httpx defaults exactly like the Step 2 "
            "probe shape) while preserving D2 per-request timeout + "
            "max_retries=0.  Unknown values fall back to 'custom' "
            "per operator binding (no silent behavior change).  This "
            "knob is the H1 falsification gate for the v14-rev13 "
            "Tier 2 blocker: 4 stream terminations with "
            "first_token=NEVER bytes_received=0 thinking=on at "
            "httpcore.ConnectTimeout in connect_tcp, despite Step 2 "
            "probe showing AsyncAnthropic() defaults complete in 1-2s. "
            "Flip for v14-rev14 only — if H1 clears the timeouts, the "
            "correct forward-port is recalibration of Limits / "
            "Timeout per measurement, NOT permanent removal of the "
            "custom client."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="custom",
        since="2026-05-14",
    ),

    # ====================================================================
    # Oracle ↔ Advisor cooperative yield (Task #88f, 2026-05-14)
    # ====================================================================
    FlagSpec(
        name="JARVIS_ORACLE_YIELD_TO_ADVISOR",
        type=FlagType.BOOL, default=True,
        description=(
            "When true, Oracle's _oracle_index_loop skips an "
            "incremental_update poll cycle if Advisor blast scans are "
            "in flight (get_advisor_busy_count() > 0).  Closes the "
            "v14-rev10 graduation soak blocker where Oracle's 29k-file "
            "main-tree polling contended with SWE op's Advisor blast "
            "(4m 46s vs <2s when Oracle was quiet).  Bounded skip via "
            "JARVIS_ORACLE_YIELD_MAX_CONSECUTIVE_SKIPS prevents "
            "indefinite Oracle starvation."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/governed_loop_service.py",
        example="true",
        since="2026-05-14",
    ),
    FlagSpec(
        name="JARVIS_ORACLE_YIELD_MAX_CONSECUTIVE_SKIPS",
        type=FlagType.INT, default=10,
        description=(
            "Maximum consecutive yields before Oracle's "
            "_oracle_index_loop forces an incremental_update regardless "
            "of advisor busy state.  Prevents indefinite Oracle "
            "starvation while still cooperating with hot-path SWE ops.  "
            "At default 3min poll * 10 skips = 30min maximum yield "
            "window before Oracle force-polls."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/governed_loop_service.py",
        example="10",
        since="2026-05-14",
    ),

    # ====================================================================
    # Park continuation timeout — thinking-aware (Task #88d, 2026-05-13)
    # ====================================================================
    FlagSpec(
        name="JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S",
        type=FlagType.INT, default=390,
        description=(
            "Timeout (in seconds) for the asyncio.wait_for that wraps "
            "the out-of-pool park continuation's provider call when "
            "thinking is enabled.  The fourth coherence layer (after "
            "Task #88 inner 360s, #88b outer 360s, #88c floor 360s): "
            "the continuation's own wait_for inherits the legacy "
            "GENERATE-phase wall (~200s for STANDARD), which falsely "
            "cancels legitimate thinking-on streams.  Default 390s "
            "= 360s single-policy budget + 30s grace for asyncio "
            "wait_for overhead.  Operator binding 2026-05-13: 'every "
            "outer waiter that can kill the stream must be >= the "
            "innermost legitimate LLM budget when thinking=yes'.  "
            "Non-thinking IMMEDIATE/trivial paths keep the legacy "
            "gen_timeout + outer_grace_s (no widening)."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/generate_park_wrapper.py",
        example="390",
        since="2026-05-13",
    ),

    # ====================================================================
    # Fallback min-guaranteed floor — thinking-aware (Task #88c, 2026-05-13)
    # ====================================================================
    FlagSpec(
        name="JARVIS_FALLBACK_MIN_GUARANTEED_THINKING_S",
        type=FlagType.INT, default=360,
        description=(
            "Floor (in seconds) for the Claude fallback budget when "
            "thinking is enabled.  The non-thinking floor remains "
            "_FALLBACK_MIN_GUARANTEED_S=90 (env "
            "OUROBOROS_FALLBACK_MIN_GUARANTEED_S).  v14-rev7 surfaced "
            "the third budget layer: even with Task #88 (inner 360s) "
            "and #88b (outer _max_cap 360s) widened, the actual Claude "
            "timeout was 90s because the DW cascade had already consumed "
            "~140s of the ~200s op deadline; the post-acquire refresh "
            "floor of 90s was the binding constraint.  Promoting the "
            "floor to 360s for thinking-on closes the single-policy "
            "invariant: thinking floor >= max(inner, outer) = 360s.  "
            "Operator binding 2026-05-13: 'Claude-floor reservation "
            "against op global deadline — DW cannot force Claude below "
            "the floor.'  Non-thinking IMMEDIATE routes keep the 90s "
            "default floor unchanged."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/candidate_generator.py",
        example="360",
        since="2026-05-13",
    ),

    # ====================================================================
    # Fallback outer-budget — thinking-aware cap (Task #88b, 2026-05-13)
    # ====================================================================
    FlagSpec(
        name="JARVIS_FALLBACK_MAX_TIMEOUT_THINKING_S",
        type=FlagType.INT, default=360,
        description=(
            "Outer asyncio.wait_for budget cap for Claude fallback "
            "calls that will have thinking enabled.  Task #88's "
            "inner rupture widening (JARVIS_STREAM_RUPTURE_TIMEOUT_"
            "THINKING_S=360s) is insufficient alone: the outer "
            "_call_fallback wait_for fires first if its cap is "
            "narrower than the inner.  Single policy with #88: outer "
            ">= inner for thinking-on calls.  Applied via max() so it "
            "never SHRINKS route-specific caps (COMPLEX=180, read-only-"
            "BG~480+).  Non-thinking routes (IMMEDIATE) keep the 120s "
            "base cap."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/candidate_generator.py",
        example="360",
        since="2026-05-13",
    ),

    # ====================================================================
    # Stream rupture — thinking-aware TTFT (Task #88, 2026-05-13)
    # ====================================================================
    FlagSpec(
        name="JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S",
        type=FlagType.INT, default=360,
        description=(
            "Seconds waiting for first TEXT token from a streaming "
            "Claude/DW call when extended thinking is enabled.  The "
            "SDK's text_stream filters out thinking_delta events, so "
            "to a text-only consumer the stream appears silent during "
            "the entire reasoning phase.  For 17k-char SWE-Bench-Pro "
            "prompts with thinking_budget=16k tokens, thinking can "
            "legitimately run 3-5 minutes.  Default 360s = 6 min "
            "covers empirically-observed durations.  Non-thinking "
            "calls fall back to JARVIS_STREAM_RUPTURE_TIMEOUT_S (120s)."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/stream_rupture.py",
        example="360",
        since="2026-05-13",
    ),

    # ====================================================================
    # DW entitlement classifier — Task #86 root fix (2026-05-13)
    # ====================================================================
    FlagSpec(
        name="JARVIS_DW_ENTITLEMENT_BLOCK_MARKERS",
        type=FlagType.STR, default=None,
        description=(
            "CSV of response-body markers that identify per-model "
            "DoubleWord entitlement blocks (distinct from global auth "
            "failures).  Matching is case-insensitive substring against "
            "the response body excerpt.  Empty/missing falls back to "
            "the defaults: 'blocked by a routing rule', 'contact your "
            "administrator', 'request access' — phrases empirically "
            "observed in DW 403 responses for non-entitled models.  "
            "Operators extend (or replace) when DW changes phrasing.  "
            "Consumed by dw_entitlement_classifier.classify_4xx() — "
            "single source of truth, no hardcoded model list anywhere."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/dw_entitlement_classifier.py",
        example="blocked by a routing rule,contact your administrator",
        since="2026-05-13",
    ),

    # ====================================================================
    # Orange PR review — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_ORANGE_PR_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Async-review path for APPROVAL_REQUIRED ops — opens a PR "
            "via gh pr create instead of blocking on CLI approval."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/orange_pr_reviewer.py",
        example="false",
        since="v1.0",
    ),

    # ====================================================================
    # Iron Gate — exploration + ASCII + multi-file — 4 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_EXPLORATION_GATE",
        type=FlagType.BOOL, default=True,
        description=(
            "Requires minimum 2 read_file/search_code/get_callers calls "
            "before any patch reaches disk."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/orchestrator.py",
        example="true",
        since="v1.0",
        posture_relevance=_HARDEN_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_EXPLORATION_LEDGER_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Diversity-weighted exploration scoring across 5 categories "
            "(comprehension / discovery / call_graph / structure / "
            "history). Opt-in; otherwise legacy int counter."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/exploration_ledger.py",
        example="false",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_ASCII_GATE",
        type=FlagType.BOOL, default=True,
        description=(
            "Iron Gate ASCII strictness — rejects any non-ASCII codepoint "
            "in candidate content to prevent Unicode corruption."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/ascii_strict_gate.py",
        example="true",
        since="v1.0",
        posture_relevance=_HARDEN_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_MULTI_FILE_GEN_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Enables multi-file coordinated generation with batch-level "
            "rollback. Iron Gate 5 (coverage) enforces all files "
            "produced reach APPLY atomically."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/orchestrator.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Risk-tier floor — 4 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_MIN_RISK_TIER",
        type=FlagType.STR, default="",
        description=(
            "Explicit floor on risk tier for auto-apply: "
            "safe_auto|notify_apply|approval_required. Strictest wins."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/risk_tier_floor.py",
        example="notify_apply",
        since="v1.0",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_PARANOIA_MODE",
        type=FlagType.BOOL, default=False,
        description=(
            "Shortcut for JARVIS_MIN_RISK_TIER=notify_apply. Forces 5s "
            "/reject window on every change."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/risk_tier_floor.py",
        example="false",
        since="v1.0",
        posture_relevance=_HARDEN_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_AUTO_APPLY_QUIET_HOURS",
        type=FlagType.STR, default="",
        description=(
            "Time-of-day window when SAFE_AUTO is forbidden, format "
            "<start>-<end> 24h. Wrap-around supported (22-7 means "
            "22:00-06:59)."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/risk_tier_floor.py",
        example="22-7",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_AUTO_APPLY_QUIET_HOURS_TZ",
        type=FlagType.STR, default="UTC",
        description=(
            "IANA timezone for quiet-hours interpretation. Defaults to "
            "UTC (implicit local-wall-clock is ambiguous across "
            "multi-operator deployments)."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/risk_tier_floor.py",
        example="America/New_York",
        since="v1.0",
    ),

    # ====================================================================
    # Semantic guard (pre-APPLY pattern detector) — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_SEMANTIC_GUARD_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the 10 pre-APPLY AST/regex patterns "
            "(credential_shape_introduced, function_body_collapsed, "
            "permission_loosened, etc.). Per-pattern kill switches "
            "available via JARVIS_SEMGUARD_<PATTERN>_ENABLED."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/semantic_guardian.py",
        example="true",
        since="v1.0",
        posture_relevance=_ALL_POSTURES_CRITICAL,
    ),

    # ====================================================================
    # Strategic direction — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_STRATEGIC_GIT_HISTORY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Infers recent development momentum from last 50 git log "
            "commits via Conventional Commit parsing; emits a Recent "
            "Development Momentum section in the strategic digest."
        ),
        category=Category.OBSERVABILITY,
        source_file="backend/core/ouroboros/governance/strategic_direction.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Developer-Memory injection (priority 3) — 3 flags. Surfaces
    # curated repo memory/*.md into every GENERATE prompt via the
    # existing crawl_memory crawler. Default-False until graduation.
    # ====================================================================
    FlagSpec(
        name="JARVIS_STRATEGIC_DEV_MEMORY_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Injects curated repo memory/*.md (recency-ranked, "
            "budget-capped) into the strategic digest as an advisory, "
            "authority-free '## Recent Developer Memory' section. "
            "Composes roadmap.source_crawlers.crawl_memory."
        ),
        category=Category.OBSERVABILITY,
        source_file="backend/core/ouroboros/governance/strategic_direction.py",
        example="false",
        since="v1.1",
    ),
    FlagSpec(
        name="JARVIS_STRATEGIC_DEV_MEMORY_MAX_CHARS",
        type=FlagType.INT, default=6000,
        description=(
            "Char budget cap for the '## Recent Developer Memory' "
            "section so it never blows the generation prompt envelope."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/strategic_direction.py",
        example="6000",
        since="v1.1",
    ),
    FlagSpec(
        name="JARVIS_STRATEGIC_DEV_MEMORY_MAX_FILES",
        type=FlagType.INT, default=8,
        description=(
            "Max number of recency-ranked memory/*.md files folded "
            "into the '## Recent Developer Memory' section."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/strategic_direction.py",
        example="8",
        since="v1.1",
    ),

    # ====================================================================
    # Rust subsystem awareness map (priority 4, Option-1) — 3 flags.
    # Surfaces native Rust crates into the strategic digest via the
    # existing crawl_rust_subsystems crawler. Default-False until
    # graduation. Awareness-only — Oracle stays Python-only.
    # ====================================================================
    FlagSpec(
        name="JARVIS_STRATEGIC_RUST_MAP_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Injects a dynamically-discovered Rust crate map "
            "(name + path + summary) into the strategic digest as an "
            "advisory, authority-free '## Rust Subsystems' section so "
            "O+V uses Venom tools on .rs. Oracle stays Python-only. "
            "Composes roadmap.source_crawlers.crawl_rust_subsystems."
        ),
        category=Category.OBSERVABILITY,
        source_file="backend/core/ouroboros/governance/strategic_direction.py",
        example="false",
        since="v1.1",
    ),
    FlagSpec(
        name="JARVIS_STRATEGIC_RUST_MAX_CHARS",
        type=FlagType.INT, default=4000,
        description=(
            "Char budget cap for the '## Rust Subsystems' section so "
            "it never blows the generation prompt envelope."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/strategic_direction.py",
        example="4000",
        since="v1.1",
    ),
    FlagSpec(
        name="JARVIS_STRATEGIC_RUST_MAX_CRATES",
        type=FlagType.INT, default=12,
        description=(
            "Max number of Rust crates folded into the "
            "'## Rust Subsystems' section (also bounds the "
            "crawl_rust_subsystems Cargo.toml scan)."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/roadmap/source_crawlers.py",
        example="12",
        since="v1.1",
    ),

    # ====================================================================
    # Max validate retries (Session U workaround) — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_MAX_VALIDATE_RETRIES",
        type=FlagType.INT, default=1,
        description=(
            "Maximum VALIDATE phase retry attempts before the op fails "
            "out. Sessions U workaround sets this to 0 in battle tests "
            "to bypass 'infra' flakiness."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/orchestrator.py",
        example="1",
        since="v1.0",
    ),

    # ====================================================================
    # Tool monitor (Ticket #4) — 2 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_TOOL_MONITOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Venom monitor tool — read-only binary allowlist with "
            "argv-only args. Graduated 2026-04-20."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/tool_executor.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_TEST_RUNNER_STREAMING_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "BackgroundMonitor-backed pytest streaming path with "
            "per-test INFO events + optional event_callback. "
            "Graduated 2026-04-20."
        ),
        category=Category.OBSERVABILITY,
        source_file="backend/core/ouroboros/governance/test_runner.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Session browser (Session History Browser arc) — 1 flag
    # ====================================================================
    FlagSpec(
        name="JARVIS_SESSION_BROWSER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Read-only index over .ouroboros/sessions/; powers "
            "/session REPL + GET /observability/sessions."
        ),
        category=Category.OBSERVABILITY,
        source_file="backend/core/ouroboros/governance/session_browser.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Vision + Visual VERIFY — 2 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_VISION_SENSOR_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Read-only Ferrari frame consumer. Tier 1 regex + Tier 2 "
            "VLM cascade. 20-op FP budget auto-pause, 120s cooldown, "
            "NOTIFY_APPLY risk floor."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/intake/sensors/vision_sensor.py",
        example="false",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_GENERATE_ATTACHMENTS_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Multi-modal ingest — emits native Claude image/document "
            "blocks or DW OpenAI-compat image_url. BG/SPEC routes strip."
        ),
        category=Category.INTEGRATION,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Worktree reap + battle test — 2 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_GOVERNED_L3_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "L3 worktree isolation for parallel execution graphs. "
            "Hard-fail on create failure (no silent fallback to shared "
            "tree per §1 boundary)."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/autonomy/subagent_scheduler.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_WORKTREE_REAP_ORPHANS",
        type=FlagType.BOOL, default=True,
        description=(
            "Boot-time sweep of orphan unit-* worktrees/branches. "
            "Recovers from SIGKILL/OOM/power-loss leftovers."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/autonomy/worktree_manager.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_BATTLE_REAP_ZOMBIES",
        type=FlagType.BOOL, default=True,
        description=(
            "ouroboros_battle_test.py harness — SIGTERM/SIGKILL "
            "escalation for zombie test processes; prevents budget "
            "competition across sessions."
        ),
        category=Category.SAFETY,
        source_file="scripts/ouroboros_battle_test.py",
        example="true",
        since="v1.0",
    ),

    # ====================================================================
    # Routing — 2 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_THINKING_BUDGET_IMMEDIATE",
        type=FlagType.INT, default=0,
        description=(
            "Claude extended thinking budget in tokens for IMMEDIATE "
            "route. Default 0 (disabled) to keep IMMEDIATE reflex-fast; "
            "raise only when acute failure signals demand reasoning."
        ),
        category=Category.ROUTING,
        source_file="backend/core/ouroboros/governance/providers.py",
        example="0",
        since="v1.0",
        posture_relevance={"HARDEN": Relevance.RELEVANT},
    ),
    FlagSpec(
        name="JARVIS_TIER1_RESERVE_S",
        type=FlagType.INT, default=25,
        description=(
            "Seconds reserved for Claude Tier 1 fallback before "
            "DoubleWord exhaustion — prevents Tier 0 starvation."
        ),
        category=Category.ROUTING,
        source_file="backend/core/ouroboros/governance/candidate_generator.py",
        example="25",
        since="v1.0",
    ),

    # ====================================================================
    # FlagRegistry itself — 3 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_FLAG_REGISTRY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the FlagRegistry + /help dispatcher "
            "surfaces (REPL, GET, SSE, typo warnings). Registry data "
            "structure stays alive when off; only surfaces revert."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/flag_registry.py",
        example="false",
        since="v1.0",
        posture_relevance=_ALL_POSTURES_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_FLAG_TYPO_WARN_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for Levenshtein typo detection on unregistered "
            "JARVIS_* env vars. Active only when master is on."
        ),
        category=Category.OBSERVABILITY,
        source_file="backend/core/ouroboros/governance/flag_registry.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_FLAG_TYPO_MAX_DISTANCE",
        type=FlagType.INT, default=3,
        description=(
            "Levenshtein distance threshold for typo suggestions. Lower "
            "= stricter (fewer false-positive typo warnings); higher = "
            "catches more transpositions."
        ),
        category=Category.TUNING,
        source_file="backend/core/ouroboros/governance/flag_registry.py",
        example="3",
        since="v1.0",
    ),
    # ====================================================================
    # Wave 3 (6) — Parallel L3 fan-out (parallel_dispatch) — 5 flags
    # Operator directive 2026-04-23: env knobs operator-visible via /help.
    # ====================================================================
    FlagSpec(
        name="JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Master kill switch for Wave 3 (6) parallel L3 fan-out. "
            "When false (graduation default), all fan-out surfaces are "
            "dead code: the post-GENERATE hook in phase_dispatcher does "
            "nothing, no ExecutionGraph is built, no scheduler submit. "
            "Flip true ALONGSIDE _SHADOW or _ENFORCE to engage."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/parallel_dispatch.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW",
        type=FlagType.BOOL, default=False,
        description=(
            "Shadow sub-flag. With master on + shadow on (+ enforce "
            "off), the post-GENERATE hook builds the ExecutionGraph "
            "and emits [ParallelDispatch] telemetry but does NOT "
            "submit to SubagentScheduler. Used for live-ops decision-"
            "correctness observation before enforce engagement."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/parallel_dispatch.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE",
        type=FlagType.BOOL, default=False,
        description=(
            "Enforce sub-flag. With master on + enforce on, eligible "
            "multi-file ops submit to SubagentScheduler via "
            "enforce_evaluate_fanout (MemoryPressureGate re-consulted "
            "immediately before submit; narrow error handling; "
            "bounded wait). Downstream APPLY consumption by "
            "slice4b_runner is a separate follow-up after Wave 3 (6) "
            "FINAL. Enforce wins when both shadow+enforce are set."
        ),
        category=Category.EXPERIMENTAL,
        source_file="backend/core/ouroboros/governance/parallel_dispatch.py",
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_WAVE3_PARALLEL_MAX_UNITS",
        type=FlagType.INT, default=3,
        description=(
            "Hard ceiling on fan-out degree, applied after posture "
            "weighting and before MemoryPressureGate.can_fanout. "
            "Default 3 per scope §12 (b); env-tunable for graduation "
            "boundary tests (2 / 3 / 4). Non-positive + unparseable "
            "values fall back to 3."
        ),
        category=Category.CAPACITY,
        source_file="backend/core/ouroboros/governance/parallel_dispatch.py",
        example="3",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S",
        type=FlagType.FLOAT, default=900.0,
        description=(
            "Per-graph wait budget for enforce_evaluate_fanout. "
            "Bounded to keep scheduler.wait_for_graph from defeating "
            "--max-wall-seconds (Ticket A1). Default 900s (15 min). "
            "Non-positive + unparseable values fall back to default."
        ),
        category=Category.TIMING,
        source_file="backend/core/ouroboros/governance/parallel_dispatch.py",
        example="900.0",
        since="v1.0",
    ),
    # ====================================================================
    # F3 (Wave 3 (6) Slice 5a side-arc, 2026-04-23) — BacklogSensor
    # default-urgency override. Graduation / harness-only knob; not for
    # production intake tuning. See
    # memory/project_followup_f3_backlog_default_urgency.md for full arc
    # rationale; F1 (intake governor enforcement) + F2 (per-entry
    # urgency_hint schema) are the proper post-graduation fixes.
    # ====================================================================
    FlagSpec(
        name="JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY",
        type=FlagType.STR, default="",
        description=(
            "Graduation/harness-only override for BacklogSensor-emitted "
            "envelope urgency. Default unset → priority→urgency map "
            "preserved byte-identical (priority 4-5 = high, 3 = normal, "
            "1-2 = low). Set to one of {critical, high, normal, low} → "
            "overrides emitted urgency for ALL BacklogSensor tasks this "
            "scan. Invalid values silently fall back to default. Setting "
            "=critical lets graduation seeds escape the source=backlog → "
            "BACKGROUND default classification via UrgencyRouter's "
            "Priority-1 IMMEDIATE gate. Production intake routing should "
            "rely on the enforcing SensorGovernor from F1 (non-blocking "
            "follow-up), not this knob."
        ),
        category=Category.EXPERIMENTAL,
        source_file=(
            "backend/core/ouroboros/governance/intake/sensors/"
            "backlog_sensor.py"
        ),
        example="critical",
        since="v1.0",
    ),
    # F2 Slice 1 — per-entry urgency_hint master flag.
    # Default-off gate for consumption of optional ``urgency_hint``
    # field on backlog.json entries. When on, per-entry hint wins over
    # both the F3 session-wide env override and the priority-map
    # default (most-specific wins). Absent / invalid hints fall back
    # to pre-F2 behavior. See memory/project_followup_f2_backlog_urgency_hint_schema.md.
    FlagSpec(
        name="JARVIS_BACKLOG_URGENCY_HINT_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "F2 Slice 1 — consume per-entry ``urgency_hint`` field on "
            "backlog.json entries. Default off → hint parsed but "
            "ignored (byte-identical to pre-F2). Set to true → hint "
            "stamps envelope urgency, winning over F3 env override AND "
            "priority-map default (most-specific wins). Invalid hint "
            "values fall back to priority-map with one WARNING per "
            "scan. Slice 1 is sensor-side stamping only; UrgencyRouter "
            "consumption lands in Slice 2."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/intake/sensors/"
            "backlog_sensor.py"
        ),
        example="true",
        since="v1.0",
    ),
    # ====================================================================
    # F1 Slice 1 — intake priority scheduler (master flag + tuning knobs)
    # ====================================================================
    # Addresses live_reachability=blocked_by_intake_starvation gap surfaced
    # by Wave 3 (6) Slice 5b graduation S1 (bt-2026-04-24-062608): F2
    # envelope stamping fires correctly but class-partitioned FIFO intake
    # queue lets BG burst emissions starve critical envelopes. F1 makes
    # urgency a dequeue-priority signal, not just an envelope label.
    #
    # Slice 1 is the primitive (IntakePriorityQueue) + default-off flag +
    # unit tests. Slice 2 wires UnifiedIntakeRouter. Default stays off
    # through the Slice 3 graduation cadence.
    FlagSpec(
        name="JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "F1 master flag — intake priority scheduler primary mode. "
            "Default off → byte-identical to pre-F1 class-partitioned "
            "FIFO intake queue. Set to true → ``IntakePriorityQueue`` "
            "becomes the source of truth for dispatch (urgency-priority "
            "heap + reserved-slot starvation guard + per-envelope "
            "deadlines + priority-inversion emergency pop + queue-depth "
            "telemetry + back-pressure). Legacy ``asyncio.PriorityQueue`` "
            "still receives puts (for WAL/back-compat) but is drained as "
            "a tombstone behind the primary queue. Graduation cadence: "
            "Slice 3 integration tests + Slice 4 live cadence + 3 clean "
            "sessions before default flip."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/intake/"
            "intake_priority_queue.py"
        ),
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW",
        type=FlagType.BOOL, default=False,
        description=(
            "F1 Slice 2 shadow flag — observational parallel "
            "IntakePriorityQueue without behavior change. When set to "
            "true AND the master flag is off, the router builds a "
            "parallel priority queue alongside the legacy queue; "
            "ingest mirrors to both; dispatch reads from legacy but "
            "logs ``[IntakePriority shadow_delta]`` whenever the "
            "priority queue would have popped a different envelope. "
            "Enables live evidence gathering of ordering improvements "
            "without risk to production dispatch. Inert when the master "
            "flag is on (primary-mode dominates)."
        ),
        category=Category.OBSERVABILITY,
        source_file=(
            "backend/core/ouroboros/governance/intake/"
            "unified_intake_router.py"
        ),
        example="true",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_INTAKE_RESERVED_N",
        type=FlagType.INT, default=5,
        description=(
            "F1 reserved-slot window size. Of every N sequential dequeues "
            "from the intake priority queue, at least ``JARVIS_INTAKE_"
            "RESERVED_M`` must be urgency >= normal IF any such envelope "
            "is in queue. Prevents pathological 'infinite low-urgency "
            "burst after a normal entry starves it'. Only consumed when "
            "JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED=true."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/intake/"
            "intake_priority_queue.py"
        ),
        example="5",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_INTAKE_RESERVED_M",
        type=FlagType.INT, default=1,
        description=(
            "F1 reserved-slot minimum: how many of the last "
            "``JARVIS_INTAKE_RESERVED_N`` dequeues must be urgency >= "
            "normal. Set to 0 to disable reserved-slot starvation guard "
            "(priority-only ordering). Only consumed when "
            "JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED=true."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/intake/"
            "intake_priority_queue.py"
        ),
        example="1",
        since="v1.0",
    ),
    FlagSpec(
        name="JARVIS_INTAKE_BACKPRESSURE_THRESHOLD",
        type=FlagType.INT, default=200,
        description=(
            "F1 queue-depth cap above which non-critical ingestion is "
            "refused with ``retry_after_s`` (sensors see the signal and "
            "back off). Critical envelopes are always admitted to "
            "prevent the exact starvation mode F1 exists to fix. Only "
            "consumed when JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED=true."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/intake/"
            "intake_priority_queue.py"
        ),
        example="200",
        since="v1.0",
    ),
    # ====================================================================
    # Priority 1 Confidence-Aware Execution (PRD §26.5.1) — 7 flags
    # graduated default-true 2026-04-29 in Slice 5. The cost contract
    # (§26.6) is enforced structurally regardless of these flags' state.
    # ====================================================================
    FlagSpec(
        name="JARVIS_CONFIDENCE_CAPTURE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 1 master kill switch — DW provider captures "
            "per-token logprobs into ConfidenceCapturer when on. "
            "When off, the request body's logprobs/top_logprobs "
            "params are NOT sent and the capturer is None."
        ),
        category=Category.OBSERVABILITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_capture.py"
        ),
        example="true",
        since="Priority 1 Slice 5 graduation",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_MONITOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 2 master — rolling-window monitor evaluates the "
            "captured margins against the posture-relevant floor. "
            "When off, evaluate() always returns OK (no verdict "
            "transitions, no SSE events, no abort)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_monitor.py"
        ),
        example="true",
        since="Priority 1 Slice 5 graduation",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_MONITOR_ENFORCE",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 2 enforce sub-flag — when on AND BELOW_FLOOR is "
            "observed, the DW stream loop raises "
            "ConfidenceCollapseError mid-stream (caught by existing "
            "RuntimeError handlers; orchestrator routes to GENERATE "
            "retry). When off (hot-revert), the monitor observes + "
            "tags ctx artifacts but does NOT raise."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_monitor.py"
        ),
        example="true",
        since="Priority 1 Slice 5 graduation",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 3 master — probe_confidence_collapse consumer "
            "dispatches HypothesisProbe to map a ConfidenceCollapseError "
            "to one of three actions (RETRY_WITH_FEEDBACK / "
            "ESCALATE_TO_OPERATOR / INCONCLUSIVE). Three-layer flag "
            "gating: this + JARVIS_HYPOTHESIS_CONSUMERS_ENABLED + "
            "JARVIS_HYPOTHESIS_PROBE_ENABLED — any one off → safe "
            "legacy default (RETRY_WITH_FEEDBACK with rendered hint)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "hypothesis_consumers.py"
        ),
        example="true",
        since="Priority 1 Slice 5 graduation",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 4 master — broadcasts confidence verdicts as SSE "
            "events (model_confidence_drop P1 / "
            "model_confidence_approaching P2 / "
            "model_sustained_low_confidence P3 / route_proposal). "
            "Independent from the route advisor flag. When off, all "
            "publish helpers return None immediately — broker is "
            "NOT consulted."
        ),
        category=Category.OBSERVABILITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_observability.py"
        ),
        example="true",
        since="Priority 1 Slice 5 graduation",
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 4 advisor master — propose_route_change emits "
            "advisory RouteProposal events for cost-side route "
            "demotions. Cost contract preservation: even with this "
            "flag on, the AST-pinned guard in _propose_route_change "
            "raises CostContractViolation on any BG/SPEC → STANDARD/"
            "COMPLEX/IMMEDIATE attempt. §26.6 four-layer defense-in-"
            "depth ensures the cost contract holds regardless of "
            "this flag's state."
        ),
        category=Category.ROUTING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_route_advisor.py"
        ),
        example="true",
        since="Priority 1 Slice 5 graduation",
    ),
    FlagSpec(
        name="JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "§26.6.2 Layer 2 — runtime CostContractViolation gate at "
            "ClaudeProvider.generate dispatch boundary. When BG/SPEC "
            "route is dispatched to Claude AND op is not read_only, "
            "raises CostContractViolation (fatal — orchestrator "
            "terminates op as failure_class=cost_contract_violation). "
            "Composes with Layer 1 (AST shipped_code_invariants seeds) "
            "+ Layer 3 (Property Oracle claim) + Layer 4 (Slice 4 "
            "advisor structural guard) for 4-layer defense-in-depth."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/cost_contract_assertion.py"
        ),
        example="true",
        since="§26.6 + Priority 1 Slice 5 graduation",
        posture_relevance=_ALL_POSTURES_CRITICAL,
    ),

    # ====================================================================
    # Priority 2 — Causality DAG + Deterministic Replay — 9 flags
    # Slices 1–6 graduated default-true 2026-04-29 (Slice 6).
    # Cost contract preservation pinned by 4 shipped_code_invariants
    # seeds (causality_dag_no_authority_imports, causality_dag_
    # bounded_traversal, dag_navigation_no_ctx_mutation,
    # dag_replay_cost_contract_preserved).
    # ====================================================================
    FlagSpec(
        name="JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 1 — gates emission of DecisionRecord lineage "
            "fields (parent_record_ids + counterfactual_of) at "
            "write time. When off (hot-revert), record() forces "
            "these to empty/None and the JSONL output is byte-for-"
            "byte identical to pre-Slice-1 ledgers. The READ path "
            "is always tolerant via dataclass defaults."
        ),
        category=Category.OBSERVABILITY,
        source_file=(
            "backend/core/ouroboros/governance/determinism/"
            "decision_runtime.py"
        ),
        example="true",
        since="Priority 2 Slice 6 graduation",
    ),
    FlagSpec(
        name="JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 2 master — gates per-worker ordinal namespace "
            "computation + emission. Fixes L3 fan-out determinism "
            "(W3(6) known debt). When off, the legacy per-(op_id, "
            "phase, kind) ordinal counter is the sole source of "
            "truth and worker_id/sub_ordinal stay at sentinels."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/determinism/"
            "decision_runtime.py"
        ),
        example="true",
        since="Priority 2 Slice 6 graduation",
    ),
    FlagSpec(
        name="JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 2 enforce sub-flag — gates whether per-worker "
            "namespace is AUTHORITATIVE for replay. When off "
            "(hot-revert to shadow), runtime tracks both old + new "
            "ordinal keys but legacy lookup still uses the legacy "
            "key. Mirrors the Priority 1 Slice 5 enforce-flip pattern."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/determinism/"
            "decision_runtime.py"
        ),
        example="true",
        since="Priority 2 Slice 6 graduation",
    ),
    FlagSpec(
        name="JARVIS_CAUSALITY_DAG_QUERY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 3 master gate for DAG construction from the JSONL "
            "decisions ledger. When false (hot-revert), build_dag "
            "returns an empty CausalityDAG immediately — no file I/O, "
            "no parsing. Graduated default-true 2026-04-29."
        ),
        category=Category.OBSERVABILITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "causality_dag.py"
        ),
        example="true",
        since="Priority 2 Slice 6 graduation",
    ),
    FlagSpec(
        name="JARVIS_DAG_MAX_RECORDS",
        type=FlagType.INT, default=100_000,
        description=(
            "Hard cap on records loaded from the JSONL ledger "
            "during build_dag. Prevents unbounded memory on "
            "very large sessions."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "causality_dag.py"
        ),
        example="100000",
        since="Priority 2 Slice 3",
    ),
    FlagSpec(
        name="JARVIS_DAG_MAX_DEPTH",
        type=FlagType.INT, default=8,
        description=(
            "Maximum BFS depth for subgraph extraction. "
            "Prevents unbounded traversal on deep causal chains."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "causality_dag.py"
        ),
        example="8",
        since="Priority 2 Slice 3",
    ),
    FlagSpec(
        name="JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD",
        type=FlagType.FLOAT, default=0.25,
        description=(
            "Node-set delta ratio above which drift is flagged "
            "between two DAGs. 0.25 = 25%% difference."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "causality_dag.py"
        ),
        example="0.25",
        since="Priority 2 Slice 3",
    ),
    FlagSpec(
        name="JARVIS_DAG_NAVIGATION_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 4 master — gates DAG navigation surfaces "
            "(REPL `/postmortems dag` family, IDE GET endpoints, "
            "SSE dag_fork_detected event). Three independent "
            "sub-flags govern individual channels (REPL/GET/SSE) "
            "and default 'on when master is on'. Hot-revert: "
            "explicit false."
        ),
        category=Category.OBSERVABILITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "dag_navigation.py"
        ),
        example="true",
        since="Priority 2 Slice 6 graduation",
    ),
    FlagSpec(
        name="JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 5 gate for --rerun-from record-level fork "
            "replay. Cost contract preservation is structural — "
            "the replay path goes through the existing orchestrator "
            "entry point (no shortcut bypass of the §26.6 four-"
            "layer defense), pinned by the dag_replay_cost_contract"
            "_preserved shipped_code_invariants seed."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "replay_from_record.py"
        ),
        example="true",
        since="Priority 2 Slice 6 graduation",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    # ====================================================================
    # Pass C — Adaptive Anti-Venom (Move 1 graduation 2026-04-29) — 7 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_ADAPTATION_LEDGER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the AdaptationLedger substrate — "
            "JSONL append-only proposal store with monotonic-"
            "tightening invariant. All 6 surface miners short-"
            "circuit when this is off (LEDGER_DISABLED). Asymmetric "
            "env semantics: empty/unset = graduated default-true; "
            "explicit falsy hot-reverts."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/ledger.py"
        ),
        example="true",
        since="Move 1 Pass C cadence",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Surface miner — proposes new SemanticGuardian patterns "
            "from clustered POSTMORTEM events. Monotonic-tightening: "
            "only adds patterns, never weakens existing ones. "
            "Operator approval required via /adapt approve."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "semantic_guardian_miner.py"
        ),
        example="true",
        since="Move 1 Pass C cadence",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Surface miner — proposes raised Iron Gate exploration "
            "floors per category (read_file/search_code/get_callers/"
            "list_dir/glob_files) from clustered bypass-failure "
            "outcomes. Floors only ratchet up, never down."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "exploration_floor_tightener.py"
        ),
        example="true",
        since="Move 1 Pass C cadence",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Surface miner — proposes lowered per-Order mutation "
            "budgets (Order 1 / Order 2) from underutilization "
            "evidence. Monotonic-tightening: budgets only shrink, "
            "never grow."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "per_order_mutation_budget.py"
        ),
        example="true",
        since="Move 1 Pass C cadence",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Surface miner — proposes risk-tier ladder extensions "
            "(new tiers between SAFE_AUTO/NOTIFY_APPLY/APPROVAL_"
            "REQUIRED/BLOCKED) from clustered escalation evidence. "
            "Only adds tiers, never collapses them."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "risk_tier_extender.py"
        ),
        example="true",
        since="Move 1 Pass C cadence",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Surface miner — proposes ExplorationLedger category "
            "weight rebalances from clustered exploration-outcome "
            "evidence. Operator approval gates the apply step."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "category_weight_rebalancer.py"
        ),
        example="true",
        since="Move 1 Pass C cadence",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_ADAPT_REPL_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the /adapt REPL — pending/show/"
            "history/stats/approve/reject subcommands over the "
            "AdaptationLedger. help always works (discoverability) "
            "even master-off. Substrate (LEDGER) master-off short-"
            "circuits read+write subcommands to LEDGER_DISABLED."
        ),
        category=Category.OBSERVABILITY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "meta_governor.py"
        ),
        example="true",
        since="Move 1 Pass C cadence",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    # ====================================================================
    # InvariantDriftAuditor (Move 4 Slice 5 graduation) — 8 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the InvariantDriftAuditor arc. "
            "Graduated 2026-04-30. When false, capture / boot "
            "snapshot / observer / bridge / GET routes all revert "
            "in lockstep. Asymmetric env semantics — empty/unset = "
            "post-graduation default true; explicit `0`/`false` "
            "hot-reverts."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_auditor.py"
        ),
        example="true",
        since="Move 4 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the periodic re-validation observer. "
            "Master must also be on. When false, no observer task "
            "spawns; boot snapshot still happens and GET routes "
            "still serve baseline+history. Allows operators to "
            "disable continuous monitoring without losing the "
            "temporal anchor."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_observer.py"
        ),
        example="true",
        since="Move 4 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the auto_action_router bridge. When "
            "false, drift signals still emit SSE events and append "
            "to history but do NOT translate into AdvisoryAction "
            "proposals in the auto-action ledger. Allows operators "
            "to silence ledger pollution while keeping observability."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_auto_action_bridge.py"
        ),
        example="true",
        since="Move 4 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S",
        type=FlagType.FLOAT, default=600.0,
        description=(
            "Base observer cadence in seconds. Floor 30s. Composes "
            "with posture multiplier × vigilance factor × failure "
            "backoff to compute the actual sleep between cycles."
        ),
        category=Category.TIMING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_observer.py"
        ),
        example="300",
        since="Move 4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_TICKS",
        type=FlagType.INT, default=3,
        description=(
            "Number of subsequent cycles to maintain tightened "
            "cadence after detecting drift. Floor 1."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_observer.py"
        ),
        example="3",
        since="Move 4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_FACTOR",
        type=FlagType.FLOAT, default=0.5,
        description=(
            "Cadence multiplier during vigilance window. 0.5 halves "
            "interval (doubles frequency). Range (0.05, 1.0]."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_observer.py"
        ),
        example="0.5",
        since="Move 4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_OBSERVER_DEDUP_WINDOW",
        type=FlagType.INT, default=5,
        description=(
            "Number of recent drift signatures kept in the dedup "
            "ring. Same signature in N consecutive cycles emits "
            "ONE signal. Floor 1."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_observer.py"
        ),
        example="5",
        since="Move 4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
        type=FlagType.JSON, default="",
        description=(
            "Optional JSON map: posture string → cadence multiplier. "
            "HARDEN tightens (default 0.5), EXPLORE loosens (1.5), "
            "etc. Missing keys fall back to defaults. Malformed "
            "JSON ignored silently."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "invariant_drift_observer.py"
        ),
        example='{"HARDEN": 0.25, "EXPLORE": 2.0}',
        since="Move 4 Slice 5",
        posture_relevance=_ALL_POSTURES_CRITICAL,
    ),
    # ====================================================================
    # ConfidenceProbeBridge (Move 5 Slice 5 graduation) — 6 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the Move 5 Confidence-Aware "
            "Autonomous Probe Loop. Graduated 2026-05-01. When "
            "false, probe_environment_executor falls through to "
            "RETRY_WITH_FEEDBACK safe legacy default; runner is "
            "never invoked. Asymmetric env semantics — empty/unset "
            "= post-graduation default true; explicit `0`/`false` "
            "hot-reverts."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_probe_bridge.py"
        ),
        example="true",
        since="Move 5 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_READONLY_EVIDENCE_PROBER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the read-only EvidenceProber. When false, "
            "ReadonlyEvidenceProber.resolve returns empty answer "
            "(zero cost); convergence detector classifies as "
            "DIVERGED at budget. Master must also be on."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "readonly_evidence_prober.py"
        ),
        example="true",
        since="Move 5 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS",
        type=FlagType.INT, default=3,
        description=(
            "Number of probe questions to generate per ambiguity. "
            "Cap structure: min(ceiling=5, max(floor=2, value)) so "
            "operators cannot loosen below structural floor or "
            "exceed ceiling."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_probe_bridge.py"
        ),
        example="3",
        since="Move 5 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM",
        type=FlagType.INT, default=2,
        description=(
            "Number of agreeing answers required to declare "
            "CONVERGED. Floor 2 (single agreement is meaningless). "
            "When K-1 of K probes agree on canonical answer, "
            "confidence elevated, op proceeds."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_probe_bridge.py"
        ),
        example="2",
        since="Move 5 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S",
        type=FlagType.FLOAT, default=30.0,
        description=(
            "Wall-clock cap for the entire probe loop. Cap "
            "structure: min(120, max(5, value)). Composes with "
            "Phase 7.6's per-probe timeout (each question's tool "
            "rounds inherit their own bound). Hits → cancel "
            "pending, return current verdict."
        ),
        category=Category.TIMING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_probe_runner.py"
        ),
        example="30",
        since="Move 5 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE",
        type=FlagType.STR, default="templates",
        description=(
            "Question generation mode: `templates` (deterministic, "
            "$0 cost — Slice 5 default) or `llm` (auxiliary-model "
            "synthesis — currently falls through to templates with "
            "logged warning; reserved for post-graduation slice)."
        ),
        category=Category.ROUTING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_probe_generator.py"
        ),
        example="templates",
        since="Move 5 Slice 5",
    ),

    # ====================================================================
    # Generative Quorum (Move 6) — 6 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_GENERATIVE_QUORUM_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for K-way Generative Quorum. "
            "Default TRUE post Q4 Priority #1 graduation "
            "(2026-05-02) — operator authorized after empirical "
            "verification that the K× generation cost is structurally "
            "bounded by three downstream gates: (a) "
            "JARVIS_QUORUM_GATE_ENABLED sub-gate, (b) risk-tier "
            "filter (APPROVAL_REQUIRED+ only), (c) COST_GATED_ROUTES "
            "frozenset excluding BACKGROUND/SPECULATIVE. K=3 default "
            "clamped [2, 5]. Set to false for instant rollback."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "generative_quorum.py"
        ),
        example="true",
        since="Move 6 Slice 5 (graduated Q4 P#1, 2026-05-02)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_QUORUM_GATE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the orchestrator hook. When master is "
            "true AND this is true, the gate fires Quorum on "
            "APPROVAL_REQUIRED+ ops on non-cost-gated routes. "
            "Operators may set false to disable invocation while "
            "keeping master on (emergency revert)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "generative_quorum_gate.py"
        ),
        example="true",
        since="Move 6 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_QUORUM_K",
        type=FlagType.INT, default=3,
        description=(
            "Number of candidate rolls per quorum. Cap structure: "
            "min(5, max(2, value)) — single-roll defeats consensus; "
            "ceiling 5 caps cost amplification."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "generative_quorum.py"
        ),
        example="3",
        since="Move 6 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_QUORUM_AGREEMENT_THRESHOLD",
        type=FlagType.INT, default=2,
        description=(
            "Minimum cluster size required to declare "
            "MAJORITY_CONSENSUS. Floor 2 because single-roll "
            "agreement is meaningless — consensus requires at "
            "least two rolls to align."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "generative_quorum.py"
        ),
        example="2",
        since="Move 6 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_AST_CANONICAL_NORMALIZE_LITERALS",
        type=FlagType.BOOL, default=True,
        description=(
            "AST signature normalization for Quorum convergence. "
            "When true (default), literal values (ints/strs/etc) "
            "are replaced with type tags before hashing — Quine-"
            "class invariance. When false, strict literal equality."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "ast_canonical.py"
        ),
        example="true",
        since="Move 6 Slice 2",
    ),
    FlagSpec(
        name="JARVIS_AST_CANONICAL_STRIP_DOCSTRINGS",
        type=FlagType.BOOL, default=False,
        description=(
            "When true, docstrings are stripped from the AST "
            "before hashing. Default false — conservative because "
            "docstring text might be semantically load-bearing. "
            "Enable when models produce different phrasings for "
            "same logic."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "ast_canonical.py"
        ),
        example="false",
        since="Move 6 Slice 2",
    ),

    # ====================================================================
    # Coherence Auditor (Priority #1) — 8 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_COHERENCE_AUDITOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the Long-Horizon Semantic "
            "Coherence Auditor. When false, all 4 slices revert "
            "in lockstep (drift compute → DISABLED, observer "
            "won't start, bridge returns empty). Graduated "
            "default-true post-Slice-5 because auditor is read-"
            "only over existing artifacts (zero LLM cost, zero "
            "K× generation amplification, periodic schedule)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_auditor.py"
        ),
        example="true",
        since="Priority #1 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_COHERENCE_OBSERVER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the periodic async observer task. When "
            "master is true AND this is true, the observer "
            "spawns and runs at posture-aware cadence. Operators "
            "may set false to disable the schedule while keeping "
            "primitive APIs callable for on-demand audits."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_observer.py"
        ),
        example="true",
        since="Priority #1 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the auto_action_router bridge. "
            "Controls whether drift verdicts produce advisory "
            "records under the monotonic-tightening contract "
            "(no auto-flag-flip path; operator approval still "
            "required)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_action_bridge.py"
        ),
        example="true",
        since="Priority #1 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_COHERENCE_WINDOW_HOURS",
        type=FlagType.INT, default=168,
        description=(
            "Bounded coherence window length in hours. Default "
            "168 = 7 days. Cap structure: min(720, max(24, "
            "value)) — ceiling 30 days; floor 24h prevents "
            "degenerate single-cycle comparison."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_window_store.py"
        ),
        example="168",
        since="Priority #1 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_COHERENCE_MAX_SIGNATURES",
        type=FlagType.INT, default=200,
        description=(
            "Bounded ring buffer cap. Read-trim-atomic-write "
            "evicts oldest when count exceeds. Cap structure: "
            "min(5000, max(10, value))."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_window_store.py"
        ),
        example="200",
        since="Priority #1 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT",
        type=FlagType.FLOAT, default=6.0,
        description=(
            "Observer cadence in EXPLORE/CONSOLIDATE/None "
            "postures. HARDEN tightens to 3h via "
            "JARVIS_COHERENCE_CADENCE_HOURS_HARDEN; MAINTAIN "
            "relaxes to 12h."
        ),
        category=Category.TIMING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_observer.py"
        ),
        example="6.0",
        since="Priority #1 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_COHERENCE_HALFLIFE_DAYS",
        type=FlagType.FLOAT, default=14.0,
        description=(
            "Recency-weight halflife for distribution "
            "aggregation. Mirrors SemanticIndex's 14d default "
            "(formula parity pinned by test). Older ops/postures "
            "decay at this halflife within the window."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_auditor.py"
        ),
        example="14.0",
        since="Priority #1 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_COHERENCE_TIGHTEN_FACTOR",
        type=FlagType.FLOAT, default=0.8,
        description=(
            "Default tightening proposer multiplier for numeric "
            "drift kinds. proposed = current × factor (smaller-"
            "is-tighter). 0.8 = 20% reduction. Cap structure: "
            "min(0.95, max(0.5, value)) — floor prevents "
            "catastrophic tightening; ceiling ensures proposals "
            "are minimally tighter."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "coherence_action_bridge.py"
        ),
        example="0.8",
        since="Priority #1 Slice 5",
    ),

    # ====================================================================
    # PostmortemRecall (Priority #2) — 6 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_POSTMORTEM_RECALL_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for cross-session "
            "PostmortemRecall. When false, the entire 4-slice "
            "pipeline reverts in lockstep (recall → DISABLED, "
            "index writes → FAILED, injector → empty string, "
            "boost → empty). Graduated default-true post-Slice-5 "
            "because PostmortemRecall is read-only over existing "
            "artifacts (zero LLM cost, runs at CONTEXT_EXPANSION "
            "not per-LLM-call, advisory-only output). Operator "
            "approval still required for any actual flag flip "
            "downstream via MetaAdaptationGovernor."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "postmortem_recall.py"
        ),
        example="true",
        since="Priority #2 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_POSTMORTEM_INDEX_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the cross-session index store. "
            "Controls whether rebuild_index_from_sessions and "
            "record_postmortem actually write."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "postmortem_recall_index.py"
        ),
        example="true",
        since="Priority #2 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_POSTMORTEM_INJECTION_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the CONTEXT_EXPANSION injection. When "
            "false, render_postmortem_recall_section returns "
            "empty string (load-bearing robust degradation: "
            "GENERATE pipeline NEVER affected by recall failure)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "postmortem_recall_injector.py"
        ),
        example="true",
        since="Priority #2 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the recurrence consumer (activates "
            "Priority #1 Slice 4's INJECT_POSTMORTEM_RECALL_HINT "
            "advisory). When detected, extends recall budget "
            "for the next-N-ops on the matched failure_class — "
            "biases the model toward prior remediation patterns "
            "and away from the recurring failure mode."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "postmortem_recall_consumer.py"
        ),
        example="true",
        since="Priority #2 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_POSTMORTEM_RECALL_TOP_K",
        type=FlagType.INT, default=3,
        description=(
            "Default top-K records returned per recall. Cap "
            "structure: min(10, max(1, value)). Slice 4's "
            "recurrence boost can extend up to "
            "JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "postmortem_recall.py"
        ),
        example="3",
        since="Priority #2 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS",
        type=FlagType.INT, default=30,
        description=(
            "Records older than this age (computed at recall "
            "time vs timestamp field) are excluded from results. "
            "Cap structure: min(365, max(1, value)). Stale "
            "postmortems shouldn't bias new ops indefinitely."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "postmortem_recall.py"
        ),
        example="30",
        since="Priority #2 Slice 5",
    ),

    # ====================================================================
    # Counterfactual Replay (Priority #3) — 6 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_COUNTERFACTUAL_REPLAY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the 4-slice Counterfactual "
            "Replay pipeline. When false, every public path "
            "short-circuits in lockstep (engine → DISABLED, "
            "comparator → DISABLED, observer → DISABLED). "
            "Graduated default-true post-Slice-5 (2026-05-02) "
            "because replay is read-only over cached artifacts "
            "(zero LLM cost by AST-pinned construction; every "
            "verdict stamps MonotonicTighteningVerdict.PASSED — "
            "observational not prescriptive). Operator approval "
            "still required for any downstream flag-flip proposal "
            "via MetaAdaptationGovernor."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "counterfactual_replay.py"
        ),
        example="true",
        since="Priority #3 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_REPLAY_ENGINE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 2 engine's loader path. When "
            "false, run_counterfactual_replay returns DISABLED "
            "with zero I/O — the Slice 1 schema stays live in "
            "serialization paths, but no engine activity. Hot-"
            "revert knob for cost-cap rollback without breaking "
            "the rest of the stack."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "counterfactual_replay_engine.py"
        ),
        example="true",
        since="Priority #3 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_REPLAY_COMPARATOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 3 aggregator. When false, "
            "compare_replay_history returns DISABLED. The "
            "stamping logic (MonotonicTighteningVerdict.PASSED) "
            "remains structurally accessible via stamp_verdict "
            "for callers that want per-verdict stamping without "
            "aggregation."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "counterfactual_replay_comparator.py"
        ),
        example="true",
        since="Priority #3 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_REPLAY_OBSERVER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 4 history store + SSE event "
            "publisher + periodic ReplayObserver. When false, "
            "record_replay_verdict returns DISABLED, no JSONL "
            "writes, no SSE events. Operators rolling back to a "
            "no-persistence stance flip this without affecting "
            "engine/comparator behavior."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "counterfactual_replay_observer.py"
        ),
        example="true",
        since="Priority #3 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT",
        type=FlagType.FLOAT, default=50.0,
        description=(
            "Minimum recurrence-reduction-pct (over actionable "
            "verdicts) for ComparisonOutcome.ESTABLISHED. Default "
            "50.0%. Cap structure: max(0.0, min(100.0, value)). "
            "Operators tighten upward (e.g., 75.0) to demand "
            "stronger empirical evidence before claiming the "
            "policy under test prevents recurrence."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "counterfactual_replay_comparator.py"
        ),
        example="50.0",
        since="Priority #3 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_REPLAY_HISTORY_MAX_RECORDS",
        type=FlagType.INT, default=1000,
        description=(
            "Bounded ring-buffer cap for the JSONL history store. "
            "Default 1000 records, clamped [10, 100000]. Rotation "
            "truncates to this size after each append (same "
            "discipline as InvariantDriftStore). Larger caps "
            "support longer-baseline empirical claims at the cost "
            "of more disk + slower full-history reads."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "counterfactual_replay_observer.py"
        ),
        example="1000",
        since="Priority #3 Slice 5",
    ),

    # ====================================================================
    # Speculative Branch Tree (Priority #4) — 6 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_SBT_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the 4-slice Speculative Branch "
            "Tree pipeline. When false, every public path "
            "short-circuits in lockstep (runner → FAILED, "
            "comparator → DISABLED, observer → DISABLED). "
            "Graduated default-true post-Slice-5 (2026-05-02) "
            "because SBT is read-only over typed evidence by "
            "AST-pinned construction (every verdict stamps "
            "MonotonicTighteningVerdict.PASSED — observational not "
            "prescriptive). Closes CC's interleaved-thinking + "
            "plan-mode-replan + speculative-branching cognitive "
            "paradigm via Antivenom-aligned tree topology."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "speculative_branch.py"
        ),
        example="true",
        since="Priority #4 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_SBT_RUNNER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 2 async tree executor. When "
            "false, run_speculative_tree returns FAILED with zero "
            "I/O — the Slice 1 schema stays live in serialization "
            "paths, but no branch execution. Hot-revert knob for "
            "cost-cap rollback without breaking the rest of the "
            "stack."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "speculative_branch_runner.py"
        ),
        example="true",
        since="Priority #4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_SBT_COMPARATOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 3 effectiveness aggregator. "
            "When false, compare_tree_history returns DISABLED. "
            "The stamping logic (MonotonicTighteningVerdict.PASSED) "
            "remains structurally accessible via stamp_tree_verdict "
            "for callers that want per-verdict stamping without "
            "aggregation."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "speculative_branch_comparator.py"
        ),
        example="true",
        since="Priority #4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_SBT_OBSERVER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 4 history store + SSE event "
            "publisher + periodic SBTObserver. When false, "
            "record_tree_verdict returns DISABLED, no JSONL "
            "writes, no SSE events. Operators rolling back to a "
            "no-persistence stance flip this without affecting "
            "runner/comparator behavior."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "speculative_branch_observer.py"
        ),
        example="true",
        since="Priority #4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_SBT_RESOLUTION_THRESHOLD_PCT",
        type=FlagType.FLOAT, default=50.0,
        description=(
            "Minimum ambiguity-resolution-pct (CONVERGED / "
            "actionable_total) for "
            "EffectivenessOutcome.ESTABLISHED. Default 50.0%. "
            "Cap structure: max(0.0, min(100.0, value)). Operators "
            "tighten upward (e.g., 75.0) to demand stronger "
            "empirical evidence before claiming SBT resolves "
            "ambiguity effectively."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "speculative_branch_comparator.py"
        ),
        example="50.0",
        since="Priority #4 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_SBT_HISTORY_MAX_RECORDS",
        type=FlagType.INT, default=1000,
        description=(
            "Bounded ring-buffer cap for the SBT JSONL history "
            "store. Default 1000 records, clamped [10, 100000]. "
            "Rotation truncates after each append (same discipline "
            "as InvariantDriftStore + Priority #3 Slice 4 "
            "observer). Larger caps support longer-baseline "
            "empirical claims at the cost of more disk + slower "
            "full-history reads."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "speculative_branch_observer.py"
        ),
        example="1000",
        since="Priority #4 Slice 5",
    ),

    # ====================================================================
    # Continuous Invariant Gradient Watcher (Priority #5) — 6 flags
    # ====================================================================
    FlagSpec(
        name="JARVIS_CIGW_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the 4-slice CIGW pipeline. "
            "When false, every public path short-circuits in "
            "lockstep (collector → empty, comparator → DISABLED, "
            "observer → DISABLED). Graduated default-true post-"
            "Slice-5 (2026-05-02) because CIGW is read-only over "
            "source files (zero LLM cost on detection path; "
            "structural metrics via stdlib ast + file.read; "
            "observational not prescriptive — every reading stamps "
            "PASSED). Closes the long-horizon semantic drift gap: "
            "per-APPLY structural metric sampling vs Move 4's "
            "per-snapshot."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "gradient_watcher.py"
        ),
        example="true",
        since="Priority #5 Slice 5",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_CIGW_COLLECTOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 2 async collector. When false, "
            "sample_target / sample_targets / sample_on_apply all "
            "return empty tuple — the Slice 1 schema stays live "
            "in serialization paths, but no metric collection. "
            "Hot-revert knob for cost-cap rollback without breaking "
            "the rest of the stack."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "gradient_collector.py"
        ),
        example="true",
        since="Priority #5 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_CIGW_COMPARATOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 3 effectiveness aggregator. "
            "When false, compare_gradient_history returns DISABLED. "
            "The stamping logic (MonotonicTighteningVerdict.PASSED) "
            "remains structurally accessible via stamp_gradient_"
            "report for callers that want per-report stamping "
            "without aggregation."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "gradient_comparator.py"
        ),
        example="true",
        since="Priority #5 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_CIGW_OBSERVER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Sub-gate for the Slice 4 history store + SSE event "
            "publisher + periodic CIGWObserver. When false, "
            "record_gradient_report returns DISABLED, no JSONL "
            "writes, no SSE events. Operators rolling back to a "
            "no-persistence stance flip this without affecting "
            "collector/comparator behavior."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "gradient_observer.py"
        ),
        example="true",
        since="Priority #5 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_CIGW_HEALTHY_THRESHOLD_PCT",
        type=FlagType.FLOAT, default=80.0,
        description=(
            "Minimum stable_rate (STABLE_count / actionable_total) "
            "for CIGWEffectivenessOutcome.HEALTHY. Default 80.0%. "
            "Cap structure: max(0.0, min(100.0, value)). Operators "
            "tighten upward (e.g., 95.0) to demand near-zero drift "
            "before claiming the codebase is structurally healthy. "
            "Note: ANY breach takes precedence over HEALTHY "
            "regardless of stable_rate (DEGRADED safer-default "
            "discipline)."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "gradient_comparator.py"
        ),
        example="80.0",
        since="Priority #5 Slice 5",
    ),
    FlagSpec(
        name="JARVIS_CIGW_HISTORY_MAX_RECORDS",
        type=FlagType.INT, default=1000,
        description=(
            "Bounded ring-buffer cap for the CIGW JSONL history "
            "store. Default 1000 records, clamped [10, 100000]. "
            "Rotation truncates after each append (same discipline "
            "as InvariantDriftStore + Priority #3/#4 Slice 4 "
            "observers). Larger caps support longer-baseline "
            "empirical claims at the cost of more disk + slower "
            "full-history reads."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "gradient_observer.py"
        ),
        example="1000",
        since="Priority #5 Slice 5",
    ),
    # ====================================================================
    # Deep Observability Gap #2 — Confidence Threshold Tuner (5 flags)
    # ====================================================================
    FlagSpec(
        name="JARVIS_CONFIDENCE_POLICY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Substrate master for the Confidence Threshold Tuner "
            "arc (Gap #2 Slice 1). Read-only over policies; needed "
            "by every consumer (Slice 2 validator, Slice 4 router). "
            "Graduated default-true 2026-05-02 (Slice 5)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "confidence_policy.py"
        ),
        example="true",
        since="Gap #2 Slice 1 (graduated Slice 5)",
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_LOAD_ADAPTED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 3 boot-time YAML loader master. When on, "
            "ConfidenceMonitor accessors consult the loader for "
            "operator-approved tightenings when env unset (env "
            "explicit > adapted YAML > hardcoded default). The "
            "tighten-only filter is structurally safe by "
            "construction. Graduated default-true 2026-05-02 "
            "(Slice 5)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "adapted_confidence_loader.py"
        ),
        example="true",
        since="Gap #2 Slice 3 (graduated Slice 5)",
    ),
    FlagSpec(
        name="JARVIS_IDE_POLICY_ROUTER_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 4 HTTP write authority surface (POST "
            "/policy/confidence/proposals + approve + reject + GET "
            "/policy/confidence). Loopback-only + per-IP "
            "rate-limited + cage-validator-gated. Mirror of "
            "JARVIS_IDE_OBSERVABILITY_ENABLED discipline. "
            "Graduated default-true 2026-05-02 (Slice 5)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/ide_policy_router.py"
        ),
        example="true",
        since="Gap #2 Slice 4 (graduated Slice 5)",
    ),
    FlagSpec(
        name="JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR",
        type=FlagType.INT, default=3,
        description=(
            "Minimum number of supporting observations a "
            "confidence-policy proposal MUST cite in "
            "evidence.observation_count to clear the surface "
            "validator (Slice 2). Operator-tunable; stricter "
            "(higher) requires more evidence per proposal."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/adaptation/"
            "confidence_threshold_tightener.py"
        ),
        example="3",
        since="Gap #2 Slice 2",
    ),
    FlagSpec(
        name="JARVIS_IDE_POLICY_ROUTER_RATE_LIMIT_PER_MIN",
        type=FlagType.INT, default=30,
        description=(
            "Max writes / minute / client key on the policy router. "
            "Lower than the read surface's 120/min by design (writes "
            "are more consequential)."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/ide_policy_router.py"
        ),
        example="30",
        since="Gap #2 Slice 4",
    ),
    # ====================================================================
    # Deep Observability Gap #3 — Worktree Topology View (2 flags)
    # ====================================================================
    FlagSpec(
        name="JARVIS_WORKTREE_TOPOLOGY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Substrate master for the L3 worktree topology read view "
            "(Gap #3 Slice 1). Pure read-only projection over "
            "SubagentScheduler in-memory state + caller-supplied git "
            "worktree paths; structurally safe to enable by default. "
            "Graduated default-true 2026-05-02 (Slice 5)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "worktree_topology.py"
        ),
        example="true",
        since="Gap #3 Slice 1 (graduated Slice 5)",
    ),
    FlagSpec(
        name="JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "SSE bridge master for the L3 worktree topology stream "
            "(Gap #3 Slice 3). Pure translator (autonomy "
            "EventEmitter → IDE StreamEventBroker), zero scheduler "
            "modification; handlers are fault-isolated by autonomy "
            "AND defense-in-depth try/except in each handler body. "
            "Graduated default-true 2026-05-02 (Slice 5)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "worktree_topology_sse_bridge.py"
        ),
        example="true",
        since="Gap #3 Slice 3 (graduated Slice 5)",
    ),

    # ====================================================================
    # RSI Pass B (Tier 3 #7) Graduation — 2026-05-03
    # ====================================================================
    # The Order-2 governance amendment protocol — 6 slices structurally
    # complete since 2026-04-26 (PRs #22298 → #22517+); 438/438 Pass B
    # regression suite green. This graduation arc:
    #   * Seeds all 8 Pass B master flags + 4 path config knobs into
    #     the FlagRegistry so they become /help flags discoverable +
    #     typo-detectable + posture-relevance-tagged.
    #   * Flips 6 read-only/observational/operator-surface flags
    #     default-true. The substrate is structurally complete; the
    #     amendment_requires_operator() invariant remains locked-true
    #     as the cost-contract cage; AST-pinned cross-file in
    #     order2_review_queue.register_shipped_invariants().
    #   * Keeps 2 write-path flags (META_PHASE_RUNNER + REPLAY_EXECUTOR)
    #     EXPLICITLY default-false. These are the autonomy-creation +
    #     mutation-execution surfaces; graduating them requires
    #     empirical soak validation that wasn't done. Operators flip
    #     them after 3-clean-session arcs per the W2(5) policy.
    FlagSpec(
        name="JARVIS_ORDER2_MANIFEST_LOADED",
        type=FlagType.BOOL, default=True,
        description=(
            "Pass B Slice 1 — load the Order-2 manifest at boot. "
            "Read-only (the manifest is a YAML allowlist of "
            "governance-code paths). Graduated default-true "
            "2026-05-03 — gates only manifest loading, not any "
            "mutation surface. Off → empty manifest, every "
            "classification falls back to ORDER_1 (Body code)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/order2_manifest.py"
        ),
        example="true",
        since="Pass B Slice 1 (graduated 2026-05-03)",
    ),
    FlagSpec(
        name="JARVIS_ORDER2_MANIFEST_PATH",
        type=FlagType.STR, default=".jarvis/order2_manifest.yaml",
        description=(
            "Filesystem path to the Order-2 manifest YAML. Defaults "
            "to <project_root>/.jarvis/order2_manifest.yaml. Operators "
            "override for split repos or alternate manifest layouts."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/meta/order2_manifest.py"
        ),
        example=".jarvis/order2_manifest.yaml",
        since="Pass B Slice 1",
    ),
    FlagSpec(
        name="JARVIS_ORDER2_RISK_CLASS_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Pass B Slice 2 — surface ORDER_2_GOVERNANCE risk class "
            "for manifest-matched paths. Advisory enrichment; the "
            "actual risk-floor application happens downstream in "
            "risk_engine and is independently flag-gated. Graduated "
            "default-true 2026-05-03."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/order2_classifier.py"
        ),
        example="true",
        since="Pass B Slice 2 (graduated 2026-05-03)",
    ),
    FlagSpec(
        name="JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Pass B Slice 3 — AST-shape validator for proposed "
            "PhaseRunner subclasses. Read-only static analysis "
            "(6 rules: ABC inheritance, no I/O in __init__, "
            "no exec/eval/compile, etc). Graduated default-true "
            "2026-05-03 — gates only validation, not patch "
            "application."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/"
            "ast_phase_runner_validator.py"
        ),
        example="true",
        since="Pass B Slice 3 (graduated 2026-05-03)",
    ),
    FlagSpec(
        name="JARVIS_SHADOW_PIPELINE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Pass B Slice 4 — shadow-pipeline replay for proposed "
            "PhaseRunner subclasses. Observational only: replays "
            "the golden corpus through the proposed runner in a "
            "sandbox + compares output against the production "
            "runner. Graduated default-true 2026-05-03."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/shadow_replay.py"
        ),
        example="true",
        since="Pass B Slice 4 (graduated 2026-05-03)",
    ),
    FlagSpec(
        name="JARVIS_SHADOW_REPLAY_CORPUS_PATH",
        type=FlagType.STR, default=".jarvis/shadow_replay_corpus",
        description=(
            "Filesystem path to the golden-replay corpus directory. "
            "Defaults to <project_root>/.jarvis/shadow_replay_corpus."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/meta/shadow_replay.py"
        ),
        example=".jarvis/shadow_replay_corpus",
        since="Pass B Slice 4",
    ),
    FlagSpec(
        name="JARVIS_META_PHASE_RUNNER_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Pass B Slice 5 — MetaPhaseRunner. THE autonomy-creation "
            "surface: when on, O+V can PROPOSE new PhaseRunner "
            "subclasses (proposals still flow through the AST "
            "validator + shadow replay + review queue + REPL "
            "approval before any execution). Graduation deliberately "
            "DEFERRED — defaults stay false until empirical soak "
            "validation per the W2(5) policy. Operators opt in "
            "after a 3-clean-session arc."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/"
            "meta_phase_runner.py"
        ),
        example="true",
        since="Pass B Slice 5 (default false pre-soak)",
    ),
    FlagSpec(
        name="JARVIS_REPLAY_EXECUTOR_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Pass B Slice 6.1 — sandboxed replay executor. THE "
            "actual mutation surface: when on AND the caller passes "
            "operator_authorized=True (only the /order2 amend REPL "
            "command does so), proposed PhaseRunner subclasses are "
            "compiled + applied. The amendment_requires_operator() "
            "cage is locked-true at the AST-pin layer — but the "
            "flag itself stays default-FALSE pending empirical soak "
            "validation. Defense-in-depth: flag + operator-cage."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/replay_executor.py"
        ),
        example="true",
        since="Pass B Slice 6.1 (default false pre-soak)",
    ),
    FlagSpec(
        name="JARVIS_ORDER2_REVIEW_QUEUE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Pass B Slice 6.2 — manifest amendment review queue. "
            "Operator surface: queues proposed amendments for "
            "operator inspection via /order2 pending. Mutation is "
            "structurally gated by amendment_requires_operator() "
            "(locked-true cage). Graduated default-true 2026-05-03."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/"
            "order2_review_queue.py"
        ),
        example="true",
        since="Pass B Slice 6.2 (graduated 2026-05-03)",
    ),
    FlagSpec(
        name="JARVIS_ORDER2_REVIEW_QUEUE_PATH",
        type=FlagType.STR, default=".jarvis/order2_review_queue.jsonl",
        description=(
            "Filesystem path to the flock'd review-queue JSONL. "
            "Defaults to <project_root>/.jarvis/"
            "order2_review_queue.jsonl."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/meta/"
            "order2_review_queue.py"
        ),
        example=".jarvis/order2_review_queue.jsonl",
        since="Pass B Slice 6.2",
    ),
    FlagSpec(
        name="JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR",
        type=FlagType.BOOL, default=True,
        description=(
            "Pass B cost-contract cage: the load-bearing invariant "
            "that locks manifest amendments to operator-only paths. "
            "AST-pinned LOCKED-TRUE in "
            "order2_review_queue.register_shipped_invariants() — "
            "operators CAN read this flag's value but flipping it "
            "false fires a structural pin violation at next "
            "graduation gate scan. Documented here for "
            "discoverability; the lock is structural."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/"
            "order2_review_queue.py"
        ),
        example="true",
        since="Pass B Slice 6.2 (locked-true cage invariant)",
    ),
    FlagSpec(
        name="JARVIS_ORDER2_REPL_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Pass B Slice 6.3 — /order2 REPL dispatcher (pending, "
            "show, amend, reject, history, help). Operator surface; "
            "/order2 amend is THE only caller in O+V that passes "
            "operator_authorized=True to the replay executor — but "
            "execution is independently gated by "
            "JARVIS_REPLAY_EXECUTOR_ENABLED (default-false). "
            "Graduated default-true 2026-05-03 — operators get the "
            "REPL discoverability without any auto-mutation surface."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/"
            "order2_repl_dispatcher.py"
        ),
        example="true",
        since="Pass B Slice 6.3 (graduated 2026-05-03)",
    ),

    # ====================================================================
    # auto_action_router VERIFY-hook flags (Tier 2 #6 follow-up Arc 1+4)
    # ====================================================================
    # Two flags landed during the Production Oracle → auto_action_router
    # VERIFY wiring arc but were not yet seeded into the centralized
    # FlagRegistry. Arc 4 closes that discoverability gap so operators
    # see them in /help flags, get typo detection, and have posture-
    # relevance tags for HARDEN-mode soaks where production-reality
    # vetoes are most operationally relevant.
    FlagSpec(
        name="JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the Production Oracle veto rule in "
            "auto_action_router (Rule 1.5). When on, a recent oracle "
            "observation with verdict=FAILED proposes "
            "ROUTE_TO_NOTIFY_APPLY (or DEMOTE_RISK_TIER for "
            "SAFE_AUTO ops); verdict=DEGRADED proposes "
            "RAISE_EXPLORATION_FLOOR. Authority-free advisory; "
            "production reality wins over internal observability. "
            "Graduated default-true 2026-05-03."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/auto_action_router.py"
        ),
        example="true",
        since="Tier 2 #6 follow-up Arc 1 (graduated 2026-05-03)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_AUTO_ACTION_VERIFY_HOOK_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Orchestrator VERIFY-phase hook for auto_action_router. "
            "When on, every successful VERIFY phase calls "
            "gather_context(include_oracle=True) → "
            "propose_advisory_action(); non-NO_ACTION proposals "
            "log + emit SSE auto_action_proposal. ADVISORY ONLY -- "
            "never blocks COMPLETE; auto-apply requires the "
            "separate JARVIS_AUTO_ACTION_ENFORCE flag (default-"
            "false, the only state shipped). Graduated default-"
            "true 2026-05-03."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/orchestrator.py"
        ),
        example="true",
        since="Tier 2 #6 follow-up Arc 1 (graduated 2026-05-03)",
        posture_relevance=_ALL_POSTURES_CRITICAL,
    ),

    # ====================================================================
    # WallClockWatchdog Defect #1 fix (2026-05-03)
    # ====================================================================
    # Soak v5 (bt-2026-05-03-060330) fired the wall-clock watchdog
    # 22 minutes AFTER the cap was hit -- the original implementation
    # used a single ``asyncio.sleep(cap_s)`` for the entire duration
    # which is vulnerable to event-loop starvation by long-running
    # coroutines doing blocking I/O. The fix: periodic check loop
    # using monotonic clock + parallel thread-based hard-deadline
    # safety net. Two env knobs control the behavior.
    FlagSpec(
        name="JARVIS_WALL_CLOCK_CHECK_INTERVAL_S",
        type=FlagType.FLOAT, default=5.0,
        description=(
            "Periodic-check tick for the WallClockWatchdog asyncio "
            "loop. Floor 1.0s (avoid busy-loop), ceiling 60.0s "
            "(cap fire delay under sane configs). Default 5s gives "
            "<=5s overshoot under normal asyncio scheduling. Lower "
            "values give tighter fire timing at modest CPU cost. "
            "Defect #1 fix (2026-05-03): replaces the original "
            "single-asyncio.sleep(cap_s) pattern that fired 22 min "
            "late in soak v5."
        ),
        category=Category.TIMING,
        source_file=(
            "backend/core/ouroboros/battle_test/harness.py"
        ),
        example="JARVIS_WALL_CLOCK_CHECK_INTERVAL_S=2.0",
        since="Defect #1 fix (2026-05-03)",
    ),
    FlagSpec(
        name="JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S",
        type=FlagType.FLOAT, default=30.0,
        description=(
            "Grace window after max_wall_seconds before the thread-"
            "based hard-deadline safety net fires. The asyncio path "
            "fires at cap_s; the thread fires at cap_s + grace. "
            "Under normal conditions the asyncio path always wins "
            "first. The thread is the backstop for cases where "
            "the asyncio loop is wedged (the soak v5 pathology). "
            "Floor 5s, ceiling 600s. Default 30s aligns with "
            "BoundedShutdownWatchdog's 30s grace."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/battle_test/harness.py"
        ),
        example="JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S=60.0",
        since="Defect #1 fix (2026-05-03)",
    ),

    # ====================================================================
    # CandidateGenerator Defect #4 fix (2026-05-03)
    # ====================================================================
    # Soak v5 saw 3 EXHAUSTION events with remaining_s=0.0 + 4 unhandled
    # asyncio task exceptions. Fix: pre-fallback budget short-circuit
    # raises a clean cause when remaining budget < min_viable, instead
    # of attempting a doomed fallback call that gets CancelledError'd
    # mid-flight. Slice A's task-leak callback consumes any straggler
    # exceptions from shielded background tasks.
    FlagSpec(
        name="JARVIS_FALLBACK_MIN_VIABLE_BUDGET_S",
        type=FlagType.FLOAT, default=5.0,
        description=(
            "Pre-fallback budget short-circuit threshold. When "
            "remaining deadline budget at _call_fallback entry is "
            "less than this many seconds, raise "
            "deadline_exhausted_pre_fallback (clean cause) instead "
            "of attempting a fallback call that will be CancelledError'd "
            "mid-flight. Floor 1s, ceiling 60s. Default 5s -- safer "
            "to skip than to attempt a doomed call. Soak v5's 3 "
            "EXHAUSTION events with remaining_s=0.0 + the 4 "
            "unhandled asyncio task exceptions pattern is "
            "structurally fixed by this short-circuit (Defect #4 "
            "Slice B 2026-05-03)."
        ),
        category=Category.TIMING,
        source_file=(
            "backend/core/ouroboros/governance/candidate_generator.py"
        ),
        example="JARVIS_FALLBACK_MIN_VIABLE_BUDGET_S=10.0",
        since="Defect #4 fix (2026-05-03)",
    ),

    # ========================================================================
    # Upgrade 3 — Failure-Mode Memory at GENERATE (PRD §31.4) — 5 flags
    # Slice 5 graduation: master flips false → true; 4 supporting knobs
    # surfaced for operator visibility into the recurrence-recall loop.
    # ========================================================================
    FlagSpec(
        name="JARVIS_FAILURE_MODE_MEMORY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the Failure-Mode Memory subsystem. "
            "Default TRUE post Slice 5 graduation (2026-05-04). "
            "Pure-RAG: extractor uses regex over postmortem evidence "
            "(zero LLM); retriever is deterministic enum-match + "
            "Jaccard + log-scale weight + 14d half-life recency; "
            "injection is markdown render with 3KB budget cap "
            "amortized by Anthropic prompt cache. Per-op cost is "
            "<= 3KB prompt bytes. Operator instant-revert via "
            "explicit env false."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/failure_mode_memory.py"
        ),
        example="true",
        since="Upgrade 3 Slice 5 (graduated PRD §31.4, 2026-05-04)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_FAILURE_MODE_HISTORY_MAX_RECORDS",
        type=FlagType.INT, default=5000,
        description=(
            "Bounded ring-buffer cap for the JSONL store at "
            ".jarvis/failure_mode_memory/failure_modes.jsonl. "
            "Default 5000 (~300KB at ~60B/record per PRD §31.4.3 "
            "cost contract). Clamped [50, 100000]. Truncation is "
            "tail-keep-most-recent under flock'd critical section."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/failure_mode_memory.py"
        ),
        example="5000",
        since="Upgrade 3 Slice 2 (2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_FAILURE_MODE_DEDUP_WINDOW_DAYS",
        type=FlagType.INT, default=30,
        description=(
            "Recurrence dedup window. Records sharing a "
            "signature_hash within this window are merged "
            "(weight++) instead of appended. Default 30 (PRD "
            "§31.4.6). Clamped [1, 365]. The min-weight=2 "
            "first-attempt-injection gate (memory pollution "
            "defense) operates within this window."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/failure_mode_memory.py"
        ),
        example="30",
        since="Upgrade 3 Slice 2 (2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_FAILURE_MODE_TOP_K",
        type=FlagType.INT, default=3,
        description=(
            "Maximum number of matches the retriever returns at "
            "first-attempt GENERATE. PRD §31.4.6 default 3 — "
            "diversity-deduped per Coherence Auditor pattern "
            "(at most one match per attempted_action_kind). "
            "Clamped [1, 10]. Higher values inflate the prompt; "
            "lower values reduce diversity coverage."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/failure_mode_memory.py"
        ),
        example="3",
        since="Upgrade 3 Slice 3 (2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_FAILURE_MODE_MIN_WEIGHT",
        type=FlagType.INT, default=2,
        description=(
            "Memory pollution defense (PRD §31.4.6). Records with "
            "``weight < N`` are filtered before retrieval — only "
            "signatures that have recurred at least N times in "
            "the dedup window are eligible for first-attempt "
            "prompt injection. Default 2; one-off failures stay "
            "in retry-context recall (PostmortemRecall) but never "
            "pollute first-attempt prompts. Clamped [1, 100]."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/failure_mode_memory.py"
        ),
        example="2",
        since="Upgrade 3 Slice 3 (2026-05-04)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),

    # ========================================================================
    # M11 — ActionOutcomeMemory at GENERATE (PRD §30.5.3) — 5 flags
    # Slice 5 graduation: master flips false → true; symmetric positive-
    # evidence pair to Upgrade 3. Closes the in-context embodiment ASCO axis.
    # ========================================================================
    FlagSpec(
        name="JARVIS_ACTION_OUTCOME_MEMORY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the ActionOutcomeMemory subsystem. "
            "Default TRUE post Slice 5 graduation (2026-05-04). "
            "Symmetric positive-evidence pair to Upgrade 3's "
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED. Pure-RAG: per-"
            "cluster JSONL persistence (Decision A3 SemanticIndex-"
            "optional with global-fallback graceful degradation); "
            "deterministic enum-match + Jaccard + log-scale weight "
            "+ 14d half-life recency + outcome-polarity scoring; "
            "markdown-render injection with 4KB budget cap. "
            "Per-op cost is ≤4KB prompt bytes. Operator instant-"
            "revert via explicit env false."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/action_outcome_memory.py"
        ),
        example="true",
        since="M11 Slice 5 (graduated PRD §30.5.3, 2026-05-04)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER",
        type=FlagType.INT, default=1000,
        description=(
            "Bounded ring-buffer cap PER cluster JSONL file under "
            ".jarvis/action_outcomes/{cluster_id}.jsonl. Default "
            "1000 (PRD §30.5.3 storage estimate: 50 clusters × "
            "1000 records × 500B ≈ 25MB total). Clamped "
            "[50, 100000]. Truncation is tail-keep-most-recent "
            "under flock'd critical section."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/action_outcome_memory.py"
        ),
        example="1000",
        since="M11 Slice 2 (2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_ACTION_OUTCOME_DEDUP_WINDOW_DAYS",
        type=FlagType.INT, default=30,
        description=(
            "Recurrence dedup window. Records sharing a "
            "signature within this window merge (weight++). "
            "Outcome is part of the dedup tuple, so two records "
            "with same situation+region+attempt but DIFFERENT "
            "outcomes coexist (M11 distinction from Upgrade 3). "
            "Default 30 (parity with Upgrade 3). Clamped "
            "[1, 365]."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/action_outcome_memory.py"
        ),
        example="30",
        since="M11 Slice 2 (2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_ACTION_OUTCOME_TOP_K",
        type=FlagType.INT, default=3,
        description=(
            "Maximum number of matches the retriever returns at "
            "first-attempt GENERATE. PRD §30.5.3 default 3 — "
            "diversity-deduped on outcome_kind so the model gets "
            "a balanced palette (one VERIFIED + one REVERTED + "
            "one REJECTED rather than three VERIFIED). Clamped "
            "[1, 10]. Higher inflates the prompt; lower reduces "
            "outcome-disposition diversity."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/action_outcome_memory.py"
        ),
        example="3",
        since="M11 Slice 3 (2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_ACTION_OUTCOME_POLARITY_MODE",
        type=FlagType.STR, default="balanced",
        description=(
            "Closed-set preset selector for outcome-polarity "
            "weighting in retrieval scoring. Three modes: "
            "``balanced`` (default — VERIFIED=1.0, REVERTED=0.7, "
            "REJECTED=0.5, DEFERRED=0.3); ``favor_positive`` "
            "(wider gap — VERIFIED=1.0, REVERTED=0.5, "
            "REJECTED=0.3, DEFERRED=0.2); ``all_equal`` (4 "
            "actionable kinds = 1.0; only DISABLED = 0.0). "
            "Unknown values fall back to ``balanced``. Polarity "
            "RANKING is a semantic choice, not a tunable "
            "threshold — preset modes encode operator intent at "
            "appropriate granularity."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/action_outcome_memory.py"
        ),
        example="balanced",
        since="M11 Slice 3 (2026-05-04)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    # ========================================================================
    # Upgrade 1 — Bounded Epistemic Loop (PRD §31.2) — 5 flags
    # Slice 5 graduation: master flips false → true. Composes Confidence-
    # Monitor + ConfidenceProbeRunner + HypothesisProbe + SBT + RiskTier-
    # Floor + tool_executor as a glue arc; one authoritative per-op budget
    # consulted at every Venom tool-round boundary.
    # ========================================================================
    FlagSpec(
        name="JARVIS_EPISTEMIC_BUDGET_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the Bounded Epistemic Loop "
            "subsystem. Default TRUE post Slice 5 graduation "
            "(2026-05-04). Composes ConfidenceMonitor + "
            "ConfidenceProbeRunner + HypothesisProbe + "
            "SpeculativeBranchTree + RiskTierFloor + tool_"
            "executor as a glue arc — one authoritative per-op "
            "budget consulted at every Venom tool-round "
            "boundary via :func:`epistemic_budget_provider_"
            "bridge.attach_to_provider_run`. Cost-gated routes "
            "(BG/SPEC) refuse PROBE/SBT structurally. "
            "Operator instant-revert via explicit env false."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/epistemic_budget.py"
        ),
        example="true",
        since="Upgrade 1 Slice 5 (graduated PRD §31.2, 2026-05-04)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_EPISTEMIC_MAX_ROUNDS",
        type=FlagType.INT, default=12,
        description=(
            "Per-op cap on Venom tool rounds before the budget "
            "is exhausted. Default 12. Clamped [1, 100]. When "
            "rounds_consumed >= max_rounds, the dispatch routes "
            "to EXHAUSTED_NOTIFY_APPLY (when below notify_apply "
            "tier) or EXHAUSTED_APPROVAL_REQUIRED (when at or "
            "above) — both fire the budget_action_taken SSE + "
            "escalate the risk tier. Captured at op-start so "
            "env changes mid-op don't shift the cap."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/epistemic_budget.py"
        ),
        example="12",
        since="Upgrade 1 Slice 1 (PRD §31.2, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD",
        type=FlagType.FLOAT, default=0.25,
        description=(
            "Drop magnitude (peak − latest in the bounded "
            "trajectory window) that triggers PROBE_TRIGGERED. "
            "Default 0.25. Clamped [0.01, 1.0]. When the drop "
            "exceeds threshold AND probe_calls_remaining > 0, "
            "the round-boundary dispatch invokes the injected "
            "ConfidenceProbeRunner synchronously (Decision B1: "
            "no background probes) — bounded by HypothesisProbe "
            "three-termination contract."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/epistemic_budget.py"
        ),
        example="0.25",
        since="Upgrade 1 Slice 1 (PRD §31.2, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_EPISTEMIC_SBT_BRANCH_CAP",
        type=FlagType.INT, default=3,
        description=(
            "Per-op cap on SpeculativeBranchTree branch "
            "invocations. Default 3. Clamped [1, 10]. SBT "
            "fires only when (a) probe verdict is "
            "INCONCLUSIVE_*, (b) risk_tier >= notify_apply "
            "(SBT cost-gate), and (c) branch_calls_remaining "
            "> 0. Captured at op-start for stability."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/epistemic_budget.py"
        ),
        example="3",
        since="Upgrade 1 Slice 1 (PRD §31.2, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_EPISTEMIC_TRACKER_TTL_S",
        type=FlagType.INT, default=3600,
        description=(
            "TTL (seconds) for orphan tracker entries. Default "
            "3600 (1h). Clamped [60, 86400]. "
            ":meth:`EpistemicBudgetTracker.reap_orphans` walks "
            "the per-op_id dict and drops entries whose "
            "last_updated_at_unix is older than now - ttl_s. "
            "Lifecycle A1: providers call close_op() in their "
            "finally block; reap_orphans is a safety net for "
            "hard kills."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/epistemic_budget.py"
        ),
        example="3600",
        since="Upgrade 1 Slice 2 (PRD §31.2, 2026-05-04)",
    ),
    # ========================================================================
    # M9 — CuriosityGradient (PRD §30.5.1) — 6 flags
    # Slice 5 graduation: master flips false → true. Per-cluster
    # prediction-error scoring (logprob entropy + Prophecy error +
    # postmortem recurrence) biases SensorGovernor weighted_cap
    # toward high-curiosity regions. Bounded multiplier
    # [floor, ceiling] structurally cannot bypass global cap.
    # ========================================================================
    FlagSpec(
        name="JARVIS_CURIOSITY_GRADIENT_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the CuriosityGradient subsystem. "
            "Default TRUE post Slice 5 graduation (2026-05-04). "
            "Composes ConfidenceMonitor logprob entropy + "
            "ProphecyEngine prediction error + Coherence "
            "Auditor RECURRENCE_DRIFT into a per-cluster "
            "curiosity score; SensorGovernor lazy-imports the "
            "score (Decision X) for opt-in curiosity_aware "
            "sensors. Bounded multiplier "
            "[curiosity_multiplier_floor, "
            "curiosity_multiplier_ceiling] cannot bypass the "
            "global emission cap. Operator instant-revert via "
            "explicit env false."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "curiosity_gradient.py"
        ),
        example="true",
        since="M9 Slice 5 (graduated PRD §30.5.1, 2026-05-04)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_CURIOSITY_HALFLIFE_DAYS",
        type=FlagType.FLOAT, default=14.0,
        description=(
            "Recency-decay halflife for observation samples. "
            "Default 14.0 days. Clamped [0.1, 365.0]. Defers "
            "to :func:`_scoring_primitives.recency_weight` — "
            "M9 NEVER duplicates the decay formula (Decision "
            "E1 AST-pinned). Captured at score-compute time "
            "for stability — env changes mid-window don't "
            "reshape past samples."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "curiosity_gradient.py"
        ),
        example="14.0",
        since="M9 Slice 1 (PRD §30.5.1, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_CURIOSITY_MIN_SAMPLES",
        type=FlagType.INT, default=8,
        description=(
            "Cold-start gate. When a region has fewer than "
            "this many observations, compute_curiosity returns "
            "INSUFFICIENT_DATA and downstream consumers default "
            "multiplier to 1.0 (no bias). Default 8; clamped "
            "[1, 1000]. Prevents random-walk-on-boot pathology."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "curiosity_gradient.py"
        ),
        example="8",
        since="M9 Slice 1 (PRD §30.5.1, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_CURIOSITY_STALE_FOCUS_HOURS",
        type=FlagType.INT, default=24,
        description=(
            "Auto-decay window. When a cluster's score has "
            "been at peak beyond this many hours without new "
            "observations, decay_reason flips to STALE_FOCUS "
            "and the consumer multiplier rebases to 1.0. "
            "Default 24; clamped [1, 720]. Prevents "
            "locked-on-degenerate-region pathology."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "curiosity_gradient.py"
        ),
        example="24",
        since="M9 Slice 1 (PRD §30.5.1, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_CURIOSITY_MULTIPLIER_FLOOR",
        type=FlagType.FLOAT, default=0.5,
        description=(
            "Lower bound for the curiosity multiplier. Default "
            "0.5; clamped [0.0, 1.0]. Floor < 1.0 means "
            "low-curiosity regions can be actively de-"
            "prioritized; floor = 1.0 means curiosity only "
            "boosts (never throttles). Operator choice. "
            "Bounded by construction so SensorGovernor's "
            "global cap is structurally never bypassed."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "curiosity_gradient.py"
        ),
        example="0.5",
        since="M9 Slice 1 (PRD §30.5.1, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_CURIOSITY_MULTIPLIER_CEILING",
        type=FlagType.FLOAT, default=2.0,
        description=(
            "Upper bound for the curiosity multiplier. Default "
            "2.0; clamped [1.0, 10.0]. Ceiling × global cap = "
            "max emission to a single high-curiosity cluster "
            "— bounded by construction so SensorGovernor's "
            "global cap is structurally never bypassed."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "curiosity_gradient.py"
        ),
        example="2.0",
        since="M9 Slice 1 (PRD §30.5.1, 2026-05-04)",
    ),
    # ========================================================================
    # Upgrade 2 — DecisionRecord Causality Graph (PRD §31.3) — 4 flags
    # Slice 5 graduation: replay master flips false → true. Builds on
    # Phase 1 Slice 1.4's already-graduated DecisionRuntime substrate
    # (JARVIS_DETERMINISM_LEDGER_ENABLED) + Priority 2's CausalityDAG
    # (JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED) — those flags stay
    # owned by their respective slices; Upgrade 2 graduates the
    # replay-as-determinism-test surface.
    # ========================================================================
    FlagSpec(
        name="JARVIS_DETERMINISM_REPLAY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master switch for the replay-as-determinism-test "
            "surface. Default TRUE post Slice 5 graduation "
            "(2026-05-04). Gates ``replay_session_consistency`` "
            "+ ``scripts/replay_determinism.py --session <id>`` "
            "+ the ``decision_drift_detected`` SSE producer in "
            "the replay job. Operator instant-revert via "
            "explicit env false. Leaves Phase 1's "
            "JARVIS_DETERMINISM_LEDGER_ENABLED untouched (the "
            "ledger writer is already graduated)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "determinism/replay_determinism.py"
        ),
        example="true",
        since="Upgrade 2 Slice 5 (graduated PRD §31.3, 2026-05-04)",
        posture_relevance=_HARDEN_AND_CONSOLIDATE,
    ),
    FlagSpec(
        name="JARVIS_DECISIONS_READER_DEFAULT_LIMIT",
        type=FlagType.INT, default=100,
        description=(
            "Default records-per-query when caller doesn't "
            "supply a limit on the decisions_reader read API "
            "(``read_records_for_session`` / "
            "``recent_records_across_sessions``). Default 100; "
            "clamped [1, 10000]. Consumed by both the "
            "``/decisions`` REPL + ``GET /observability/"
            "decisions[/session/{id}]`` HTTP routes."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "determinism/decisions_reader.py"
        ),
        example="100",
        since="Upgrade 2 Slice 3 (PRD §31.3, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_DECISIONS_READER_MAX_RECORDS",
        type=FlagType.INT, default=10_000,
        description=(
            "Hard ceiling on per-session record count returned "
            "by the decisions_reader read API. Default 10000; "
            "clamped [100, 1000000]. Bounds memory under "
            "operator-supplied ?limit query parameters that "
            "exceed sane size."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "determinism/decisions_reader.py"
        ),
        example="10000",
        since="Upgrade 2 Slice 3 (PRD §31.3, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_DECISIONS_READER_MAX_SESSIONS",
        type=FlagType.INT, default=1_000,
        description=(
            "Hard ceiling on session-list size returned by "
            "``list_available_sessions``. Default 1000; clamped "
            "[10, 100000]. Bounds the cross-session aggregation "
            "in ``recent_records_across_sessions`` + the "
            "``/decisions sessions`` REPL surface."
        ),
        category=Category.CAPACITY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "determinism/decisions_reader.py"
        ),
        example="1000",
        since="Upgrade 2 Slice 3 (PRD §31.3, 2026-05-04)",
    ),
    # ====================================================================
    # M10 ArchitectureProposer (PRD §32.4) — 5 flags (Slice 5)
    # Master is OPERATOR-PINNED default-FALSE per §30.5.2 — does NOT
    # graduate default-true at Slice 5; flips only after 30+
    # proposal-acceptance audit. AST-pinned at Slice 5.
    # ====================================================================
    FlagSpec(
        name="JARVIS_M10_ARCH_PROPOSER_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Master kill switch for the M10 ArchitectureProposer "
            "(PRD §32.4). When false the UnhandledPatternMiner / "
            "ProposalSynthesizer / ProposalLifecycleOrchestrator + "
            "/m10 REPL + GET /observability/m10 + SSE "
            "m10_proposal_emitted all revert in lockstep. "
            "OPERATOR-PINNED default-FALSE per §30.5.2 — does NOT "
            "graduate default-true at Slice 5; flips only after a "
            "30+ proposal-acceptance audit."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/m10/primitives.py"
        ),
        example="false",
        since="M10 Slice 5 (PRD §32.4, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_M10_ADAPTIVE_MIN_THRESHOLD",
        type=FlagType.INT, default=2,
        description=(
            "Minimum recurrence count below which the Bayesian "
            "adaptive threshold cannot drop. Default 2; clamped "
            "[1, 100]. Provides a structural floor on the "
            "miner's emit gate even when posterior + diversity "
            "would otherwise produce a sub-2 threshold."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/m10/primitives.py"
        ),
        example="2",
        since="M10 Slice 5 (PRD §32.4, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_M10_ADAPTIVE_CONFIDENCE",
        type=FlagType.FLOAT, default=2.0,
        description=(
            "Bayesian confidence multiplier on the Beta(1+s, 1+f) "
            "posterior used by ``compute_threshold``. Default 2.0; "
            "clamped [0.1, 100.0]. Higher values demand more "
            "evidence (inflates threshold); lower values relax it."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/m10/primitives.py"
        ),
        example="2.0",
        since="M10 Slice 5 (PRD §32.4, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_M10_MAX_DAILY",
        type=FlagType.INT, default=5,
        description=(
            "Hard cap on M10 proposals emitted per UTC day. "
            "Default 5; clamped [1, 100]. Composes with the "
            "STANDARD-route × Quorum-K=3 cost contract to bound "
            "spend at ≤$0.075/day max. UnhandledPatternMiner "
            "returns DAILY_CAP_REACHED beyond this."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/m10/primitives.py"
        ),
        example="5",
        since="M10 Slice 5 (PRD §32.4, 2026-05-04)",
    ),
    # ====================================================================
    # Slice 5b consolidation Slice 2 — module_discovery substrate (1 flag)
    # ====================================================================
    FlagSpec(
        name="JARVIS_MODULE_DISCOVERY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the module_discovery "
            "substrate (PRD §32.5 / §32.11 Slice 5b "
            "consolidation Slice 2). When false, discovery is "
            "a fast no-op returning a zero-count "
            "DiscoveryReport; consumers (flag_registry_seed / "
            "shipped_code_invariants / help_dispatcher) fall "
            "back to their static seed lists. Three consumers "
            "delegate to this primitive — no parallel walkers "
            "in production code (AST-pinned)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/meta/"
            "module_discovery.py"
        ),
        example="true",
        since="Slice 5b Slice 2 (PRD §32.5, 2026-05-04)",
    ),
    # ====================================================================
    # Move 7 — Cross-op Semantic Budget (PRD §29.4) Slice 1 — 4 flags
    # Master is OPERATOR-PINNED default-FALSE per §33.1 graduation
    # contract pattern; flips only after empirical Phase 9 baseline.
    # ====================================================================
    FlagSpec(
        name="JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Master kill switch for Move 7 Cross-op Semantic "
            "Budget (PRD §29.4). When false (the default), "
            "compute_semantic_budget() returns DISABLED and "
            "the upstream observer / SSE / REPL surfaces "
            "(Slices 2-4, deferred) revert in lockstep. "
            "OPERATOR-PINNED default-FALSE per §33.1 — flips "
            "only after empirical Phase 9 baseline establishes "
            "the per-op drift envelope for this codebase."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "cross_op_semantic_budget.py"
        ),
        example="false",
        since="Move 7 Slice 1 (PRD §29.4, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE",
        type=FlagType.INT, default=50,
        description=(
            "Number of most-recent op centroids the rolling-"
            "window primitive integrates over. Default 50 "
            "(≈ a couple hours of normal operation; large "
            "enough to surface 1%/op compounding without "
            "hyper-noise). Clamped [2, 10000]."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "cross_op_semantic_budget.py"
        ),
        example="50",
        since="Move 7 Slice 1 (PRD §29.4, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_CROSS_OP_SEMANTIC_THRESHOLD",
        type=FlagType.FLOAT, default=0.30,
        description=(
            "Operator budget knob — integrated cosine-distance "
            "summed over the window MUST NOT exceed this "
            "fraction. Default 0.30 (30% — calibrated for the "
            "§29.4 \"1%/op compounding over 100 cycles\" "
            "framing). Clamped (0, 100]."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "cross_op_semantic_budget.py"
        ),
        example="0.30",
        since="Move 7 Slice 1 (PRD §29.4, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_CROSS_OP_SEMANTIC_APPROACHING_RATIO",
        type=FlagType.FLOAT, default=0.8,
        description=(
            "Fraction of threshold above which the verdict "
            "ladder transitions to APPROACHING. Default 0.8 "
            "(warn at 80% of budget). Clamped [0.1, 1.0]."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "cross_op_semantic_budget.py"
        ),
        example="0.8",
        since="Move 7 Slice 1 (PRD §29.4, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the Phase 10 graduation "
            "contract harness (PRD §9 / §32.8.1 / §1610). "
            "When false, is_ready_for_purge() always returns "
            "ContractVerdict.DISABLED so the master flag flip "
            "(JARVIS_TOPOLOGY_SENTINEL_ENABLED) is structurally "
            "blocked. Production should leave this on; intended "
            "for operator troubleshooting only."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "phase10_graduation_contract.py"
        ),
        example="true",
        since="Phase 10 Slice 5 (PRD §32.8.1, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS",
        type=FlagType.INT, default=3,
        description=(
            "Number of consecutive forced-clean once-proofs "
            "required before the Phase 10 graduation contract "
            "reports READY_FOR_PURGE. Default 3 per PRD §1612; "
            "clamped [1, 10]. Lowering below 3 violates "
            "operator binding and SHOULD NOT be used outside "
            "test fixtures."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "phase10_graduation_contract.py"
        ),
        example="3",
        since="Phase 10 Slice 5 (PRD §32.8.1, 2026-05-05)",
    ),
    # ====================================================================
    # Move 8 — Proactive Curiosity Loop (PRD §29.7) Slice 1 — 4 flags
    # Master is OPERATOR-PINNED default-FALSE per §33.1 graduation
    # contract pattern; flips only after Slice 3's empirical contract
    # proves the loop respects SensorGovernor caps.
    # ====================================================================
    FlagSpec(
        name="JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED",
        type=FlagType.BOOL, default=False,
        description=(
            "Master kill switch for Move 8 Proactive Curiosity "
            "Loop substrate (PRD §29.7). When false (the default), "
            "rank_curious_clusters() returns an empty tuple and "
            "Slice 2's ProactiveExplorationSensor wire-up "
            "short-circuits — composes M9 producer side without "
            "auto-spawning intents. OPERATOR-PINNED default-FALSE "
            "per §33.1; flips only after Slice 3's graduation "
            "contract proves the loop doesn't overrun "
            "SensorGovernor caps."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "proactive_curiosity_reader.py"
        ),
        example="false",
        since="Move 8 Slice 1 (PRD §29.7, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_PROACTIVE_CURIOSITY_TOP_K",
        type=FlagType.INT, default=3,
        description=(
            "Number of curious clusters to surface per scan. "
            "Default 3 (matches the existing per-scan emit-cap "
            "discipline of cluster_coverage — small enough to "
            "avoid intake flood). Clamped [1, 16]."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "proactive_curiosity_reader.py"
        ),
        example="3",
        since="Move 8 Slice 1 (PRD §29.7, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_PROACTIVE_CURIOSITY_MAGNITUDE_FLOOR",
        type=FlagType.FLOAT, default=0.40,
        description=(
            "Minimum curiosity magnitude to consider for "
            "ranking. Default 0.40 (matches the existing "
            "JARVIS_EXPLORATION_ENTROPY_THRESHOLD precedent for "
            "'this is interesting enough to surface'). Clamped "
            "[0.0, 1.0]."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "proactive_curiosity_reader.py"
        ),
        example="0.40",
        since="Move 8 Slice 1 (PRD §29.7, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_PROACTIVE_CURIOSITY_COOLDOWN_S",
        type=FlagType.INT, default=14400,
        description=(
            "Minimum interval (seconds) between repeated "
            "rankings of the same cluster_id. Cross-call dedup "
            "in the in-process cooldown ledger. Default 14400 "
            "(4h — long enough to give the cluster a chance to "
            "drift; short enough to re-fire within a work "
            "session). Clamped [60, 7d]."
        ),
        category=Category.TUNING,
        source_file=(
            "backend/core/ouroboros/governance/"
            "proactive_curiosity_reader.py"
        ),
        example="14400",
        since="Move 8 Slice 1 (PRD §29.7, 2026-05-05)",
    ),
    # ====================================================================
    # Move 8 — Proactive Curiosity Loop Slice 3 graduation contract
    # — 3 flags. Harness master is default-TRUE per §33.1
    # (operator-binding lives on Slice 1's flag).
    # ====================================================================
    FlagSpec(
        name=(
            "JARVIS_PROACTIVE_CURIOSITY_GRADUATION_"
            "CONTRACT_ENABLED"
        ),
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the Move 8 Proactive "
            "Curiosity Loop graduation contract harness "
            "(PRD §29.7 / §33.1). When false, "
            "is_ready_for_graduation() always returns "
            "CuriosityGraduationVerdict.DISABLED so the master "
            "flag flip (JARVIS_PROACTIVE_CURIOSITY_READER_"
            "ENABLED) is structurally blocked from any "
            "automated harness. Production should leave this "
            "on; intended for operator troubleshooting only."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "proactive_curiosity_loop_graduation_contract.py"
        ),
        example="true",
        since="Move 8 Slice 3 (PRD §29.7, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_PROACTIVE_CURIOSITY_REQUIRED_EMISSIONS",
        type=FlagType.INT, default=12,
        description=(
            "Minimum surfaced emissions required before "
            "READY_FOR_GRADUATION verdict. Default 12 (3× "
            "across each of 4 postures, plus headroom). "
            "Clamped [3, 1000]."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "proactive_curiosity_loop_graduation_contract.py"
        ),
        example="12",
        since="Move 8 Slice 3 (PRD §29.7, 2026-05-05)",
    ),
    FlagSpec(
        name=(
            "JARVIS_PROACTIVE_CURIOSITY_MAX_GOVERNOR_THROTTLES"
        ),
        type=FlagType.INT, default=0,
        description=(
            "Maximum SensorGovernor cap-hit observations "
            "tolerable before EXCESSIVE_THROTTLES verdict. "
            "Default 0 (the contract is 'the loop integrates "
            "cleanly'). Clamped [0, 100]."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "proactive_curiosity_loop_graduation_contract.py"
        ),
        example="0",
        since="Move 8 Slice 3 (PRD §29.7, 2026-05-05)",
    ),
    # ====================================================================
    # Phase 9 synthetic workload — 1 flag.
    # Hard cap on synthetic envelope injection to prevent misconfigured
    # cron from spamming ops or spending budget. Operating value is
    # passed via --seed-intents on the harness CLI (typically 3); this
    # is defense-in-depth ceiling.
    # ====================================================================
    FlagSpec(
        name="JARVIS_PHASE9_SEED_INTENTS_MAX",
        type=FlagType.INT, default=16,
        description=(
            "Hard ceiling on the number of synthetic envelopes "
            "Phase 9 cadence may inject in one harness "
            "invocation. Default 16 (clamped [1, 64]). The "
            "cadence wrapper passes a much smaller N (typically "
            "3) via --seed-intents; this knob is defense-in-"
            "depth against a misconfigured cron entry."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/graduation/"
            "phase_9_synthetic_workload.py"
        ),
        example="16",
        since="Phase 9 Slice 1 (PRD §36.5, 2026-05-05)",
    ),
    FlagSpec(
        name="JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the REPL dispatch "
            "auto-discovery substrate (PRD §32.5 / §32.11 "
            "Slice 5b consolidation Slice 4). When true (the "
            "default), SerpentREPL routes verb-shaped lines "
            "through repl_dispatch_registry.try_dispatch which "
            "resolves verb→dispatcher via the auto-discovered "
            "verb→callable map (17+ verbs covering 5 legacy + "
            "12 newly-unlocked surfaces). When false, the "
            "registry returns no-match and the legacy hardcoded "
            "ladder in serpent_flow.py carries the load "
            "(preserved for instant rollback). Custom-handler "
            "verbs (budget/risk/goal/cancel/plan/postmortems/"
            "inline) are excluded regardless of master flag — "
            "they retain bespoke operator semantics."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/battle_test/"
            "repl_dispatch_registry.py"
        ),
        example="true",
        since="Slice 5b Slice 4 (PRD §32.5, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Master kill switch for the observability route "
            "auto-discovery substrate (PRD §32.5 / §32.11 "
            "Slice 5b consolidation Slice 3). When true (the "
            "default), event_channel boot calls "
            "discover_and_mount_observability_routes which "
            "auto-mounts every module-level register_routes "
            "across the curated provider packages — closing "
            "5+ dormant observability surfaces structurally. "
            "When false, the legacy explicit register_routes "
            "blocks in event_channel.py carry the load "
            "(preserved for instant rollback)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/"
            "observability_route_registry.py"
        ),
        example="true",
        since="Slice 5b Slice 3 (PRD §32.5, 2026-05-04)",
    ),
    FlagSpec(
        name="JARVIS_M10_APPROVAL_TIMEOUT_S",
        type=FlagType.FLOAT, default=86_400.0,
        description=(
            "Approval timeout in seconds for M10 proposals "
            "sitting in AWAITING_APPROVAL phase. Default 86400 "
            "(24h); proposals beyond this transition to EXPIRED. "
            "Clamped [60, 7 days]."
        ),
        category=Category.TIMING,
        source_file=(
            "backend/core/ouroboros/governance/m10/primitives.py"
        ),
        example="86400",
        since="M10 Slice 5 (PRD §32.4, 2026-05-04)",
    ),
]


import logging as _logging

_logger = _logging.getLogger(__name__)


# Curated list of provider PACKAGES whose direct submodules may
# contribute flags via ``register_flags(registry)``. Adding a NEW
# flag inside an existing module requires zero edits here — the
# discovery loop picks it up automatically. Adding a flag in a NEW
# package requires one entry. This is metadata about WHERE flags
# live, not the flags themselves.
_FLAG_PROVIDER_PACKAGES: tuple = (
    "backend.core.ouroboros.governance",  # top-level (semantic_firewall, etc.)
    "backend.core.ouroboros.governance.verification",  # SBT/CIGW/Replay/etc.
    "backend.core.ouroboros.battle_test",  # TerminationHookRegistry Slice 4
)


def _discover_module_provided_flags(
    registry: FlagRegistry,
) -> int:
    """Dynamically discover modules that own their FlagSpec
    declarations.

    Walks every package in ``_FLAG_PROVIDER_PACKAGES`` for direct
    submodules exposing ``register_flags(registry) -> int``. Each
    matching module registers its own flags + returns the count.

    Architecture: instead of hardcoding flags into SEED_SPECS, modules
    that ADD a new flag declare it co-located with the consuming code
    via their own ``register_flags`` function. Adding a new V5/V6/...
    surface requires zero edits to this file — the discovery loop
    finds the new module's registrar and invokes it natively.

    NEVER raises. Per-module failures logged + skipped — boot is never
    blocked by one misconfigured module.

    Implementation: delegates to
    :func:`module_discovery.discover_module_provided_callable`
    (Slice 5b consolidation Slice 2, PRD §32.5). Single source of
    truth for the walk pattern (AST-pinned)."""
    try:
        from backend.core.ouroboros.governance.meta.module_discovery import (  # noqa: E501
            discover_module_provided_callable,
            make_registry_handler,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        _logger.debug(
            "[FlagRegistry] module_discovery primitive "
            "unavailable: %s", exc,
        )
        return 0
    report = discover_module_provided_callable(
        packages=_FLAG_PROVIDER_PACKAGES,
        attr_name="register_flags",
        handler=make_registry_handler(registry=registry),
        excluded_modules=(__name__,),
        log_prefix="FlagRegistry",
    )
    return report.discovered_count


def seed_default_registry(registry: FlagRegistry) -> int:
    """Install all SEED_SPECS + dynamically-discovered module flags
    into ``registry``. Returns total count installed.

    Two-tier registration:
      1. **Static seeds** — the legacy ``SEED_SPECS`` curated list
         (modules predating the dynamic-discovery pattern).
      2. **Module-owned** — walks ``verification/`` for modules with
         ``register_flags(registry)``; each such module declares its
         own FlagSpecs co-located with the consuming code.

    Called once by ``flag_registry.ensure_seeded()``. Idempotent —
    duplicate calls override-in-place. NEVER raises."""
    registry.bulk_register(SEED_SPECS, override=True)
    discovered = _discover_module_provided_flags(registry)
    return len(SEED_SPECS) + discovered


__all__ = [
    "SEED_SPECS",
    "seed_default_registry",
]
