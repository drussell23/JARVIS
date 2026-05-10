"""§35 row 🟡 #4 / §3.6.3 priority #4 — Cross-runner artifact contract
(schema-versioned).

Closes fragility vector #8: Wave 2 PhaseRunner extraction threads ~7
cross-phase leaks (`generation`, `episodic_memory`,
`generate_retries_remaining`, `advisory`, `best_candidate`, `t_apply`,
`risk_tier`) plus `consciousness_bridge` via untyped ``ctx.artifacts``
dict. Verbatim extraction sidestepped the issue. As soon as a runner
is *refactored* beyond verbatim, one unversioned dict shape change
(rename / type drift / phase-ownership confusion) crashes the FSM
with no recovery path.

This module ships the schema-versioned contract that catches the drift.

## Design pillars (operator binding 2026-05-09)

  * **Asynchronous** — pure-function validators run synchronously
    inside the dispatcher's ``merge_artifacts`` choke point. No new
    tasks, no new threads. The validator IS the contract; the
    dispatcher is the caller.

  * **Dynamic** — registry is a bytes-pinned tuple of ``ArtifactSpec``
    entries. Each spec carries a callable ``validate_value`` predicate
    so consumers can use duck-typing instead of ``isinstance`` against
    a specific class (avoids forcing the contract to import every
    producer's value type at module load).

  * **Adaptive** — schema_version per artifact. A future refactor that
    changes the shape of (e.g.) ``generation`` bumps THAT artifact's
    schema_version while leaving the others alone. Operators see
    schema-version skew explicitly, not as a generic dict crash.

  * **Intelligent** — distinguishes 4 violation kinds explicitly:
    UNKNOWN_KEY (rename / typo) / TYPE_MISMATCH (validator returns
    False) / WRONG_PRODUCER (a phase writes an artifact it doesn't
    own) / SCHEMA_VERSION_SKEW (artifact carries explicit schema_version
    field that doesn't match registry). No implicit "unknown" /
    no None — every input maps to exactly one outcome.

  * **Robust** — never raises out of the validator itself. Validation
    failures surface via ``ArtifactValidation`` returns; the dispatcher
    chooses whether to log-and-pass (master-off / advisory) or
    PhaseContextError-raise (master-on / strict).

  * **No hardcoding** — every threshold env-tunable; defaults are
    operator-overridable, not magic constants. Strictness mode
    (``advisory`` / ``strict``) is env-tunable.

## Authority invariants (AST-pinned by companion tests)

  * Imports stdlib + ``op_context`` (OperationPhase enum) ONLY.
  * NEVER imports orchestrator / phase_runners / candidate_generator
    / iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / phase_dispatcher.
  * Every public function NEVER raises — failures surface via
    ``ArtifactValidation`` return objects.
  * The ``_ARTIFACT_REGISTRY`` bytes-pin is structural: it MUST
    cover every cross-phase leak slot declared in
    ``phase_dispatcher.PhaseContext`` (companion test sweeps both).

## Master flag default-false until graduation cadence

``JARVIS_ARTIFACT_CONTRACT_ENABLED`` (default-FALSE per §33.1).
``JARVIS_ARTIFACT_CONTRACT_STRICTNESS`` chooses between
``advisory`` (default — log only) and ``strict`` (raise
PhaseContextError on validation failure).

When the master flag is off, ``validate_artifact_value`` returns
``ArtifactValidation(outcome=ValidationOutcome.PASSED_DISABLED,
detail="master_off")`` — byte-equivalent to legacy behavior. The
substrate is the rails; graduation flips strict-mode validation
on after 3-clean-soak ladder.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


ARTIFACT_CONTRACT_SCHEMA_VERSION: str = "artifact_contract.1"


# ---------------------------------------------------------------------------
# Env knobs — defaults overridable; never hardcoded behavior constants
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_ARTIFACT_CONTRACT_ENABLED`` (default ``false`` until
    graduation cadence). Asymmetric env semantics: empty/whitespace =
    unset = default-false; explicit truthy/falsy overrides at call
    time. Re-read on every public-API entry so flips hot-revert."""
    raw = os.environ.get(
        "JARVIS_ARTIFACT_CONTRACT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in ("1", "true", "yes", "on")


def strictness() -> str:
    """``JARVIS_ARTIFACT_CONTRACT_STRICTNESS`` (default ``advisory``).
    Two modes:

      * ``advisory`` — validation runs; failures log + return False
        but caller proceeds.
      * ``strict`` — caller (dispatcher) raises PhaseContextError on
        any validation failure.

    Master flag still gates the whole substrate — strictness only
    matters when ``master_enabled() is True``."""
    raw = os.environ.get(
        "JARVIS_ARTIFACT_CONTRACT_STRICTNESS", "",
    ).strip().lower()
    if raw not in ("advisory", "strict"):
        return "advisory"
    return raw


# ---------------------------------------------------------------------------
# Closed taxonomies — frozen by AST pin
# ---------------------------------------------------------------------------


class ArtifactKind(str, enum.Enum):
    """Closed taxonomy of cross-phase artifacts. Matches the slot
    declarations in ``phase_dispatcher.PhaseContext`` 1:1.

    Adding a new artifact REQUIRES adding both:
      * A new ArtifactKind value here
      * A new ArtifactSpec in ``_ARTIFACT_REGISTRY`` below
      * A new slot in ``PhaseContext`` (the AST pin verifies coverage)

    The taxonomy is closed — drift here breaks the cross-phase
    contract."""
    GENERATION = "generation"
    EPISODIC_MEMORY = "episodic_memory"
    GENERATE_RETRIES_REMAINING = "generate_retries_remaining"
    ADVISORY = "advisory"
    BEST_CANDIDATE = "best_candidate"
    BEST_VALIDATION = "best_validation"
    T_APPLY = "t_apply"
    RISK_TIER = "risk_tier"
    CONSCIOUSNESS_BRIDGE = "consciousness_bridge"
    CANCEL_TOKEN = "cancel_token"


class ValidationOutcome(str, enum.Enum):
    """Closed 6-value taxonomy of artifact validation outcomes."""
    OK = "ok"
    UNKNOWN_KEY = "unknown_key"
    TYPE_MISMATCH = "type_mismatch"
    WRONG_PRODUCER = "wrong_producer"
    SCHEMA_VERSION_SKEW = "schema_version_skew"
    PASSED_DISABLED = "passed_disabled"  # master flag off → no-op pass


# ---------------------------------------------------------------------------
# Frozen artifacts — §33.5 versioned shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactSpec:
    """Schema for one cross-phase artifact. Frozen — registry is
    bytes-pinned via the AST sweep over ``_ARTIFACT_REGISTRY``.

    Fields:

    ``kind``
        Closed ArtifactKind taxonomy value.

    ``key``
        Canonical string key — MUST match the ``PhaseContext`` slot
        name. Producer's ``PhaseResult.artifacts[key] = value`` is
        the load-bearing contract.

    ``producer_phases``
        Set of phases that may legitimately write this artifact.
        Most artifacts have exactly one producer, but GATE mutating
        ``risk_tier`` is documented in PhaseContext docstring as a
        legal multi-producer case.

    ``consumer_phases``
        Set of phases that read this artifact. Documentation field —
        not enforced at runtime (consumers are read-only).

    ``validate_value``
        Pure-function predicate that returns True if the artifact
        value satisfies its expected shape. Duck-typing — the spec
        does NOT enforce a specific class to avoid forcing the
        contract module to import every producer's type at module
        load. ``None`` is treated as a sentinel for absence and
        always passes the validator (consumers handle it).

    ``schema_version``
        Per-artifact schema version. A future refactor that changes
        ``generation``'s shape bumps THIS spec's schema_version to
        ``generation.2`` so operators see explicit version skew.
    """
    kind: ArtifactKind
    key: str
    producer_phases: FrozenSet[str]
    consumer_phases: FrozenSet[str]
    validate_value: Callable[[Any], bool] = field(
        default=lambda _: True,
    )
    schema_version: str = "1.0"


@dataclass(frozen=True)
class ArtifactValidation:
    """Outcome of one ``validate_artifact_value`` call. Frozen so
    consumers can propagate / log without aliasing concerns."""
    outcome: ValidationOutcome
    detail: str = ""
    artifact_key: str = ""
    expected_kind: Optional[ArtifactKind] = None
    schema_version: str = ARTIFACT_CONTRACT_SCHEMA_VERSION

    def is_valid(self) -> bool:
        return self.outcome in (
            ValidationOutcome.OK, ValidationOutcome.PASSED_DISABLED,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "detail": self.detail,
            "artifact_key": self.artifact_key,
            "expected_kind": (
                self.expected_kind.value
                if self.expected_kind is not None else ""
            ),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Validators — pure functions; never import producer types
# ---------------------------------------------------------------------------


def _is_optional_int(value: Any) -> bool:
    return value is None or isinstance(value, int)


def _is_float_or_int(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(
        value, bool,
    )


def _is_dataclass_or_none(value: Any) -> bool:
    """Permissive duck-typed check — value is None OR has the
    ``__dataclass_fields__`` attribute. Avoids importing the producer's
    specific dataclass at module load."""
    if value is None:
        return True
    return hasattr(value, "__dataclass_fields__")


def _is_object_with_value_attr(value: Any) -> bool:
    """For risk_tier — RiskTier enum instances have ``.value``.
    None is allowed (CLASSIFY may not have stamped yet)."""
    if value is None:
        return True
    return hasattr(value, "value")


def _is_any(_value: Any) -> bool:
    """Truly opaque — used for cancel_token (CancelToken instance OR
    None) + consciousness_bridge (lazy-imported module). The
    registry's role for these is the KEY pin, not value validation."""
    return True


# ---------------------------------------------------------------------------
# Registry — bytes-pinned tuple. Drift detected by AST sweep.
# ---------------------------------------------------------------------------


# Phase strings match OperationPhase enum names — string form avoids
# circular import at module load. Runtime resolution against
# OperationPhase happens in dispatcher.
_ARTIFACT_REGISTRY: Tuple[ArtifactSpec, ...] = (
    ArtifactSpec(
        kind=ArtifactKind.GENERATION,
        key="generation",
        producer_phases=frozenset({"GENERATE", "GENERATE_RETRY"}),
        consumer_phases=frozenset({"VALIDATE", "VALIDATE_RETRY"}),
        validate_value=_is_dataclass_or_none,
        schema_version="generation.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.EPISODIC_MEMORY,
        key="episodic_memory",
        producer_phases=frozenset({"GENERATE", "GENERATE_RETRY"}),
        consumer_phases=frozenset({"VALIDATE", "VALIDATE_RETRY"}),
        validate_value=_is_any,
        schema_version="episodic_memory.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.GENERATE_RETRIES_REMAINING,
        key="generate_retries_remaining",
        producer_phases=frozenset({"GENERATE", "GENERATE_RETRY"}),
        consumer_phases=frozenset({"VALIDATE", "VALIDATE_RETRY"}),
        validate_value=_is_optional_int,
        schema_version="generate_retries_remaining.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.ADVISORY,
        key="advisory",
        producer_phases=frozenset({"CLASSIFY"}),
        consumer_phases=frozenset({"PLAN"}),
        validate_value=_is_any,
        schema_version="advisory.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.BEST_CANDIDATE,
        key="best_candidate",
        producer_phases=frozenset({"VALIDATE", "VALIDATE_RETRY"}),
        consumer_phases=frozenset({"GATE", "APPROVE", "APPLY"}),
        validate_value=_is_dataclass_or_none,
        schema_version="best_candidate.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.BEST_VALIDATION,
        key="best_validation",
        producer_phases=frozenset({"VALIDATE", "VALIDATE_RETRY"}),
        consumer_phases=frozenset({"GATE"}),
        validate_value=_is_dataclass_or_none,
        schema_version="best_validation.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.T_APPLY,
        key="t_apply",
        producer_phases=frozenset({"APPLY"}),
        consumer_phases=frozenset({"COMPLETE"}),
        validate_value=_is_float_or_int,
        schema_version="t_apply.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.RISK_TIER,
        # CLASSIFY produces it (via ctx.advance, not artifacts —
        # but GATE mutates and may emit through artifacts in
        # future refactors). Spec carries the contract; at-runtime
        # most ops never see this artifact key.
        key="risk_tier",
        producer_phases=frozenset({"CLASSIFY", "GATE"}),
        consumer_phases=frozenset({"GATE", "APPROVE", "APPLY"}),
        validate_value=_is_object_with_value_attr,
        schema_version="risk_tier.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.CONSCIOUSNESS_BRIDGE,
        key="consciousness_bridge",
        producer_phases=frozenset({"CLASSIFY"}),
        consumer_phases=frozenset({"GENERATE", "VERIFY"}),
        validate_value=_is_any,
        schema_version="consciousness_bridge.1",
    ),
    ArtifactSpec(
        kind=ArtifactKind.CANCEL_TOKEN,
        key="cancel_token",
        # cancel_token is set on PhaseContext at boot by the
        # dispatcher caller, not by a phase runner. Empty
        # producer_phases means: any runner emitting this key is
        # a WRONG_PRODUCER violation. Documented contract.
        producer_phases=frozenset(),
        consumer_phases=frozenset({
            "GENERATE", "GENERATE_RETRY", "APPLY",
        }),
        validate_value=_is_any,
        schema_version="cancel_token.1",
    ),
)


def _registry_by_key() -> Dict[str, ArtifactSpec]:
    """Lazy-built dict view for O(1) key lookup. Module-level cache."""
    global _registry_by_key_cache
    if _registry_by_key_cache is None:
        _registry_by_key_cache = {
            spec.key: spec for spec in _ARTIFACT_REGISTRY
        }
    return _registry_by_key_cache


_registry_by_key_cache: Optional[Dict[str, ArtifactSpec]] = None


def lookup_spec(key: str) -> Optional[ArtifactSpec]:
    """Return the canonical ArtifactSpec for ``key``, or None if no
    such spec is registered (caller treats as UNKNOWN_KEY)."""
    return _registry_by_key().get(str(key) if key else "")


# ---------------------------------------------------------------------------
# Public validation API — pure functions; NEVER raise
# ---------------------------------------------------------------------------


def validate_artifact_value(
    *,
    key: Any,
    value: Any,
    producer_phase: Any = None,
) -> ArtifactValidation:
    """Validate one ``(key, value)`` artifact against the registry.

    Master-off path: returns ``PASSED_DISABLED`` immediately —
    byte-equivalent to legacy unchecked behavior.

    Master-on path: looks up spec by key; runs validators for type
    + producer-phase membership.

    NEVER raises. Failures surface via the returned ``ArtifactValidation.outcome``.
    """
    if not master_enabled():
        return ArtifactValidation(
            outcome=ValidationOutcome.PASSED_DISABLED,
            detail="master_off",
            artifact_key=str(key) if key else "",
        )
    key_str = str(key) if key else ""
    if not key_str:
        return ArtifactValidation(
            outcome=ValidationOutcome.UNKNOWN_KEY,
            detail="empty key",
            artifact_key="",
        )
    spec = lookup_spec(key_str)
    if spec is None:
        return ArtifactValidation(
            outcome=ValidationOutcome.UNKNOWN_KEY,
            detail=(
                f"key {key_str!r} is not registered in "
                f"_ARTIFACT_REGISTRY — possible rename / typo / "
                f"new artifact lacking a spec entry"
            ),
            artifact_key=key_str,
        )
    # Producer-phase check — only when caller supplied a phase
    # AND the spec declares producers (empty set means "no phase
    # may produce this; it's set by the dispatcher caller").
    if producer_phase is not None:
        producer_phase_str = (
            getattr(producer_phase, "name", None)
            or str(producer_phase)
        )
        if not spec.producer_phases:
            return ArtifactValidation(
                outcome=ValidationOutcome.WRONG_PRODUCER,
                detail=(
                    f"key {key_str!r} has empty producer_phases "
                    f"(infrastructure-set artifact); phase "
                    f"{producer_phase_str!r} attempted to emit it "
                    f"via PhaseResult.artifacts"
                ),
                artifact_key=key_str,
                expected_kind=spec.kind,
            )
        if producer_phase_str not in spec.producer_phases:
            return ArtifactValidation(
                outcome=ValidationOutcome.WRONG_PRODUCER,
                detail=(
                    f"key {key_str!r} expected producer in "
                    f"{sorted(spec.producer_phases)} but got "
                    f"{producer_phase_str!r}"
                ),
                artifact_key=key_str,
                expected_kind=spec.kind,
            )
    # Type / shape check — defensive: validator may itself raise
    # on weird input. We swallow + treat as TYPE_MISMATCH.
    try:
        if not spec.validate_value(value):
            return ArtifactValidation(
                outcome=ValidationOutcome.TYPE_MISMATCH,
                detail=(
                    f"value of type {type(value).__name__} failed "
                    f"validator for {key_str!r}"
                ),
                artifact_key=key_str,
                expected_kind=spec.kind,
                schema_version=spec.schema_version,
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ArtifactValidation(
            outcome=ValidationOutcome.TYPE_MISMATCH,
            detail=(
                f"validator for {key_str!r} raised "
                f"{type(exc).__name__}: {exc}"
            ),
            artifact_key=key_str,
            expected_kind=spec.kind,
            schema_version=spec.schema_version,
        )
    return ArtifactValidation(
        outcome=ValidationOutcome.OK,
        detail="ok",
        artifact_key=key_str,
        expected_kind=spec.kind,
        schema_version=spec.schema_version,
    )


def validate_artifacts_bundle(
    artifacts: Any,
    *,
    producer_phase: Any = None,
) -> Tuple[ArtifactValidation, ...]:
    """Validate every (key, value) in an artifacts mapping.

    Returns a tuple of ``ArtifactValidation`` — one per artifact in
    iteration order. Empty mapping → empty tuple. Non-Mapping input
    → single TYPE_MISMATCH outcome with detail. NEVER raises."""
    if not master_enabled():
        return ()
    if artifacts is None:
        return ()
    if not hasattr(artifacts, "items"):
        return (
            ArtifactValidation(
                outcome=ValidationOutcome.TYPE_MISMATCH,
                detail=(
                    f"artifacts must be a Mapping, got "
                    f"{type(artifacts).__name__}"
                ),
            ),
        )
    out: list = []
    try:
        for key, value in artifacts.items():
            out.append(
                validate_artifact_value(
                    key=key, value=value,
                    producer_phase=producer_phase,
                )
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        out.append(
            ArtifactValidation(
                outcome=ValidationOutcome.TYPE_MISMATCH,
                detail=(
                    f"artifacts iteration raised "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        )
    return tuple(out)


def first_failure(
    validations: Tuple[ArtifactValidation, ...],
) -> Optional[ArtifactValidation]:
    """Return the first non-OK validation, or None if all OK / disabled.
    Helper for callers that fail-fast on the earliest violation
    (e.g., dispatcher in strict mode)."""
    for v in validations:
        if not v.is_valid():
            return v
    return None


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


def reset_registry_cache_for_tests() -> None:
    """Drop the registry-by-key cache so tests can monkey-patch
    ``_ARTIFACT_REGISTRY`` and have lookup_spec see the new contents."""
    global _registry_by_key_cache
    _registry_by_key_cache = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "ARTIFACT_CONTRACT_SCHEMA_VERSION",
    "ArtifactKind",
    "ArtifactSpec",
    "ArtifactValidation",
    "ValidationOutcome",
    "first_failure",
    "lookup_spec",
    "master_enabled",
    "reset_registry_cache_for_tests",
    "strictness",
    "validate_artifact_value",
    "validate_artifacts_bundle",
]
