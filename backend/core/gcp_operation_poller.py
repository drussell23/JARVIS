"""
GCP Operation Lifecycle Poller v1.0
====================================
Scope-aware, registry-backed, dedup-safe GCP zone/region/global operation poller.

Replaces ad-hoc _wait_for_operation() loops in gcp_vm_manager.py.
Canonical implementation shared by JARVIS and JARVIS-Prime.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Awaitable

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ScopeContractError(Exception):
    """Operation object lacks the zone/selfLink needed to infer its scope."""

class SplitBrainFenceError(Exception):
    """Attempted to update an operation record with a stale supervisor epoch."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TerminalReason(str, Enum):
    OP_DONE_SUCCESS              = "op_done_success"
    OP_DONE_FAILURE              = "op_done_failure"
    NOT_FOUND_CORRELATED         = "not_found_correlated"
    NOT_FOUND_UNCORRELATED       = "not_found_uncorrelated"
    NOT_FOUND_SCOPE_MISMATCH     = "not_found_scope_mismatch"
    NOT_FOUND_NO_POSTCONDITION   = "not_found_no_postcondition"
    NOT_FOUND_POSTCONDITION_FAIL = "not_found_postcondition_fail"
    PERMISSION_DENIED            = "permission_denied"
    INVALID_REQUEST              = "invalid_request"
    RETRY_BUDGET_EXHAUSTED       = "retry_budget_exhausted"
    TIMEOUT                      = "timeout"
    CANCELLED                    = "cancelled"
    SCOPE_CONTRACT_ERROR         = "scope_contract_error"


# ---------------------------------------------------------------------------
# OperationScope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperationScope:
    project: str
    zone: Optional[str]     # set for zonal operations
    region: Optional[str]   # set for regional operations
    scope_type: str         # "zonal" | "regional" | "global"

    @classmethod
    def from_operation(cls, op: Any, fallback_project: str) -> "OperationScope":
        """
        Extract scope exclusively from operation.zone, operation.region, or
        operation.self_link URL.  Never accepts a caller-supplied default zone.
        Raises ScopeContractError if none of those fields provides usable scope.
        """
        zone_url: str = getattr(op, "zone", "") or ""
        region_url: str = getattr(op, "region", "") or ""
        self_link: str = getattr(op, "self_link", "") or ""

        # Parse zone from zone URL or self_link
        zone = cls._extract_segment(zone_url, "zones")
        if not zone:
            zone = cls._extract_segment(self_link, "zones")

        # Parse region from region URL or self_link
        region = cls._extract_segment(region_url, "regions")
        if not region:
            region = cls._extract_segment(self_link, "regions")

        # Extract project from any available URL
        project = cls._extract_project(zone_url or region_url or self_link) or fallback_project

        if zone:
            return cls(project=project, zone=zone, region=None, scope_type="zonal")
        if region:
            return cls(project=project, zone=None, region=region, scope_type="regional")

        # Check if self_link indicates global scope
        if self_link and "/global/" in self_link:
            return cls(project=project, zone=None, region=None, scope_type="global")

        raise ScopeContractError(
            f"Cannot infer operation scope from op.zone={zone_url!r}, "
            f"op.region={region_url!r}, op.self_link={self_link!r}. "
            "Operation object must contain at least one scope field."
        )

    @staticmethod
    def _extract_segment(url: str, segment_type: str) -> Optional[str]:
        """Extract the value after /zones/ or /regions/ from a GCP URL."""
        if not url:
            return None
        marker = f"/{segment_type}/"
        idx = url.find(marker)
        if idx == -1:
            return None
        rest = url[idx + len(marker):]
        return rest.split("/")[0] or None

    @staticmethod
    def _extract_project(url: str) -> Optional[str]:
        """Extract project from /projects/<project>/... URL."""
        marker = "/projects/"
        idx = url.find(marker)
        if idx == -1:
            return None
        rest = url[idx + len(marker):]
        return rest.split("/")[0] or None
