"""
Cross-repo contract definitions for JARVIS ecosystem.

This is a neutral contract module — not owned by any single repo.
All capability, version, and routing authority definitions live here.
"""
from .capability_taxonomy import Capability, CAPABILITY_REGISTRY
from .contract_version import ContractVersion
from .routing_authority import RoutingAuthority, ROUTING_INVARIANTS
from .manifest_schema import ProviderManifest
