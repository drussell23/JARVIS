"""
Provider capability manifest — published by Prime, consumed by JARVIS.
"""
from dataclasses import dataclass
from typing import FrozenSet, Tuple


@dataclass(frozen=True)
class ProviderManifest:
    """Capability manifest published by a model provider."""
    provider_id: str
    capabilities: FrozenSet[str]
    contract_version: Tuple[int, int, int]
    policy_hash: str
    timestamp: float

    def supports(self, capability: str) -> bool:
        """Check if provider supports a capability."""
        return capability in self.capabilities

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "capabilities": sorted(self.capabilities),
            "contract_version": list(self.contract_version),
            "policy_hash": self.policy_hash,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderManifest":
        return cls(
            provider_id=data["provider_id"],
            capabilities=frozenset(data.get("capabilities", [])),
            contract_version=tuple(data.get("contract_version", [0, 0, 0])),
            policy_hash=data.get("policy_hash", ""),
            timestamp=data.get("timestamp", 0.0),
        )
