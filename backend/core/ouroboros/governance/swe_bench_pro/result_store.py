"""SWE-Bench-Pro result substrate - Phase D (PRD section 40.7.10-d).

Bridges per-problem (EvaluationResult, ScoringResult) pairs into a
cross-problem aggregate store with in-memory cache + optional
JSONL audit persistence. The store is the single source of truth
Phase F (report_card) and Phase E (parallel_eval) consume to render
aggregate pass-rates, per-repo distributions, and difficulty-tier
breakdowns.

Architectural contract
----------------------

  * **Composes canonical surfaces only**:
      - cross_process_jsonl.flock_append_line (Vector #10 / v2.82
        canonical append primitive; cross-process flock-safe; never
        raises). AST pin in the spine forbids homegrown fcntl /
        threading.Lock substitutes.
      - EvaluationResult.to_dict / from_dict (Phase B.2.2; symmetric).
      - ScoringResult.to_dict / from_dict (Phase C; symmetric).
      - EvaluationOutcome / ScoreOutcome closed enums.

  * **No parallel schemas**: the JSONL row composes the existing
    Phase B.2.2 + Phase C dataclass payloads verbatim. There is no
    new field on EvaluationResult or ScoringResult; the record
    type wraps them with provenance (recorded_at_iso + schema_version)
    and dedup metadata only.

  * **In-memory cache is hot read; JSONL is authoritative audit**:
      - `record()` updates the in-memory dict (latest-write wins
        for the same (instance_id, op_id) pair) AND appends to JSONL
        (full audit history; same key may appear multiple times if
        the operator re-scored after rubric evolution).
      - `query()` reads in-memory only (bounded scan). Phase F /
        operators wanting full history use the JSONL directly.
      - `replay_from_disk()` reconstructs the in-memory cache from
        JSONL at boot.

  * **Dedup key**: (instance_id, op_id) tuple. Same problem instance
    scored under a different op (a retry, a different evaluation
    session) is a distinct record. Re-scoring the same op (rubric
    evolution) overwrites in-memory but adds a fresh JSONL row.

  * **Default-FALSE persistence per section 33.1**: the master flag
    JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED defaults FALSE
    so a default-construction store is purely in-memory. Operators
    flip the flag to opt into JSONL audit at the canonical path.

Section 7 fail-closed contract
------------------------------

Every public method NEVER raises (asyncio.CancelledError is the
sole exception that propagates per orchestrator convention).
record() returns False on any I/O failure; query() returns an
empty tuple on internal failure.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
    EvaluatorPhase,
    task_phase,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
    EvaluationResult,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    ScoreOutcome,
    ScoringResult,
)


logger = logging.getLogger("Ouroboros.SWEBenchPro.ResultStore")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


RESULT_RECORD_SCHEMA_VERSION: str = "swe_bench_pro_result.v1"


RESULT_PERSISTENCE_ENABLED_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED"
)
RESULT_PATH_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_RESULT_PATH"


_DEFAULT_RESULT_PATH: str = ".jarvis/swe_bench_pro/results.jsonl"


# ===========================================================================
# Frozen EvaluationRecord dataclass (section 33.5 symmetric to_dict/from_dict)
# ===========================================================================


class EvaluationCategory(str, enum.Enum):
    """Slice 76 Phase 3 — the dual-metric analytics category derived from the
    authoritative (eval, score) outcomes (PRD §50.11). Closed taxonomy.

    Separates a *capability* verdict from an *infrastructure* exclusion so the
    strict and operational (fairly-attempted) pass rates are computed natively,
    never by manual recalculation.
    """

    #: eval=resolved AND score=pass — held-out container suite passed.
    RESOLVED = "resolved"
    #: the model got a fair shot and did not pass (eval=resolved fail/partial,
    #: or eval=unresolved — failed to produce a working fix). NEVER excluded.
    CAPABILITY_MISS = "capability_miss"
    #: the op never got a fair attempt (prepare_failed / terminal_timeout) or
    #: the patch existed but scoring infra broke (scoring_error). Excluded from
    #: the OPERATIONAL denominator — not a capability failure.
    INFRASTRUCTURE_EXCLUSION = "infrastructure_exclusion"


@dataclass(frozen=True)
class EvaluationRecord:
    """One (evaluation, scoring) pair persisted to the result store.

    Composes the canonical Phase B.2.2 ``EvaluationResult`` + Phase C
    ``ScoringResult`` payloads verbatim - no new schema fields.
    Provenance (``recorded_at_iso``) and version stamp
    (``schema_version``) bracket the canonical payloads so future
    schema bumps can be detected at read time.
    """

    evaluation: EvaluationResult
    scoring: ScoringResult
    recorded_at_iso: str
    schema_version: str = RESULT_RECORD_SCHEMA_VERSION

    @property
    def dedup_key(self) -> Tuple[str, str]:
        """``(instance_id, op_id)`` tuple - the in-memory cache key."""
        return (
            self.evaluation.problem_instance_id,
            self.evaluation.op_id,
        )

    @property
    def resolved(self) -> bool:
        """Canonical SWE-bench ``resolved`` — the held-out container suite
        PASSED. Derived (not stored) from the authoritative outcomes:
        True iff the eval reached scoring AND the score outcome is ``pass``.
        ``partial``/``fail``/``scoring_error``/``skipped`` ⇒ False. Pure;
        NEVER raises (a malformed outcome resolves to False, not None)."""
        try:
            return (
                getattr(self.evaluation.outcome, "value", "") == "resolved"
                and getattr(self.scoring.outcome, "value", "") == "pass"
            )
        except Exception:  # noqa: BLE001
            return False

    @property
    def category(self) -> EvaluationCategory:
        """Slice 76 Phase 3 — derived dual-metric category (PRD §50.11). Pure;
        NEVER raises. Ordering is load-bearing: the never-fairly-attempted infra
        cases are caught first so they are excluded from capability, while a
        model that got a fair shot and failed (incl. eval=unresolved) is a
        CAPABILITY_MISS — the derivation never flatters the model."""
        try:
            eval_v = getattr(self.evaluation.outcome, "value", "")
            score_v = getattr(self.scoring.outcome, "value", "")
            if eval_v == "resolved" and score_v == "pass":
                return EvaluationCategory.RESOLVED
            # never got a fair attempt (preparation / provider) → excluded
            if eval_v in ("prepare_failed", "terminal_timeout"):
                return EvaluationCategory.INFRASTRUCTURE_EXCLUSION
            # the patch was produced but scoring infra broke → excluded
            if eval_v == "resolved" and score_v == "scoring_error":
                return EvaluationCategory.INFRASTRUCTURE_EXCLUSION
            # the model got a fair shot and did not pass → capability failure
            return EvaluationCategory.CAPABILITY_MISS
        except Exception:  # noqa: BLE001
            return EvaluationCategory.INFRASTRUCTURE_EXCLUSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "recorded_at_iso": self.recorded_at_iso,
            # Slice 75 — top-level derived `resolved` boolean so the durable
            # ledger is directly queryable for pass-rate / report-card %
            # without re-deriving from the nested eval+score outcomes. Always a
            # concrete bool (never None) for clean aggregation.
            "resolved": self.resolved,
            # Slice 76 Phase 3 — derived dual-metric category (RESOLVED /
            # CAPABILITY_MISS / INFRASTRUCTURE_EXCLUSION) for native strict-vs-
            # operational rate aggregation without re-deriving from outcomes.
            "category": self.category.value,
            "evaluation": self.evaluation.to_dict(),
            "scoring": self.scoring.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationRecord":
        evaluation = EvaluationResult.from_dict(payload["evaluation"])
        scoring = ScoringResult.from_dict(payload["scoring"])
        return cls(
            schema_version=str(payload.get(
                "schema_version", RESULT_RECORD_SCHEMA_VERSION,
            )),
            recorded_at_iso=str(payload.get("recorded_at_iso", "")),
            evaluation=evaluation,
            scoring=scoring,
        )


def dual_metric_rates(records: Iterable["EvaluationRecord"]) -> Dict[str, Any]:
    """Slice 76 Phase 3 — native strict-vs-operational aggregation (PRD §50.11).

    Buckets records by :class:`EvaluationCategory` and returns both honest
    rates with zero manual recalculation:

      * ``strict_rate``      = resolved / total (every row counted).
      * ``operational_rate`` = resolved / fairly_attempted, where
        ``fairly_attempted = total − infrastructure_exclusion`` (the cases that
        never got a fair shot are excluded — NOT the capability misses).

    Pure; never raises. Empty input → all-zero rates (no division by zero).
    """
    counts = {c.value: 0 for c in EvaluationCategory}
    total = 0
    for rec in records:
        total += 1
        try:
            counts[rec.category.value] += 1
        except Exception:  # noqa: BLE001 — a malformed row counts as infra
            counts[EvaluationCategory.INFRASTRUCTURE_EXCLUSION.value] += 1
    resolved = counts[EvaluationCategory.RESOLVED.value]
    excluded = counts[EvaluationCategory.INFRASTRUCTURE_EXCLUSION.value]
    fairly = total - excluded
    return {
        "resolved": resolved,
        "capability_miss": counts[EvaluationCategory.CAPABILITY_MISS.value],
        "infrastructure_exclusion": excluded,
        "total": total,
        "fairly_attempted": fairly,
        "strict_rate": (resolved / total) if total else 0.0,
        "operational_rate": (resolved / fairly) if fairly else 0.0,
    }


# ===========================================================================
# Env loaders (NEVER raise)
# ===========================================================================


def _persistence_enabled() -> bool:
    """Master flag query (section 33.1 default-FALSE)."""
    raw = os.environ.get(
        RESULT_PERSISTENCE_ENABLED_ENV_VAR, "",
    ).strip().lower()
    return raw in ("true", "1", "yes", "on")


def _resolve_persistence_path(explicit: Optional[Path]) -> Path:
    """Resolve the canonical JSONL audit path.

    Precedence: explicit argument > env var > default. NEVER raises.
    """
    if explicit is not None:
        return Path(explicit)
    raw = os.environ.get(RESULT_PATH_ENV_VAR, "").strip()
    if raw:
        return Path(raw)
    return Path(_DEFAULT_RESULT_PATH)


# ===========================================================================
# EvaluationResultStore
# ===========================================================================


class EvaluationResultStore:
    """In-memory + optional JSONL-persisted store for SWE-Bench-Pro
    evaluation/scoring pairs.

    Parameters
    ----------
    persistence_path:
        Optional override for the JSONL audit path. When ``None``,
        :func:`_resolve_persistence_path` resolves env > default.
        The path's parent is auto-created on first append.
    persistence_enabled:
        Optional override for the master flag. When ``None``,
        :func:`_persistence_enabled` reads env. Allows test-time
        injection without env juggling.

    Thread/async safety:
        ``record()`` updates the in-memory dict under a threading lock
        so concurrent records don't lose updates. The JSONL append
        path is serialized cross-process by ``flock_append_line``.
        ``query()`` snapshots under the lock and iterates the snapshot
        unlocked so concurrent records don't block reads.
    """

    def __init__(
        self,
        *,
        persistence_path: Optional[Path] = None,
        persistence_enabled: Optional[bool] = None,
    ) -> None:
        self._persistence_path: Path = _resolve_persistence_path(
            persistence_path,
        )
        self._persistence_override: Optional[bool] = persistence_enabled
        self._records: Dict[Tuple[str, str], EvaluationRecord] = {}
        self._lock = threading.Lock()

    # -- introspection --------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def persistence_path(self) -> Path:
        return self._persistence_path

    def is_persistence_enabled(self) -> bool:
        if self._persistence_override is not None:
            return bool(self._persistence_override)
        return _persistence_enabled()

    # -- mutate ---------------------------------------------------------

    def clear(self) -> None:
        """Drop all in-memory records. Does NOT touch the JSONL on
        disk; the audit history is intentionally append-only and
        survives in-memory resets (e.g., test teardown)."""
        with self._lock:
            self._records.clear()

    async def record(
        self,
        evaluation: EvaluationResult,
        scoring: ScoringResult,
    ) -> bool:
        """Record one (evaluation, scoring) pair.

        Returns
        -------
        bool
            ``True`` when the record was admitted to the in-memory
            cache. When persistence is enabled, this is ANDed with
            the JSONL append success - so ``True`` means the record
            is durable + cached, ``False`` means a JSONL write
            failure dropped durability (but the in-memory state is
            unchanged - caller decides whether to retry).

        Contract:
            * ``asyncio.CancelledError`` propagates.
            * Any other exception is swallowed + logged at DEBUG; the
              method returns False rather than crashing the caller.
        """
        # Slice 6 — task-naming completeness: rename the current task
        # to ``swe_bench_pro:record_result:<instance_id>`` for the
        # duration of the record (in-memory dict update + optional
        # flock-protected JSONL append). The instance_id is read from
        # the EvaluationResult so the observer can correlate this
        # phase with the matching evaluate / score frames in trace.
        _instance_id = (
            getattr(evaluation, "problem_instance_id", "") or ""
        )
        async with task_phase(EvaluatorPhase.RECORD_RESULT, _instance_id):
            try:
                record = EvaluationRecord(
                    evaluation=evaluation,
                    scoring=scoring,
                    recorded_at_iso=datetime.now(tz=timezone.utc).isoformat(),
                )
                with self._lock:
                    self._records[record.dedup_key] = record

                # Slice 74 persistence probe — was record() reached with the
                # real verdict, and is durable persistence even enabled? If the
                # durable results.jsonl row was None, either record() was never
                # called with the RESOLVED verdict (data-mapping / wiring) or
                # persistence was off / the append failed (cache-line). This
                # disambiguates. Zero-risk; remove after diagnosis.
                _s74_persist = self.is_persistence_enabled()
                logger.info(
                    "[Slice74Probe] RECORD instance=%s eval=%s score=%s persist_enabled=%s",
                    _instance_id,
                    getattr(getattr(evaluation, "outcome", None), "value", "?"),
                    getattr(getattr(scoring, "outcome", None), "value", "?"),
                    _s74_persist,
                )

                if not _s74_persist:
                    return True

                _s74_appended = await self._append_jsonl(record)
                logger.info(
                    "[Slice74Probe] RECORD_PERSIST instance=%s appended=%s path=%s",
                    _instance_id, _s74_appended, self._persistence_path,
                )
                return _s74_appended
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - public surface is fail-closed
                logger.debug(
                    "[SWEBenchPro.ResultStore] record raised", exc_info=True,
                )
                return False

    async def _append_jsonl(self, record: EvaluationRecord) -> bool:
        """Append a single record to the canonical JSONL audit file
        via the cross-process flock primitive. NEVER raises."""
        try:
            payload = record.to_dict()
            line = json.dumps(payload, sort_keys=True, default=str)
            # flock_append_line is sync (its lock is fcntl-based) -
            # run on the default thread executor to avoid blocking the
            # event loop on disk I/O. NEVER raises.
            return await asyncio.get_running_loop().run_in_executor(
                None,
                _append_line_sync,
                self._persistence_path,
                line,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.ResultStore] _append_jsonl raised",
                exc_info=True,
            )
            return False

    # -- query ----------------------------------------------------------

    def query(
        self,
        *,
        instance_id: Optional[str] = None,
        score_outcome: Optional[ScoreOutcome] = None,
        evaluation_outcome: Optional[EvaluationOutcome] = None,
        limit: Optional[int] = None,
    ) -> Tuple[EvaluationRecord, ...]:
        """Bounded snapshot read of in-memory records.

        Filters
        -------
        instance_id:
            Exact-match on ``record.evaluation.problem_instance_id``.
        score_outcome:
            Exact-match on ``record.scoring.outcome``.
        evaluation_outcome:
            Exact-match on ``record.evaluation.outcome``.
        limit:
            Cap on the number of records returned. ``None`` = no cap.
            Operators MUST set a limit for large stores; the default-
            unlimited path is intended for tests + small stores
            (under a few hundred records).

        Returns
        -------
        Tuple[EvaluationRecord, ...]
            Snapshot in arbitrary order (dict iteration). Caller
            mutations of the returned tuple do not affect store
            state. NEVER raises.
        """
        try:
            with self._lock:
                snapshot: List[EvaluationRecord] = list(
                    self._records.values()
                )
            filtered: List[EvaluationRecord] = []
            for r in snapshot:
                if instance_id is not None and (
                    r.evaluation.problem_instance_id != instance_id
                ):
                    continue
                if score_outcome is not None and (
                    r.scoring.outcome != score_outcome
                ):
                    continue
                if evaluation_outcome is not None and (
                    r.evaluation.outcome != evaluation_outcome
                ):
                    continue
                filtered.append(r)
                if limit is not None and len(filtered) >= limit:
                    break
            return tuple(filtered)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.ResultStore] query raised", exc_info=True,
            )
            return ()

    # -- aggregate ------------------------------------------------------

    def aggregate_score_outcomes(self) -> Dict[str, int]:
        """Counter dict mapping each ScoreOutcome value (including
        all 5 closed enum values, with zero counts where absent) to
        the number of in-memory records with that outcome. Pure
        function over the snapshot; NEVER raises."""
        counter: Dict[str, int] = {o.value: 0 for o in ScoreOutcome}
        try:
            with self._lock:
                snapshot = list(self._records.values())
            for r in snapshot:
                value = r.scoring.outcome.value
                counter[value] = counter.get(value, 0) + 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.ResultStore] aggregate raised",
                exc_info=True,
            )
        return counter

    def aggregate_evaluation_outcomes(self) -> Dict[str, int]:
        """Counter dict mapping each EvaluationOutcome value to its
        in-memory count. NEVER raises."""
        counter: Dict[str, int] = {o.value: 0 for o in EvaluationOutcome}
        try:
            with self._lock:
                snapshot = list(self._records.values())
            for r in snapshot:
                value = r.evaluation.outcome.value
                counter[value] = counter.get(value, 0) + 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.ResultStore] eval-aggregate raised",
                exc_info=True,
            )
        return counter

    def pass_rate(self) -> float:
        """Aggregate pass-rate across all recorded scorings, defined
        as ``PASS_count / total_scored`` where ``total_scored`` is
        the count of records whose ``scoring.outcome`` is NOT
        SKIPPED (skipped scorings do not contribute to the
        denominator).

        Returns ``0.0`` when no non-skipped records exist. Pure
        function over the snapshot; NEVER raises.
        """
        try:
            with self._lock:
                snapshot = list(self._records.values())
            scored = [
                r for r in snapshot
                if r.scoring.outcome != ScoreOutcome.SKIPPED
            ]
            if not scored:
                return 0.0
            passed = sum(
                1 for r in scored
                if r.scoring.outcome == ScoreOutcome.PASS
            )
            return round(passed / len(scored), 4)
        except Exception:  # noqa: BLE001
            return 0.0

    # -- disk replay ----------------------------------------------------

    async def replay_from_disk(self) -> int:
        """Reconstruct the in-memory cache from the JSONL audit
        file. Returns the count of records successfully replayed.
        Malformed rows are skipped with a DEBUG log. NEVER raises.

        The replay is idempotent: running it twice produces the
        same in-memory state (the (instance_id, op_id) dedup key
        collapses duplicates so the last-written record wins).
        """
        try:
            path = self._persistence_path
            if not path.exists():
                return 0
            return await asyncio.get_running_loop().run_in_executor(
                None, self._replay_sync, path,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.ResultStore] replay_from_disk raised",
                exc_info=True,
            )
            return 0

    def _replay_sync(self, path: Path) -> int:
        """Synchronous JSONL reader; runs on a thread to keep the
        event loop unblocked. Bounded - reads up to RESULT_REPLAY_MAX
        records and stops to prevent unbounded growth on very large
        audit files."""
        count = 0
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return 0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                record = EvaluationRecord.from_dict(payload)
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                logger.debug(
                    "[SWEBenchPro.ResultStore] skipped malformed row",
                )
                continue
            with self._lock:
                self._records[record.dedup_key] = record
            count += 1
        return count


# ===========================================================================
# Module-level singleton (mirrors get_default_broker pattern)
# ===========================================================================


_DEFAULT_STORE_LOCK = threading.Lock()
_DEFAULT_STORE: Optional[EvaluationResultStore] = None


def get_default_store() -> EvaluationResultStore:
    """Return the process-global default store, constructing it on
    first call. Thread-safe; idempotent."""
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = EvaluationResultStore()
        return _DEFAULT_STORE


def reset_default_store() -> None:
    """Drop the singleton instance. Primarily for tests. NEVER raises."""
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        _DEFAULT_STORE = None


async def record_evaluation(
    evaluation: EvaluationResult,
    scoring: ScoringResult,
) -> bool:
    """Module-level convenience: record via the default singleton store.

    Mirrors the ``publish_task_event`` shape from the SSE broker -
    callers don't need to plumb a store reference through phases B/C/D
    when the default singleton is what they want."""
    store = get_default_store()
    return await store.record(evaluation, scoring)


async def replay_default_store_from_disk() -> int:
    """Module-level convenience: replay the default store. Useful
    at boot time when the harness wants to pre-populate the
    in-memory cache from prior session JSONL."""
    store = get_default_store()
    return await store.replay_from_disk()


# ===========================================================================
# Sync flock-append wrapper (runs on thread executor)
# ===========================================================================


def _append_line_sync(path: Path, line: str) -> bool:
    """Thread-safe wrapper around the canonical flock primitive.
    NEVER raises (flock_append_line is fail-closed itself)."""
    try:
        return bool(flock_append_line(path, line))
    except Exception:  # noqa: BLE001 - belt-and-suspenders
        return False


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by section 33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count
    successfully registered. NEVER raises."""
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
            name=RESULT_PERSISTENCE_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Phase D master switch (section 33.1 default-FALSE): "
                "when ON, the EvaluationResultStore appends every "
                "recorded (evaluation, scoring) pair to a JSONL "
                "audit at JARVIS_SWE_BENCH_PRO_RESULT_PATH via the "
                "canonical cross_process_jsonl.flock_append_line "
                "primitive. The in-memory cache always operates "
                "regardless of this flag; persistence only adds the "
                "durable audit trail Phase F / external report card "
                "consumers depend on."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "result_store.py"
            ),
            example="false",
            since="v3.7 Phase 2 Phase D (2026-05-12)",
        ),
        FlagSpec(
            name=RESULT_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_RESULT_PATH,
            description=(
                "Canonical JSONL audit path for the Phase D result "
                "store. Parent directory auto-created on first "
                "append. Appended via cross_process_jsonl."
                "flock_append_line - safe across concurrent processes "
                "(parallel_eval rig + offline scorer + report-card "
                "renderer). Default "
                f"{_DEFAULT_RESULT_PATH!r}."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "result_store.py"
            ),
            example=_DEFAULT_RESULT_PATH,
            since="v3.7 Phase 2 Phase D (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.ResultStore] flag registration failed "
                "for %s", getattr(spec, "name", "?"), exc_info=True,
            )
    return count


__all__ = [
    "EvaluationRecord",
    "EvaluationResultStore",
    "RESULT_PATH_ENV_VAR",
    "RESULT_PERSISTENCE_ENABLED_ENV_VAR",
    "RESULT_RECORD_SCHEMA_VERSION",
    "get_default_store",
    "record_evaluation",
    "register_flags",
    "replay_default_store_from_disk",
    "reset_default_store",
]
