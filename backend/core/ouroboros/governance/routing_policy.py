# backend/core/ouroboros/governance/routing_policy.py
"""
Routing Policy — Deterministic Task Routing
=============================================

Makes deterministic routing decisions based on task category, resource
pressure, GCP availability, and cost guardrails.  No LLM calls.

Routing Matrix (design doc section 2.7)::

    Task Type           | Normal    | CPU>80%   | RAM>85%   | GCP Down
    --------------------|-----------|-----------|-----------|----------
    Single-file fix     | LOCAL     | LOCAL     | LOCAL     | LOCAL
    Multi-file analysis | LOCAL     | GCP_PRIME | GCP_PRIME | QUEUE
    Cross-repo planning | GCP_PRIME | GCP_PRIME | GCP_PRIME | QUEUE
    Candidate gen (3+)  | GCP_PRIME | GCP_PRIME | GCP_PRIME | QUEUE
    Test execution      | LOCAL     | LOCAL     | LOCAL     | LOCAL
    Blast radius calc   | LOCAL     | LOCAL     | LOCAL     | LOCAL
"""

from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.core.ouroboros.governance.degradation import DegradationMode
from backend.core.ouroboros.governance.resource_monitor import (
    PressureLevel,
    ResourceSnapshot,
)

logger = logging.getLogger("Ouroboros.RoutingPolicy")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskCategory(enum.Enum):
    """Categories of Ouroboros tasks for routing decisions."""

    SINGLE_FILE_FIX = "single_file_fix"
    MULTI_FILE_ANALYSIS = "multi_file_analysis"
    CROSS_REPO_PLANNING = "cross_repo_planning"
    CANDIDATE_GENERATION = "candidate_generation"
    TEST_EXECUTION = "test_execution"
    BLAST_RADIUS_CALC = "blast_radius_calc"


class RoutingDecision(enum.Enum):
    """Where a task should be executed."""

    LOCAL = "local"
    GCP_PRIME = "gcp_prime"
    QUEUE = "queue"


# Tasks that always run locally regardless of conditions
_ALWAYS_LOCAL: set = {
    TaskCategory.SINGLE_FILE_FIX,
    TaskCategory.TEST_EXECUTION,
    TaskCategory.BLAST_RADIUS_CALC,
}

# Tasks that prefer GCP when available
_PREFER_GCP: set = {
    TaskCategory.CROSS_REPO_PLANNING,
    TaskCategory.CANDIDATE_GENERATION,
}


# ---------------------------------------------------------------------------
# Cost Guardrail
# ---------------------------------------------------------------------------


class CostGuardrail:
    """Tracks GCP usage costs and enforces daily budget caps."""

    def __init__(self) -> None:
        self._daily_cap: float = float(
            os.environ.get("OUROBOROS_GCP_DAILY_BUDGET", "10.0")
        )
        self._usage_today: float = 0.0
        self._reset_date: str = time.strftime("%Y-%m-%d")

    @property
    def daily_usage(self) -> float:
        """Current day's GCP usage."""
        self._check_date_reset()
        return self._usage_today

    @property
    def over_budget(self) -> bool:
        """Whether daily budget has been exceeded."""
        self._check_date_reset()
        return self._usage_today >= self._daily_cap

    def record_gcp_usage(self, cost: float) -> None:
        """Record a GCP cost event."""
        self._check_date_reset()
        self._usage_today += cost

    def _check_date_reset(self) -> None:
        """Reset counter at date boundary."""
        today = time.strftime("%Y-%m-%d")
        if today != self._reset_date:
            self._usage_today = 0.0
            self._reset_date = today


# ---------------------------------------------------------------------------
# RoutingPolicy
# ---------------------------------------------------------------------------


class RoutingPolicy:
    """Deterministic routing policy for Ouroboros tasks."""

    def __init__(self) -> None:
        self.cost_guardrail = CostGuardrail()

    def route(
        self,
        task: TaskCategory,
        snapshot: ResourceSnapshot,
        degradation_mode: DegradationMode,
        gcp_available: bool,
    ) -> RoutingDecision:
        """Make a deterministic routing decision."""
        # Always-local tasks never route elsewhere
        if task in _ALWAYS_LOCAL:
            return RoutingDecision.LOCAL

        # Cost guardrail: over budget queues GCP tasks
        if self.cost_guardrail.over_budget and task in _PREFER_GCP:
            logger.info(
                "Routing %s -> QUEUE (over daily GCP budget)", task.value
            )
            return RoutingDecision.QUEUE

        # GCP unavailable: queue heavy tasks, local for light
        if not gcp_available:
            if task in _PREFER_GCP:
                return RoutingDecision.QUEUE
            return RoutingDecision.LOCAL

        # GCP-preferred tasks route to GCP when available
        if task in _PREFER_GCP:
            return RoutingDecision.GCP_PRIME

        # Pressure-based routing for medium tasks (multi-file analysis)
        pressure = snapshot.overall_pressure
        if pressure >= PressureLevel.ELEVATED and gcp_available:
            return RoutingDecision.GCP_PRIME

        return RoutingDecision.LOCAL
