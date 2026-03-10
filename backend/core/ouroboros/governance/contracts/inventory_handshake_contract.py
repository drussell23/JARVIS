# governance/contracts/inventory_handshake_contract.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Mapping, Sequence, Protocol
from abc import ABC, abstractmethod


class HandshakeMode(str, Enum):
    HARD_FAIL = "hard_fail"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class BrainDescriptor:
    brain_id: str
    provider: str
    capabilities: FrozenSet[str]
    routable: bool
    health_state: str
    version: str
    contract_version: str


@dataclass(frozen=True)
class PolicyManifest:
    schema_version: str
    contract_version: str
    min_runtime_contract_version: str
    max_runtime_contract_version: str
    required_brains: FrozenSet[str]
    optional_brains: FrozenSet[str]
    allowed_brains: FrozenSet[str]
    required_capabilities: Mapping[str, FrozenSet[str]]
    mode: HandshakeMode


@dataclass(frozen=True)
class RuntimeInventory:
    schema_version: str
    contract_version: str
    generated_at_epoch_s: int
    brains: Mapping[str, BrainDescriptor]


@dataclass(frozen=True)
class HandshakeDiff:
    phantom_required: FrozenSet[str]       # required in policy but not routable+ready in runtime
    optional_missing: FrozenSet[str]
    unexpected_runtime: FrozenSet[str]
    capability_mismatch: FrozenSet[str]    # brain_id present but missing required capabilities


@dataclass(frozen=True)
class HandshakeResult:
    accepted: bool
    degraded: bool
    reason_codes: Sequence[str]
    active_brain_set: FrozenSet[str]
    diff: HandshakeDiff


class PolicyLoader(Protocol):
    async def load_policy(self) -> PolicyManifest:
        ...


class RuntimeInventoryProvider(Protocol):
    async def fetch_runtime_inventory(self) -> RuntimeInventory:
        ...


class HandshakeEngine(ABC):
    @abstractmethod
    def validate_schema(self, policy: PolicyManifest, runtime: RuntimeInventory) -> None:
        raise NotImplementedError

    @abstractmethod
    def validate_contract_versions(self, policy: PolicyManifest, runtime: RuntimeInventory) -> None:
        raise NotImplementedError

    @abstractmethod
    def diff(self, policy: PolicyManifest, runtime: RuntimeInventory) -> HandshakeDiff:
        raise NotImplementedError

    @abstractmethod
    def evaluate(self, policy: PolicyManifest, runtime: RuntimeInventory) -> HandshakeResult:
        """
        Required acceptance logic:
        - if phantom_required non-empty => hard fail unless policy mode explicitly allows degraded start
        - unexpected_runtime not routable unless allowlisted by policy
        - active_brain_set = allowed ∩ runtime_routable_ready
        """
        raise NotImplementedError


class RouteTablePublisher(Protocol):
    async def publish_active_brain_set(self, active_brain_set: FrozenSet[str], snapshot_hash: str) -> None:
        ...


# Authority boundary (must remain true):
# - unified_supervisor owns handshake validation + active brain admission/withdrawal
# - governed loop can only select from active_brain_set
# - governed loop cannot bypass handshake gate
