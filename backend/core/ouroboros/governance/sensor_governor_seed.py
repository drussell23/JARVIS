"""Seed budget specs for the 16 autonomous sensors.

Each sensor gets:
  * ``base_cap_per_hour`` — target rate under posture=MAINTAIN
  * ``posture_weights`` — multiplier per posture (HARDEN/EXPLORE/...)
  * ``urgency_multipliers`` — optional overrides (default table in
    sensor_governor.py)

Weight philosophy:
  - HARDEN favors stabilization sensors (TestFailure, RuntimeHealth,
    PerformanceRegression) and starves exploration (OpportunityMiner,
    ProactiveExploration, IntentDiscovery)
  - EXPLORE flips the favor — discovery + intent sensors get
    extra budget; stabilization sensors get neutral (1.0) or slightly
    reduced budget
  - CONSOLIDATE favors docs + TODO + backlog + cross-repo drift (all
    close-the-thread sensors)
  - MAINTAIN is neutral (1.0 everywhere)
"""
from __future__ import annotations

from backend.core.ouroboros.governance.sensor_governor import (
    SensorBudgetSpec,
    SensorGovernor,
)


_STABILIZATION_WEIGHTS = {
    "EXPLORE": 0.8, "CONSOLIDATE": 1.0, "HARDEN": 1.8, "MAINTAIN": 1.0,
}
_DISCOVERY_WEIGHTS = {
    "EXPLORE": 1.5, "CONSOLIDATE": 0.5, "HARDEN": 0.3, "MAINTAIN": 1.0,
}
_CONSOLIDATION_WEIGHTS = {
    "EXPLORE": 0.8, "CONSOLIDATE": 1.3, "HARDEN": 0.6, "MAINTAIN": 1.0,
}
_NEUTRAL_WEIGHTS = {
    "EXPLORE": 1.0, "CONSOLIDATE": 1.0, "HARDEN": 1.0, "MAINTAIN": 1.0,
}


SEED_SPECS: list = [
    # --- stabilization sensors: favored in HARDEN ---
    SensorBudgetSpec(
        sensor_name="TestFailureSensor",
        base_cap_per_hour=20,
        posture_weights=_STABILIZATION_WEIGHTS,
        description="Event-driven test-failure capture; floor-rate 20/hr.",
    ),
    SensorBudgetSpec(
        sensor_name="RuntimeHealthSensor",
        base_cap_per_hour=5,
        posture_weights={"EXPLORE": 1.0, "CONSOLIDATE": 1.0, "HARDEN": 1.5,
                         "MAINTAIN": 1.0},
        description="Daily system-health probe; low baseline, HARDEN boost.",
    ),
    SensorBudgetSpec(
        sensor_name="PerformanceRegressionSensor",
        base_cap_per_hour=10,
        posture_weights={"EXPLORE": 1.0, "CONSOLIDATE": 1.0, "HARDEN": 1.5,
                         "MAINTAIN": 1.0},
        description="CI-webhook driven regression detection.",
    ),

    # --- discovery sensors: favored in EXPLORE ---
    SensorBudgetSpec(
        sensor_name="OpportunityMinerSensor",
        base_cap_per_hour=15,
        posture_weights=_DISCOVERY_WEIGHTS,
        description="Layered storm-guard; heavy EXPLORE weight.",
    ),
    SensorBudgetSpec(
        sensor_name="ProactiveExplorationSensor",
        base_cap_per_hour=3,
        posture_weights=_DISCOVERY_WEIGHTS,
        description="7200s polling; strong EXPLORE preference.",
    ),
    SensorBudgetSpec(
        sensor_name="IntentDiscoverySensor",
        base_cap_per_hour=10,
        posture_weights={"EXPLORE": 1.4, "CONSOLIDATE": 0.8, "HARDEN": 0.5,
                         "MAINTAIN": 1.0},
        description="Conversation-bus driven; silence-window gated.",
    ),
    SensorBudgetSpec(
        sensor_name="CapabilityGapSensor",
        base_cap_per_hour=5,
        posture_weights={"EXPLORE": 1.3, "CONSOLIDATE": 1.0, "HARDEN": 0.8,
                         "MAINTAIN": 1.0},
        description="Entropy-driven capability discovery.",
    ),
    SensorBudgetSpec(
        sensor_name="WebIntelligenceSensor",
        base_cap_per_hour=4,
        posture_weights={"EXPLORE": 1.3, "CONSOLIDATE": 1.0, "HARDEN": 0.7,
                         "MAINTAIN": 1.0},
        description="Daily external-intel poll.",
    ),

    # --- consolidation sensors: favored in CONSOLIDATE ---
    SensorBudgetSpec(
        sensor_name="DocStalenessSensor",
        base_cap_per_hour=6,
        posture_weights={"EXPLORE": 0.8, "CONSOLIDATE": 1.3, "HARDEN": 0.6,
                         "MAINTAIN": 1.0},
        description="GitHub push → doc review pipeline.",
    ),
    SensorBudgetSpec(
        sensor_name="TodoScannerSensor",
        base_cap_per_hour=8,
        posture_weights={"EXPLORE": 1.0, "CONSOLIDATE": 1.4, "HARDEN": 0.8,
                         "MAINTAIN": 1.0},
        description="FS-event driven TODO harvesting.",
    ),
    SensorBudgetSpec(
        sensor_name="BacklogSensor",
        base_cap_per_hour=6,
        posture_weights=_CONSOLIDATION_WEIGHTS,
        description="Queue-saturation + stale-work detection.",
    ),
    SensorBudgetSpec(
        sensor_name="CrossRepoDriftSensor",
        base_cap_per_hour=4,
        posture_weights={"EXPLORE": 1.0, "CONSOLIDATE": 1.2, "HARDEN": 1.0,
                         "MAINTAIN": 1.0},
        description="Multi-repo convention drift (GitHub webhook).",
    ),

    # --- integration / neutral sensors ---
    SensorBudgetSpec(
        sensor_name="GitHubIssueSensor",
        base_cap_per_hour=30,
        posture_weights={"EXPLORE": 1.0, "CONSOLIDATE": 1.0, "HARDEN": 1.2,
                         "MAINTAIN": 1.0},
        description="GitHub issue webhook fan-in.",
    ),
    SensorBudgetSpec(
        sensor_name="VoiceCommandSensor",
        base_cap_per_hour=60,
        posture_weights=_NEUTRAL_WEIGHTS,
        description="Voice I/O triggered ops — neutral; voice is user-driven.",
    ),
    SensorBudgetSpec(
        sensor_name="VisionSensor",
        base_cap_per_hour=10,
        posture_weights={"EXPLORE": 1.2, "CONSOLIDATE": 0.8, "HARDEN": 1.0,
                         "MAINTAIN": 1.0},
        description="Ferrari frame consumer; FP budget auto-pause built-in.",
    ),
    SensorBudgetSpec(
        sensor_name="ScheduledSensor",
        base_cap_per_hour=24,  # typical cron cadence: hourly * 24
        posture_weights=_NEUTRAL_WEIGHTS,
        description="Cron-driven; baseline cadence is the contract.",
    ),
]


def seed_default_governor(governor: SensorGovernor) -> int:
    governor.bulk_register(SEED_SPECS)
    return len(SEED_SPECS)


__all__ = [
    "SEED_SPECS",
    "seed_default_governor",
]
