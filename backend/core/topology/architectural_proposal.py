"""ArchitecturalProposal — frozen versioned output contract for Sentinel explorations."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from backend.core.topology.curiosity_engine import CuriosityTarget
    from backend.core.topology.hardware_env import HardwareEnvironmentState


@dataclass(frozen=True)
class ShadowTestResult:
    test_name: str
    passed: bool
    duration_ms: float
    output: str


@dataclass(frozen=True)
class ArchitecturalProposal:
    """Formal output contract for a completed Sentinel exploration.

    Immutable after creation. Serialized to JSON and committed to
    proposals/<capability_name>/<proposal_id>.json on a dedicated
    non-merging branch. Never auto-merged to main.
    """
    proposal_id: str
    capability_name: str
    capability_domain: str
    repo_owner: str

    ucb_score: float
    entropy_score: float
    feasibility_score: float
    curiosity_rationale: str

    hardware_tier: str
    ram_available_mb: int
    gpu_vram_free_mb: int

    generated_files: List[str]
    shadow_test_results: List[ShadowTestResult]
    all_tests_passed: bool
    sentinel_elapsed_seconds: float

    content_hash: str
    created_at: float

    @classmethod
    def create(
        cls,
        target: CuriosityTarget,
        hardware: HardwareEnvironmentState,
        generated_files: list,
        shadow_results: list,
        sentinel_elapsed: float,
    ) -> ArchitecturalProposal:
        file_contents = "".join(
            Path(f).read_text(errors="replace") for f in generated_files if Path(f).exists()
        )
        content_hash = hashlib.sha256(file_contents.encode()).hexdigest()
        return cls(
            proposal_id=str(uuid.uuid4()),
            capability_name=target.capability.name,
            capability_domain=target.capability.domain,
            repo_owner=target.capability.repo_owner,
            ucb_score=target.ucb_score,
            entropy_score=target.entropy_score,
            feasibility_score=target.feasibility_score,
            curiosity_rationale=target.rationale,
            hardware_tier=hardware.compute_tier.value,
            ram_available_mb=hardware.ram_available_mb,
            gpu_vram_free_mb=hardware.gpu.vram_free_mb if hardware.gpu else 0,
            generated_files=generated_files,
            shadow_test_results=shadow_results,
            all_tests_passed=all(r.passed for r in shadow_results),
            sentinel_elapsed_seconds=sentinel_elapsed,
            content_hash=content_hash,
            created_at=time.time(),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def summary(self) -> str:
        test_summary = f"{sum(r.passed for r in self.shadow_test_results)}/{len(self.shadow_test_results)} tests passing"
        return (
            f"Proposal {self.proposal_id[:8]}: Add capability '{self.capability_name}' "
            f"to {self.repo_owner} ({self.capability_domain} domain).\n"
            f"Curiosity rationale: {self.curiosity_rationale}\n"
            f"Shadow tests: {test_summary}. "
            f"Generated {len(self.generated_files)} file(s). "
            f"Elapsed: {self.sentinel_elapsed_seconds:.0f}s."
        )
