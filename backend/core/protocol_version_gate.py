"""
Protocol Version Gate v1.0
==========================

Phase 12 Disease 2: Hot-update compatibility window.

Root cause: Rolling updates and hot-swaps (GCP VM promotion, PrimeClient
endpoint swap, DLM lock metadata exchange) perform NO version negotiation.
When a v235.3 supervisor hot-swaps to a v234.0 J-Prime, the APARS fields,
health response shapes, and inference request formats may be incompatible.
version_negotiation.py (600+ lines) exists but is NEVER imported in any
hot-swap path.

This module provides:
  1. ProtocolVersion — lightweight wrapper around SemanticVersion from
     existing version_negotiation.py. Adds min/max range support for
     compatibility windows.
  2. VersionGate — gate that blocks hot-swap unless the remote version
     falls within the local component's compatible range.
  3. check_health_schema() — validates health endpoint responses against
     startup_contracts.py schemas (wires the existing unused function).

All convenience functions are FAIL-OPEN: on import errors or exceptions
they return permissive defaults so JARVIS never hangs on version checks.

IMPORTS from existing modules (no duplication):
  - backend.core.version_negotiation: SemanticVersion, Capability, CapabilitySet
  - backend.core.startup_contracts: validate_health_response, HEALTH_SCHEMAS

v276.0 Phase 12 hardening.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Import from existing modules (FAIL-OPEN)
# ============================================================================

_SemanticVersion = None
_validate_health_response = None

try:
    from backend.core.version_negotiation import (
        SemanticVersion as _SemanticVersion,
    )
except ImportError:
    try:
        from core.version_negotiation import (
            SemanticVersion as _SemanticVersion,
        )
    except ImportError:
        pass

try:
    from backend.core.startup_contracts import (
        validate_health_response as _validate_health_response,
    )
except ImportError:
    try:
        from core.startup_contracts import (
            validate_health_response as _validate_health_response,
        )
    except ImportError:
        pass


# ============================================================================
# Protocol Version
# ============================================================================

@dataclass(frozen=True)
class ProtocolVersion:
    """
    Lightweight version descriptor with min/max compatibility range.

    Wraps SemanticVersion from version_negotiation.py when available,
    falls back to simple (major, minor, patch) tuple comparison.

    A remote version is compatible if:
      remote.major == local.major AND remote >= min_version AND remote <= max_version
    """

    major: int
    minor: int
    patch: int
    min_compatible: Optional[Tuple[int, int, int]] = None  # (major, minor, patch)
    max_compatible: Optional[Tuple[int, int, int]] = None  # (major, minor, patch)

    @classmethod
    def parse(cls, version_str: str,
              min_compat: Optional[str] = None,
              max_compat: Optional[str] = None) -> "ProtocolVersion":
        """Parse version string with optional compatibility range."""
        sv = _parse_semver(version_str)
        min_c = _parse_tuple(min_compat) if min_compat else None
        max_c = _parse_tuple(max_compat) if max_compat else None
        return cls(major=sv[0], minor=sv[1], patch=sv[2],
                   min_compatible=min_c, max_compatible=max_c)

    @classmethod
    def from_env(cls, prefix: str = "JARVIS") -> "ProtocolVersion":
        """
        Build from environment variables.

        Reads:
          {prefix}_PROTOCOL_VERSION (default "1.0.0")
          {prefix}_PROTOCOL_MIN_COMPAT (default None)
          {prefix}_PROTOCOL_MAX_COMPAT (default None)
        """
        # v274.1: Default to "0.0.0" (pre-stable) to match all Trinity
        # components.  Bump to "1.0.0" when a deliberate protocol break is
        # declared across supervisor + J-Prime + Reactor-Core simultaneously.
        ver_str = os.environ.get(f"{prefix}_PROTOCOL_VERSION", "0.0.0")
        min_str = os.environ.get(f"{prefix}_PROTOCOL_MIN_COMPAT")
        max_str = os.environ.get(f"{prefix}_PROTOCOL_MAX_COMPAT")
        return cls.parse(ver_str, min_str, max_str)

    def as_tuple(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def is_compatible_with(self, remote: "ProtocolVersion") -> Tuple[bool, str]:
        """
        Check if a remote version is compatible with this local version.

        Returns (compatible, reason).
        """
        # Major version must match
        if self.major != remote.major:
            return False, (
                f"major version mismatch: local={self.major}, "
                f"remote={remote.major}"
            )

        remote_tuple = remote.as_tuple()

        # Check min_compatible
        if self.min_compatible is not None and remote_tuple < self.min_compatible:
            return False, (
                f"remote {remote_tuple} below min compatible "
                f"{self.min_compatible}"
            )

        # Check max_compatible
        if self.max_compatible is not None and remote_tuple > self.max_compatible:
            return False, (
                f"remote {remote_tuple} above max compatible "
                f"{self.max_compatible}"
            )

        return True, "compatible"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


# ============================================================================
# Version Gate
# ============================================================================

class VersionGate:
    """
    Gate that blocks hot-swap unless the remote version is compatible.

    Thread-safe. Maintains an allow/deny decision with optional feature
    flag negotiation.

    Usage:
        gate = VersionGate(local_version=ProtocolVersion(1, 2, 0, min_compatible=(1, 0, 0)))
        allowed, reason = gate.check(remote_version_str="1.1.0")
        if not allowed:
            logger.warning("Hot-swap blocked: %s", reason)
            return False
    """

    def __init__(
        self,
        local_version: Optional[ProtocolVersion] = None,
        required_capabilities: Optional[Set[str]] = None,
    ):
        self._local = local_version or ProtocolVersion.from_env()
        self._required_caps = required_capabilities or set()
        self._lock = threading.Lock()
        self._last_check_result: Optional[Tuple[bool, str]] = None

    @property
    def local_version(self) -> ProtocolVersion:
        return self._local

    def check(
        self,
        remote_version_str: Optional[str] = None,
        remote_version: Optional[ProtocolVersion] = None,
        remote_capabilities: Optional[Set[str]] = None,
    ) -> Tuple[bool, str]:
        """
        Check if a remote component is compatible for hot-swap.

        Args:
            remote_version_str: Version string like "1.2.3"
            remote_version: Pre-parsed ProtocolVersion
            remote_capabilities: Set of capability names the remote supports

        Returns:
            (allowed, reason)
        """
        with self._lock:
            try:
                # Parse remote version
                if remote_version is not None:
                    rv = remote_version
                elif remote_version_str:
                    rv = ProtocolVersion.parse(remote_version_str)
                else:
                    self._last_check_result = (True, "no version provided, allowing")
                    return self._last_check_result

                # Version compatibility
                compat, reason = self._local.is_compatible_with(rv)
                if not compat:
                    self._last_check_result = (False, reason)
                    return self._last_check_result

                # Capability check
                if self._required_caps and remote_capabilities is not None:
                    missing = self._required_caps - remote_capabilities
                    if missing:
                        msg = f"missing required capabilities: {sorted(missing)}"
                        self._last_check_result = (False, msg)
                        return self._last_check_result

                self._last_check_result = (True, "compatible")
                return self._last_check_result

            except Exception as e:
                # Fail-open: allow on error
                msg = f"version check error (allowing): {e}"
                logger.debug("[VersionGate] %s", msg)
                self._last_check_result = (True, msg)
                return self._last_check_result


# ============================================================================
# Health Schema Validation
# ============================================================================

def check_health_schema(
    endpoint: str,
    data: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Validate a health response against startup_contracts.py schemas.

    Wires the existing ``validate_health_response()`` function that was
    never called in production.

    Args:
        endpoint: Schema key (e.g., "/health", "prime:/health")
        data: Parsed JSON response

    Returns:
        (valid, violations) — valid is True when no violations found.
        Fail-open: returns (True, []) on import error.
    """
    if _validate_health_response is None:
        return True, []
    try:
        violations = _validate_health_response(endpoint, data)
        return len(violations) == 0, violations
    except Exception as e:
        logger.debug("[VersionGate] Health schema check error: %s", e)
        return True, []


# ============================================================================
# Convenience Functions (FAIL-OPEN)
# ============================================================================

def version_check_for_hotswap(
    remote_version_str: Optional[str] = None,
    local_version_str: Optional[str] = None,
    min_compat_str: Optional[str] = None,
    max_compat_str: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Quick version compatibility check for hot-swap paths.

    Fail-open: returns (True, "...") on any error so hot-swap is never
    blocked by version gate failures.
    """
    try:
        if not remote_version_str:
            return True, "no remote version provided"

        local = ProtocolVersion.parse(
            local_version_str or os.environ.get("JARVIS_PROTOCOL_VERSION", "1.0.0"),
            min_compat_str or os.environ.get("JARVIS_PROTOCOL_MIN_COMPAT"),
            max_compat_str or os.environ.get("JARVIS_PROTOCOL_MAX_COMPAT"),
        )
        remote = ProtocolVersion.parse(remote_version_str)
        return local.is_compatible_with(remote)
    except Exception as e:
        return True, f"version check error (allowing): {e}"


def extract_version_from_health(
    health_data: Dict[str, Any],
    version_key: str = "protocol_version",
) -> Optional[str]:
    """
    Extract protocol version string from a health response.

    Searches common key names. Returns None if not found.
    """
    try:
        for key in (version_key, "version", "api_version", "startup_script_version"):
            val = health_data.get(key)
            if val is not None:
                return str(val)
        return None
    except Exception:
        return None


def validate_health_before_swap(
    endpoint: str,
    health_data: Dict[str, Any],
    remote_version_str: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Combined version + schema check before a hot-swap.

    Returns (allowed, reason). Fail-open: (True, "...") on any error.
    """
    try:
        # Schema validation
        schema_ok, violations = check_health_schema(endpoint, health_data)
        if not schema_ok:
            return False, f"health schema violations: {violations}"

        # Version gate
        if remote_version_str is None:
            remote_version_str = extract_version_from_health(health_data)

        if remote_version_str:
            return version_check_for_hotswap(remote_version_str)

        return True, "no version to check"
    except Exception as e:
        return True, f"pre-swap validation error (allowing): {e}"


# ============================================================================
# Internal Helpers
# ============================================================================

def _parse_semver(version_str: str) -> Tuple[int, int, int]:
    """Parse a version string into (major, minor, patch). Uses SemanticVersion if available."""
    if _SemanticVersion is not None:
        try:
            sv = _SemanticVersion.parse(version_str)
            return (sv.major, sv.minor, sv.patch)
        except Exception:
            pass
    # Fallback: simple parse
    version_str = version_str.strip().lstrip("vV")
    parts = version_str.split(".")
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch_str = parts[2].split("-")[0] if len(parts) > 2 else "0"
    patch = int(patch_str)
    return (major, minor, patch)


def _parse_tuple(version_str: str) -> Tuple[int, int, int]:
    """Parse a version string into a (major, minor, patch) tuple."""
    return _parse_semver(version_str)
