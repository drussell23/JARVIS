"""Multi-brain model governance contract.

Every trained model artifact exported by reactor-core must carry a
ModelArtifactManifest. J-Prime only loads artifacts whose capability
tags match the requested brain and whose runtime version is compatible.

This is the foundation for:
- Brain-scoped promotion (triage vs voice vs planner)
- Canary/shadow/active rollout state machines
- Automatic rollback on eval regression
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class BrainCapability(Enum):
    """Capability tags for J-Prime brains."""
    EMAIL_CLASSIFICATION = "email_classification"
    VOICE_PROCESSING = "voice_processing"
    PLANNING = "planning"
    REASONING = "reasoning"
    CODE_GENERATION = "code_generation"
    GENERAL = "general"


@dataclass(frozen=True)
class ModelArtifactManifest:
    """Immutable manifest for a trained model artifact.

    Attributes:
        brain_id: Unique identifier for the brain this model serves.
        model_name: Human-readable model name (e.g., "jarvis-triage-v3").
        capabilities: Tuple of BrainCapability tags this model supports.
        schema_version: Contract schema version for forward compatibility.
        min_runtime_version: Minimum J-Prime runtime version required.
        target_runtime_version: Optimal runtime version (optional).
        eval_scores: Evaluation scores from reactor-core (accuracy, f1, etc.).
        rollback_parent: Model name of the parent this was trained from.
        training_data_hash: Hash of training data for provenance.
    """
    brain_id: str
    model_name: str
    capabilities: Tuple[BrainCapability, ...] = ()
    schema_version: str = "1.0"
    min_runtime_version: str = "1.0.0"
    target_runtime_version: Optional[str] = None
    eval_scores: Dict[str, float] = field(default_factory=dict)
    rollback_parent: Optional[str] = None
    training_data_hash: Optional[str] = None


def _parse_version(v: str) -> Tuple[int, ...]:
    """Parse semver string into comparable tuple."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_compatible(
    manifest: ModelArtifactManifest,
    runtime_version: str,
    requested_capability: BrainCapability,
) -> bool:
    """Check if a model artifact is compatible with the runtime and request.

    Args:
        manifest: The model's manifest.
        runtime_version: Current J-Prime runtime version string.
        requested_capability: The capability being requested.

    Returns:
        True if the model supports the capability and the runtime is new enough.
    """
    if requested_capability not in manifest.capabilities:
        return False

    if manifest.min_runtime_version:
        if _parse_version(runtime_version) < _parse_version(manifest.min_runtime_version):
            return False

    return True
