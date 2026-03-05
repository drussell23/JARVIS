"""
Cross-repo contract definitions for Trinity integration.

Provides:
- HealthContractV1: schema-versioned health response parsing
- Typed error hierarchy for cross-repo failure classification

v311.0: Created per hardening design Phase 3A.
"""
from dataclasses import dataclass, fields
from typing import Any, Dict


class CrossRepoError(Exception):
    """Base class for all cross-repo integration errors."""
    pass


class RepoNotFoundError(CrossRepoError):
    """Repository path does not exist on disk."""
    pass


class RepoImportError(CrossRepoError):
    """Python import of repository module failed."""
    pass


class RepoUnreachableError(CrossRepoError):
    """Repository exists but health endpoint did not respond."""
    pass


class RepoContractError(CrossRepoError):
    """Repository responded but with incompatible schema version."""
    pass


class UnsupportedContractVersion(RepoContractError):
    """Remote repo uses a contract version this client does not understand."""
    pass


@dataclass(frozen=True)
class HealthContractV1:
    """Schema for cross-repo health endpoint responses (v1)."""

    contract_version: int = 0
    status: str = "unknown"
    model_loaded: bool = False
    ready_for_inference: bool = False
    trinity_connected: bool = False

    @classmethod
    def from_response(cls, data: Dict[str, Any]) -> "HealthContractV1":
        """Parse a health response dict into a typed contract.

        Raises:
            UnsupportedContractVersion: if contract_version > 1
        """
        version = data.get("contract_version", 0)

        if version > 1:
            raise UnsupportedContractVersion(
                f"Expected contract_version <= 1, got {version}"
            )

        field_names = {f.name for f in fields(cls)}
        kwargs = {k: data[k] for k in field_names if k in data}

        if version == 0:
            kwargs["contract_version"] = 0

        return cls(**kwargs)
