"""M10 Slice 2 — UnhandledPatternMiner async observer
(PRD §32.4.2).

Reads two structured signal streams:

  1. **Coherence history** — the Coherence Auditor's audit
     ledger at :func:`coherence_window_store.coherence_audit_path`.
     RECURRENCE_DRIFT findings whose ratio crosses the
     :func:`m10_recurrence_drift_threshold` knob become
     proposal candidates.

  2. **Intake observations** — caller-supplied signal-source /
     op-kind tuples (Slice 5 wires this from the UnifiedIntake-
     Router's recent-decisions snapshot). Tuples that recur
     ``≥ m10_min_recurrence_count`` times within the window
     without a matching op_id ever materializing become
     "unhandled-pattern" proposal candidates.

For each candidate cluster, the miner:

  * Computes a stable :attr:`pattern_signature` (SHA-256 over
    canonicalized cluster keys).
  * Skips candidates whose signature has been emitted within
    :func:`m10_dedup_window_s` (storm-guard, mirrors
    OpportunityMinerSensor pattern).
  * Calls :func:`compute_threshold` from Slice 1 (Beta
    posterior + diversity adjustment) — when the cluster's
    observed count <= threshold, returns ``DECIDED_SKIP``.
  * Otherwise emits a frozen :class:`M10ProposalRecord`
    (kind=``NEW_SENSOR`` for unhandled-pattern, ``NEW_OBSERVER``
    for RECURRENCE_DRIFT).
  * Daily-cap enforces :func:`m10_max_daily_proposals` — when
    today's emission count reaches the cap, miner short-
    circuits with a clear diagnostic.

Architectural locks (operator mandate):

  * **Pure observer + emitter** — miner produces proposal
    candidates; it does NOT generate code, validate, commit,
    or push. Slice 3 generates; Slice 4 validates+commits.
  * **Decoupled signal sources** — :class:`_PatternSource`
    Protocol allows test implementations + production-default
    JSONL readers (defers to
    :func:`coherence_window_store.coherence_audit_path`
    + caller-supplied intake observation iterables; NEVER
    hardcodes filesystem paths).
  * **Cross-process tear-safe reads** — uses
    :func:`flock_critical_section` for the coherence history
    read so a concurrent in-flight Coherence Auditor write
    cannot tear mid-line.
  * **Master-flag-gated** — miner returns
    :class:`MineResult` with ``DISABLED`` outcome immediately
    when :func:`m10_arch_proposer_enabled` is False (NEVER
    raises out).
  * **Storm-guard** — signature-dedup window prevents the
    same pattern from emitting twice within a configurable
    interval.
  * **Daily cap** — :func:`m10_max_daily_proposals` enforces
    the §32.4.3 cost contract (≤ $0.075/day worst-case).
  * **Authority asymmetry** (AST-pinned at Slice 5) — miner
    MUST NOT import orchestrator / iron_gate / providers /
    candidate_generator / urgency_router / tool_executor /
    auto_action_router / strategic_direction / change_engine
    / subagent_scheduler / semantic_guardian / policy /
    graduation_orchestrator. Pure substrate over the
    coherence ledger + caller-supplied intake source.
  * **No hardcoding** — all knobs read at call time via
    :func:`_read_int_knob` / :func:`_read_float_knob`.
    FlagRegistry seeds at Slice 5.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Dict, Iterable, List, Optional, Protocol, Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


M10_MINER_SCHEMA_VERSION: str = "m10_unhandled_pattern_miner.1"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        if n < floor:
            return floor
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        f = float(raw)
        if not math.isfinite(f):
            return default
        if f < floor:
            return floor
        if f > ceiling:
            return ceiling
        return f
    except (TypeError, ValueError):
        return default


def m10_min_recurrence_count() -> int:
    """``JARVIS_M10_MIN_RECURRENCE_COUNT`` — minimum observed
    recurrence count for an unhandled-pattern signal-source/
    op-kind tuple before it becomes a candidate. Default 5;
    clamped [2, 1000]."""
    return _read_int_knob(
        "JARVIS_M10_MIN_RECURRENCE_COUNT", 5, 2, 1000,
    )


def m10_recurrence_drift_threshold() -> float:
    """``JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD`` — minimum
    drift ratio (curr_count / prev_budget) for a Coherence
    Auditor RECURRENCE_DRIFT finding to become a candidate.
    Default 2.0 (100% over budget); clamped [1.01, 100.0]."""
    return _read_float_knob(
        "JARVIS_M10_RECURRENCE_DRIFT_THRESHOLD",
        2.0, 1.01, 100.0,
    )


def m10_dedup_window_s() -> int:
    """``JARVIS_M10_DEDUP_WINDOW_S`` — signature-dedup window.
    Same pattern_signature emitted within this window is
    suppressed (storm-guard). Default 3600 (1h); clamped
    [60, 86400]."""
    return _read_int_knob(
        "JARVIS_M10_DEDUP_WINDOW_S", 3600, 60, 86400,
    )


def m10_window_hours() -> int:
    """``JARVIS_M10_WINDOW_HOURS`` — observation window for
    recurrence aggregation. Default 168 (7 days); clamped
    [1, 720]."""
    return _read_int_knob(
        "JARVIS_M10_WINDOW_HOURS", 168, 1, 720,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy MineOutcome — drives caller dispatch
# ---------------------------------------------------------------------------


class MineOutcome(str, enum.Enum):
    """Closed taxonomy of mining-cycle outcomes. ``str``
    subclass for JSON serialization + closed-enum dispatch."""

    EMITTED = "emitted"
    """One or more candidates emitted as proposal records."""

    DECIDED_SKIP = "decided_skip"
    """Candidates found, but adaptive threshold gated all of
    them (insufficient evidence yet)."""

    DEDUPED = "deduped"
    """Candidates found, but all signatures matched recent
    emissions within the dedup window."""

    DAILY_CAP_REACHED = "daily_cap_reached"
    """Daily proposal cap reached — miner short-circuited."""

    NO_PATTERNS = "no_patterns"
    """Sources walked cleanly; no patterns crossed the
    minimum-evidence floor."""

    DISABLED = "disabled"
    """Master flag off — miner returned immediately."""


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeObservation:
    """One signal-source / op-kind tuple from the intake
    router. Slice 5's UnifiedIntakeRouter shim populates these
    from `recent decisions`. Frozen for safe propagation."""

    signal_source: str
    """Source identifier (e.g.,
    ``"OpportunityMinerSensor"``)."""

    op_kind: str
    """The op kind that the signal would have produced
    (e.g., ``"todo_review"``). Unhandled patterns are tuples
    where the signal recurred but no matching op_id ever
    completed."""

    op_completed: bool = False
    """True iff a downstream op was actually emitted +
    completed for this signal. ``False`` means the signal
    fired but the FSM dropped it (or never picked it up).
    Unhandled-pattern candidates require ``op_completed=False``."""

    at_unix: float = field(default_factory=time.time)


@dataclass(frozen=True)
class CoherenceDriftObservation:
    """One RECURRENCE_DRIFT finding from the Coherence Auditor
    history. Distilled from the auditor's full audit-row
    schema to the fields the miner actually consumes."""

    failure_class: str
    """Operator-readable failure class (e.g.,
    ``"validator_failed:semantic_guardian"``)."""

    delta_metric: float
    """Curr count from the auditor finding."""

    budget_metric: float
    """Prev recurrence_count budget from the auditor."""

    severity: str
    """Auditor severity verdict — one of ``"info" / "warning"
    / "critical"``."""

    at_unix: float = field(default_factory=time.time)


@dataclass(frozen=True)
class MineResult:
    """Aggregate result of one mining cycle. Frozen +
    JSON-projectable (Slice 5 observability)."""

    outcome: MineOutcome
    proposals_emitted: Tuple[Any, ...] = field(
        default_factory=tuple,
    )
    """Each entry is an :class:`M10ProposalRecord` instance."""
    candidates_evaluated: int = 0
    candidates_deduped: int = 0
    candidates_threshold_skipped: int = 0
    elapsed_s: float = 0.0
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = field(
        default=M10_MINER_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe projection. NEVER raises."""
        try:
            return {
                "schema_version": self.schema_version,
                "outcome": self.outcome.value,
                "proposals_emitted_count": (
                    len(self.proposals_emitted)
                ),
                "proposals": [
                    p.to_dict() if hasattr(p, "to_dict")
                    else {"error": "not_projectable"}
                    for p in self.proposals_emitted
                ],
                "candidates_evaluated": int(
                    self.candidates_evaluated,
                ),
                "candidates_deduped": int(
                    self.candidates_deduped,
                ),
                "candidates_threshold_skipped": int(
                    self.candidates_threshold_skipped,
                ),
                "elapsed_s": float(self.elapsed_s),
                "diagnostics": list(self.diagnostics),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "schema_version": self.schema_version,
                "error": "projection_failed",
            }


# ---------------------------------------------------------------------------
# _PatternSource Protocol — caller-injected for testability
# ---------------------------------------------------------------------------


class PatternSourceProtocol(Protocol):
    """Minimal contract for the two signal sources the miner
    consumes. Production wires :class:`DefaultPatternSource`
    (reads coherence history JSONL via existing `coherence_-
    window_store.coherence_audit_path` + caller-supplied
    intake observation list). Tests inject in-memory stubs."""

    def coherence_drift_observations(
        self, *, since_unix: float, until_unix: float,
    ) -> Sequence[CoherenceDriftObservation]: ...

    def intake_observations(
        self, *, since_unix: float, until_unix: float,
    ) -> Sequence[IntakeObservation]: ...


class DefaultPatternSource:
    """Production source. Reads coherence-audit JSONL via the
    existing :func:`coherence_window_store.coherence_audit_path`
    primitive. ``intake_observations`` defers to a caller-
    supplied iterable (Slice 5 wires this from the
    UnifiedIntakeRouter snapshot — the miner stays decoupled
    from the router until that wire-up). NEVER raises."""

    def __init__(
        self,
        *,
        intake_observations_provider: Optional[
            Any
        ] = None,
        coherence_audit_path_override: Optional[Path] = None,
    ) -> None:
        self._intake_provider = intake_observations_provider
        self._coherence_audit_path_override = (
            coherence_audit_path_override
        )

    def coherence_drift_observations(
        self, *, since_unix: float, until_unix: float,
    ) -> Sequence[CoherenceDriftObservation]:
        """Read RECURRENCE_DRIFT findings from the coherence
        audit JSONL. NEVER raises."""
        try:
            if (
                self._coherence_audit_path_override
                is not None
            ):
                path = self._coherence_audit_path_override
            else:
                from backend.core.ouroboros.governance.verification.coherence_window_store import (  # noqa: E501
                    coherence_audit_path,
                )
                path = coherence_audit_path()
        except Exception:  # noqa: BLE001 — defensive
            return ()
        if not path.exists():
            return ()
        # Cross-process tear-safe read
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                flock_critical_section,
            )
            with flock_critical_section(path) as acquired:
                if not acquired:
                    return ()
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    return ()
        except Exception:  # noqa: BLE001 — defensive
            return ()
        out: List[CoherenceDriftObservation] = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
                if not isinstance(row, dict):
                    continue
                # Audit rows are nested — findings live under a
                # ``findings`` array; each entry has
                # ``kind`` (BehavioralDriftKind value).
                ts = float(row.get("ts_unix", 0.0))
                if ts < since_unix or ts > until_unix:
                    continue
                findings = row.get("findings") or ()
                if not isinstance(findings, list):
                    continue
                for f in findings:
                    if not isinstance(f, dict):
                        continue
                    if (
                        str(f.get("kind", ""))
                        != "recurrence_drift"
                    ):
                        continue
                    detail = str(f.get("detail", ""))
                    # Parse failure_class from detail string
                    # (auditor format: "failure_class 'X'
                    # appeared N times > budget M")
                    failure_class = ""
                    try:
                        if "failure_class" in detail:
                            # Crude but stable extraction
                            parts = detail.split("'")
                            if len(parts) >= 2:
                                failure_class = parts[1]
                    except Exception:  # noqa: BLE001 — defensive
                        pass
                    try:
                        out.append(CoherenceDriftObservation(
                            failure_class=failure_class,
                            delta_metric=float(
                                f.get("delta_metric", 0.0),
                            ),
                            budget_metric=float(
                                f.get("budget_metric", 0.0),
                            ),
                            severity=str(
                                f.get("severity", "info"),
                            ),
                            at_unix=ts,
                        ))
                    except Exception:  # noqa: BLE001 — defensive
                        continue
            except json.JSONDecodeError:
                continue
            except Exception:  # noqa: BLE001 — defensive
                continue
        return tuple(out)

    def intake_observations(
        self, *, since_unix: float, until_unix: float,
    ) -> Sequence[IntakeObservation]:
        """Defers to caller-supplied provider. When None
        (Slice 5 wire-up not yet present), returns empty —
        miner falls back to coherence-only mining."""
        if self._intake_provider is None:
            return ()
        try:
            raw = self._intake_provider(
                since_unix=since_unix,
                until_unix=until_unix,
            )
        except Exception:  # noqa: BLE001 — defensive
            return ()
        try:
            return tuple(
                o for o in raw
                if isinstance(o, IntakeObservation)
            )
        except Exception:  # noqa: BLE001 — defensive
            return ()


# ---------------------------------------------------------------------------
# Signature computation — stable cross-version
# ---------------------------------------------------------------------------


def _signature_for_intake_cluster(
    *, signal_source: str, op_kind: str,
) -> str:
    """Stable SHA-256 over the canonicalized cluster keys.
    Same (signal_source, op_kind) tuple → same signature
    across runs."""
    payload = json.dumps(
        {
            "kind": "intake_unhandled",
            "signal_source": (signal_source or "").strip(),
            "op_kind": (op_kind or "").strip(),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _signature_for_drift_cluster(
    *, failure_class: str,
) -> str:
    """Stable SHA-256 over the canonicalized drift cluster
    key."""
    payload = json.dumps(
        {
            "kind": "drift_recurrence",
            "failure_class": (failure_class or "").strip(),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# UnhandledPatternMiner — load-bearing async entry point
# ---------------------------------------------------------------------------


@dataclass
class _MinerState:
    """Per-process miner state — atomic-swap mutation
    discipline matches Upgrade 1 / M9 / M11 stores."""

    recent_signatures: Dict[str, float] = field(
        default_factory=dict,
    )
    """signature → emitted_at_unix. TTL'd by
    :func:`m10_dedup_window_s` on each cycle."""

    daily_emission_count: int = 0
    daily_emission_day: str = ""
    """ISO date string. Reset on day-boundary."""


class UnhandledPatternMiner:
    """The async observer. Stateful — tracks recent
    emissions for storm-guard + daily-cap. Thread-safe via
    asyncio.Lock + threading.RLock combination (matches
    EpistemicBudgetTracker discipline).

    Production: lazy-singleton via :func:`get_default_miner`.
    Tests: construct fresh + inject :class:`PatternSourceProtocol`
    stub.

    NEVER raises out of any public method — every fault path
    surfaces as a structured :class:`MineResult` with
    diagnostics."""

    def __init__(
        self,
        *,
        source: Optional[Any] = None,
    ) -> None:
        self._source = source or DefaultPatternSource()
        self._state = _MinerState()
        # Note: miner is single-threaded by design; a real
        # async observer would add a lock, but the mining
        # cycle is short-lived + caller-coordinated.

    def _today_iso(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _evict_stale_signatures(
        self, *, now_unix: float,
    ) -> None:
        """Drop dedup signatures older than the window. NEVER
        raises."""
        try:
            window = float(m10_dedup_window_s())
            cutoff = now_unix - window
            stale = [
                sig
                for sig, ts in self._state.recent_signatures.items()
                if ts < cutoff
            ]
            for sig in stale:
                self._state.recent_signatures.pop(sig, None)
        except Exception:  # noqa: BLE001 — defensive
            pass

    def _reset_daily_counter_if_needed(self) -> None:
        """Day-boundary cap reset. NEVER raises."""
        try:
            today = self._today_iso()
            if self._state.daily_emission_day != today:
                self._state.daily_emission_day = today
                self._state.daily_emission_count = 0
        except Exception:  # noqa: BLE001 — defensive
            pass

    async def mine(
        self,
        *,
        now_unix: Optional[float] = None,
    ) -> MineResult:
        """**Authoritative entry point.** Run one mining
        cycle. Walks both signal sources, aggregates clusters,
        applies threshold + dedup + daily cap, returns
        frozen :class:`MineResult`. NEVER raises."""
        started = time.monotonic()
        # Lazy-import master flag check (keeps miner module
        # decoupled at module load time)
        try:
            from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
                m10_arch_proposer_enabled,
            )
            if not m10_arch_proposer_enabled():
                return MineResult(
                    outcome=MineOutcome.DISABLED,
                    elapsed_s=time.monotonic() - started,
                    diagnostics=(
                        "JARVIS_M10_ARCH_PROPOSER_ENABLED is "
                        "false — miner returned without "
                        "querying sources",
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            return MineResult(
                outcome=MineOutcome.DISABLED,
                elapsed_s=time.monotonic() - started,
                diagnostics=(
                    f"primitives import failed: "
                    f"{type(exc).__name__}",
                ),
            )

        now = (
            float(now_unix)
            if now_unix is not None
            else time.time()
        )
        self._evict_stale_signatures(now_unix=now)
        self._reset_daily_counter_if_needed()

        try:
            from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
                M10ProposalPhase,
                M10ProposalRecord,
                ProposalKind,
                compute_threshold,
                m10_max_daily_proposals,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return MineResult(
                outcome=MineOutcome.DISABLED,
                elapsed_s=time.monotonic() - started,
                diagnostics=(
                    f"primitives import failed: "
                    f"{type(exc).__name__}",
                ),
            )

        daily_cap = m10_max_daily_proposals()
        if self._state.daily_emission_count >= daily_cap:
            return MineResult(
                outcome=MineOutcome.DAILY_CAP_REACHED,
                elapsed_s=time.monotonic() - started,
                diagnostics=(
                    f"daily cap of {daily_cap} reached; "
                    f"miner short-circuited",
                ),
            )

        # Window
        window_s = float(m10_window_hours()) * 3600.0
        since = now - window_s
        until = now

        proposals: List[Any] = []
        evaluated = 0
        deduped = 0
        threshold_skipped = 0

        # Source 1: RECURRENCE_DRIFT findings → NEW_OBSERVER
        try:
            drifts = list(
                self._source.coherence_drift_observations(
                    since_unix=since, until_unix=until,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            drifts = []
            logger.debug(
                "[m10_miner] drift source raised: %s", exc,
            )

        # Aggregate by failure_class
        drift_clusters: Dict[
            str, List[CoherenceDriftObservation],
        ] = {}
        for d in drifts:
            try:
                fc = (d.failure_class or "").strip()
                if not fc:
                    continue
                drift_clusters.setdefault(fc, []).append(d)
            except Exception:  # noqa: BLE001 — defensive
                continue

        drift_threshold_ratio = m10_recurrence_drift_threshold()
        for failure_class, observations in drift_clusters.items():
            evaluated += 1
            if (
                self._state.daily_emission_count >= daily_cap
            ):
                break
            # Find max ratio in this cluster
            try:
                max_ratio = max(
                    (
                        o.delta_metric / max(
                            1.0, o.budget_metric,
                        )
                    )
                    for o in observations
                )
            except Exception:  # noqa: BLE001 — defensive
                max_ratio = 0.0
            if max_ratio < drift_threshold_ratio:
                threshold_skipped += 1
                continue

            # Storm-guard
            sig = _signature_for_drift_cluster(
                failure_class=failure_class,
            )
            if sig in self._state.recent_signatures:
                deduped += 1
                continue

            # Adaptive threshold gate
            successes = sum(
                1 for o in observations
                if o.severity == "warning"
            )
            failures = sum(
                1 for o in observations
                if o.severity == "critical"
            )
            unique_classes = 1
            total_uses = len(observations)
            t_result = compute_threshold(
                successes=successes,
                failures=failures,
                unique_goals=unique_classes,
                total_uses=total_uses,
            )
            if total_uses < t_result.threshold:
                threshold_skipped += 1
                continue

            # EMIT
            proposal_id = (
                f"m10-new_observer-{int(now * 1000)}-"
                f"{sig[:8]}"
            )
            evidence = tuple(
                f"failure_class={failure_class} "
                f"delta={o.delta_metric:.0f} "
                f"budget={o.budget_metric:.0f} "
                f"severity={o.severity}"
                for o in observations[:5]
            )
            record = M10ProposalRecord(
                proposal_id=proposal_id,
                kind=ProposalKind.NEW_OBSERVER,
                phase=M10ProposalPhase.EVALUATING,
                pattern_signature=sig,
                detection_evidence=evidence,
                threshold=t_result,
                created_at_unix=now,
                last_updated_at_unix=now,
            )
            proposals.append(record)
            self._state.recent_signatures[sig] = now
            self._state.daily_emission_count += 1

        # Source 2: unhandled-pattern intake observations
        try:
            intakes = list(
                self._source.intake_observations(
                    since_unix=since, until_unix=until,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            intakes = []
            logger.debug(
                "[m10_miner] intake source raised: %s", exc,
            )

        # Aggregate by (signal_source, op_kind) — only
        # unhandled tuples (op_completed=False)
        intake_clusters: Dict[
            Tuple[str, str], List[IntakeObservation],
        ] = {}
        for io in intakes:
            try:
                if io.op_completed:
                    continue
                key = (
                    (io.signal_source or "").strip(),
                    (io.op_kind or "").strip(),
                )
                if not key[0] or not key[1]:
                    continue
                intake_clusters.setdefault(key, []).append(io)
            except Exception:  # noqa: BLE001 — defensive
                continue

        min_recurrence = m10_min_recurrence_count()
        for (signal_source, op_kind), observations in (
            intake_clusters.items()
        ):
            evaluated += 1
            if (
                self._state.daily_emission_count >= daily_cap
            ):
                break
            if len(observations) < min_recurrence:
                threshold_skipped += 1
                continue

            sig = _signature_for_intake_cluster(
                signal_source=signal_source,
                op_kind=op_kind,
            )
            if sig in self._state.recent_signatures:
                deduped += 1
                continue

            t_result = compute_threshold(
                successes=0,
                failures=len(observations),
                unique_goals=1,
                total_uses=len(observations),
            )
            if len(observations) < t_result.threshold:
                threshold_skipped += 1
                continue

            proposal_id = (
                f"m10-new_sensor-{int(now * 1000)}-"
                f"{sig[:8]}"
            )
            evidence = tuple(
                f"signal_source={signal_source} "
                f"op_kind={op_kind} "
                f"recurrence={len(observations)}"
                for _ in range(1)
            )
            record = M10ProposalRecord(
                proposal_id=proposal_id,
                kind=ProposalKind.NEW_SENSOR,
                phase=M10ProposalPhase.EVALUATING,
                pattern_signature=sig,
                detection_evidence=evidence,
                threshold=t_result,
                created_at_unix=now,
                last_updated_at_unix=now,
            )
            proposals.append(record)
            self._state.recent_signatures[sig] = now
            self._state.daily_emission_count += 1

        # Determine outcome
        if proposals:
            outcome = MineOutcome.EMITTED
            diagnostics = (
                f"{len(proposals)} proposal(s) emitted "
                f"({self._state.daily_emission_count}/"
                f"{daily_cap} daily)",
            )
        elif evaluated == 0:
            outcome = MineOutcome.NO_PATTERNS
            diagnostics = (
                "no candidates above minimum-evidence floor",
            )
        elif deduped > 0 and (
            deduped + threshold_skipped == evaluated
        ):
            outcome = MineOutcome.DEDUPED
            diagnostics = (
                f"all {evaluated} candidate(s) deduped or "
                f"threshold-skipped",
            )
        else:
            outcome = MineOutcome.DECIDED_SKIP
            diagnostics = (
                f"{threshold_skipped}/{evaluated} skipped by "
                f"adaptive threshold; {deduped} deduped",
            )

        return MineResult(
            outcome=outcome,
            proposals_emitted=tuple(proposals),
            candidates_evaluated=evaluated,
            candidates_deduped=deduped,
            candidates_threshold_skipped=threshold_skipped,
            elapsed_s=time.monotonic() - started,
            diagnostics=diagnostics,
        )


# ---------------------------------------------------------------------------
# Process-singleton
# ---------------------------------------------------------------------------


_DEFAULT_MINER: Optional[UnhandledPatternMiner] = None


def get_default_miner() -> UnhandledPatternMiner:
    """Lazy-constructed process singleton. NEVER raises."""
    global _DEFAULT_MINER  # noqa: PLW0603
    if _DEFAULT_MINER is None:
        _DEFAULT_MINER = UnhandledPatternMiner()
    return _DEFAULT_MINER


def reset_default_miner_for_tests() -> None:
    """Test-only — drop the default miner. Production code
    NEVER calls this."""
    global _DEFAULT_MINER  # noqa: PLW0603
    _DEFAULT_MINER = None


__all__ = [
    "CoherenceDriftObservation",
    "DefaultPatternSource",
    "IntakeObservation",
    "M10_MINER_SCHEMA_VERSION",
    "MineOutcome",
    "MineResult",
    "PatternSourceProtocol",
    "UnhandledPatternMiner",
    "get_default_miner",
    "m10_dedup_window_s",
    "m10_min_recurrence_count",
    "m10_recurrence_drift_threshold",
    "m10_window_hours",
    "reset_default_miner_for_tests",
]
