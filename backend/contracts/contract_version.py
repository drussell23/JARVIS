"""
Contract versioning with N/N-1 rolling compatibility.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ContractVersion:
    """Versioned contract with rolling compatibility window."""
    current: Tuple[int, int, int]
    min_supported: Tuple[int, int, int]
    max_supported: Tuple[int, int, int]

    def is_compatible(self, remote_version: Tuple[int, int, int]) -> Tuple[bool, str]:
        """Check if a remote version is compatible with this contract."""
        if remote_version < self.min_supported:
            return False, f"remote {remote_version} below min_supported {self.min_supported}"
        if remote_version > self.max_supported:
            return False, f"remote {remote_version} above max_supported {self.max_supported}"
        return True, "compatible"

    def to_dict(self) -> dict:
        return {
            "current": list(self.current),
            "min_supported": list(self.min_supported),
            "max_supported": list(self.max_supported),
        }


LOCAL_CONTRACT = ContractVersion(
    current=(0, 3, 0),
    min_supported=(0, 2, 0),
    max_supported=(0, 4, 0),
)


def compute_policy_hash(policy_data: dict) -> str:
    """Deterministic hash of policy data for drift detection."""
    canonical = json.dumps(policy_data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
