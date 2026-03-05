"""Managed-mode contract utilities for JARVIS subsystems.

When JARVIS_ROOT_MANAGED=true, subsystems (Prime, Reactor) run under the
root authority's supervision.  This module provides the shared helpers that
every managed subsystem needs:

  * Environment-variable readers for the control-plane handshake.
  * Deterministic fingerprinting / hashing for capability contracts.
  * HMAC-based authentication for control-plane messages.
  * Health-envelope builder that enriches responses in managed mode
    while passing them through unchanged in standalone mode.

**Portability note:** This module is designed to be copied verbatim into
the Prime and Reactor repos.  It uses *only* stdlib imports.

Schema version follows semver and is compared by the ContractGate at boot
to reject incompatible subsystem builds.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

# ---------------------------------------------------------------------------
# Schema version (semver) — bumped on breaking envelope changes
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# Well-known exit codes
# ---------------------------------------------------------------------------

EXIT_CLEAN: int = 0
EXIT_CONFIG_ERROR: int = 100
EXIT_CONTRACT_MISMATCH: int = 101
EXIT_DEPENDENCY_FAILURE: int = 200
EXIT_RUNTIME_FATAL: int = 300

# ---------------------------------------------------------------------------
# Boot-time captures (never reset on hot reload)
# ---------------------------------------------------------------------------

_BOOT_TIME_NS: int = time.monotonic_ns()
_PID: int = os.getpid()
_EXEC_FINGERPRINT: Optional[str] = None  # computed lazily via get_exec_fingerprint()


# ===================================================================
# Environment-variable readers
# ===================================================================


def is_root_managed() -> bool:
    """Return True when JARVIS_ROOT_MANAGED is set to a truthy value.

    Reads the environment variable *fresh* on every call so that
    monkeypatching in tests (and dynamic reconfiguration) works.
    """
    return os.environ.get("JARVIS_ROOT_MANAGED", "").lower() == "true"


def get_session_id() -> str:
    """Read JARVIS_ROOT_SESSION_ID from the environment.

    Returns an empty string if unset (standalone mode).
    """
    return os.environ.get("JARVIS_ROOT_SESSION_ID", "")


def get_subsystem_role() -> str:
    """Read JARVIS_SUBSYSTEM_ROLE from the environment.

    Returns an empty string if unset (standalone mode).
    """
    return os.environ.get("JARVIS_SUBSYSTEM_ROLE", "")


def get_control_plane_secret() -> str:
    """Read JARVIS_CONTROL_PLANE_SECRET from the environment.

    Returns an empty string if unset (standalone / unauth mode).
    """
    return os.environ.get("JARVIS_CONTROL_PLANE_SECRET", "")


# ===================================================================
# Exec fingerprinting
# ===================================================================


def compute_exec_fingerprint(
    binary_path: str,
    cmdline: Union[List[str], tuple],
) -> str:
    """Compute a deterministic fingerprint for a process identity.

    Returns ``"sha256:<16-hex-chars>"`` derived from the binary path
    and its command-line arguments.
    """
    payload = json.dumps(
        {"binary": binary_path, "cmdline": list(cmdline)},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def get_exec_fingerprint() -> str:
    """Lazy singleton: fingerprint for the current process.

    Uses ``sys.executable`` and ``sys.argv``.  Computed once, then
    cached in ``_EXEC_FINGERPRINT`` for the lifetime of the process.
    """
    global _EXEC_FINGERPRINT  # noqa: PLW0603
    if _EXEC_FINGERPRINT is None:
        _EXEC_FINGERPRINT = compute_exec_fingerprint(sys.executable, sys.argv)
    return _EXEC_FINGERPRINT


# ===================================================================
# Capability hashing
# ===================================================================


def compute_capability_hash(capabilities: Dict[str, Any]) -> str:
    """Deterministic SHA-256 hash of a capabilities dict.

    Keys are sorted so that insertion order is irrelevant.
    Returns the full 64-char hex digest.
    """
    canonical = json.dumps(capabilities, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ===================================================================
# HMAC authentication
# ===================================================================


def build_hmac_auth(session_id: str, secret: str) -> str:
    """Build a control-plane auth header.

    Format: ``"<unix_timestamp>:<nonce>:<hmac_hex>"``

    The HMAC is ``HMAC-SHA256(secret, "<timestamp>:<nonce>:<session_id>")``.
    """
    ts = str(time.time())
    nonce = uuid.uuid4().hex[:16]
    msg = f"{ts}:{nonce}:{session_id}".encode("utf-8")
    sig = _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{ts}:{nonce}:{sig}"


def verify_hmac_auth(
    header: str,
    session_id: str,
    secret: str,
    tolerance_s: float = 30.0,
) -> bool:
    """Verify a control-plane auth header.

    Returns ``False`` on any parse error, HMAC mismatch, or timestamp
    outside the tolerance window.
    """
    try:
        parts = header.split(":", 2)
        if len(parts) != 3:
            return False
        ts_str, nonce, received_sig = parts
        ts = float(ts_str)
    except (ValueError, TypeError):
        return False

    # Timestamp tolerance check
    if abs(time.time() - ts) > tolerance_s:
        return False

    # Recompute expected HMAC
    msg = f"{ts_str}:{nonce}:{session_id}".encode("utf-8")
    expected_sig = _hmac.new(
        secret.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()

    return _hmac.compare_digest(received_sig, expected_sig)


# ===================================================================
# Health envelope builder
# ===================================================================


def build_health_envelope(
    base_response: Dict[str, Any],
    readiness: str,
    drain_id: Optional[str] = None,
    capability_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Enrich a health response with managed-mode metadata.

    In **standalone mode** (``JARVIS_ROOT_SESSION_ID`` not set), the
    ``base_response`` dict is returned unchanged — no enrichment keys
    are added.

    In **managed mode**, the returned dict contains all original fields
    from ``base_response`` plus the contract-mandated enrichment fields.
    """
    session_id = get_session_id()
    if not session_id:
        # Standalone mode — pass through unchanged
        return dict(base_response)

    envelope: Dict[str, Any] = dict(base_response)
    envelope.update(
        {
            "liveness": "up",
            "readiness": readiness,
            "session_id": session_id,
            "pid": _PID,
            "start_time_ns": _BOOT_TIME_NS,
            "exec_fingerprint": get_exec_fingerprint(),
            "subsystem_role": get_subsystem_role(),
            "schema_version": SCHEMA_VERSION,
            "capability_hash": capability_hash,
            "observed_at_ns": time.monotonic_ns(),
            "wall_time_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    if drain_id is not None:
        envelope["drain_id"] = drain_id

    return envelope
