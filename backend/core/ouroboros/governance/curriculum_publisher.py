"""CurriculumPublisher — periodic weighted failure signal to Reactor."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.integration import PerformanceRecordPersistence

logger = logging.getLogger(__name__)

TASK_TAXONOMY = (
    "code_improvement", "refactoring", "bug_fix", "code_review",
    "testing", "documentation", "performance", "security",
)


@dataclass(frozen=True)
class CurriculumEntry:
    task_type: str
    priority: float
    failure_rate: float
    sample_size: int
    confidence: float


@dataclass(frozen=True)
class CurriculumPayload:
    schema_version: str
    event_type: str
    generated_at: str
    top_k: list[CurriculumEntry]


class CurriculumPublisher:
    def __init__(
        self,
        persistence: "PerformanceRecordPersistence",
        event_dir: Path,
        window_n: int = 50,
        top_k: int = 5,
        impact_weights: dict[str, float] | None = None,
        min_sample_size: int = 3,
        half_life_hours: float = 24.0,
    ) -> None:
        self._persistence = persistence
        self._event_dir = event_dir
        self._window_n = window_n
        self._top_k = top_k
        self._impact_weights: dict[str, float] = impact_weights or {}
        self._min_sample_size = min_sample_size
        self._half_life_hours = half_life_hours

    async def publish(self) -> Optional[CurriculumPayload]:
        try:
            return await self._publish_inner()
        except Exception as exc:
            logger.exception("[CurriculumPublisher] publish() failed: %s", exc)
            return None

    async def _publish_inner(self) -> Optional[CurriculumPayload]:
        now = datetime.now(tz=timezone.utc)
        # (task_type, raw_priority, failure_rate, sample_size, confidence)
        entries: list[tuple[str, float, float, int, float]] = []

        for task_type in TASK_TAXONOMY:
            records = await self._persistence.get_records_by_task(
                task_type=task_type,
                limit=self._window_n,
            )
            if len(records) < self._min_sample_size:
                continue

            failure_rate = 1.0 - sum(float(r.success) for r in records) / len(records)
            impact_weight = self._impact_weights.get(task_type, 1.0)

            # Recency: exponential decay based on mean age
            mean_age_hours = 0.0
            aged = [r for r in records if getattr(r, "timestamp", None)]
            if aged:
                ages = []
                for r in aged:
                    ts = r.timestamp
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ages.append((now - ts).total_seconds() / 3600.0)
                mean_age_hours = sum(ages) / len(ages)
            recency_weight = math.exp(-math.log(2) * mean_age_hours / self._half_life_hours)

            confidence_weight = min(len(records), self._window_n) / self._window_n
            raw_priority = failure_rate * impact_weight * recency_weight * confidence_weight
            entries.append((task_type, raw_priority, failure_rate, len(records), confidence_weight))

        if not entries:
            return None

        # Sort descending, take top-K
        entries.sort(key=lambda x: x[1], reverse=True)
        top = entries[: self._top_k]

        total = sum(e[1] for e in top)
        if total == 0.0:
            logger.debug("[CurriculumPublisher] All qualifying task types have zero failure rate; skipping publish")
            return None

        payload_entries = [
            CurriculumEntry(
                task_type=task_type,
                priority=raw / total,
                failure_rate=fail_rate,
                sample_size=sample_size,
                confidence=conf,
            )
            for task_type, raw, fail_rate, sample_size, conf in top
        ]

        payload = CurriculumPayload(
            schema_version="curriculum.1",
            event_type="curriculum_signal",
            generated_at=now.isoformat(),
            top_k=payload_entries,
        )

        ts_ms = int(now.timestamp() * 1000)
        out_path = self._event_dir / f"curriculum_{ts_ms}.json"
        out_path.write_text(
            json.dumps(
                {
                    "schema_version": payload.schema_version,
                    "event_type": payload.event_type,
                    "generated_at": payload.generated_at,
                    "top_k": [asdict(e) for e in payload.top_k],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(
            "[CurriculumPublisher] Wrote %s (%d task types)",
            out_path.name, len(payload_entries),
        )
        return payload
