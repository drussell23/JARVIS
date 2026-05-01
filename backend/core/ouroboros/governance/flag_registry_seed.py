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
        type=FlagType.BOOL, default=False,
        description=(
            "Master kill switch for K-way Generative Quorum. "
            "Default false post Slice 5 graduation — operators "
            "explicitly opt in because Quorum incurs K× generation "
            "cost per APPROVAL_REQUIRED+ op. When false, the gate "
            "short-circuits to FALL_THROUGH_SINGLE on every op (no "
            "behavior change from pre-Move-6 baseline)."
        ),
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/verification/"
            "generative_quorum.py"
        ),
        example="false",
        since="Move 6 Slice 5",
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
]


def seed_default_registry(registry: FlagRegistry) -> int:
    """Install all SEED_SPECS into ``registry``. Returns count installed.

    Called once by ``flag_registry.ensure_seeded()``. Idempotent —
    duplicate calls override-in-place (same content, no warning)."""
    registry.bulk_register(SEED_SPECS, override=True)
    return len(SEED_SPECS)


__all__ = [
    "SEED_SPECS",
    "seed_default_registry",
]
