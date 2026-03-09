"""
IntakeLayerService — Supervisor Zone 6.9 lifecycle manager.

Owns UnifiedIntakeRouter, all 4 sensors, and the A-narrator (salience-gated
preflight awareness). Mirrors GovernedLoopService pattern: no side effects in
constructor; all async initialization in start().

Delivery semantics: at-least-once intake (WAL) + idempotent execution (dedup_key).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("Ouroboros.IntakeLayer")

# ---------------------------------------------------------------------------
# IntakeServiceState
# ---------------------------------------------------------------------------


class IntakeServiceState(Enum):
    INACTIVE = auto()
    STARTING = auto()
    ACTIVE = auto()
    DEGRADED = auto()
    STOPPING = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# IntakeLayerConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeLayerConfig:
    """Frozen configuration for IntakeLayerService."""

    project_root: Path
    dedup_window_s: float = 60.0
    backlog_scan_interval_s: float = 30.0
    miner_scan_interval_s: float = 300.0
    miner_complexity_threshold: int = 10
    miner_scan_paths: List[str] = field(default_factory=lambda: ["backend/", "tests/"])
    voice_stt_confidence_threshold: float = 0.70
    a_narrator_enabled: bool = True
    a_narrator_debounce_s: float = 10.0
    test_failure_min_count_for_narration: int = 2

    @classmethod
    def from_env(cls, project_root: Optional[Path] = None) -> "IntakeLayerConfig":
        resolved = project_root or Path(os.getenv("JARVIS_PROJECT_ROOT", os.getcwd()))
        return cls(
            project_root=resolved,
            dedup_window_s=float(os.getenv("JARVIS_INTAKE_DEDUP_WINDOW_S", "60.0")),
            backlog_scan_interval_s=float(
                os.getenv("JARVIS_INTAKE_BACKLOG_SCAN_INTERVAL_S", "30.0")
            ),
            miner_scan_interval_s=float(
                os.getenv("JARVIS_INTAKE_MINER_SCAN_INTERVAL_S", "300.0")
            ),
            miner_complexity_threshold=int(
                os.getenv("JARVIS_INTAKE_MINER_COMPLEXITY_THRESHOLD", "10")
            ),
            voice_stt_confidence_threshold=float(
                os.getenv("JARVIS_INTAKE_VOICE_STT_THRESHOLD", "0.70")
            ),
            a_narrator_enabled=os.getenv(
                "JARVIS_INTAKE_A_NARRATOR_ENABLED", "true"
            ).lower() not in ("0", "false", "no"),
            a_narrator_debounce_s=float(
                os.getenv("JARVIS_INTAKE_A_NARRATOR_DEBOUNCE_S", "10.0")
            ),
            test_failure_min_count_for_narration=int(
                os.getenv("JARVIS_INTAKE_TF_MIN_COUNT", "2")
            ),
        )


# ---------------------------------------------------------------------------
# IntakeLayerService (stub — fully implemented in Task 3)
# ---------------------------------------------------------------------------

class IntakeLayerService:
    """Stub. Full implementation in Task 3."""
    pass
