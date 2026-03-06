"""Audit capabilities for the reactive state store.

Tracks schema violations (e.g. unknown enum values coerced via
``default_with_violation``) and provides a post-replay invariant audit
function that checks cross-key consistency after journal replay.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only (plus sibling types).
* Bounded history via ``collections.deque(maxlen=N)``.
* ``post_replay_invariant_audit`` never raises -- it collects findings.
"""
from __future__ import annotations

import enum
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List

from backend.core.reactive_state.types import StateEntry

logger = logging.getLogger(__name__)


# ── Enums ───────────────────────────────────────────────────────────────


class AuditSeverity(str, enum.Enum):
    """Severity level for audit findings."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ── Frozen value objects ────────────────────────────────────────────────


@dataclass(frozen=True)
class SchemaViolation:
    """Record of a schema violation detected during value coercion.

    Attributes
    ----------
    key:
        The state key whose value violated the schema.
    original_value:
        The raw value before coercion.
    coerced_value:
        The value after applying the coercion policy.
    schema_version:
        Schema version that detected the violation.
    policy:
        Coercion policy applied (e.g. ``"default_with_violation"``
        or ``"map_to:<value>"``).
    global_revision:
        Store-wide revision at the time of the violation.
    """

    key: str
    original_value: Any
    coerced_value: Any
    schema_version: int
    policy: str  # "default_with_violation" | "map_to:<value>"
    global_revision: int


@dataclass(frozen=True)
class AuditFinding:
    """A single finding from an audit check.

    Attributes
    ----------
    severity:
        How severe the finding is.
    category:
        Classification of the finding (e.g. ``"cross_key_invariant"``,
        ``"replay_invariant"``, ``"schema_violation"``).
    key:
        The primary state key related to the finding.
    message:
        Human-readable description of the finding.
    snapshot_revision:
        Store-wide revision at the time of the audit.
    """

    severity: AuditSeverity
    category: str  # "cross_key_invariant" | "replay_invariant" | "schema_violation"
    key: str
    message: str
    snapshot_revision: int


# ── AuditLog ────────────────────────────────────────────────────────────


class AuditLog:
    """Bounded, append-only log of schema violations and audit findings.

    Parameters
    ----------
    max_violations:
        Maximum number of ``SchemaViolation`` records to retain.
    max_findings:
        Maximum number of ``AuditFinding`` records to retain.
    """

    def __init__(
        self, max_violations: int = 1000, max_findings: int = 1000
    ) -> None:
        self._violations: deque[SchemaViolation] = deque(maxlen=max_violations)
        self._findings: deque[AuditFinding] = deque(maxlen=max_findings)

    def record_violation(self, violation: SchemaViolation) -> None:
        """Append a schema violation to the log."""
        self._violations.append(violation)
        logger.info(
            "Schema violation on key=%s: %r coerced to %r (policy=%s, rev=%d)",
            violation.key,
            violation.original_value,
            violation.coerced_value,
            violation.policy,
            violation.global_revision,
        )

    def record_finding(self, finding: AuditFinding) -> None:
        """Append an audit finding to the log."""
        self._findings.append(finding)
        if finding.severity == AuditSeverity.ERROR:
            logger.error(
                "Audit finding [%s] %s: key=%s — %s (rev=%d)",
                finding.severity.value,
                finding.category,
                finding.key,
                finding.message,
                finding.snapshot_revision,
            )
        elif finding.severity == AuditSeverity.WARNING:
            logger.warning(
                "Audit finding [%s] %s: key=%s — %s (rev=%d)",
                finding.severity.value,
                finding.category,
                finding.key,
                finding.message,
                finding.snapshot_revision,
            )
        else:
            logger.info(
                "Audit finding [%s] %s: key=%s — %s (rev=%d)",
                finding.severity.value,
                finding.category,
                finding.key,
                finding.message,
                finding.snapshot_revision,
            )

    @property
    def violations(self) -> List[SchemaViolation]:
        """Return a snapshot of all recorded schema violations."""
        return list(self._violations)

    @property
    def findings(self) -> List[AuditFinding]:
        """Return a snapshot of all recorded audit findings."""
        return list(self._findings)

    def has_critical_findings(self) -> bool:
        """Return ``True`` if any finding has ``ERROR`` severity."""
        return any(f.severity == AuditSeverity.ERROR for f in self._findings)


# ── Post-replay invariant audit ─────────────────────────────────────────


def post_replay_invariant_audit(
    snapshot: Dict[str, StateEntry],
    global_revision: int,
) -> List[AuditFinding]:
    """Run cross-key invariant checks against replayed state.

    This function inspects the state snapshot after journal replay and
    returns a list of ``AuditFinding`` instances for any violated
    invariants.  It **never raises** -- all problems are collected as
    findings.

    Parameters
    ----------
    snapshot:
        Mapping of state keys to their current ``StateEntry`` values.
    global_revision:
        The store-wide revision after replay completed.

    Returns
    -------
    List[AuditFinding]
        Findings for any violated cross-key invariants.
    """
    findings: List[AuditFinding] = []

    # Invariant 1: gcp.offload_active=True requires gcp.node_ip non-empty
    offload = snapshot.get("gcp.offload_active")
    ip = snapshot.get("gcp.node_ip")
    if offload is not None and offload.value is True:
        if ip is None or not ip.value:
            findings.append(
                AuditFinding(
                    severity=AuditSeverity.ERROR,
                    category="cross_key_invariant",
                    key="gcp.offload_active",
                    message=(
                        "gcp.offload_active is True but gcp.node_ip is "
                        "empty after replay"
                    ),
                    snapshot_revision=global_revision,
                )
            )

    # Invariant 2: hollow.client_active=True requires gcp.offload_active=True
    hollow = snapshot.get("hollow.client_active")
    if hollow is not None and hollow.value is True:
        if offload is None or offload.value is not True:
            findings.append(
                AuditFinding(
                    severity=AuditSeverity.ERROR,
                    category="cross_key_invariant",
                    key="hollow.client_active",
                    message=(
                        "hollow.client_active is True but "
                        "gcp.offload_active is not True after replay"
                    ),
                    snapshot_revision=global_revision,
                )
            )

    return findings
