"""CuriosityEngine — deterministic capability gap selection via Shannon Entropy + UCB1."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from backend.core.topology.hardware_env import HardwareEnvironmentState
from backend.core.topology.topology_map import CapabilityNode, TopologyMap


UCB_EXPLORATION_CONSTANT = math.sqrt(2)


@dataclass(frozen=True)
class CuriosityTarget:
    """The output of the curiosity engine — what to explore next."""
    capability: CapabilityNode
    ucb_score: float
    entropy_score: float
    feasibility_score: float
    rationale: str


class CuriosityEngine:
    """Deterministic capability gap selection using Shannon Entropy + UCB1.

    Lives in JARVIS Prime. Reads TopologyMap (updated from TelemetryBus).
    Has zero LLM dependency. Same inputs -> same output, every time.
    """

    def __init__(self, topology: TopologyMap, hardware: HardwareEnvironmentState) -> None:
        self._topology = topology
        self._hardware = hardware

    def _entropy(self, domain: str) -> float:
        return self._topology.entropy_over_domain(domain)

    def _feasibility(self, node: CapabilityNode) -> float:
        """Composite feasibility score 0..1.

        Combines hardware feasibility (binary) with dependency readiness
        (fraction of required dependencies that are already active).
        """
        if not self._topology.feasible_for_hardware(node, self._hardware):
            return 0.0
        deps = self._topology.edges.get(node.name, set())
        if not deps:
            return 1.0
        ready = sum(
            1 for d in deps
            if d in self._topology.nodes and self._topology.nodes[d].active
        )
        return ready / len(deps)

    def _ucb_score(self, node: CapabilityNode, total_attempts: int) -> float:
        """UCB1 score for a single capability node.

        Laplace smoothing: N is floored at n_i+1 so log(N/n_i) > 0 even on
        a brand-new system where all exploration_attempts are zero.
        """
        entropy = self._entropy(node.domain)
        feasibility = self._feasibility(node)
        estimated_value = entropy * feasibility
        n_i = max(1, node.exploration_attempts)
        # Laplace smooth: guarantee N > n_i so log ratio is always positive.
        N = max(n_i + 1, total_attempts)
        exploration_bonus = UCB_EXPLORATION_CONSTANT * math.sqrt(math.log(N) / n_i)
        return estimated_value + exploration_bonus

    def score_all(self) -> List[Tuple[CapabilityNode, float]]:
        """Score every inactive, feasible capability. Returns sorted list."""
        total_attempts = sum(
            n.exploration_attempts for n in self._topology.nodes.values()
        )
        scored = []
        for node in self._topology.nodes.values():
            if node.active:
                continue
            feasibility = self._feasibility(node)
            if feasibility <= 0.0:
                continue
            score = self._ucb_score(node, total_attempts)
            if score > 0.0:
                scored.append((node, score))
        return sorted(scored, key=lambda x: x[1], reverse=True)

    def select_target(self) -> Optional[CuriosityTarget]:
        """Select the single highest-value capability to explore next.
        Returns None if no feasible target exists.
        """
        ranked = self.score_all()
        if not ranked:
            return None
        best_node, best_score = ranked[0]
        entropy = self._entropy(best_node.domain)
        feasibility = self._feasibility(best_node)
        rationale = (
            f"Domain '{best_node.domain}' has Shannon Entropy H={entropy:.3f} "
            f"(coverage={self._topology.domain_coverage(best_node.domain):.1%}). "
            f"Hardware feasibility={feasibility:.2f}. "
            f"UCB={best_score:.4f} across {best_node.exploration_attempts} prior attempts."
        )
        return CuriosityTarget(
            capability=best_node,
            ucb_score=best_score,
            entropy_score=entropy,
            feasibility_score=feasibility,
            rationale=rationale,
        )
