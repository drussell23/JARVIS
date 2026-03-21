"""
Topology package — Proactive Autonomous Drive
==============================================

Dynamic hardware discovery, capability DAG with Shannon Entropy,
Little's Law idle verification, UCB1 curiosity engine, PID resource
governor, sandboxed exploration sentinel, and architectural proposal
output contract.

Zero LLM dependency. Pure systems engineering, mathematics, and
control theory.
"""
from backend.core.topology.hardware_env import (
    ComputeTier,
    GPUState,
    HardwareEnvironmentState,
)
from backend.core.topology.topology_map import CapabilityNode, TopologyMap
from backend.core.topology.idle_verifier import (
    LittlesLawVerifier,
    ProactiveDrive,
    QueueSample,
)
from backend.core.topology.curiosity_engine import CuriosityEngine, CuriosityTarget
from backend.core.topology.resource_governor import PIDController, ResourceGovernor
from backend.core.topology.sentinel import (
    DeadEndClass,
    DeadEndClassifier,
    ExplorationSentinel,
    SentinelOutcome,
)
from backend.core.topology.architectural_proposal import (
    ArchitecturalProposal,
    ShadowTestResult,
)
from backend.core.topology.macos_host_observer import (
    EnvironmentChange,
    HostEvent,
    MacOSHostObserver,
    get_host_observer,
)
from backend.core.topology.tendril_manager import (
    TendrilManager,
    TendrilOutcome,
    TendrilState,
)

__all__ = [
    "ComputeTier", "GPUState", "HardwareEnvironmentState",
    "CapabilityNode", "TopologyMap",
    "LittlesLawVerifier", "ProactiveDrive", "QueueSample",
    "CuriosityEngine", "CuriosityTarget",
    "PIDController", "ResourceGovernor",
    "DeadEndClass", "DeadEndClassifier", "ExplorationSentinel", "SentinelOutcome",
    "ArchitecturalProposal", "ShadowTestResult",
    "EnvironmentChange", "HostEvent", "MacOSHostObserver", "get_host_observer",
    "TendrilManager", "TendrilOutcome", "TendrilState",
]
