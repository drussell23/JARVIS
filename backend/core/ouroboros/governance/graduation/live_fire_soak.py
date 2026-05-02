"""Phase 9.1 — Live-Fire Graduation Soak Harness.

Per PRD §9 Phase 9 (the critical blocker for A-level RSI): the
substrate is *built*; this module proves it works under real model
output by automating a 3-clean-session-per-flag soak cadence.

## Composition with existing graduation infrastructure

This module **does not duplicate** the existing graduation ledger.
It composes against
``backend.core.ouroboros.governance.adaptation.graduation_ledger``:

  * ``CADENCE_POLICY``  — 24 substrate flags (15 from v2.32 + 9 added
    in v2.52 for Phase 8 substrate/surface + CuriosityEngine).
  * ``GraduationLedger.record_session(flag, session_id, outcome,
    recorded_by, notes)`` — append-only JSONL with cross-process flock.
  * ``GraduationLedger.is_eligible(flag_name)`` — clean ≥ required AND
    runner == 0.
  * ``GraduationLedger.eligible_flags()`` — list currently eligible.
  * ``SessionOutcome`` enum (CLEAN / INFRA / RUNNER / MIGRATION).

This harness adds:

  * **Pick-next algorithm**: substrate flags graduate BEFORE surface
    flags (dependency ordering); within an order, alpha-stable.
  * **Subprocess invocation**: forks ``ouroboros_battle_test.py`` with
    ONLY the target flag + dependencies set to ``true`` in env;
    everything else stays default. Pinned ``--headless
    --max-wall-seconds 2400 --cost-cap 0.50``.
  * **Outcome classification**: parses ``summary.json`` + reads
    ``debug.log`` tail to map session_outcome / stop_reason → one of
    the 4 ``SessionOutcome`` values.
  * **Rich evidence persistence**: ``.jarvis/live_fire_graduation_history.jsonl``
    captures per-session metadata beyond the bare-bones graduation
    ledger — session_id, flag, outcome, stop_reason, cost_total,
    duration_s, ops_count, raw failure-class counts, runner_attributed
    boolean, harness version. Read-only summary view consumed by the
    CLI's ``evidence`` subcommand.

## Authority posture (locked + pinned)

  * **Read/write only over `.jarvis/live_fire_graduation_history.jsonl`
    + delegating to `GraduationLedger.record_session`** for the
    canonical clean-count.
  * **Stdlib + subprocess + adaptation.graduation_ledger only** at
    top level (battle-test path is invoked via subprocess, NOT
    imported — we don't want to pull the 6-layer stack into the
    harness process).
  * **Master flag** ``JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED``
    (default ``false`` until graduation-of-the-grader cadence).
    When off, every public entry returns a structured stub.
  * **NEVER raises** — every error path is logged once and returns
    a structured failure status.
  * **No imports from gate / execution modules**: pinned by AST
    scan in tests (orchestrator / iron_gate / risk_tier_floor /
    semantic_guardian / policy_engine / candidate_generator /
    tool_executor / change_engine all banned).
  * **Bounded outputs**: at most ``MAX_HISTORY_FILE_BYTES=8 MiB`` /
    ``MAX_HISTORY_RECORDS_LOADED=10000`` / ``MAX_NOTES_CHARS=2000``.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Bounded caps for the rich evidence ledger.
MAX_HISTORY_FILE_BYTES: int = 8 * 1024 * 1024
MAX_HISTORY_RECORDS_LOADED: int = 10000
MAX_NOTES_CHARS: int = 2000
MAX_FAILURE_CLASS_COUNT_KEYS: int = 32

# Default subprocess invocation parameters.
DEFAULT_COST_CAP_USD: float = 0.50
DEFAULT_MAX_WALL_SECONDS: int = 2400  # 40 minutes
DEFAULT_SUBPROCESS_TIMEOUT_S: int = 3600  # 60-minute kill cap

# Battle-test entry script path (relative to project root).
BATTLE_TEST_SCRIPT_REL = Path("scripts") / "ouroboros_battle_test.py"

# Schema version of the rich-evidence row layout.
EVIDENCE_SCHEMA_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def is_soak_harness_enabled() -> bool:
    """Master flag — ``JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED``
    (default ``false``)."""
    return os.environ.get(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "",
    ).strip().lower() in _TRUTHY


def is_paused() -> bool:
    """Operator pause switch —
    ``JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED`` (default ``false``).
    Set to ``true`` to halt the cron without disabling the harness."""
    return os.environ.get(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Dependency ordering: substrate flags graduate BEFORE surface flags
# ---------------------------------------------------------------------------
#
# Pick-next algorithm: prefer flags whose dependencies are all already
# graduated. Within tied dependency states, alpha-stable.
#
# Phase 8 surface flags depend on Phase 8 substrate flags. Pass C
# activation flags depend on the corresponding Pass C mining flags.
# CuriosityEngine depends on Phase 7.6 hypothesis probe primitive.
#
# Map shape: { flag_name: frozenset(dependency_flag_names) }


_DEPENDENCY_MAP: Dict[str, FrozenSet[str]] = {
    # Phase 8 surfaces consume Phase 8 substrate.
    "JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED": frozenset({
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
        "JARVIS_MULTI_OP_TIMELINE_ENABLED",
    }),
    "JARVIS_PHASE8_SSE_BRIDGE_ENABLED": frozenset({
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
    }),
    "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED": frozenset({
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        "JARVIS_MULTI_OP_TIMELINE_ENABLED",
    }),
    # Item #3 production prober depends on Phase 7.6 substrate.
    "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED": frozenset({
        "JARVIS_HYPOTHESIS_PROBE_ENABLED",
    }),
    "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED": frozenset({
        "JARVIS_HYPOTHESIS_PROBE_ENABLED",
    }),
    # CuriosityEngine depends on Phase 7.6 hypothesis substrate.
    "JARVIS_CURIOSITY_ENGINE_ENABLED": frozenset({
        "JARVIS_HYPOTHESIS_PROBE_ENABLED",
    }),
    # Pass C activation flags depend on the corresponding loader flag.
    # (Pass C activation flag → mining surface; loader flag → substrate.)
    # Pass C mining surfaces depend on the loader flag landing first.
    "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED": frozenset({
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS",
        "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
    }),
    "JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED": frozenset({
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
        "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
    }),
    "JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED": frozenset({
        "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
        "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
    }),
    "JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED": frozenset({
        "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
        "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
    }),
    "JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED": frozenset({
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
        "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
    }),
    # Phase 7.9 sunset detector depends on adaptive_semantic_guardian
    # being live (it generates sunset_candidates against mined patterns).
    "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED": frozenset({
        "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
    }),
}


def get_dependencies(flag_name: str) -> FrozenSet[str]:
    """Return the dependency flag set for ``flag_name`` (empty
    frozenset for flags with no dependencies)."""
    return _DEPENDENCY_MAP.get(flag_name, frozenset())


def all_dependency_flags() -> FrozenSet[str]:
    """Return the union of every flag that any other flag depends on
    — used as a bit-rot guard so every dep is itself a known flag."""
    out: set = set()
    for deps in _DEPENDENCY_MAP.values():
        out |= deps
    return frozenset(out)


# ---------------------------------------------------------------------------
# Evidence row (rich, per-session)
# ---------------------------------------------------------------------------


class HarnessStatus(str, enum.Enum):
    """Status of a single harness invocation."""

    OK = "ok"                           # subprocess ran + outcome classified
    SKIPPED_DISABLED = "skipped_disabled"  # master flag off
    SKIPPED_PAUSED = "skipped_paused"      # paused by operator
    SKIPPED_NO_FLAG = "skipped_no_flag"    # no eligible flag to run
    SKIPPED_UNKNOWN_FLAG = "skipped_unknown_flag"
    SKIPPED_DEPS_NOT_GRADUATED = "skipped_deps_not_graduated"
    SUBPROCESS_FAILED = "subprocess_failed"
    SUBPROCESS_TIMEOUT = "subprocess_timeout"
    SUMMARY_PARSE_FAILED = "summary_parse_failed"
    LEDGER_WRITE_FAILED = "ledger_write_failed"
    # Phase 9.1b — breadcrumb persistence on hang/kill
    SUBPROCESS_IN_FLIGHT = "subprocess_in_flight"  # written BEFORE
                                                   # subprocess returns;
                                                   # paired with later
                                                   # OK / SUBPROCESS_*
                                                   # row on same session
    INTERRUPTED = "interrupted"          # caught SystemExit /
                                         # KeyboardInterrupt mid-soak
                                         # (operator SIGTERM, etc.)


@dataclass(frozen=True)
class EvidenceRow:
    """One soak's rich-evidence row. Frozen — append-only history."""

    schema_version: str
    harness_status: str
    flag_name: str
    session_id: str
    outcome: str  # SessionOutcome string value
    runner_attributed: bool
    stop_reason: str
    cost_total_usd: float
    duration_s: float
    ops_count: int
    failure_class_counts: Dict[str, int]
    deps_set: List[str]
    started_at_iso: str
    started_at_epoch: float
    finished_at_iso: str
    finished_at_epoch: float
    notes: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "harness_status": self.harness_status,
            "flag_name": self.flag_name,
            "session_id": self.session_id,
            "outcome": self.outcome,
            "runner_attributed": self.runner_attributed,
            "stop_reason": self.stop_reason,
            "cost_total_usd": self.cost_total_usd,
            "duration_s": self.duration_s,
            "ops_count": self.ops_count,
            "failure_class_counts": dict(self.failure_class_counts),
            "deps_set": list(self.deps_set),
            "started_at_iso": self.started_at_iso,
            "started_at_epoch": self.started_at_epoch,
            "finished_at_iso": self.finished_at_iso,
            "finished_at_epoch": self.finished_at_epoch,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class HarnessResult:
    """Returned by ``LiveFireSoakHarness.run_soak``. Always carries
    ``status`` even when soak short-circuits before subprocess fork."""

    status: HarnessStatus
    detail: str
    evidence: Optional[EvidenceRow] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def history_path() -> Path:
    raw = os.environ.get("JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "live_fire_graduation_history.jsonl"


# ---------------------------------------------------------------------------
# Outcome classification helpers
# ---------------------------------------------------------------------------


# Failure-class regexes / signatures classifying a session outcome.
# Per PRD §3.6.6 + memory project_async_shutdown_race_triage.md:
# "Runner-attributed failures block flip; infra-noise tagged as
# waiver rows, non-blocking."
_RUNNER_FAILURE_CLASSES: FrozenSet[str] = frozenset({
    "phase_runner_error",
    "candidate_validate_error",
    "iron_gate_violation",
    "semantic_guardian_block",
    "change_engine_error",
    "verify_regression",
    "l2_repair_error",
    "fsm_state_corruption",
    "artifact_contract_drift",
})

_INFRA_FAILURE_CLASSES: FrozenSet[str] = frozenset({
    "provider_api_error",
    "provider_rate_limited",
    "provider_timeout",
    "tls_handshake_error",
    "network_error",
    "out_of_memory",
    "disk_full",
    "battery_low",
    "git_lock_contention",
    "async_shutdown_race",
    # Cascading state vector fix (2026-05-01): worktree isolation
    # failures are now a distinct failure_class from generic "infra"
    # so the retry budget can distinguish them. But for graduation
    # aggregate statistics, they're still infrastructure-category.
    "worktree_isolation",
})

_MIGRATION_STOP_REASONS: FrozenSet[str] = frozenset({
    "schema_version_skew",
    "config_migration",
    "branch_rebased",
})


def classify_outcome(
    summary: Mapping[str, Any],
    *,
    debug_log_tail: str = "",
) -> Tuple[str, bool, str]:
    """Map a parsed ``summary.json`` (+ optional debug-log tail) to a
    ``(SessionOutcome.value, runner_attributed, classification_notes)``
    tuple.

    Pure-function. NEVER raises.

    Decision tree:
      1. ``session_outcome == "complete"`` AND no runner-class failures
         in counts → ``CLEAN``.
      2. ``stop_reason`` matches a migration signature → ``MIGRATION``.
      3. ANY runner-class failure count > 0 → ``RUNNER`` (blocks flip).
      4. ANY infra-class failure count > 0 OR ``stop_reason`` is
         shutdown-noise (sigterm/sighup/sigint/wall_clock_cap) →
         ``INFRA`` (waiver row).
      5. Default → ``RUNNER`` (conservative — unknown fault-class
         blocks rather than waivers).
    """
    if not isinstance(summary, dict):
        return ("runner", True, "non_dict_summary")
    session_outcome = str(summary.get("session_outcome") or "")
    stop_reason = str(summary.get("stop_reason") or "")
    failure_counts = summary.get("failure_class_counts") or {}
    if not isinstance(failure_counts, dict):
        failure_counts = {}
    # Step 1: clean path.
    runner_hits = [
        k for k, v in failure_counts.items()
        if k in _RUNNER_FAILURE_CLASSES and _is_positive_int(v)
    ]
    if session_outcome == "complete" and not runner_hits:
        return ("clean", False, "complete_no_runner_failures")
    # Step 2: migration.
    if stop_reason in _MIGRATION_STOP_REASONS:
        return ("migration", False, f"migration_stop_reason:{stop_reason}")
    # Step 3: runner-class failure → blocks.
    if runner_hits:
        return (
            "runner", True,
            f"runner_classes:{sorted(runner_hits)}",
        )
    # Step 4: infra-class failure or shutdown-noise.
    infra_hits = [
        k for k, v in failure_counts.items()
        if k in _INFRA_FAILURE_CLASSES and _is_positive_int(v)
    ]
    shutdown_noise = stop_reason in {
        "sigterm", "sighup", "sigint", "wall_clock_cap",
        "harness_idle_timeout",
    }
    if infra_hits or shutdown_noise:
        return (
            "infra", False,
            f"infra_classes:{sorted(infra_hits)}|stop:{stop_reason}",
        )
    # Step 5: default — conservative classification.
    return (
        "runner", True,
        f"default_runner:outcome={session_outcome}|stop={stop_reason}",
    )


def _is_positive_int(v: Any) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class LiveFireSoakHarness:
    """Forks one ``ouroboros_battle_test.py`` invocation per soak,
    classifies the outcome, persists rich evidence, delegates to the
    canonical ``GraduationLedger`` for clean-count tracking.

    Per-instance state is the rate tracker only — the rest is
    delegated to module-level helpers + the existing graduation
    ledger singleton."""

    project_root: Path = field(
        default_factory=lambda: _resolve_project_root(),
    )
    history_file: Path = field(default_factory=history_path)

    # ----- pick-next algorithm -----

    def pick_next_flag(self) -> Optional[str]:
        """Return the next ungraduated flag whose dependencies are
        all already graduated. Substrate flags before surface flags;
        within tier, alpha-stable.

        Returns None when all flags are graduated OR no flag has
        all dependencies satisfied (deadlock-style — caller surfaces
        SKIPPED_NO_FLAG).
        """
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            CADENCE_POLICY, get_default_ledger,
        )
        ledger = get_default_ledger()
        # Index flags by graduated state.
        graduated: set = set()
        for entry in CADENCE_POLICY:
            if ledger.is_eligible(entry.flag_name):
                graduated.add(entry.flag_name)
        # Pick the first not-yet-graduated flag whose deps are all
        # graduated; substrate flags (no deps) come first by
        # construction.
        candidates: List[str] = []
        for entry in CADENCE_POLICY:
            if entry.flag_name in graduated:
                continue
            deps = get_dependencies(entry.flag_name)
            if deps.issubset(graduated):
                candidates.append(entry.flag_name)
        if not candidates:
            return None
        # Alpha-stable tie-break.
        return sorted(candidates)[0]

    def queue_view(self) -> List[Dict[str, Any]]:
        """Return a serializable list of all flags + their pick-
        eligibility. Used by CLI ``queue`` subcommand."""
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            CADENCE_POLICY, get_default_ledger,
        )
        ledger = get_default_ledger()
        out: List[Dict[str, Any]] = []
        graduated: set = set()
        for entry in CADENCE_POLICY:
            if ledger.is_eligible(entry.flag_name):
                graduated.add(entry.flag_name)
        for entry in CADENCE_POLICY:
            deps = sorted(get_dependencies(entry.flag_name))
            deps_satisfied = set(deps).issubset(graduated)
            progress = ledger.progress(entry.flag_name)
            out.append({
                "flag_name": entry.flag_name,
                "required_clean_sessions": entry.required_clean_sessions,
                "cadence_class": entry.cadence_class.value,
                "graduated": entry.flag_name in graduated,
                "deps": deps,
                "deps_satisfied": deps_satisfied,
                "progress": progress,
                "description": entry.description,
            })
        return out

    # ----- subprocess invocation -----

    def run_soak(
        self,
        *,
        flag_name: Optional[str] = None,
        cost_cap_usd: float = DEFAULT_COST_CAP_USD,
        max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS,
        subprocess_timeout_s: int = DEFAULT_SUBPROCESS_TIMEOUT_S,
        subprocess_runner: Optional[Any] = None,
        recorded_by: str = "live_fire_soak_harness",
    ) -> HarnessResult:
        """Run ONE soak.

        ``flag_name=None`` → pick-next-flag. Else use the supplied
        flag (must be in CADENCE_POLICY).

        ``subprocess_runner`` — optional callable injected for
        testing. When None, uses :func:`_run_battle_test_subprocess`.
        Signature: ``runner(env: Dict[str, str], cost_cap_usd: float,
        max_wall_seconds: int, timeout_s: int, project_root: Path)
        -> Tuple[int, Dict[str, Any], str]`` returning
        ``(exit_code, parsed_summary, debug_log_tail)``.

        NEVER raises into the caller.
        """
        if not is_soak_harness_enabled():
            return HarnessResult(
                status=HarnessStatus.SKIPPED_DISABLED,
                detail="master_off",
            )
        if is_paused():
            return HarnessResult(
                status=HarnessStatus.SKIPPED_PAUSED,
                detail="operator_paused",
            )
        # Resolve flag.
        chosen = flag_name or self.pick_next_flag()
        if chosen is None:
            return HarnessResult(
                status=HarnessStatus.SKIPPED_NO_FLAG,
                detail="no_eligible_flag_or_deadlocked",
            )
        # Validate flag is known.
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            known_flags,
        )
        if chosen not in known_flags():
            return HarnessResult(
                status=HarnessStatus.SKIPPED_UNKNOWN_FLAG,
                detail=f"flag_not_in_policy:{chosen}",
            )
        # Validate deps when flag was operator-supplied (pick_next
        # already guarantees this for picked flags).
        if flag_name is not None:
            from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
                get_default_ledger,
            )
            ledger = get_default_ledger()
            deps = get_dependencies(chosen)
            ungraduated_deps = [
                d for d in deps if not ledger.is_eligible(d)
            ]
            if ungraduated_deps:
                return HarnessResult(
                    status=HarnessStatus.SKIPPED_DEPS_NOT_GRADUATED,
                    detail=(
                        f"missing_graduated_deps:"
                        f"{sorted(ungraduated_deps)}"
                    ),
                )
        # Build env: only chosen + dependencies set to "true".
        env = self._build_env_for_flag(chosen)
        deps_set = sorted(get_dependencies(chosen) | {chosen})
        # Run the subprocess.
        runner = subprocess_runner or _run_battle_test_subprocess
        started_at_epoch = time.time()
        started_at_iso = _utc_now_iso()
        # Phase 9.1b — breadcrumb persistence:
        # Write a SUBPROCESS_IN_FLIGHT row BEFORE invoking the runner.
        # If the harness CLI is externally killed (SIGTERM / SIGKILL)
        # mid-subprocess (e.g. when the battle-test hangs in atexit
        # cleanup like Phase 9.1 once-run revealed), this breadcrumb
        # row is the only evidence on disk. Operator can grep
        # `harness_status=subprocess_in_flight` rows in the ledger
        # and see hung soaks. On normal subprocess return, the
        # finally-block doesn't add a duplicate (handled by
        # `_persisted` flag).
        self._append_history_row(EvidenceRow(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            harness_status=HarnessStatus.SUBPROCESS_IN_FLIGHT.value,
            flag_name=chosen,
            session_id="in-flight",
            outcome="infra",  # treated as waiver if no completion-pair
            runner_attributed=False,
            stop_reason="subprocess_in_flight",
            cost_total_usd=0.0,
            duration_s=0.0,
            ops_count=0,
            failure_class_counts={},
            deps_set=deps_set,
            started_at_iso=started_at_iso,
            started_at_epoch=started_at_epoch,
            finished_at_iso="",
            finished_at_epoch=0.0,
            notes="breadcrumb — written before subprocess returns",
        ))
        try:
            try:
                exit_code, summary, debug_tail = runner(
                    env=env,
                    cost_cap_usd=cost_cap_usd,
                    max_wall_seconds=max_wall_seconds,
                    timeout_s=subprocess_timeout_s,
                    project_root=self.project_root,
                )
            except subprocess.TimeoutExpired:
                return self._persist_failure(
                    chosen, deps_set, started_at_iso, started_at_epoch,
                    HarnessStatus.SUBPROCESS_TIMEOUT,
                    "subprocess_timeout_expired",
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "[LiveFireSoak] subprocess invocation failed "
                    "flag=%r: %s", chosen, exc,
                )
                return self._persist_failure(
                    chosen, deps_set, started_at_iso, started_at_epoch,
                    HarnessStatus.SUBPROCESS_FAILED,
                    f"subprocess_exception:{type(exc).__name__}",
                )
        except BaseException as exc:  # noqa: BLE001 — catches
            # SystemExit / KeyboardInterrupt (SIGTERM-induced).
            # Persist an INTERRUPTED row paired with the breadcrumb
            # so operator can grep "this soak was killed mid-
            # subprocess." Then re-raise so the caller's SystemExit
            # / KeyboardInterrupt propagates cleanly.
            try:
                self._persist_failure(
                    chosen, deps_set,
                    started_at_iso, started_at_epoch,
                    HarnessStatus.INTERRUPTED,
                    (
                        f"interrupted_mid_subprocess:"
                        f"{type(exc).__name__}"
                    ),
                )
            except Exception:  # noqa: BLE001
                # Last-ditch swallow — interrupt propagation must
                # not be blocked by a persistence failure.
                logger.debug(
                    "[LiveFireSoak] interrupt-persistence raised",
                    exc_info=True,
                )
            raise
        finished_at_epoch = time.time()
        finished_at_iso = _utc_now_iso()
        # Validate parsed summary.
        if not isinstance(summary, dict):
            return self._persist_failure(
                chosen, deps_set, started_at_iso, started_at_epoch,
                HarnessStatus.SUMMARY_PARSE_FAILED,
                f"summary_not_dict:exit_code={exit_code}",
                finished_at_epoch=finished_at_epoch,
                finished_at_iso=finished_at_iso,
            )
        # Classify outcome.
        outcome_str, runner_attributed, class_notes = classify_outcome(
            summary, debug_log_tail=debug_tail or "",
        )
        # Phase 9.2 — consult per-flag GraduationContract (opt-in via
        # JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT). Contract can:
        #   * Override clean predicate (e.g. require ≥1 hypothesis
        #     for CuriosityEngine flag).
        #   * Override failure-class blocklist (treat specific runner-
        #     class failures as INFRA waivers for this flag).
        # When master-off, contract consultation is a no-op — behavior
        # is byte-identical to pre-9.2.
        outcome_str, runner_attributed, class_notes = (
            self._maybe_apply_contract(
                chosen, summary,
                outcome_str, runner_attributed, class_notes,
            )
        )
        # Build evidence row.
        session_id = str(summary.get("session_id") or "unknown")
        cost_total = _safe_float(summary.get("cost_total"))
        duration_s = _safe_float(summary.get("duration_s"))
        ops_count = _safe_int(summary.get("ops_count"))
        failure_counts_raw = summary.get("failure_class_counts") or {}
        failure_counts = self._truncate_failure_counts(failure_counts_raw)
        evidence = EvidenceRow(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            harness_status=HarnessStatus.OK.value,
            flag_name=chosen,
            session_id=session_id,
            outcome=outcome_str,
            runner_attributed=runner_attributed,
            stop_reason=str(summary.get("stop_reason") or ""),
            cost_total_usd=cost_total,
            duration_s=duration_s,
            ops_count=ops_count,
            failure_class_counts=failure_counts,
            deps_set=deps_set,
            started_at_iso=started_at_iso,
            started_at_epoch=started_at_epoch,
            finished_at_iso=finished_at_iso,
            finished_at_epoch=finished_at_epoch,
            notes=class_notes[:MAX_NOTES_CHARS],
        )
        # Persist rich-evidence row.
        self._append_history_row(evidence)
        # Delegate to canonical graduation ledger.
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            SessionOutcome, get_default_ledger,
        )
        ledger = get_default_ledger()
        try:
            outcome_enum = SessionOutcome(outcome_str)
        except ValueError:
            outcome_enum = SessionOutcome.RUNNER
        ok, ledger_detail = ledger.record_session(
            flag_name=chosen,
            session_id=session_id,
            outcome=outcome_enum,
            recorded_by=recorded_by,
            notes=class_notes[:1000],
        )
        if not ok:
            logger.warning(
                "[LiveFireSoak] ledger.record_session failed: %s",
                ledger_detail,
            )
            return HarnessResult(
                status=HarnessStatus.LEDGER_WRITE_FAILED,
                detail=ledger_detail,
                evidence=evidence,
            )
        return HarnessResult(
            status=HarnessStatus.OK,
            detail=f"recorded:outcome={outcome_str}",
            evidence=evidence,
        )

    # ----- evidence read view -----

    def evidence_for_flag(
        self, flag_name: str,
    ) -> List[Dict[str, Any]]:
        """Return all evidence rows for ``flag_name``, oldest-first.
        NEVER raises."""
        rows = self._read_history_rows()
        return [r for r in rows if r.get("flag_name") == flag_name]

    def all_evidence(
        self, *, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        rows = self._read_history_rows()
        if limit is None:
            return rows
        n = max(0, min(limit, MAX_HISTORY_RECORDS_LOADED))
        return rows[-n:]

    # ----- internals -----

    def _build_env_for_flag(self, flag_name: str) -> Dict[str, str]:
        """Build the env dict for the subprocess: inherit current
        env + set ONLY (flag_name + dependencies) to ``true`` + EXPLICITLY
        forward AsyncTopologySentinel-related env vars.

        Other JARVIS_* substrate flags are NOT touched — the subprocess
        sees the inherited env exactly. This matches the contract that
        substrate flags are individually graduated; flipping multiple
        in one soak would muddle the evidence.

        **Slice 3.5 — explicit sentinel env propagation** (directive
        2026-04-27): Sentinel-related env vars are forwarded VIA AN
        EXPLICIT ALLOWLIST (``topology_sentinel.sentinel_propagated_vars``)
        rather than relying on ``dict(os.environ)`` inheritance alone.
        This makes the propagation contract:

          (a) Discoverable — operators grep ``_SENTINEL_PROPAGATED_VARS``
              in topology_sentinel.py to see the full list.
          (b) Defensible — if ``os.environ`` ever gets stripped or a
              future refactor breaks the inherit-everything assumption,
              the explicit forwarding still holds.
          (c) Testable — ``test_sentinel_env_propagation_contract``
              asserts the harness forwards every sentinel env var the
              dispatcher reads.

        Closes the boundary-isolation gap that bit session
        bt-2026-04-27-194550 (sentinel module loaded inside the
        subprocess but the dispatcher never entered its branch).
        """
        env = dict(os.environ)
        env[flag_name] = "true"
        for dep in get_dependencies(flag_name):
            env[dep] = "true"
        # Always enable the harness's own master flag inside the
        # subprocess so the subprocess can record into the live-fire
        # ledger if it chooses (informational; the harness still
        # records on subprocess return).
        env["JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED"] = "true"
        # Ensure graduation ledger is enabled in the subprocess so
        # the canonical record_session call inside the harness's
        # post-subprocess phase succeeds.
        env["JARVIS_GRADUATION_LEDGER_ENABLED"] = "true"
        # Explicit sentinel env propagation (Slice 3.5). Re-asserts
        # the parent's value (or absence) for every sentinel-related
        # env var. If the parent set JARVIS_TOPOLOGY_SENTINEL_ENABLED=true,
        # the subprocess will see it; if unset in parent, it stays
        # unset (default behavior). Same effect as inheritance, but
        # explicit + AST-grep-able.
        try:
            from backend.core.ouroboros.governance.topology_sentinel import (
                sentinel_propagated_vars,
            )
            for name in sentinel_propagated_vars():
                value = os.environ.get(name)
                if value is not None:
                    env[name] = value
                # If unset in parent, do NOT inject a default — the
                # sentinel module's own defaults handle that.
        except ImportError:
            # Sentinel module not available (e.g. on a branch that
            # hasn't merged Slice 1). Inheritance still applies via
            # dict(os.environ) above; degrade gracefully.
            logger.debug(
                "[LiveFireSoak] sentinel_propagated_vars unavailable — "
                "falling back to inheritance-only env propagation"
            )
        return env

    def _maybe_apply_contract(
        self,
        flag_name: str,
        summary: Mapping[str, Any],
        outcome_str: str,
        runner_attributed: bool,
        class_notes: str,
    ) -> Tuple[str, bool, str]:
        """Phase 9.2 — refine classification via per-flag contract.

        When master flag ``JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT``
        is off → no-op (returns inputs unchanged, byte-identical
        behavior). When on:

          1. Custom clean_predicate may DOWNGRADE a default-CLEAN
             outcome to RUNNER (e.g. CuriosityEngine flag was on but
             zero hypotheses generated → not actually clean).
          2. ``failure_class_blocklist_overrides`` may UPGRADE a
             default-RUNNER outcome to INFRA (waiver) for specific
             flag-known infra-class failures.

        Contract NEVER raises into the caller — try/except wraps
        every consultation."""
        try:
            from backend.core.ouroboros.governance.graduation.graduation_contract import (  # noqa: E501
                get_contract, is_contract_consultation_enabled,
            )
        except Exception:  # noqa: BLE001 — defensive
            return (outcome_str, runner_attributed, class_notes)
        if not is_contract_consultation_enabled():
            return (outcome_str, runner_attributed, class_notes)
        try:
            contract = get_contract(flag_name)
        except Exception:  # noqa: BLE001
            return (outcome_str, runner_attributed, class_notes)
        notes_extra: List[str] = []
        # Step 1: custom clean predicate downgrade.
        if (
            outcome_str == "clean"
            and contract.clean_predicate is not None
        ):
            try:
                still_clean = contract.is_clean(summary)
            except Exception:  # noqa: BLE001
                still_clean = True  # defensive: don't downgrade on error
            if not still_clean:
                outcome_str = "runner"
                runner_attributed = True
                notes_extra.append("contract_predicate_downgraded_clean")
        # Step 2: blocklist override upgrade (RUNNER → INFRA).
        if (
            outcome_str == "runner"
            and contract.failure_class_blocklist_overrides
        ):
            failure_counts = summary.get("failure_class_counts") or {}
            if isinstance(failure_counts, dict):
                positive_classes = {
                    k for k, v in failure_counts.items()
                    if _is_positive_int(v)
                }
                # If EVERY positive-count failure class is in the
                # contract's blocklist, treat as INFRA waiver.
                if (
                    positive_classes
                    and positive_classes.issubset(
                        contract.failure_class_blocklist_overrides,
                    )
                ):
                    outcome_str = "infra"
                    runner_attributed = False
                    notes_extra.append(
                        "contract_blocklist_upgraded_runner_to_infra",
                    )
        if notes_extra:
            class_notes = (
                class_notes + "|" + ",".join(notes_extra)
            )[:MAX_NOTES_CHARS]
        return (outcome_str, runner_attributed, class_notes)

    def _truncate_failure_counts(
        self, raw: Mapping[str, Any],
    ) -> Dict[str, int]:
        out: Dict[str, int] = {}
        if not isinstance(raw, dict):
            return out
        for i, (k, v) in enumerate(raw.items()):
            if i >= MAX_FAILURE_CLASS_COUNT_KEYS:
                break
            try:
                out[str(k)[:64]] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def _persist_failure(
        self,
        flag_name: str,
        deps_set: List[str],
        started_at_iso: str,
        started_at_epoch: float,
        status: HarnessStatus,
        detail: str,
        *,
        finished_at_epoch: Optional[float] = None,
        finished_at_iso: Optional[str] = None,
    ) -> HarnessResult:
        """Persist a failure-status evidence row + return result."""
        finished_e = (
            finished_at_epoch if finished_at_epoch is not None
            else time.time()
        )
        finished_i = finished_at_iso or _utc_now_iso()
        evidence = EvidenceRow(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            harness_status=status.value,
            flag_name=flag_name,
            session_id="unknown",
            outcome="infra",  # treated as waiver — harness fault
            runner_attributed=False,
            stop_reason=status.value,
            cost_total_usd=0.0,
            duration_s=max(0.0, finished_e - started_at_epoch),
            ops_count=0,
            failure_class_counts={},
            deps_set=deps_set,
            started_at_iso=started_at_iso,
            started_at_epoch=started_at_epoch,
            finished_at_iso=finished_i,
            finished_at_epoch=finished_e,
            notes=detail[:MAX_NOTES_CHARS],
        )
        self._append_history_row(evidence)
        return HarnessResult(status=status, detail=detail, evidence=evidence)

    def _append_history_row(self, row: EvidenceRow) -> None:
        """Append one evidence row to the rich-evidence JSONL.
        NEVER raises."""
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[LiveFireSoak] history mkdir failed: %s", exc,
            )
            return
        try:
            line = json.dumps(row.to_dict(), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[LiveFireSoak] history serialize failed: %s", exc,
            )
            return
        try:
            with self.history_file.open(
                "a", encoding="utf-8",
            ) as f:
                # Reuse Phase 7.8's flock for cross-process safety.
                from backend.core.ouroboros.governance.adaptation._file_lock import (  # noqa: E501
                    flock_exclusive,
                )
                with flock_exclusive(f.fileno()):
                    f.write(line)
                    f.write("\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
        except OSError as exc:
            logger.warning(
                "[LiveFireSoak] history append failed: %s", exc,
            )

    def _read_history_rows(self) -> List[Dict[str, Any]]:
        """Best-effort read of all evidence rows. NEVER raises."""
        try:
            if not self.history_file.exists():
                return []
        except OSError:
            return []
        try:
            size = self.history_file.stat().st_size
        except OSError:
            return []
        if size > MAX_HISTORY_FILE_BYTES:
            logger.warning(
                "[LiveFireSoak] history exceeds %d bytes (was %d) — "
                "refusing to load", MAX_HISTORY_FILE_BYTES, size,
            )
            return []
        try:
            text = self.history_file.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for line in text.splitlines():
            if len(out) >= MAX_HISTORY_RECORDS_LOADED:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


# ---------------------------------------------------------------------------
# Subprocess runner (default; tests inject a fake)
# ---------------------------------------------------------------------------


def _run_battle_test_subprocess(
    *,
    env: Dict[str, str],
    cost_cap_usd: float,
    max_wall_seconds: int,
    timeout_s: int,
    project_root: Path,
) -> Tuple[int, Dict[str, Any], str]:
    """Default battle-test runner. Forks the subprocess with the
    pinned ``--headless --max-wall-seconds N --cost-cap U`` args,
    parses the resulting ``summary.json``, returns
    ``(exit_code, summary_dict, debug_log_tail)``.

    NEVER raises into caller; returns ``(exit_code, {}, "")`` on any
    parse failure (caller maps to SUMMARY_PARSE_FAILED).
    """
    script_path = project_root / BATTLE_TEST_SCRIPT_REL
    if not script_path.exists():
        logger.error(
            "[LiveFireSoak] battle-test script not found at %s",
            script_path,
        )
        return (-1, {}, "")
    cmd = [
        sys.executable,
        str(script_path),
        "--headless",
        "--max-wall-seconds", str(max_wall_seconds),
        "--cost-cap", f"{cost_cap_usd:.2f}",
    ]
    logger.info(
        "[LiveFireSoak] launching subprocess cmd=%s", cmd,
    )
    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    # Locate the most-recent session dir written by this subprocess.
    sessions_root = project_root / ".ouroboros" / "sessions"
    summary, debug_tail = _read_most_recent_session(
        sessions_root, after_epoch=time.time() - timeout_s - 60,
    )
    return (proc.returncode, summary, debug_tail)


def _read_most_recent_session(
    sessions_root: Path, *, after_epoch: float,
) -> Tuple[Dict[str, Any], str]:
    """Return ``(summary_dict, debug_log_tail)`` for the most-recent
    session dir created after ``after_epoch``. NEVER raises."""
    try:
        if not sessions_root.exists():
            return ({}, "")
    except OSError:
        return ({}, "")
    try:
        candidates = sorted(
            sessions_root.iterdir(), reverse=True,
        )
    except OSError:
        return ({}, "")
    for d in candidates:
        try:
            if not d.is_dir():
                continue
            if d.stat().st_mtime < after_epoch:
                continue
        except OSError:
            continue
        summary_path = d / "summary.json"
        debug_path = d / "debug.log"
        summary: Dict[str, Any] = {}
        if summary_path.exists():
            try:
                summary = json.loads(
                    summary_path.read_text(encoding="utf-8"),
                )
            except (OSError, json.JSONDecodeError):
                summary = {}
        debug_tail = ""
        if debug_path.exists():
            try:
                text = debug_path.read_text(
                    encoding="utf-8", errors="replace",
                )
                debug_tail = text[-8192:]
            except OSError:
                debug_tail = ""
        if isinstance(summary, dict):
            return (summary, debug_tail)
    return ({}, "")


def _resolve_project_root() -> Path:
    raw = os.environ.get("JARVIS_REPO_PATH")
    if raw:
        return Path(raw)
    # Walk up from this file: governance/graduation/live_fire_soak.py
    # → 3 parents = governance, ouroboros, core, backend, repo.
    here = Path(__file__).resolve()
    return here.parent.parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_DEFAULT_HARNESS: Optional[LiveFireSoakHarness] = None


def get_default_harness() -> LiveFireSoakHarness:
    global _DEFAULT_HARNESS
    if _DEFAULT_HARNESS is None:
        _DEFAULT_HARNESS = LiveFireSoakHarness()
    return _DEFAULT_HARNESS


def reset_default_harness() -> None:
    global _DEFAULT_HARNESS
    _DEFAULT_HARNESS = None


__all__ = [
    "BATTLE_TEST_SCRIPT_REL",
    "DEFAULT_COST_CAP_USD",
    "DEFAULT_MAX_WALL_SECONDS",
    "DEFAULT_SUBPROCESS_TIMEOUT_S",
    "EVIDENCE_SCHEMA_VERSION",
    "EvidenceRow",
    "HarnessResult",
    "HarnessStatus",
    "LiveFireSoakHarness",
    "MAX_FAILURE_CLASS_COUNT_KEYS",
    "MAX_HISTORY_FILE_BYTES",
    "MAX_HISTORY_RECORDS_LOADED",
    "MAX_NOTES_CHARS",
    "all_dependency_flags",
    "classify_outcome",
    "get_default_harness",
    "get_dependencies",
    "history_path",
    "is_paused",
    "is_soak_harness_enabled",
    "reset_default_harness",
]
