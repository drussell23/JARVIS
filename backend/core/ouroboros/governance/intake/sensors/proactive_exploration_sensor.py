"""
ProactiveExplorationSensor — Curiosity-driven domain exploration.

P3 Gap: Ouroboros waits for problems vs seeking knowledge. This sensor
uses the chronic entropy signal to identify domains with high uncertainty,
then triggers Oracle re-indexing and context enrichment for those areas.

Boundary Principle:
  Deterministic: Read chronic entropy scores, identify high-uncertainty
  domains, trigger re-indexing. No model inference for detection.
  Agentic: The enriched context feeds into future GENERATE prompts
  where the model can reason about the newly indexed knowledge.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = float(
    os.environ.get("JARVIS_EXPLORATION_INTERVAL_S", "7200")  # Every 2 hours
)
_ENTROPY_EXPLORATION_THRESHOLD = float(
    os.environ.get("JARVIS_EXPLORATION_ENTROPY_THRESHOLD", "0.4")
)


class ProactiveExplorationSensor:
    """Curiosity sensor — explores domains where the organism is uncertain.

    Reads the LearningConsolidator's domain rules and chronic entropy
    scores. When a domain has persistently high uncertainty, triggers:
    1. Oracle re-indexing of files in that domain
    2. IntentEnvelope emission for proactive context gathering

    The organism doesn't just fix problems — it seeks to understand
    areas where it's weak, BEFORE failures occur.
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        project_root: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._project_root = project_root or Path(".")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._explored_domains: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"exploration_sensor_{self._repo}"
        )
        logger.info("[ExplorationSensor] Started for repo=%s", self._repo)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self) -> None:
        await asyncio.sleep(600.0)  # Let system stabilize
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[ExplorationSensor] Poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[str]:
        """Identify high-uncertainty domains and trigger exploration."""
        explored: List[str] = []

        try:
            from backend.core.ouroboros.governance.adaptive_learning import (
                LearningConsolidator,
            )
            consolidator = LearningConsolidator()
            all_rules = consolidator._rules

            for domain_key, rules in all_rules.items():
                if domain_key in self._explored_domains:
                    continue

                # Check if domain has high failure rules
                failure_rules = [
                    r for r in rules
                    if r.rule_type == "common_failure" and r.confidence > _ENTROPY_EXPLORATION_THRESHOLD
                ]

                if not failure_rules:
                    continue

                # This domain has persistent issues — trigger exploration
                self._explored_domains.add(domain_key)
                explored.append(domain_key)

                # Emit an IntentEnvelope for proactive investigation
                top_rule = max(failure_rules, key=lambda r: r.confidence)
                try:
                    envelope = make_envelope(
                        source="exploration",
                        description=(
                            f"Proactive exploration: domain '{domain_key}' has "
                            f"persistent uncertainty (confidence={top_rule.confidence:.0%}, "
                            f"n={top_rule.sample_size}). {top_rule.description}"
                        ),
                        target_files=self._infer_target_files(domain_key),
                        repo=self._repo,
                        confidence=0.70,
                        urgency="low",
                        evidence={
                            "category": "proactive_exploration",
                            "domain_key": domain_key,
                            "rule_confidence": top_rule.confidence,
                            "rule_type": top_rule.rule_type,
                            "sensor": "ProactiveExplorationSensor",
                        },
                        requires_human_ack=False,
                    )
                    await self._router.ingest(envelope)
                    logger.info(
                        "[ExplorationSensor] Exploring domain=%s "
                        "(confidence=%.0f%%, n=%d)",
                        domain_key, top_rule.confidence * 100, top_rule.sample_size,
                    )
                except Exception:
                    logger.debug(
                        "[ExplorationSensor] Emit failed for %s", domain_key
                    )

        except ImportError as exc:
            # LearningConsolidator unavailable — log once at debug to aid ops
            # triage ("why is proactive_exploration emitting zero signals?")
            # without spamming the poll loop every 2 hours.
            if not self._explored_domains:
                logger.debug(
                    "[ExplorationSensor] LearningConsolidator import failed; "
                    "sensor will be inert this cycle: %s",
                    exc,
                )
        except Exception:
            logger.debug("[ExplorationSensor] Scan error", exc_info=True)

        return explored

    def _infer_target_files(self, domain_key: str) -> Tuple[str, ...]:
        """Infer representative target files from domain key.

        domain_key format: "category::extension"
        Maps to likely file paths. Deterministic.
        """
        parts = domain_key.split("::")
        ext = parts[1] if len(parts) > 1 else ".py"
        category = parts[0] if parts else "code_gen"

        # Map categories to likely directories
        category_dirs = {
            "code_gen": "backend/core/",
            "test_fix": "tests/",
            "config": "backend/api/config/",
            "dependency": "requirements.txt",
            "documentation": "docs/",
        }
        base_dir = category_dirs.get(category, "backend/")

        if base_dir == "requirements.txt":
            return ("requirements.txt",)

        return (base_dir,)

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "ProactiveExplorationSensor",
            "repo": self._repo,
            "running": self._running,
            "explored_domains": len(self._explored_domains),
        }
