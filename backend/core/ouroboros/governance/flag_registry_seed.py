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
