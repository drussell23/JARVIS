"""ModelAttributionRecorder — per-task-type quality delta at model promotion."""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from statistics import mean
from typing import TYPE_CHECKING, Optional

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


class ModelAttributionRecorder:
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
        task_types: Optional[list[str]] = None,
    ) -> "list[ModelAttributionRecord]":
        from backend.core.ouroboros.integration import ModelAttributionRecord

        to_process = task_types if task_types else list(TASK_TAXONOMY)
        results: list[ModelAttributionRecord] = []
        summary_parts: list[str] = []

        for task_type in to_process:
            new_records = await self._persistence.get_records_by_model_and_task(
                new_model_id, task_type, limit=self._lookback_n
            )
            old_records = await self._persistence.get_records_by_model_and_task(
                previous_model_id, task_type, limit=self._lookback_n
            )
            n_new, n_old = len(new_records), len(old_records)
            if min(n_new, n_old) < self._min_sample_size:
                summary_parts.append(f"{task_type}: insufficient data [n={min(n_new, n_old)}]")
                continue

            sr_delta = mean(float(r.success) for r in new_records) - mean(float(r.success) for r in old_records)
            lat_delta = mean(r.latency_ms for r in new_records) - mean(r.latency_ms for r in old_records)
            q_delta = mean(r.code_quality_score for r in new_records) - mean(r.code_quality_score for r in old_records)
            confidence = min(1.0, min(n_new, n_old) / self._lookback_n)
            sample_size = min(n_new, n_old)

            def sign(v: float) -> str:
                return "+" if v >= 0 else ""

            summary_parts.append(
                f"{task_type} success {sign(sr_delta)}{sr_delta*100:.1f}% "
                f"quality {sign(q_delta)}{q_delta:.2f} "
                f"latency {sign(lat_delta)}{lat_delta:.0f}ms [n={sample_size}]"
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
                summary="",  # filled below after we build full summary
                recorded_at=datetime.now(tz=timezone.utc),
            )
            results.append(rec)

        full_summary = (
            f"Model {new_model_id} ({training_batch_size} experiences): "
            + "; ".join(summary_parts)
        )
        logger.info("[ModelAttribution] %s", full_summary)

        final_results: list[ModelAttributionRecord] = []
        for rec in results:
            rec_with_summary = dataclasses.replace(rec, summary=full_summary)
            await self._persistence.save_attribution_record(rec_with_summary)
            final_results.append(rec_with_summary)

        return final_results
