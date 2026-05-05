"""Versioned Artifact Contract — PRD §3.6.2 vector #8 closure
(Wave 3 hygiene Item 6, 2026-05-05).

The §28.5.1 v9 brutal review identified "cross-runner artifact
contract drift" as a latent landmine: multiple ``*Artifact``
dataclasses ship without a unified schema-versioning discipline,
so a future cross-runner consumer reading an old-shape artifact
emitted by an upstream-version producer can't detect the drift
structurally. This module ships the canonical contract that
closes the class.

## Pattern (§33.5 candidate)

Every artifact dataclass that can cross runner / process
boundaries (saga ledger writes consumed by audit readers,
rollback artifacts persisted across battle-test sessions,
producer-emitted records consumed by observability surfaces)
MUST:

  1. Inherit / register as a :class:`VersionedArtifact` (Protocol).
  2. Carry a ``schema_version: str`` field defaulting to the
     module's canonical ``<MODULE>_ARTIFACT_SCHEMA_VERSION``
     constant.
  3. Bump ``schema_version`` (e.g. ``"foo_artifact.1"`` →
     ``"foo_artifact.2"``) when adding / removing / renaming
     fields; readers can branch on the version string.
  4. Expose ``to_dict()`` / ``from_dict(raw)`` symmetric
     projection methods so the artifact survives JSON round-
     trip with explicit version-aware parsing.

## Why this exists

Pre-Wave-3-hygiene, three ``*Artifact`` classes existed without
any of the above: ``RollbackArtifact`` (in-process only —
low-risk), ``SagaLedgerArtifact`` and ``WorkUnitLedgerArtifact``
(both designed for cross-runner audit but ship without
``schema_version``). The latter two are currently dormant
(zero importers) but the §28.5.1 review correctly flagged that
*activating* them in a future arc — say, an audit consumer that
readers via JSONL — would create the drift class structurally.
By landing the contract substrate now, future activations
inherit the discipline by reference rather than re-discovering
it after a soak surfaces a torn-read.

## Architectural locks

  * **Pure substrate** — stdlib only (``typing.Protocol``,
    ``inspect``, ``ast``). No governance imports.
  * **Caller-driven validation** — the substrate exposes a
    ``verify_artifact_schema(payload, expected, allowed_legacy)``
    helper readers use at deserialization time; the substrate
    never mandates a specific reader contract.
  * **Backward-compatible** — existing artifacts can adopt
    incrementally. The AST pin (§33.5 enforcement) flags
    artifacts that emit a ``schema_version`` literal but
    lack a registered constant; it does NOT force every
    dataclass named ``*Artifact`` to carry the field if the
    operator decides it stays in-process only.
  * **NEVER raises** — every helper function emits structured
    diagnostics on drift; readers branch on the result.

## Authority asymmetry

Imports stdlib + ``typing`` + ``dataclasses`` ONLY. NEVER imports
orchestrator / iron_gate / policy / providers /
candidate_generator / change_engine / semantic_guardian.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


VERSIONED_ARTIFACT_SCHEMA_VERSION: str = "versioned_artifact.1"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class VersionedArtifact(Protocol):
    """Structural protocol for artifacts that cross runner /
    process boundaries.

    Concrete artifact dataclasses don't need to inherit — duck-
    typing applies. Adopting the protocol just means:

      * Has a ``schema_version: str`` attribute / field
      * Has a ``to_dict() -> Dict[str, Any]`` method
      * (Optional) Has a ``from_dict(raw) -> Optional[<Self>]``
        classmethod that returns ``None`` on parse failure

    This is the contract the §3.6.2 vector #8 closure pins:
    artifacts emitted as JSON / JSONL across processes must
    surface their version so readers can branch."""

    schema_version: str

    def to_dict(self) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Verdict types — frozen, JSON-projectable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaVerdict:
    """Result of :func:`verify_artifact_schema` — frozen so
    consumers can branch on the verdict + propagate the
    diagnostic across async boundaries."""

    accepted: bool
    actual_schema: str
    expected_schema: str
    is_legacy: bool = False
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "actual_schema": self.actual_schema,
            "expected_schema": self.expected_schema,
            "is_legacy": self.is_legacy,
            "diagnostic": self.diagnostic,
        }


# ---------------------------------------------------------------------------
# Public API — verify_artifact_schema
# ---------------------------------------------------------------------------


def verify_artifact_schema(
    payload: Any,
    *,
    expected_schema: str,
    allowed_legacy: Iterable[str] = (),
) -> SchemaVerdict:
    """Verify that a deserialized artifact's ``schema_version``
    matches ``expected_schema`` (current) or appears in
    ``allowed_legacy`` (back-compat readers).

    Returns a frozen :class:`SchemaVerdict` with
    ``accepted=True`` for matching versions or legacy versions
    explicitly allowed; ``accepted=False`` with a structured
    diagnostic otherwise. NEVER raises.

    ``payload`` may be a dict (deserialized JSON) or any object
    with a ``.schema_version`` attribute. Missing / non-string
    schema_version → ``accepted=False`` (operator-binding:
    artifacts without a version cannot be trusted across runner
    boundaries)."""
    actual = ""
    try:
        if isinstance(payload, dict):
            actual = str(payload.get("schema_version", ""))
        else:
            actual = str(getattr(payload, "schema_version", ""))
    except Exception as exc:  # noqa: BLE001 — defensive
        return SchemaVerdict(
            accepted=False,
            actual_schema="",
            expected_schema=expected_schema,
            diagnostic=(
                f"schema_version_extract_failed: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )

    if not actual:
        return SchemaVerdict(
            accepted=False,
            actual_schema="",
            expected_schema=expected_schema,
            diagnostic="schema_version_missing_or_empty",
        )

    if actual == expected_schema:
        return SchemaVerdict(
            accepted=True,
            actual_schema=actual,
            expected_schema=expected_schema,
        )

    legacy_set = frozenset(allowed_legacy or ())
    if actual in legacy_set:
        return SchemaVerdict(
            accepted=True,
            actual_schema=actual,
            expected_schema=expected_schema,
            is_legacy=True,
            diagnostic=(
                f"legacy_schema_accepted: {actual} (current: "
                f"{expected_schema})"
            ),
        )

    return SchemaVerdict(
        accepted=False,
        actual_schema=actual,
        expected_schema=expected_schema,
        diagnostic=(
            f"schema_drift: actual={actual} expected="
            f"{expected_schema} legacy_allowed={sorted(legacy_set)}"
        ),
    )


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Pin asserts the substrate module's authority asymmetry +
    NEVER-raises contract. Per-Artifact pins are registered
    elsewhere (each owning module's ``register_shipped_invariants``)
    so artifacts can adopt incrementally without forcing every
    dataclass-named-Artifact to carry ``schema_version`` if the
    operator decides it stays in-process only."""
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_versioned_artifact_purity(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``versioned_artifact.py`` MUST stay pure substrate —
        no orchestrator / iron_gate / providers imports;
        stdlib + typing only."""
        violations: list = []
        forbidden = (
            "orchestrator",
            "iron_gate",
            "policy",
            "providers",
            "candidate_generator",
            "urgency_router",
            "change_engine",
            "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"versioned_artifact.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "versioned_artifact_authority_asymmetry"
            ),
            target_file=(
                "backend/core/ouroboros/governance/meta/"
                "versioned_artifact.py"
            ),
            description=(
                "versioned_artifact.py MUST stay pure "
                "substrate — stdlib + typing + dataclasses "
                "ONLY (no governance imports)."
            ),
            validate=_validate_versioned_artifact_purity,
        ),
    ]


__all__ = [
    "SchemaVerdict",
    "VERSIONED_ARTIFACT_SCHEMA_VERSION",
    "VersionedArtifact",
    "register_shipped_invariants",
    "verify_artifact_schema",
]
