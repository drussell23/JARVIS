"""Item #4 — graduation ledger for per-loader cadence tracking.

Phase 7 + Items #2/#3 shipped 12+ master flags that default to
``false`` until per-loader 3-clean-session cadences (Pass B
discipline) or 5-clean-session cadences (Pass C discipline) flip
them to ``true``. This module codifies the cadence machinery.

## What "clean" means

A "clean" session is a battle-test session that ran with the
target master flag explicitly set to ``"1"`` AND completed without
any RUNNER-attributed failures. Infra failures (OOM, TLS errors,
network flakes) are waived per the Wave 1 closure ledger
(``feedback_wave_1_closure_and_slice5_policy.md``).

This module does NOT execute sessions itself — it tracks
operator-recorded session outcomes. The operator (or a scheduled
agent per ``feedback_agent_conducted_soak_delegation.md``) runs
sessions externally; this module records what happened + tells
the operator which flags are eligible to flip.

## Append-only JSONL audit log

Path: ``.jarvis/graduation_ledger.jsonl``. Each row records ONE
session outcome:

```jsonl
{"flag_name": "...", "session_id": "...", "outcome": "clean|infra|runner",
 "recorded_at": "...", "recorded_by": "...", "notes": "..."}
```

State queries (``progress(flag_name)``, ``eligible_flags()``)
reduce the log to per-flag clean-session counts. Master-flag flips
themselves are made by the operator editing source — this module
just SIGNALS readiness.

## Per-flag cadence policy

The policy table below pins, for each known flag:
  * ``required_clean_sessions``: 3 (Pass B / Phase 7 default) or
    5 (Pass C — higher bar per Pass C §4.6)
  * ``cadence_class``: 'pass_b' or 'pass_c'
  * ``description``: human-readable

Adding a new flag = single-line entry in ``CADENCE_POLICY``.

## Default-off

``JARVIS_GRADUATION_LEDGER_ENABLED`` (default false). When off,
``record_session()`` is a no-op + ``progress()`` returns 0.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Hard cap on the ledger file size (defends against operator-typo
# session_id storms bloating the file).
MAX_LEDGER_FILE_BYTES: int = 4 * 1024 * 1024

# Cap on records loaded per `progress()` call (defense against
# pathological reads).
MAX_RECORDS_LOADED: int = 50_000

# Cap on the per-flag clean-session counter return value (just
# defensive — real counts will always be small).
MAX_CLEAN_COUNT: int = 1_000

# Cap on free-form notes per session record.
MAX_NOTES_CHARS: int = 1_000


def is_ledger_enabled() -> bool:
    """Master flag — ``JARVIS_GRADUATION_LEDGER_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_GRADUATION_LEDGER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def ledger_path() -> Path:
    """Return the ledger path. Env-overridable via
    ``JARVIS_GRADUATION_LEDGER_PATH``; defaults to
    ``.jarvis/graduation_ledger.jsonl`` under cwd."""
    raw = os.environ.get("JARVIS_GRADUATION_LEDGER_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "graduation_ledger.jsonl"


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class SessionOutcome(str, enum.Enum):
    """Per-session outcome contributing to (or skipped from) the
    clean-session count."""

    CLEAN = "clean"           # contributes to required_clean_sessions
    INFRA = "infra"           # waived (OOM / TLS / network flake)
    RUNNER = "runner"         # runner-attributed; resets confidence
    MIGRATION = "migration"   # transient setup change; waived


# ---------------------------------------------------------------------------
# Cadence policy
# ---------------------------------------------------------------------------


class CadenceClass(str, enum.Enum):
    PASS_B = "pass_b"   # Phase 7 + Items 2/3 default — 3 clean
    PASS_C = "pass_c"   # Pass C surfaces — 5 clean (higher bar)


@dataclass(frozen=True)
class CadencePolicyEntry:
    """One flag's cadence policy. Frozen — pinned by source-grep
    + tests so the policy table cannot drift silently."""

    flag_name: str
    required_clean_sessions: int
    cadence_class: CadenceClass
    description: str


# ---------------------------------------------------------------------------
# CANONICAL CADENCE POLICY — the 12+ flags from Phase 7 + Items 2/3
# Adding a new flag = single-line entry below. Removing requires
# operator approval (graduation history may exist for the flag).
# ---------------------------------------------------------------------------


CADENCE_POLICY: Tuple[CadencePolicyEntry, ...] = (
    # Phase 7.1 — SemanticGuardian adapted patterns
    CadencePolicyEntry(
        flag_name="JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 7.1 — SemanticGuardian boot-time adapted-pattern loader"
        ),
    ),
    # Phase 7.2 — IronGate adapted floors
    CadencePolicyEntry(
        flag_name="JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.2 — IronGate adapted-floor boot-time loader",
    ),
    # Phase 7.3 — ScopedToolBackend per-Order budget
    CadencePolicyEntry(
        flag_name="JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 7.3 — ScopedToolBackend adapted per-Order budget loader"
        ),
    ),
    # Phase 7.4 — Risk-tier ladder adapted extensions
    CadencePolicyEntry(
        flag_name="JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.4 — risk-tier ladder adapted-tier loader",
    ),
    # Phase 7.5 — Category-weight rebalance
    CadencePolicyEntry(
        flag_name="JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.5 — ExplorationLedger category-weight loader",
    ),
    # Phase 7.6 — HypothesisProbe primitive
    CadencePolicyEntry(
        flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 7.6 — bounded HypothesisProbe primitive",
    ),
    # Phase 7.9 — Stale-pattern sunset detector (Pass C surface)
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Phase 7.9 — StalePatternDetector sunset signal",
    ),
    # Item #2 — MetaGovernor YAML writer
    CadencePolicyEntry(
        flag_name="JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Item #2 — MetaGovernor YAML writer (/adapt approve writes YAML)"
        ),
    ),
    # Item #3 — Production EvidenceProber
    CadencePolicyEntry(
        flag_name="JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Item #3 — AnthropicVenomEvidenceProber (production prober)"
        ),
    ),
    # Item #3 — Bridges
    CadencePolicyEntry(
        flag_name="JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Item #3 — bridges (CONFIRMED→AdaptationLedger; "
            "terminal→HypothesisLedger)"
        ),
    ),
    # 5 Pass C mining surfaces (substrate flags — graduate after
    # callers wire payload). Higher bar per Pass C §4.6 = 5 clean.
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description=(
            "Pass C Slice 2 — SemanticGuardian POSTMORTEM-mined patterns"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description=(
            "Pass C Slice 3 — IronGate exploration-floor auto-tightener"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Pass C Slice 4a — per-Order mutation budget calibrator",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Pass C Slice 4b — risk-tier ladder extender",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description="Pass C Slice 5 — category-weight rebalancer",
    ),
    # ----------------------------------------------------------------
    # Phase 8 — Temporal Observability substrate (5 modules from v2.44)
    # ----------------------------------------------------------------
    CadencePolicyEntry(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 8.1 — DecisionTraceLedger append-only JSONL",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 8.2 — LatentConfidenceRing in-memory ring buffer",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_MULTI_OP_TIMELINE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 8.3 — multi-op timeline merger",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 8.4 — FlagChangeMonitor snapshot+diff",
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description="Phase 8.5 — LatencySLODetector per-phase p95",
    ),
    # ----------------------------------------------------------------
    # Phase 8 surface wiring (3 slices from v2.48-2.50)
    # ----------------------------------------------------------------
    CadencePolicyEntry(
        flag_name="JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 8 Slice 1 — Phase8ObservabilityRouter (8 GET endpoints)"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_PHASE8_SSE_BRIDGE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 8 Slice 2 — SSE event bridges (5 new event types)"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 8 Slice 3 — multi-op CLI renderer + battle-test --multi-op"
        ),
    ),
    # ----------------------------------------------------------------
    # CuriosityEngine v2.45 (autonomous hypothesis-generation primitive)
    # ----------------------------------------------------------------
    CadencePolicyEntry(
        flag_name="JARVIS_CURIOSITY_ENGINE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "CuriosityEngine — POSTMORTEM clusters → falsifiable hypotheses"
        ),
    ),
    # ----------------------------------------------------------------
    # Move 6.5 — Multi-Prior Speculative Execution (5 producer flags;
    # Slice 6 is harness default-TRUE, NOT a graduation candidate)
    # ----------------------------------------------------------------
    CadencePolicyEntry(
        flag_name="JARVIS_MULTI_PRIOR_PLANNING_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Move 6.5 Slice 1 — multi-prior materializer "
            "(pure decision; no I/O)"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_MULTI_PRIOR_RUNNER_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description=(
            "Move 6.5 Slice 2 — K-prior async runner with "
            "cost-cap watchdog (mutation surface — Pass C bar)"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_MULTI_PRIOR_DISPATCH_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description=(
            "Move 6.5 Slice 3 — dispatch adapter (route + "
            "posture gate composition; Pass C bar — gates "
            "actual K-prior firing)"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Move 6.5 Slice 4 — observer trio "
            "(read-only ledger + chatter-suppressed SSE)"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_MULTI_PRIOR_CANVAS_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Move 6.5 Slice 5 — canvas + diff-fan-out "
            "renderer (read-only operator surface)"
        ),
    ),
    # ----------------------------------------------------------------
    # Phase 3 — Autonomy observability trio (3 read-only bridges)
    # ----------------------------------------------------------------
    CadencePolicyEntry(
        flag_name="JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 3 A1 — orchestrator terminal-state path "
            "→ canonical ExecutionMonitor singleton + "
            "bounded JSONL ledger"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_EXEC_GRAPH_BRIDGE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 3 A2 — read-only projection of canonical "
            "ExecutionGraphProgressTracker → SerpentFlow / "
            "canvas / SSE"
        ),
    ),
    CadencePolicyEntry(
        flag_name="JARVIS_COMMAND_BUS_BRIDGE_ENABLED",
        required_clean_sessions=3,
        cadence_class=CadenceClass.PASS_B,
        description=(
            "Phase 3 A3 — read-only polling of canonical "
            "CommandBus.snapshot_all() with chatter-suppressed "
            "delta emission"
        ),
    ),
    # ----------------------------------------------------------------
    # Capstone Dogfood — LiveKernelValidator (Sovereign Telemetry
    # Unification, 2026-06-15). Kernel-touching: the validator reroutes
    # live-fire-failing candidates back through GENERATE_RETRY as
    # failure_class=build. Its per-flag GraduationContract demands
    # harvester-proven self-heal evidence (fired + no-OOM + recovered),
    # so it carries the stricter Pass C bar (5 clean sessions).
    # ----------------------------------------------------------------
    CadencePolicyEntry(
        flag_name="JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED",
        required_clean_sessions=5,
        cadence_class=CadenceClass.PASS_C,
        description=(
            "LiveKernelValidator — live-fire boot validation reroutes "
            "kernel-touching candidates to self-heal (mutation-adjacent "
            "gate — Pass C bar; Metrics-aware capstone contract)"
        ),
    ),
)


_POLICY_BY_FLAG: Dict[str, CadencePolicyEntry] = {
    e.flag_name: e for e in CADENCE_POLICY
}


def get_policy(flag_name: str) -> Optional[CadencePolicyEntry]:
    """Return the cadence policy for ``flag_name`` or None if unknown."""
    return _POLICY_BY_FLAG.get(flag_name)


def known_flags() -> FrozenSet[str]:
    """Return the set of all flags governed by this ledger."""
    return frozenset(_POLICY_BY_FLAG.keys())


# ---------------------------------------------------------------------------
# Session record + persistence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """One ledger row. Frozen — append-only history.

    Slice 6 (2026-05-05): :attr:`runner_attributed_kind` carries
    the structured taxonomy classifier emitted by the harness.
    Legacy rows (pre-Slice-6) have no field — readers default to
    ``None`` and the suffix back-compat shim
    (:func:`is_legacy_contract_downgrade`) stays load-bearing for
    those rows ONLY. New rows route through this typed field;
    note-string parsing is a back-compat path, not the canonical
    one.
    """

    flag_name: str
    session_id: str
    outcome: SessionOutcome
    recorded_at_iso: str
    recorded_at_epoch: float
    recorded_by: str
    notes: str = ""
    # Slice 6 — Optional[str]; serialized only when present so
    # legacy rows on disk stay byte-identical.
    runner_attributed_kind: Optional[str] = None

    def to_dict(self) -> Dict:
        out: Dict = {
            "flag_name": self.flag_name,
            "session_id": self.session_id,
            "outcome": self.outcome.value,
            "recorded_at_iso": self.recorded_at_iso,
            "recorded_at_epoch": self.recorded_at_epoch,
            "recorded_by": self.recorded_by,
            "notes": self.notes,
        }
        if self.runner_attributed_kind is not None:
            out["runner_attributed_kind"] = self.runner_attributed_kind
        return out


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class GraduationLedger:
    """Append-only JSONL ledger of per-flag session outcomes.

    Best-effort — every public method NEVER raises. Same discipline
    as AdaptationLedger.
    """

    path: Path = field(default_factory=ledger_path)

    # ----- write -----

    def record_session(
        self,
        *,
        flag_name: str,
        session_id: str,
        outcome: SessionOutcome,
        recorded_by: str,
        notes: str = "",
        runner_attributed_kind: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Append ONE session outcome. Returns ``(ok, detail)``.

        Pre-checks:
          1. Master flag off → (False, "master_off")
          2. flag_name not in known_flags → (False, "unknown_flag")
          3. session_id empty → (False, "empty_session_id")

        Slice 6: ``runner_attributed_kind`` (Optional[str]) carries
        the structured taxonomy classifier from
        :class:`runner_kind.RunnerAttributedKind`. When ``None``
        (legacy callers / non-runner outcomes), the row writes
        without the field — byte-identical to pre-Slice-6 rows.
        Unknown / malformed values are coerced to ``None`` via
        :func:`runner_kind.coerce_kind` so corrupted callers
        never store invalid taxonomy values.

        NEVER raises.
        """
        if not is_ledger_enabled():
            return (False, "master_off")
        flag_clean = (flag_name or "").strip()
        if flag_clean not in _POLICY_BY_FLAG:
            return (False, f"unknown_flag:{flag_clean}")
        sid = (session_id or "").strip()
        if not sid:
            return (False, "empty_session_id")
        recorded_by_clean = (recorded_by or "").strip()[:120] or "unknown"
        notes_clean = (notes or "")[:MAX_NOTES_CHARS]
        # Slice 6: validate kind through the canonical coercer.
        # Unknown values become None (write a legacy-shape row
        # rather than persisting an invalid taxonomy value).
        kind_clean: Optional[str] = None
        if runner_attributed_kind is not None:
            try:
                from backend.core.ouroboros.governance.graduation.runner_kind import (  # noqa: E501
                    coerce_kind,
                )
                coerced = coerce_kind(runner_attributed_kind)
                kind_clean = coerced.value if coerced is not None else None
            except ImportError:
                # Substrate unavailable — back-compat path.
                kind_clean = None
        record = SessionRecord(
            flag_name=flag_clean,
            session_id=sid,
            outcome=outcome,
            recorded_at_iso=_utc_now_iso(),
            recorded_at_epoch=time.time(),
            recorded_by=recorded_by_clean,
            notes=notes_clean,
            runner_attributed_kind=kind_clean,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[GraduationLedger] mkdir failed: %s", exc,
            )
            return (False, f"mkdir_failed:{exc}")
        try:
            line = json.dumps(record.to_dict(), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return (False, f"serialize_failed:{exc}")
        # Wave 3 v2.26 canonical cross-process append (sibling .lock
        # file via fcntl.flock). Replaces the pre-Wave-3 legacy
        # `flock_exclusive(fileno)` pattern — same TOCTOU safety,
        # routed through the single canonical substrate per
        # adaptation/ledger.py:752 contract.
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                flock_append_line,
            )
        except ImportError:
            return self._append_legacy_fileno_flock(
                line, flag_clean, sid, outcome, recorded_by_clean,
            )
        ok = flock_append_line(self.path, line)
        if not ok:
            return (False, "flock_append_failed")
        logger.info(
            "[GraduationLedger] flag=%s session=%s outcome=%s by=%s",
            flag_clean, sid, outcome.value, recorded_by_clean,
        )
        return (True, "ok")

    def _append_legacy_fileno_flock(
        self,
        line: str,
        flag_clean: str,
        sid: str,
        outcome: Any,
        recorded_by_clean: str,
    ) -> Tuple[bool, str]:
        """Pre-Wave-3 legacy fallback — kept as a NEVER-raises path
        when the canonical ``cross_process_jsonl`` substrate is
        unavailable. Mirrors adaptation/ledger.py's substrate-
        unavailable contract."""
        try:
            with self.path.open("a", encoding="utf-8") as f:
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
            return (False, f"append_failed:{exc}")
        logger.info(
            "[GraduationLedger] flag=%s session=%s outcome=%s by=%s "
            "(legacy fallback)",
            flag_clean, sid, outcome.value, recorded_by_clean,
        )
        return (True, "ok")

    # ----- read -----

    def _read_all(self) -> List[SessionRecord]:
        """Read every record. Bounded + fail-open."""
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size > MAX_LEDGER_FILE_BYTES:
            logger.warning(
                "[GraduationLedger] %s exceeds MAX_LEDGER_FILE_BYTES=%d "
                "(was %d) — refusing to load",
                self.path, MAX_LEDGER_FILE_BYTES, size,
            )
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[SessionRecord] = []
        for line in text.splitlines():
            if len(out) >= MAX_RECORDS_LOADED:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            try:
                outcome = SessionOutcome(str(obj.get("outcome") or ""))
            except ValueError:
                continue
            # Slice 6 — round-trip the structured kind. Legacy
            # rows have no field → reads as None and falls
            # through to the suffix back-compat shim in
            # ``progress()``.
            kind_raw = obj.get("runner_attributed_kind")
            kind_str: Optional[str] = None
            if kind_raw is not None:
                try:
                    from backend.core.ouroboros.governance.graduation.runner_kind import (  # noqa: E501
                        coerce_kind,
                    )
                    coerced = coerce_kind(kind_raw)
                    kind_str = (
                        coerced.value if coerced is not None else None
                    )
                except ImportError:
                    kind_str = None
            out.append(SessionRecord(
                flag_name=str(obj.get("flag_name") or ""),
                session_id=str(obj.get("session_id") or ""),
                outcome=outcome,
                recorded_at_iso=str(obj.get("recorded_at_iso") or ""),
                recorded_at_epoch=float(obj.get("recorded_at_epoch") or 0.0),
                recorded_by=str(obj.get("recorded_by") or ""),
                notes=str(obj.get("notes") or ""),
                runner_attributed_kind=kind_str,
            ))
        return out

    def progress(self, flag_name: str) -> Dict[str, int]:
        """Return per-flag counts: clean / infra / runner / migration /
        unique_sessions / required.

        Slice 5 lineage waiver (2026-05-05): rows whose ``(outcome,
        notes)`` matches the canonical Phase 9.4 legacy-contract-
        downgrade lineage are filtered out of the canonical ``runner``
        bucket and surfaced separately as ``runner_legacy_downgrade``.
        This refines eligibility (per operator binding) WITHOUT
        weakening the "no runner failures" semantics — real
        runner-class failures (Venom / orchestrator / iron-gate /
        change-engine errors) still block.

        Slice 6 (2026-05-05): the structured
        :attr:`SessionRecord.runner_attributed_kind` field is the
        CANONICAL knower of legacy-downgrade lineage for new
        rows. Routing precedence:

          1. If the row carries a structured kind, route via
             :func:`runner_kind.is_legacy_downgrade_kind` —
             zero string parsing, zero collision risk.
          2. Otherwise (legacy rows, no structured field),
             fall through to the existing
             :func:`is_legacy_contract_downgrade(outcome, notes)`
             back-compat shim. Both paths route to the SAME
             ``runner_legacy_downgrade`` bucket.

        Master-off → all zeros (best-effort).

        Counts UNIQUE session_ids per outcome — the same session
        recorded twice (e.g. operator double-tap) counts once.
        """
        if not is_ledger_enabled():
            return _zero_progress(flag_name)
        policy = _POLICY_BY_FLAG.get(flag_name)
        if policy is None:
            return _zero_progress(flag_name)
        # Lazy-import the lineage waiver + Slice 6 structured-field
        # selector to avoid a startup cycle. Fallback path: if
        # either substrate is unavailable (rollback branch), the
        # corresponding filter degrades to a no-op — runner rows
        # count as before. Defensive; NEVER raises.
        try:
            from backend.core.ouroboros.governance.graduation.lineage_waiver import (  # noqa: E501
                is_incomplete_summary_runner_lineage,
                is_legacy_contract_downgrade,
                is_pre_slice_7c_shutdown_misclassification,
            )
        except ImportError:
            def is_legacy_contract_downgrade(  # type: ignore
                *, outcome: str, notes: str,
            ) -> bool:
                return False
            def is_incomplete_summary_runner_lineage(  # type: ignore
                *, outcome: str, notes: str,
            ) -> bool:
                return False
            def is_pre_slice_7c_shutdown_misclassification(  # type: ignore
                *, outcome: str, notes: str,
            ) -> bool:
                return False
        try:
            from backend.core.ouroboros.governance.graduation.runner_kind import (  # noqa: E501
                coerce_kind as _coerce_kind,
                is_legacy_downgrade_kind as _is_legacy_downgrade_kind,
            )
        except ImportError:
            _coerce_kind = None  # type: ignore
            _is_legacy_downgrade_kind = None  # type: ignore
        counts = {
            "clean": 0, "infra": 0, "runner": 0, "migration": 0,
            "runner_legacy_downgrade": 0,
            "runner_incomplete_summary_waived": 0,
            "unique_sessions": 0, "required": policy.required_clean_sessions,
        }
        seen_per_outcome: Dict[str, set] = {
            "clean": set(), "infra": set(),
            "runner": set(), "migration": set(),
            "runner_legacy_downgrade": set(),
            "runner_incomplete_summary_waived": set(),
        }
        all_sessions: set = set()
        for r in self._read_all():
            if r.flag_name != flag_name:
                continue
            all_sessions.add(r.session_id)
            outcome_key = r.outcome.value
            # Slice 5 + Slice 6 lineage waiver: re-route runner
            # rows whose lineage marks them as legacy contract-
            # downgrades into the audit-visible legacy bucket.
            # Routing precedence:
            #   1. If the row has a STRUCTURED kind, route via
            #      runner_kind.is_legacy_downgrade_kind (Slice 6
            #      canonical path; zero string parsing).
            #   2. Otherwise, fall through to the suffix back-
            #      compat shim (Slice 5 — preserved for legacy
            #      rows written before Slice 6).
            # The original row stays in the append-only ledger
            # untouched; only the in-memory AGGREGATION re-
            # classifies it for eligibility purposes.
            if outcome_key == "runner":
                routed = False
                # Slice 7 (2026-05-07) — empty-summary lineage
                # waiver. Fires REGARDLESS of structured kind:
                # the May 7 EXPLORATION_LEDGER row carries
                # kind=DEFAULT_CONSERVATIVE (which would
                # otherwise block) AND notes matching the
                # canonical empty-summary bytes. Notes equality
                # is the load-bearing signal — DEFAULT_CONSERVATIVE
                # is also emitted for legitimate "unknown
                # fault-class" rows whose notes carry diagnostic
                # signal (non-empty outcome OR stop_reason);
                # those rows DO NOT match the empty-summary bytes
                # and remain blocking. Pure-string equality on
                # `INCOMPLETE_SUMMARY_RUNNER_NOTES` keeps the
                # waiver tight.
                if is_incomplete_summary_runner_lineage(
                    outcome=outcome_key,
                    notes=r.notes,
                ):
                    outcome_key = (
                        "runner_incomplete_summary_waived"
                    )
                    routed = True
                # Slice 7c (2026-05-07) — pre-Slice-7c shutdown
                # misclassification waiver. Detects rows
                # written with composite stop_reason (e.g.,
                # `wall_clock_cap+atexit_fallback`) OR
                # `incomplete_kill` outcome BEFORE the Slice 7c
                # forward fix landed. Routes to the same audit-
                # visible non-blocking bucket.
                if not routed and (
                    is_pre_slice_7c_shutdown_misclassification(
                        outcome=outcome_key,
                        notes=r.notes,
                    )
                ):
                    outcome_key = (
                        "runner_incomplete_summary_waived"
                    )
                    routed = True
                # Slice 6 canonical path.
                if (
                    not routed
                    and r.runner_attributed_kind is not None
                    and _coerce_kind is not None
                    and _is_legacy_downgrade_kind is not None
                ):
                    coerced_kind = _coerce_kind(
                        r.runner_attributed_kind,
                    )
                    if _is_legacy_downgrade_kind(coerced_kind):
                        outcome_key = "runner_legacy_downgrade"
                        routed = True
                # Slice 5 back-compat shim — only fires when the
                # row has NO structured kind (legacy rows on disk).
                if (
                    not routed
                    and r.runner_attributed_kind is None
                    and is_legacy_contract_downgrade(
                        outcome=outcome_key,
                        notes=r.notes,
                    )
                ):
                    outcome_key = "runner_legacy_downgrade"
            bucket = seen_per_outcome.get(outcome_key)
            if bucket is None:
                continue
            if r.session_id in bucket:
                continue
            bucket.add(r.session_id)
        for k in (
            "clean", "infra", "runner", "migration",
            "runner_legacy_downgrade",
            "runner_incomplete_summary_waived",
        ):
            counts[k] = min(len(seen_per_outcome[k]), MAX_CLEAN_COUNT)
        counts["unique_sessions"] = min(
            len(all_sessions), MAX_CLEAN_COUNT,
        )
        return counts

    def is_eligible(self, flag_name: str) -> bool:
        """True iff the flag has reached its required clean-session
        count AND has zero runner-attributed failures."""
        progress = self.progress(flag_name)
        return (
            progress["clean"] >= progress["required"]
            and progress["runner"] == 0
        )

    def eligible_flags(self) -> List[str]:
        """Return all flag_names eligible to flip."""
        return sorted(
            f for f in _POLICY_BY_FLAG if self.is_eligible(f)
        )

    def all_progress(self) -> Dict[str, Dict[str, int]]:
        """Return progress for every known flag — useful for
        operator overview rendering."""
        return {
            f: self.progress(f) for f in sorted(_POLICY_BY_FLAG)
        }


def _zero_progress(flag_name: str) -> Dict[str, int]:
    policy = _POLICY_BY_FLAG.get(flag_name)
    return {
        "clean": 0, "infra": 0, "runner": 0, "migration": 0,
        # Slice 5 lineage waiver — included for shape parity with
        # the live progress() return so callers never KeyError on
        # the audit bucket regardless of master-on/off state.
        "runner_legacy_downgrade": 0,
        # Slice 7 lineage waiver — empty-summary attribution
        # bucket; same shape-parity contract as Slice 5.
        "runner_incomplete_summary_waived": 0,
        "unique_sessions": 0,
        "required": policy.required_clean_sessions if policy else 3,
    }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_DEFAULT_LEDGER: Optional[GraduationLedger] = None


def get_default_ledger() -> GraduationLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = GraduationLedger()
    return _DEFAULT_LEDGER


def reset_default_ledger() -> None:
    """Test-only: reset the singleton."""
    global _DEFAULT_LEDGER
    _DEFAULT_LEDGER = None


__all__ = [
    "CADENCE_POLICY",
    "CadenceClass",
    "CadencePolicyEntry",
    "GraduationLedger",
    "MAX_CLEAN_COUNT",
    "MAX_LEDGER_FILE_BYTES",
    "MAX_NOTES_CHARS",
    "MAX_RECORDS_LOADED",
    "SessionOutcome",
    "SessionRecord",
    "get_default_ledger",
    "get_policy",
    "is_ledger_enabled",
    "known_flags",
    "ledger_path",
    "reset_default_ledger",
]
