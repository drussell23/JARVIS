"""Priority A Slice A2 — Default-claim registry + synthesizer.

The brain layer of mandatory claim density. Provides:

  * ``DefaultClaimSpec`` — frozen, hashable spec describing one
    default claim including its filter conditions (when does this
    claim apply?) and evidence-template (what does its evaluator
    need?).
  * ``register_default_claim_spec`` / ``unregister`` — registry
    surface that mirrors PropertyOracle's ``register_evaluator``
    pattern (operator-extensible at runtime; no YAML, no hardcoded
    constants).
  * ``synthesize_default_claims`` — pure function from operation
    context → tuple of ``PropertyClaim``. Applies each spec's
    filters dynamically (file-pattern, posture, risk-tier).
  * Three seed specs registered at module load:
    - ``file_parses_after_change`` (must_hold, .py-only)
    - ``test_set_hash_stable`` (must_hold, all ops touching tests/)
    - ``no_new_credential_shapes`` (must_hold, all ops with diffs)

Master flag ``JARVIS_DEFAULT_CLAIMS_ENABLED`` (graduated default
``true``). When off, ``synthesize_default_claims`` returns ``()`` so
the PLAN-runner instrumentation (Slice A3) becomes a no-op without
touching the runner code path.

Authority invariants (AST-pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator.
  * Pure stdlib + verification.* sub-modules only.
  * NEVER raises out of any public method — defensive everywhere.

Per PRD §25.5.1 — without this module, every verification_postmortem
record has ``total_claims=0`` and Phase 2's graduation is theatrical.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time as _time
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from backend.core.ouroboros.governance.verification.property_capture import (
    PropertyClaim,
    SEVERITY_MUST_HOLD,
    _derive_claim_id,
)
from backend.core.ouroboros.governance.verification.property_oracle import (
    Property,
)

logger = logging.getLogger(__name__)


DEFAULT_CLAIMS_SCHEMA_VERSION = "default_claim_spec.1"


# ---------------------------------------------------------------------------
# Master flag — same asymmetric pattern as the rest of Phase 2
# ---------------------------------------------------------------------------


def default_claims_enabled() -> bool:
    """``JARVIS_DEFAULT_CLAIMS_ENABLED`` (default ``true``).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_DEFAULT_CLAIMS_ENABLED=false`` returns ``synthesize_default_-
    claims`` to a pure no-op (returns empty tuple). The PLAN-runner
    instrumentation (Slice A3) calls the synthesizer unconditionally;
    the master flag governs whether claims actually materialize."""
    raw = os.environ.get(
        "JARVIS_DEFAULT_CLAIMS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# DefaultClaimSpec — the registry value type
# ---------------------------------------------------------------------------


# Type alias for the optional file-pattern filter. A spec applies to
# an op iff one of the op's target files matches the pattern. Pattern
# is fnmatch-style (e.g., "*.py", "tests/**/*.py").
FilePatternFilter = Optional[str]

# Posture filter — None means "applies in any posture". Otherwise a
# tuple of posture strings (e.g., ("HARDEN", "CONSOLIDATE")).
PostureFilter = Optional[Tuple[str, ...]]

# Evidence collector hint — the spec declares which evidence keys
# its evaluator needs so the post-APPLY collector knows what to
# gather. The actual collection is done by Slice 2.4's evidence
# collector; this is a forward-declaration.


@dataclass(frozen=True)
class DefaultClaimSpec:
    """One default-claim entry. Frozen + hashable for safe registry
    storage and replay-stability.

    Fields
    ------
    claim_kind:
        The PropertyOracle evaluator kind (e.g.,
        ``"file_parses_after_change"``). Must be a registered
        evaluator (validated at synthesizer time, NOT at register
        time, so adapter ordering is permissive).
    severity:
        One of the canonical severity strings. Default seeds use
        ``SEVERITY_MUST_HOLD`` so failures escalate via POSTMORTEM.
    evidence_required:
        Tuple of evidence keys the spec's evaluator needs at
        verification time. Forward-declares the collector's contract.
    rationale:
        Human-readable why-this-claim. Persisted in the ledger record
        so future operators can audit the claim set.
    file_pattern_filter:
        Fnmatch glob (e.g., ``"*.py"``). Spec applies iff at least
        one of the op's target files matches. ``None`` = always apply.
    posture_filter:
        Tuple of posture names (e.g., ``("HARDEN",)``). Spec applies
        iff current posture is in this set. ``None`` = always apply.
    extra_metadata:
        Free-form metadata stamped onto the synthesized
        ``Property.metadata``. Useful for collectors needing
        per-spec configuration.
    """

    claim_kind: str
    severity: str = SEVERITY_MUST_HOLD
    evidence_required: Tuple[str, ...] = ()
    rationale: str = ""
    file_pattern_filter: FilePatternFilter = None
    posture_filter: PostureFilter = None
    extra_metadata: Tuple[Tuple[str, Any], ...] = field(default_factory=tuple)
    schema_version: str = DEFAULT_CLAIMS_SCHEMA_VERSION

    def applies_to_op(
        self,
        *,
        target_files: Sequence[str] = (),
        posture: Optional[str] = None,
    ) -> bool:
        """Pure predicate — does this spec apply to an op with the
        given target files + posture? NEVER raises.

        Composability:
          * file_pattern_filter and posture_filter are AND-composed.
          * Empty target_files + non-None file_pattern_filter →
            False (the spec wants files; we have none to match).
          * None filters always pass.
        """
        try:
            if self.file_pattern_filter is not None:
                pattern = str(self.file_pattern_filter)
                if not target_files:
                    return False
                if not any(
                    fnmatch.fnmatch(str(f), pattern) for f in target_files
                ):
                    return False
            if self.posture_filter is not None:
                if posture is None:
                    return False
                if str(posture).upper() not in {
                    p.upper() for p in self.posture_filter
                }:
                    return False
            return True
        except Exception:  # noqa: BLE001 — predicate must never raise
            return False

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly serialization for the ledger / observability."""
        return {
            "schema_version": self.schema_version,
            "claim_kind": self.claim_kind,
            "severity": self.severity,
            "evidence_required": list(self.evidence_required),
            "rationale": self.rationale,
            "file_pattern_filter": self.file_pattern_filter,
            "posture_filter": (
                list(self.posture_filter)
                if self.posture_filter is not None else None
            ),
            "extra_metadata": list(self.extra_metadata),
        }


# ---------------------------------------------------------------------------
# Registry — module-level, lock-protected
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, DefaultClaimSpec] = {}
_REGISTRY_LOCK = threading.RLock()


def register_default_claim_spec(
    spec: DefaultClaimSpec, *, overwrite: bool = False,
) -> None:
    """Install a DefaultClaimSpec in the registry. NEVER raises.

    Idempotent: re-registering the same (kind, callable-equivalent)
    spec is a silent no-op. Re-registering a kind with a DIFFERENT
    spec requires ``overwrite=True`` (defensive — prevents accidental
    silent override during module reloads / test suites).

    Operators amend the seed registry by registering additional
    specs from their own modules. The amend ceremony for "the seed
    set itself" is governance-controlled (Pass B Slice 6.2 amend
    queue), but adding new specs at runtime is permitted.
    """
    if not isinstance(spec, DefaultClaimSpec):
        return
    safe_kind = (str(spec.claim_kind).strip() if spec.claim_kind else "")
    if not safe_kind:
        return
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(safe_kind)
        if existing is not None:
            if existing == spec:
                return  # silent no-op on identical re-register
            if not overwrite:
                logger.info(
                    "[verification.default_claims] spec for kind=%r "
                    "already registered; use overwrite=True to replace",
                    safe_kind,
                )
                return
        _REGISTRY[safe_kind] = spec


def unregister_default_claim_spec(claim_kind: str) -> bool:
    """Remove a spec from the registry. Returns True if removed,
    False if not present. NEVER raises.

    Useful for tests + for operators rolling back a custom spec
    without restarting."""
    safe_kind = (str(claim_kind).strip() if claim_kind else "")
    if not safe_kind:
        return False
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(safe_kind, None) is not None


def list_default_claim_specs() -> Tuple[DefaultClaimSpec, ...]:
    """Return all registered specs in stable alphabetical order by
    claim_kind. NEVER raises."""
    with _REGISTRY_LOCK:
        return tuple(
            _REGISTRY[k] for k in sorted(_REGISTRY.keys())
        )


def reset_registry_for_tests() -> None:
    """Test isolation — clear the registry and re-seed from scratch.
    NEVER raises."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _register_seed_specs()


# ---------------------------------------------------------------------------
# Synthesizer — pure function from ctx → claims
# ---------------------------------------------------------------------------


def synthesize_default_claims(
    *,
    op_id: str,
    target_files: Sequence[str] = (),
    posture: Optional[str] = None,
    session_id: Optional[str] = None,
    claim_index_offset: int = 0,
) -> Tuple[PropertyClaim, ...]:
    """Walk the registered specs, apply per-spec filters, and emit
    a tuple of ``PropertyClaim`` ready for ``capture_claims``.

    Pure deterministic mapping — same (op_id, target_files, posture)
    → byte-identical output across calls (claim_id is derived via
    Slice 1.1 entropy on op_seed). NEVER raises.

    When the master flag is OFF, returns ``()`` immediately — the
    PLAN-runner instrumentation can call this unconditionally and
    the off-state is a pure no-op.

    Parameters
    ----------
    op_id:
        Required. Empty string → empty tuple.
    target_files:
        Optional sequence of file paths the op touches. Used for
        file-pattern filtering (e.g., ``file_parses_after_change``
        only applies to ops touching .py files).
    posture:
        Optional current StrategicPosture. Used for posture filtering.
    session_id:
        Optional. If unset, falls back to OUROBOROS_BATTLE_SESSION_ID
        env var or "default" — same precedence chain as Slice 2.3.
    claim_index_offset:
        Starting index for claim_id derivation. Useful when callers
        want to interleave synthesized + non-synthesized claims
        without ID collisions.
    """
    if not default_claims_enabled():
        return ()
    safe_op_id = str(op_id or "").strip()
    if not safe_op_id:
        return ()

    safe_files = tuple(str(f) for f in (target_files or ()))
    safe_posture = (
        str(posture).upper() if posture is not None else None
    )

    claims: List[PropertyClaim] = []
    ts = _time.time()
    with _REGISTRY_LOCK:
        specs = sorted(_REGISTRY.values(), key=lambda s: s.claim_kind)

    for index, spec in enumerate(specs):
        try:
            if not spec.applies_to_op(
                target_files=safe_files, posture=safe_posture,
            ):
                continue
            metadata: Dict[str, Any] = dict(spec.extra_metadata)
            metadata.setdefault("default_claim", True)
            metadata.setdefault("file_pattern", spec.file_pattern_filter)
            prop = Property.make(
                kind=spec.claim_kind,
                name=f"default::{spec.claim_kind}",
                evidence_required=spec.evidence_required,
                metadata=metadata,
            )
            claim_index = claim_index_offset + index
            claim = PropertyClaim(
                op_id=safe_op_id,
                claimed_at_phase="PLAN",
                property=prop,
                rationale=spec.rationale or (
                    f"default {spec.severity} claim for kind="
                    f"{spec.claim_kind} (Priority A — mandatory "
                    f"claim density)"
                ),
                severity=spec.severity,
                claim_id=_derive_claim_id(
                    safe_op_id, claim_index, session_id=session_id,
                ),
                ts_unix=ts,
            )
            claims.append(claim)
        except Exception:  # noqa: BLE001 — defensive per-spec
            logger.debug(
                "[verification.default_claims] spec failed for "
                "kind=%s op_id=%s — skipped",
                spec.claim_kind, safe_op_id, exc_info=True,
            )
            continue
    return tuple(claims)


# ---------------------------------------------------------------------------
# Seed specs — registered at module load
# ---------------------------------------------------------------------------


def _register_seed_specs() -> None:
    """Module-load: register the three Priority A seed specs.
    Idempotent — re-registering the same spec is a silent no-op.

    The seed set is intentionally minimal — three claims that
    apply to virtually every op. Operators can extend by importing
    ``register_default_claim_spec`` and registering additional
    specs from elsewhere; the seed set itself is amend-via-Pass-B
    governance (manifest-listed, AST-validated)."""
    register_default_claim_spec(
        DefaultClaimSpec(
            claim_kind="file_parses_after_change",
            severity=SEVERITY_MUST_HOLD,
            evidence_required=("target_files_post",),
            rationale=(
                "Every Python file touched by this op must parse "
                "cleanly post-APPLY (deterministic ast.parse check; "
                "load-bearing for code-quality regression detection)."
            ),
            file_pattern_filter="*.py",
        ),
    )
    register_default_claim_spec(
        DefaultClaimSpec(
            claim_kind="test_set_hash_stable",
            severity=SEVERITY_MUST_HOLD,
            evidence_required=("test_files_pre", "test_files_post"),
            rationale=(
                "Existing test inventory must be preserved across "
                "the op (additions OK; deletions/renames flagged) — "
                "guards against silent test-removal regressions."
            ),
            # No file_pattern_filter: this claim applies to ALL ops,
            # because a "trivial" op should still not silently delete
            # tests elsewhere in the repo. Filtering would create a
            # bypass surface.
            file_pattern_filter=None,
        ),
    )
    register_default_claim_spec(
        DefaultClaimSpec(
            claim_kind="no_new_credential_shapes",
            severity=SEVERITY_MUST_HOLD,
            evidence_required=("diff_text",),
            rationale=(
                "The diff produced by APPLY must NOT contain any "
                "credential/secret shape (5 canonical patterns from "
                "semantic_firewall) — guards against accidental "
                "secret leakage even on trivial-op exits."
            ),
            file_pattern_filter=None,
        ),
    )


_register_seed_specs()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_CLAIMS_SCHEMA_VERSION",
    "DefaultClaimSpec",
    "default_claims_enabled",
    "list_default_claim_specs",
    "register_default_claim_spec",
    "reset_registry_for_tests",
    "synthesize_default_claims",
    "unregister_default_claim_spec",
]
