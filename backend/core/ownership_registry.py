"""
JARVIS Cross-Repo Ownership Registry v1.0
==========================================
Declares which module owns each cross-cutting concern so that boundary
violations are visible and auditable.

Root cause cured:
  - GCP VM termination callable from 6 independent modules with no hierarchy
  - unified_supervisor accesses cross_repo's private _trinity_gcp_ready_event
  - Startup mode env vars mutated by multiple layers
  - No declaration of who owns what — impossible to audit boundary violations

This registry is ADVISORY ONLY — check_caller_authorized() logs warnings
but never raises or blocks. The goal is to make erosion visible first,
then progressively tighten in future phases.

v272.0: Created as part of Phase 9 — cross-repo boundary erosion.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ===========================================================================
# Data Model
# ===========================================================================

@dataclass(frozen=True)
class OwnershipDeclaration:
    """Declares who owns a cross-module concern.

    Advisory only — check_caller_authorized() logs warnings but
    never raises or blocks.
    """
    concept: str                  # e.g., "gcp_vm_lifecycle"
    description: str
    owning_module: str            # Canonical owner module path
    allowed_callers: tuple        # Module paths allowed to invoke operations
    version: str = "1.0.0"


# ===========================================================================
# Canonical Ownership Declarations
# ===========================================================================

OWNERSHIP_DECLARATIONS: Dict[str, OwnershipDeclaration] = {
    "gcp_vm_lifecycle": OwnershipDeclaration(
        concept="gcp_vm_lifecycle",
        description="GCP VM create/terminate/stop lifecycle operations",
        owning_module="backend.core.gcp_vm_manager",
        allowed_callers=(
            "backend.core.supervisor_gcp_controller",
            "backend.core.gcp_hybrid_prime_router",
            "unified_supervisor",
            "backend.supervisor.cross_repo_startup_orchestrator",
            "backend.core.cloud_ml_router",
            "backend.voice_unlock.cloud_ecapa_client",
        ),
    ),
    "startup_mode": OwnershipDeclaration(
        concept="startup_mode",
        description="Startup memory mode env var mutations (JARVIS_STARTUP_MEMORY_MODE)",
        owning_module="unified_supervisor",
        allowed_callers=(
            "unified_supervisor",
        ),
    ),
    "prime_routing": OwnershipDeclaration(
        concept="prime_routing",
        description="Prime inference routing decisions (local/GCP/cloud promotion/demotion)",
        owning_module="backend.core.prime_router",
        allowed_callers=(
            "backend.core.prime_router",
            "unified_supervisor",
            "backend.core.gcp_hybrid_prime_router",
        ),
    ),
    "startup_lifecycle": OwnershipDeclaration(
        concept="startup_lifecycle",
        description="Startup phase transitions and completion signaling (JARVIS_STARTUP_COMPLETE)",
        owning_module="unified_supervisor",
        allowed_callers=(
            "unified_supervisor",
        ),
    ),
    "trinity_gcp_ready_event": OwnershipDeclaration(
        concept="trinity_gcp_ready_event",
        description="Cross-repo GCP VM readiness asyncio.Event coordination",
        owning_module="backend.supervisor.cross_repo_startup_orchestrator",
        allowed_callers=(
            "backend.supervisor.cross_repo_startup_orchestrator",
            "unified_supervisor",
        ),
    ),
}


# ===========================================================================
# Query API
# ===========================================================================

def get_owner(concept: str) -> Optional[OwnershipDeclaration]:
    """Get the ownership declaration for a concept. Returns None if not declared."""
    return OWNERSHIP_DECLARATIONS.get(concept)


def check_caller_authorized(concept: str, caller_module: str) -> bool:
    """Advisory check — logs WARNING if caller not in allowed_callers.

    Returns True if authorized or concept unknown.
    Returns False (with warning log) if unauthorized.
    Never raises, never blocks.
    """
    decl = OWNERSHIP_DECLARATIONS.get(concept)
    if decl is None:
        return True  # Unknown concept — don't block
    for allowed in decl.allowed_callers:
        if caller_module.endswith(allowed) or allowed.endswith(caller_module):
            return True
        # Also match just the filename (e.g., "gcp_vm_manager" matches "backend.core.gcp_vm_manager")
        allowed_base = allowed.rsplit(".", 1)[-1] if "." in allowed else allowed
        caller_base = caller_module.rsplit(".", 1)[-1] if "." in caller_module else caller_module
        if caller_base == allowed_base:
            return True
    logger.warning(
        "[OwnershipRegistry] Advisory: caller '%s' accessing '%s' "
        "(owner: %s, declared callers: %s)",
        caller_module, concept, decl.owning_module, decl.allowed_callers,
    )
    return False


def list_concepts() -> List[str]:
    """List all declared ownership concepts."""
    return list(OWNERSHIP_DECLARATIONS.keys())
