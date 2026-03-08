"""backend/core/ouroboros/governance/autonomy/state.py

Autonomy State Persistence — saves/loads tier data to JSON.

State persists to ~/.jarvis/autonomy/state.json by default.
Survives process restarts. Break-glass reset clears the file.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §5
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .tiers import (
    AutonomyTier,
    CognitiveLoad,
    GraduationMetrics,
    SignalAutonomyConfig,
    WorkContext,
)

logger = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = Path.home() / ".jarvis" / "autonomy" / "state.json"


class AutonomyState:
    """Persists autonomy tier configurations to JSON."""

    def __init__(self, state_path: Path = _DEFAULT_STATE_PATH) -> None:
        self._path = state_path

    def save(self, configs: Tuple[SignalAutonomyConfig, ...]) -> None:
        """Save all configs to the state file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: List[Dict[str, Any]] = []
        for config in configs:
            data.append({
                "trigger_source": config.trigger_source,
                "repo": config.repo,
                "canary_slice": config.canary_slice,
                "current_tier": config.current_tier.value,
                "graduation_metrics": {
                    "observations": config.graduation_metrics.observations,
                    "false_positives": config.graduation_metrics.false_positives,
                    "successful_ops": config.graduation_metrics.successful_ops,
                    "rollback_count": config.graduation_metrics.rollback_count,
                    "postmortem_streak": config.graduation_metrics.postmortem_streak,
                    "human_confirmations": config.graduation_metrics.human_confirmations,
                },
                "defer_during_cognitive_load": config.defer_during_cognitive_load.name,
                "defer_during_work_context": [
                    wc.value for wc in config.defer_during_work_context
                ],
                "require_user_active": config.require_user_active,
            })
        # Atomic write: write to temp file then rename
        with tempfile.NamedTemporaryFile(
            "w", dir=self._path.parent, delete=False, suffix=".tmp",
        ) as f:
            f.write(json.dumps(data, indent=2))
            tmp = Path(f.name)
        tmp.rename(self._path)
        logger.debug("Autonomy state saved: %d configs to %s", len(data), self._path)

    def load(self) -> Tuple[SignalAutonomyConfig, ...]:
        """Load configs from the state file. Returns () if missing/corrupt."""
        if not self._path.exists():
            return ()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Autonomy state corrupted, returning empty: %s", exc)
            return ()

        configs: List[SignalAutonomyConfig] = []
        for entry in raw:
            try:
                metrics = GraduationMetrics(**entry["graduation_metrics"])
                config = SignalAutonomyConfig(
                    trigger_source=entry["trigger_source"],
                    repo=entry["repo"],
                    canary_slice=entry["canary_slice"],
                    current_tier=AutonomyTier(entry["current_tier"]),
                    graduation_metrics=metrics,
                    defer_during_cognitive_load=CognitiveLoad[
                        entry.get("defer_during_cognitive_load", "HIGH")
                    ],
                    defer_during_work_context=tuple(
                        WorkContext(v)
                        for v in entry.get("defer_during_work_context", ["meetings"])
                    ),
                    require_user_active=entry.get("require_user_active", False),
                )
                configs.append(config)
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed autonomy config: %s", exc)

        return tuple(configs)

    def reset(self) -> None:
        """Delete the state file (break-glass reset)."""
        if self._path.exists():
            self._path.unlink()
            logger.info("Autonomy state reset: %s deleted", self._path)
