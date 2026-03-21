"""TopologyMap — cross-repo capability DAG with Shannon Entropy."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, FrozenSet, Set

if TYPE_CHECKING:
    from backend.core.topology.hardware_env import HardwareEnvironmentState


@dataclass(unsafe_hash=True)
class CapabilityNode:
    """A discrete capability known to Trinity."""
    name: str
    domain: str
    repo_owner: str
    active: bool = False
    coverage_score: float = 0.0
    exploration_attempts: int = 0


@dataclass
class TopologyMap:
    """Live directed graph of Trinity's known capability space.

    Nodes = capabilities. Edges = dependencies (A requires B).
    Built from scheduler.graph_state@1.0.0 envelopes at boot,
    extended by Prime's capability scanner on each GLS cycle.
    """
    nodes: Dict[str, CapabilityNode] = field(default_factory=dict)
    edges: Dict[str, Set[str]] = field(default_factory=dict)

    def register(self, node: CapabilityNode) -> None:
        self.nodes[node.name] = node
        if node.name not in self.edges:
            self.edges[node.name] = set()

    def domain_coverage(self, domain: str) -> float:
        """Fraction of known capabilities in *domain* that are active."""
        domain_nodes = [n for n in self.nodes.values() if n.domain == domain]
        if not domain_nodes:
            return 1.0
        active = sum(1 for n in domain_nodes if n.active)
        return active / len(domain_nodes)

    def entropy_over_domain(self, domain: str) -> float:
        """Shannon Entropy H(domain) — measures ignorance about this domain.

        H(X) = -p * log2(p) - (1-p) * log2(1-p)
        where p = coverage fraction.
        H=0 means fully known; H=1 means maximum ignorance.
        """
        p = self.domain_coverage(domain)
        if p <= 0.0 or p >= 1.0:
            return 0.0
        return -p * math.log2(p) - (1 - p) * math.log2(1 - p)

    def all_domains(self) -> FrozenSet[str]:
        return frozenset(n.domain for n in self.nodes.values())

    def feasible_for_hardware(
        self, node: CapabilityNode, hw: HardwareEnvironmentState
    ) -> bool:
        """Topology-aware hardware feasibility check.

        GPU capabilities require a GPU tier. Large-model capabilities
        require minimum VRAM. Returns False if hardware cannot support it.
        """
        if "gpu" in node.name.lower() or "vision" in node.domain.lower():
            if hw.gpu is None:
                return False
            if hw.gpu.vram_free_mb < 4096:
                return False
        return True
