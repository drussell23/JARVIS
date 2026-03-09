"""ModelAttributionRecorder — per-task-type quality delta at model promotion."""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from statistics import mean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.ouroboros.integration import (
        ModelAttributionRecord,
        PerformanceRecordPersistence,
    )

logger = logging.getLogger(__name__)

TASK_TAXONOMY = (
    "code_improvement", "refactoring", "bug_fix", "code_review",
    "testing", "documentation", "performance", "security",
)

# Reusable sign prefix helper — defined at module scope to avoid re-creation per loop iteration.
def _sign(v: float) -> str:
    return "+" if v >= 0 else ""


class ModelAttributionRecorder:
    """Records per-task-type quality deltas when a new model replaces a previous one.

    The recording process follows a two-phase build pattern:

    Phase 1 (build): For each task type in ``to_process``, fetch performance
    records for both the new and previous model IDs, compute deltas, and
    accumulate ``ModelAttributionRecord`` objects.  Any DB read error on a
    single task type is isolated — it logs a warning and the loop continues to
    the next task type so a single failing query cannot abort the entire
    transition.

    Phase 2 (persist): Each record from Phase 1 has its ``summary`` field
    filled in (the full cross-task summary string) and is then persisted via
    ``save_attribution_record``.  Only records that are successfully persisted
    are included in the returned list; a write failure for one record is
    isolated in the same way.
    """

    def __init__(
        self,
        persistence: "PerformanceRecordPersistence",
        lookback_n: int = 20,
        min_sample_size: int = 3,
    ) -> None:
        self._persistence = persistence
        self._lookback_n = lookback_n
        self._min_sample_size = min_sample_size

    async def record_model_transition(
        self,
        new_model_id: str,
        previous_model_id: str,
        training_batch_size: int,
        task_types: list[str] | None = None,
    ) -> "list[ModelAttributionRecord]":
        """Compute and persist quality deltas for a model promotion.

        For every task type in *task_types* (defaults to the full
        ``TASK_TAXONOMY``), fetch recent records for both *new_model_id* and
        *previous_model_id*, compute success-rate / latency / quality deltas,
        and write ``ModelAttributionRecord`` rows to the persistence layer.

        The method uses a two-phase build pattern (see class docstring).  DB
        read errors on a per-task-type basis and write errors on a per-record
        basis are both fault-isolated: they log a warning and continue rather
        than aborting the whole operation.

        Returns only the records that were successfully persisted.
        """
        from backend.core.ouroboros.integration import ModelAttributionRecord

        to_process = task_types if task_types else list(TASK_TAXONOMY)

        if not to_process:
            logger.warning("[ModelAttribution] task_types is empty — no records will be produced")
            return []

        results: list[ModelAttributionRecord] = []
        summary_parts: list[str] = []

        # ── Phase 1: build ────────────────────────────────────────────────────
        for task_type in to_process:
            try:
                new_records = await self._persistence.get_records_by_model_and_task(
                    new_model_id, task_type, limit=self._lookback_n
                )
                old_records = await self._persistence.get_records_by_model_and_task(
                    previous_model_id, task_type, limit=self._lookback_n
                )
            except Exception as exc:
                logger.warning(
                    "[ModelAttribution] DB read error for task_type=%r, skipping: %s",
                    task_type, exc,
                )
                continue

            n_new, n_old = len(new_records), len(old_records)
            if min(n_new, n_old) < self._min_sample_size:
                summary_parts.append(f"{task_type}: insufficient data [n={min(n_new, n_old)}]")
                continue

            sr_delta = mean(float(r.success) for r in new_records) - mean(float(r.success) for r in old_records)
            lat_delta = mean(r.latency_ms for r in new_records) - mean(r.latency_ms for r in old_records)
            q_delta = mean(r.code_quality_score for r in new_records) - mean(r.code_quality_score for r in old_records)
            confidence = min(1.0, min(n_new, n_old) / self._lookback_n)
            sample_size = min(n_new, n_old)

            summary_parts.append(
                f"{task_type} success {_sign(sr_delta)}{sr_delta*100:.1f}% "
                f"quality {_sign(q_delta)}{q_delta:.2f} "
                f"latency {_sign(lat_delta)}{lat_delta:.0f}ms [n={sample_size}]"
            )

            rec = ModelAttributionRecord(
                model_id=new_model_id,
                previous_model_id=previous_model_id,
                training_batch_size=training_batch_size,
                task_type=task_type,
                success_rate_delta=sr_delta,
                latency_delta_ms=lat_delta,
                quality_delta=q_delta,
                sample_size=sample_size,
                confidence=confidence,
                summary="",  # filled in Phase 2 once the full summary is known
                recorded_at=datetime.now(tz=timezone.utc),
            )
            results.append(rec)

        # ── Phase 2: persist ──────────────────────────────────────────────────
        full_summary = (
            f"Model {new_model_id} ({training_batch_size} experiences): "
            + "; ".join(summary_parts)
        )
        logger.info("[ModelAttribution] %s", full_summary)

        final_results: list[ModelAttributionRecord] = []
        for rec in results:
            rec_with_summary = dataclasses.replace(rec, summary=full_summary)
            try:
                await self._persistence.save_attribution_record(rec_with_summary)
            except Exception as exc:
                logger.warning(
                    "[ModelAttribution] Write error for task_type=%r, record excluded from results: %s",
                    rec_with_summary.task_type, exc,
                )
                continue
            final_results.append(rec_with_summary)

        return final_results
