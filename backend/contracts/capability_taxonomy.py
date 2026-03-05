"""
Capability Taxonomy — stable string IDs with deprecation metadata.

Capabilities are strings (not Enums) to survive partial upgrades.
"""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Capability:
    """A capability that a model provider can offer."""
    id: str
    deprecated: bool = False
    deprecated_by: Optional[str] = None
    since_version: str = "0.1.0"


CAPABILITY_REGISTRY: Dict[str, Capability] = {
    "chat": Capability(id="chat"),
    "reasoning": Capability(id="reasoning"),
    "code": Capability(id="code"),
    "tool_use": Capability(id="tool_use"),
    "embedding": Capability(id="embedding"),
    "vision": Capability(id="vision"),
    "multimodal": Capability(id="multimodal"),
    "screen_analysis": Capability(id="screen_analysis"),
    "vision_analyze_heavy": Capability(id="vision_analyze_heavy"),
    "object_detection": Capability(id="object_detection"),
    "ui_detection": Capability(id="ui_detection"),
    "voice_activation": Capability(id="voice_activation"),
    "wake_word_detection": Capability(id="wake_word_detection"),
    "similarity_search": Capability(id="similarity_search"),
    "semantic_search": Capability(id="semantic_search"),
}


def is_valid_capability(cap_id: str) -> bool:
    """Check if a capability ID is in the canonical registry."""
    return cap_id in CAPABILITY_REGISTRY


def get_active_capabilities() -> Dict[str, Capability]:
    """Return non-deprecated capabilities only."""
    return {k: v for k, v in CAPABILITY_REGISTRY.items() if not v.deprecated}
