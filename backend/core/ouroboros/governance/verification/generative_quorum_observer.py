"""Slice 5b C — Generative Quorum bounded JSONL observer.

Per-op-triggered (NOT periodic) recorder for ``QuorumRunResult``
artifacts. Mirrors the SBT / CIGW recorder discipline structurally
but omits the periodic-observer machinery — Move 6 is consensus-
per-op, not time-driven, so there is no separate aggregation pass.

Substrate reuse — zero new primitives:

  * ``cross_process_jsonl.flock_append_line`` — flock'd append
    (Move 6 is concurrent-safe across L3 worktree fan-out).
  * ``cross_process_jsonl.flock_critical_section`` — for ring-
    buffer rotation under read-modify-write.
  * ``QuorumRunResult.to_dict`` — canonical serialization.
  * ``ide_observability_stream.stream_enabled`` /
    ``get_default_broker`` — SSE plumbing already exists in
    ``generative_quorum_runner.publish_quorum_outcome`` (we do not
    re-publish; persistence is a separate concern).

Public surface:

  * :func:`record_quorum_run`            — append one run to JSONL
  * :func:`read_quorum_history`          — bounded recent-window
    read with optional limit + since_ts filters
  * :func:`compute_recent_quorum_stats`  — derived insights:
    outcome distribution, avg elapsed, avg agreement, failed-roll
    rate, stability score (CONSENSUS fraction)

Master gating:

  * ``JARVIS_GENERATIVE_QUORUM_ENABLED`` (Slice 1) — required-on
    sentinel; if off, recorder short-circuits to DISABLED.
  * ``JARVIS_QUORUM_OBSERVER_ENABLED`` (this slice) — sub-flag so
    operators can disable persistence without disabling consensus.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + verification.generative_quorum primitive +
    governance.cross_process_jsonl helper ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / tool_executor.
  * Never raises out of any public function — all faults map to
    a closed RecordOutcome enum.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)
from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
    ConsensusOutcome,
    GENERATIVE_QUORUM_SCHEMA_VERSION,
    quorum_enabled,
)
from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
    GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION,
    QuorumRunResult,
)

logger = logging.getLogger(__name__)


GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION: str = (
    "generative_quorum_observer.1"
)


# ---------------------------------------------------------------------------
# Env-knob helpers — same shape + clamping discipline as SBT/CIGW
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str,
    default: int,
    floor: int,
    ceiling: int,
) -> int:
    """Bounded integer env-knob read. NEVER raises."""
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


def quorum_observer_enabled() -> bool:
    """``JARVIS_QUORUM_OBSERVER_ENABLED`` — observer sub-flag.
    Default ``true`` post-Slice 5b C graduation. Asymmetric env
    semantics: any truthy literal enables, anything else (including
    ``""``) leaves the default.

    Composes with the master ``quorum_enabled()`` (Slice 1):
    BOTH must be true for ``record_quorum_run`` to persist."""
    raw = os.environ.get(
        "JARVIS_QUORUM_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    # Default — graduated post-Slice 5b C.
    return True


def quorum_history_dir() -> Path:
    """``JARVIS_QUORUM_HISTORY_DIR`` — base directory for the
    bounded ring-buffer JSONL. Default ``.jarvis/quorum_history``,
    same root convention as SBT / CIGW / coherence."""
    raw = os.environ.get(
        "JARVIS_QUORUM_HISTORY_DIR", ".jarvis/quorum_history",
    ).strip()
    return Path(raw or ".jarvis/quorum_history")


def quorum_history_path() -> Path:
    """``<quorum_history_dir()>/quorum.jsonl``. Per-process callers
    consume this directly; flock'd writers + readers all resolve
    via this helper so the path is consistent."""
    return quorum_history_dir() / "quorum.jsonl"


def quorum_history_max_records() -> int:
    """``JARVIS_QUORUM_HISTORY_MAX_RECORDS`` — bounded ring-buffer
    cap. Default 1000, clamped [10, 100000]. Rotation truncates after
    each append (same discipline as SBT + InvariantDriftStore)."""
    return _read_int_knob(
        "JARVIS_QUORUM_HISTORY_MAX_RECORDS",
        1000, 10, 100_000,
    )


def quorum_recent_stats_window() -> int:
    """``JARVIS_QUORUM_RECENT_STATS_WINDOW`` — default sample window
    for ``compute_recent_quorum_stats``. Default 200, clamped
    [10, max_records]."""
    cap = quorum_history_max_records()
    return _read_int_knob(
        "JARVIS_QUORUM_RECENT_STATS_WINDOW",
        min(200, cap), 10, cap,
    )


# ---------------------------------------------------------------------------
# RecordOutcome — closed taxonomy for record_quorum_run
# ---------------------------------------------------------------------------


class RecordOutcome(str, enum.Enum):
    """5-value closed taxonomy for ``record_quorum_run``. Mirrors
    SBT's RecordOutcome; caller branches on the enum, never on
    free-form fields."""

    OK = "ok"
    """Record landed in the JSONL store."""

    DISABLED = "disabled"
    """Master flag or observer sub-flag is off — no record."""

    REJECTED = "rejected"
    """Garbage input (non-QuorumRunResult or DISABLED outcome with
    no rolls). Quorum runs that DISABLED for cost-contract reasons
    are intentionally not persisted (zero noise floor)."""

    PERSIST_ERROR = "persist_error"
    """Disk fault during flock'd append. Caller treats the run as
    unrecorded but should not assume process state is bad."""

    SERIALIZE_ERROR = "serialize_error"
    """``QuorumRunResult.to_dict`` returned a non-JSON-serializable
    object (should not happen given the frozen dataclass contract,
    but defensive)."""


# ---------------------------------------------------------------------------
# Stamped record — recorded_at_ts + op_id wrapper around the run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StampedQuorumRun:
    """JSONL record shape — frozen for safe propagation. Combines
    one ``QuorumRunResult.to_dict()`` payload with ingestion-time
    metadata (recorded_at_ts, op_id, schema_version)."""

    op_id: str
    recorded_at_ts: float
    run: Mapping[str, Any]
    schema_version: str = (
        GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "recorded_at_ts": self.recorded_at_ts,
            "run": dict(self.run),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Public: record_quorum_run
# ---------------------------------------------------------------------------


def record_quorum_run(
    result: QuorumRunResult,
    *,
    op_id: str = "",
    enabled_override: Optional[bool] = None,
    now_ts: Optional[float] = None,
) -> RecordOutcome:
    """Persist one ``QuorumRunResult`` to the bounded JSONL store.

    Decision tree:

      1. Flag check — ``enabled_override`` OR
         (``quorum_enabled()`` AND ``quorum_observer_enabled()``).
      2. Type check + DISABLED skip — DISABLED outcomes are not
         persisted (zero noise floor when consensus is master-off).
      3. Stamp + serialize via ``QuorumRunResult.to_dict``.
      4. flock'd append.
      5. Best-effort ring-buffer rotation under flock'd critical
         section.

    NEVER raises. All faults map to closed RecordOutcome."""
    try:
        # Step 1 — flag check
        if enabled_override is False:
            return RecordOutcome.DISABLED
        if enabled_override is None:
            if not quorum_enabled():
                return RecordOutcome.DISABLED
            if not quorum_observer_enabled():
                return RecordOutcome.DISABLED

        # Step 2 — type check
        if not isinstance(result, QuorumRunResult):
            return RecordOutcome.REJECTED
        verdict = result.verdict
        if verdict.outcome is ConsensusOutcome.DISABLED:
            # DISABLED outcomes carry zero observational signal +
            # would inflate the ring buffer with no-ops.
            return RecordOutcome.REJECTED

        # Step 3 — stamp + serialize
        ts = now_ts if now_ts is not None else time.time()
        stamped = StampedQuorumRun(
            op_id=str(op_id or ""),
            recorded_at_ts=float(ts),
            run=result.to_dict(),
        )
        try:
            line = json.dumps(
                stamped.to_dict(),
                sort_keys=True,
                ensure_ascii=True,
            )
        except (TypeError, ValueError) as exc:
            logger.debug(
                "[QuorumObserver] serialize failed: %s", exc,
            )
            return RecordOutcome.SERIALIZE_ERROR

        # Step 4 — flock'd append
        path = quorum_history_path()
        appended = flock_append_line(path, line)
        if not appended:
            return RecordOutcome.PERSIST_ERROR

        # Step 5 — rotate ring buffer best-effort
        try:
            _rotate_history(path, quorum_history_max_records())
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[QuorumObserver] rotate failed: %s", exc,
            )

        return RecordOutcome.OK
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[QuorumObserver] record_quorum_run raised: %s", exc,
        )
        return RecordOutcome.PERSIST_ERROR


def _rotate_history(path: Path, max_records: int) -> bool:
    """Truncate the JSONL to the last ``max_records`` lines under a
    flock'd critical section. Same read-modify-write discipline as
    SBT + InvariantDriftStore. NEVER raises."""
    if max_records < 1:
        return False
    if not path.exists():
        return True
    try:
        with flock_critical_section(path) as acquired:
            if not acquired:
                return False
            try:
                with path.open("r", encoding="utf-8") as fh:
                    lines = [ln for ln in fh if ln.strip()]
            except OSError:
                return False
            if len(lines) <= max_records:
                return True
            tail = lines[-max_records:]
            try:
                with path.open("w", encoding="utf-8") as fh:
                    for ln in tail:
                        if not ln.endswith("\n"):
                            ln = ln + "\n"
                        fh.write(ln)
                    fh.flush()
                return True
            except OSError:
                return False
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[QuorumObserver] _rotate_history: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Public: read_quorum_history — bounded recent-window read
# ---------------------------------------------------------------------------


def _parse_stamped_line(raw: str) -> Optional[StampedQuorumRun]:
    """Parse one JSONL line into a StampedQuorumRun. NEVER raises —
    corrupt lines return None."""
    try:
        s = raw.strip()
        if not s:
            return None
        payload = json.loads(s)
        if not isinstance(payload, Mapping):
            return None
        run_dict = payload.get("run")
        if not isinstance(run_dict, Mapping):
            return None
        try:
            ts = float(payload.get("recorded_at_ts", 0.0))
        except (TypeError, ValueError):
            ts = 0.0
        return StampedQuorumRun(
            op_id=str(payload.get("op_id", "")),
            recorded_at_ts=ts,
            run=dict(run_dict),
            schema_version=str(
                payload.get(
                    "schema_version",
                    GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION,
                ),
            ),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug(
            "[QuorumObserver] _parse_stamped_line corrupt: %s", exc,
        )
        return None


def read_quorum_history(
    *,
    limit: Optional[int] = None,
    since_ts: float = 0.0,
) -> Tuple[StampedQuorumRun, ...]:
    """Read up to the last ``limit`` records from the JSONL store
    with ``recorded_at_ts >= since_ts``.

    ``limit=None`` → returns all records (capped at
    :func:`quorum_history_max_records`).

    NEVER raises. Returns empty tuple on missing file or any parse
    fault. Tolerates corrupt lines (skipped silently)."""
    try:
        path = quorum_history_path()
        if not path.exists():
            return ()
        cap = (
            int(limit) if limit is not None
            else quorum_history_max_records()
        )
        cap = max(0, min(cap, quorum_history_max_records()))
        if cap == 0:
            return ()

        try:
            with path.open("r", encoding="utf-8") as fh:
                lines = [ln for ln in fh if ln.strip()]
        except OSError:
            return ()
        if not lines:
            return ()

        result: List[StampedQuorumRun] = []
        for raw in lines:
            stamped = _parse_stamped_line(raw)
            if stamped is None:
                continue
            if stamped.recorded_at_ts < float(since_ts or 0.0):
                continue
            result.append(stamped)

        # Chronological ascending; tail clamp.
        result.sort(key=lambda s: s.recorded_at_ts)
        if cap < len(result):
            result = result[-cap:]
        return tuple(result)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[QuorumObserver] read_quorum_history raised: %s", exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Public: compute_recent_quorum_stats — adaptive insights
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuorumStatsReport:
    """Aggregate stats over a bounded recent-window. Frozen.

    Fields express *fractions* (in [0, 1]) when relevant, integers
    when counts. ``stability_score`` is the strongest empirical
    signal — fraction of recent runs that hit unanimous CONSENSUS
    (highest-confidence outcome)."""

    sample_size: int
    outcome_distribution: Mapping[str, int] = field(
        default_factory=dict,
    )
    avg_elapsed_seconds: float = 0.0
    avg_agreement_count: float = 0.0
    avg_distinct_signatures: float = 0.0
    avg_failed_roll_fraction: float = 0.0
    stability_score: float = 0.0
    actionable_score: float = 0.0
    most_recent_signature: Optional[str] = None
    most_recent_outcome: Optional[str] = None
    most_recent_op_id: Optional[str] = None
    schema_version: str = (
        GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "outcome_distribution": dict(
                self.outcome_distribution,
            ),
            "avg_elapsed_seconds": self.avg_elapsed_seconds,
            "avg_agreement_count": self.avg_agreement_count,
            "avg_distinct_signatures": (
                self.avg_distinct_signatures
            ),
            "avg_failed_roll_fraction": (
                self.avg_failed_roll_fraction
            ),
            "stability_score": self.stability_score,
            "actionable_score": self.actionable_score,
            "most_recent_signature": (
                self.most_recent_signature
            ),
            "most_recent_outcome": self.most_recent_outcome,
            "most_recent_op_id": self.most_recent_op_id,
            "schema_version": self.schema_version,
        }


_EMPTY_STATS = QuorumStatsReport(sample_size=0)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compute_recent_quorum_stats(
    *,
    limit: Optional[int] = None,
    since_ts: float = 0.0,
) -> QuorumStatsReport:
    """Aggregate insights over the last ``limit`` records (default
    :func:`quorum_recent_stats_window`).

    Computes:

      * ``outcome_distribution``    — count per ConsensusOutcome.
      * ``avg_elapsed_seconds``     — wall-clock per run.
      * ``avg_agreement_count``     — agreement count per run.
      * ``avg_distinct_signatures`` — distinct-AST count per run
        (signal of generation diversity).
      * ``avg_failed_roll_fraction`` — failed/total per run, then
        averaged. Captures generator reliability over the window.
      * ``stability_score``         — fraction of runs hitting
        unanimous CONSENSUS. Strongest empirical signal.
      * ``actionable_score``        — fraction hitting CONSENSUS
        OR MAJORITY_CONSENSUS — broader actionability indicator.
      * ``most_recent_*``           — last run summary for replay.

    NEVER raises. Empty/missing history → ``_EMPTY_STATS``."""
    try:
        sample_cap = (
            int(limit) if limit is not None
            else quorum_recent_stats_window()
        )
        if sample_cap < 1:
            return _EMPTY_STATS
        history = read_quorum_history(
            limit=sample_cap, since_ts=since_ts,
        )
        if not history:
            return _EMPTY_STATS

        n = len(history)
        outcome_dist: Dict[str, int] = {}
        elapsed_sum = 0.0
        agreement_sum = 0
        distinct_sum = 0
        failed_fraction_sum = 0.0
        consensus_count = 0
        actionable_count = 0

        for stamped in history:
            run = stamped.run
            verdict = run.get("verdict") if isinstance(
                run, Mapping,
            ) else None
            outcome = (
                str(verdict.get("outcome", ""))
                if isinstance(verdict, Mapping)
                else ""
            )
            outcome_dist[outcome] = (
                outcome_dist.get(outcome, 0) + 1
            )
            if outcome == ConsensusOutcome.CONSENSUS.value:
                consensus_count += 1
                actionable_count += 1
            elif outcome == (
                ConsensusOutcome.MAJORITY_CONSENSUS.value
            ):
                actionable_count += 1
            elapsed_sum += _safe_float(
                run.get("elapsed_seconds") if isinstance(
                    run, Mapping,
                ) else 0.0,
            )
            if isinstance(verdict, Mapping):
                agreement_sum += _safe_int(
                    verdict.get("agreement_count"),
                )
                distinct_sum += _safe_int(
                    verdict.get("distinct_count"),
                )
                total_rolls = _safe_int(
                    verdict.get("total_rolls"),
                )
            else:
                total_rolls = 0
            failed_ids = (
                run.get("failed_roll_ids")
                if isinstance(run, Mapping) else None
            )
            failed_count = (
                len(failed_ids)
                if isinstance(failed_ids, list) else 0
            )
            if total_rolls > 0:
                failed_fraction_sum += (
                    failed_count / total_rolls
                )

        last = history[-1]
        last_run = last.run if isinstance(last.run, Mapping) else {}
        last_verdict = (
            last_run.get("verdict")
            if isinstance(last_run, Mapping) else None
        )
        most_recent_outcome = (
            str(last_verdict.get("outcome"))
            if isinstance(last_verdict, Mapping) else None
        )
        most_recent_signature = (
            last_verdict.get("canonical_signature")
            if isinstance(last_verdict, Mapping) else None
        )

        return QuorumStatsReport(
            sample_size=n,
            outcome_distribution=outcome_dist,
            avg_elapsed_seconds=elapsed_sum / n,
            avg_agreement_count=agreement_sum / n,
            avg_distinct_signatures=distinct_sum / n,
            avg_failed_roll_fraction=failed_fraction_sum / n,
            stability_score=consensus_count / n,
            actionable_score=actionable_count / n,
            most_recent_signature=(
                str(most_recent_signature)
                if most_recent_signature is not None else None
            ),
            most_recent_outcome=most_recent_outcome,
            most_recent_op_id=last.op_id or None,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[QuorumObserver] compute_recent_quorum_stats: %s",
            exc,
        )
        return _EMPTY_STATS


__all__ = [
    "GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION",
    "QuorumStatsReport",
    "RecordOutcome",
    "StampedQuorumRun",
    "compute_recent_quorum_stats",
    "quorum_history_dir",
    "quorum_history_max_records",
    "quorum_history_path",
    "quorum_observer_enabled",
    "quorum_recent_stats_window",
    "read_quorum_history",
    "record_quorum_run",
    # Re-exports for downstream consumer convenience
    "GENERATIVE_QUORUM_SCHEMA_VERSION",
    "GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION",
]
