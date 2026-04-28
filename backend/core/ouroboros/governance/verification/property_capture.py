"""Phase 2 Slice 2.3 — Property Capture (PLAN-time claim recording).

Closes the missing bridge between PLAN-phase claims and VERIFY-phase
verdicts. Today the planner produces a structured plan with implicit
claims ("test X must pass", "no regression in path Y", "behavior
preserved on signature Z") — but these claims are LOST by the time
VERIFY runs. VERIFY just runs the test suite; it has no idea what
was claimed.

Slice 2.3 extracts claims at PLAN time, persists them via Slice 1.2's
``decide()`` runtime, and surfaces them for VERIFY-time evaluation
through Slices 2.1 / 2.2.

ROOT PROBLEM SOLVED:

Without claim capture, post-mortem audit can only reconstruct
"what did this op claim it would do?" from PLAN logs. Worse, the
claims are SOFT — operator memory only, no machine-readable record.
Slice 2.3 makes every PLAN-phase claim a structured, replay-safe
ledger record:

  PLAN time:
    plan = await PlanGenerator.generate_plan(ctx, ...)
    claims = synthesize_claims_from_plan(plan, op_id)
    await capture_claims(op_id=op_id, claims=claims)

  VERIFY time:
    claims = await get_recorded_claims(op_id=op_id)
    for claim in claims:
        verdict = await runner.run(prop=claim.property,
                                    evidence_collector=...)
        # If must_hold + FAILED → POSTMORTEM (Slice 2.4)

LAYERING (no duplication):

  * Slice 1.1 — entropy_for: deterministic claim_id generation
  * Slice 1.2 — DecisionRuntime: per-session JSONL ledger
  * Slice 1.3 — capture_phase_decision: phase-shaped wrapper
  * Slice 2.1 — Property: the claim shape
  * Slice 2.2 — RepeatRunner: statistical verification
  * Slice 2.3 (THIS) — bridge: synthesizer + capture + reader

Synthesizer is a PURE FUNCTION (deterministic mapping from plan.1
schema → PropertyClaim list). No LLM, no side effects. Same plan
input → same claim list, replay-safe.

Capture uses Slice 1.3's ``capture_phase_decision`` directly —
each claim becomes one ledger record under (PLAN, "property_claim").
ZERO new persistence layer.

Reader walks the per-session JSONL via the same parsing path Slice
1.2 uses for replay. ZERO new disk-format code.

OPERATOR'S DESIGN CONSTRAINTS APPLIED:

  * Asynchronous — capture + reader are async (match Slice 1.3 +
    Slice 1.2 conventions). Sync callers wrap with asyncio.run.
  * Dynamic — claim severity is a free-form string with three
    canonical values; operators can extend via metadata. Property
    kinds are whatever the synthesizer or operator constructs.
  * Adaptive — synthesizer skips malformed plan fields silently.
    Reader skips unparseable ledger records. Capture failures
    don't block PLAN.
  * Intelligent — claim_id derived from (op_id, claim_index) via
    Slice 1.1 entropy → reproducible across replay sessions.
  * Robust — every public method NEVER raises. Defensive
    try/except on every external surface (Slice 1.1/1.2/1.3
    imports, ledger I/O, plan-dict access).
  * No hardcoding — synthesizer reads schema fields by name; new
    plan.1 fields require zero code changes (just operator
    extension via the registry).
  * Leverages existing — Slices 1.1, 1.2, 1.3, 2.1, 2.2.
    ZERO duplication.

AUTHORITY INVARIANTS (pinned by tests):

  * NEVER imports orchestrator / phase_runner (base) /
    candidate_generator
  * NEVER imports providers
  * Every public method NEVER raises
  * Capture failures NEVER block PLAN-phase progress
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.verification.property_oracle import (
    Property,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def property_capture_enabled() -> bool:
    """``JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED`` (default
    ``false``).

    Phase 2 Slice 2.3 master flag. Re-read at call time so monkeypatch
    works in tests + operators can flip live without re-init. Default
    flips to ``true`` at Phase 2 Slice 2.5 graduation.

    When ``false``: capture is a pure passthrough (claims synthesized
    + returned but NOT recorded). When ``true``: claims persisted
    via Slice 1.3 capture_phase_decision."""
    raw = os.environ.get(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Severity levels (canonical strings, free-form-extensible)
# ---------------------------------------------------------------------------


# Canonical severity values. Operators MAY introduce additional
# values via the metadata field — Slice 2.4 (POSTMORTEM integration)
# only branches on these three.
SEVERITY_MUST_HOLD = "must_hold"
"""Claim MUST hold post-APPLY. Verification failure → POSTMORTEM
escalation (Slice 2.4). Treat as a regression."""

SEVERITY_SHOULD_HOLD = "should_hold"
"""Claim should hold but isn't load-bearing. Verification failure
→ structured warning, no auto-block."""

SEVERITY_IDEAL = "ideal"
"""Claim is desirable but speculative. Verification failure → log
only, no operator-visible signal."""

CANONICAL_SEVERITIES: Tuple[str, ...] = (
    SEVERITY_MUST_HOLD, SEVERITY_SHOULD_HOLD, SEVERITY_IDEAL,
)


# ---------------------------------------------------------------------------
# PropertyClaim schema
# ---------------------------------------------------------------------------


PROPERTY_CLAIM_SCHEMA_VERSION = "property_claim.1"


@dataclass(frozen=True)
class PropertyClaim:
    """A claim made by an op at PLAN time, to be verified post-APPLY.

    Frozen + hashable for safe ledger persistence + cross-thread
    sharing. Two claims are equal iff all fields match.

    Field semantics:
      * ``op_id`` — operation identifier from OperationContext
      * ``claimed_at_phase`` — typically "PLAN"; may be "GENERATE"
        for claims emitted by the generator
      * ``property`` — the actual Property shape (kind + name +
        evidence_required + metadata)
      * ``rationale`` — human-readable why ("plan declared this
        test must pass post-APPLY")
      * ``severity`` — must_hold / should_hold / ideal (canonical
        strings; operators may extend)
      * ``claim_id`` — deterministic UUID via Slice 1.1 entropy
        (reproducible across replay sessions)
      * ``ts_unix`` — wall-clock at synthesis time
      * ``schema_version`` — pinned for ledger forward-compat
    """
    op_id: str
    claimed_at_phase: str
    property: Property
    rationale: str = ""
    severity: str = SEVERITY_SHOULD_HOLD
    claim_id: str = ""
    ts_unix: float = 0.0
    schema_version: str = PROPERTY_CLAIM_SCHEMA_VERSION

    @property
    def is_load_bearing(self) -> bool:
        """True iff severity is must_hold — these block via POSTMORTEM
        when verification fails."""
        return self.severity == SEVERITY_MUST_HOLD

    def to_dict(self) -> dict:
        """JSON-friendly serialization for the ledger."""
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "claimed_at_phase": self.claimed_at_phase,
            "property": {
                "kind": self.property.kind,
                "name": self.property.name,
                "evidence_required": list(self.property.evidence_required),
                "metadata": dict(self.property.metadata),
            },
            "rationale": self.rationale,
            "severity": self.severity,
            "claim_id": self.claim_id,
            "ts_unix": self.ts_unix,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Optional["PropertyClaim"]:
        """Parse from a ledger record. NEVER raises — returns None
        on unparseable input."""
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != PROPERTY_CLAIM_SCHEMA_VERSION:
                return None
            prop_raw = raw.get("property") or {}
            if not isinstance(prop_raw, Mapping):
                return None
            prop = Property.make(
                kind=str(prop_raw.get("kind", "")),
                name=str(prop_raw.get("name", "")),
                evidence_required=tuple(
                    str(e) for e in prop_raw.get("evidence_required", [])
                ),
                metadata=dict(prop_raw.get("metadata") or {}),
            )
            return cls(
                op_id=str(raw.get("op_id", "")),
                claimed_at_phase=str(raw.get("claimed_at_phase", "")),
                property=prop,
                rationale=str(raw.get("rationale", "")),
                severity=str(
                    raw.get("severity", SEVERITY_SHOULD_HOLD),
                ),
                claim_id=str(raw.get("claim_id", "")),
                ts_unix=float(raw.get("ts_unix", 0.0) or 0.0),
            )
        except (TypeError, ValueError, KeyError):
            return None


# ---------------------------------------------------------------------------
# Deterministic claim_id derivation (uses Slice 1.1 entropy)
# ---------------------------------------------------------------------------


def _derive_claim_id(
    op_id: str, claim_index: int,
    *, session_id: Optional[str] = None,
) -> str:
    """Deterministic claim_id via Slice 1.1 entropy primitives.

    Critical: must use a FRESH ``DeterministicEntropy`` per call,
    not the cached singleton from ``entropy_for``. The cached stream
    advances on each ``.uuid4()`` call, so the second call with the
    same (session, op_id, claim_index) returns a DIFFERENT UUID.
    Building a fresh stream from the deterministically-derived seed
    gives true reproducibility — same inputs always yield byte-
    identical output.

    Falls back to a wall-clock-based ID if entropy primitives are
    unavailable (cross-session reproducibility lost but uniqueness
    preserved within run). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.determinism.entropy import (
            DeterministicEntropy,
            _derive_op_seed,
            get_session_entropy,
        )
        sid = session_id
        if sid is None or not str(sid).strip():
            sid = os.environ.get(
                "OUROBOROS_BATTLE_SESSION_ID", "",
            ).strip() or "default"
        se = get_session_entropy()
        session_seed = se.seed_for_session(sid)
        op_seed = _derive_op_seed(
            session_seed, f"{op_id}:claim-{claim_index}",
        )
        # Fresh stream each call — uuid4() always returns the same
        # UUID for the same op_seed.
        ent = DeterministicEntropy(op_seed)
        return str(ent.uuid4())
    except Exception:  # noqa: BLE001 — defensive
        return f"fallback-{int(_time.monotonic() * 1e6):016x}-{claim_index}"


# ---------------------------------------------------------------------------
# Synthesizer — pure function from plan.1 dict to PropertyClaim list
# ---------------------------------------------------------------------------


def synthesize_claims_from_plan(
    plan: Mapping[str, Any],
    *,
    op_id: str,
    session_id: Optional[str] = None,
) -> Tuple[PropertyClaim, ...]:
    """Extract verifiable claims from a plan.1 dict.

    Pure deterministic mapping — same plan input → same claim list.
    NEVER raises; malformed plan fields are silently skipped.

    Current extraction surface (extensible):

      1. ``test_strategy.tests_to_pass[]`` — each test_name becomes a
         ``test_passes`` claim with severity=must_hold. The plan
         declared these tests gate the change; verification failure
         → POSTMORTEM.

      2. ``risk_factors[].type == "regression"`` with mitigation —
         becomes a ``key_present`` claim with severity=should_hold.
         Plan flagged a regression risk + recorded its mitigation;
         verification proves the mitigation held.

      3. ``test_strategy.tests_to_skip[]`` — each test becomes a
         ``key_present`` claim with severity=ideal AND metadata
         documenting the skip-rationale. Audit trail only; doesn't
         affect VERIFY decisions.

      4. ``approach.signature_invariants[]`` — function signatures
         the plan claims will not change. Becomes ``string_matches``
         claims with severity=must_hold (each invariant pair).

    Operators can extend by registering additional synthesizers via
    ``register_synthesizer`` (future slice). Slice 2.3 ships only
    the four-rule core."""
    if not isinstance(plan, Mapping) or not op_id:
        return tuple()

    claims: List[PropertyClaim] = []
    ts = _time.time()
    op_id_str = str(op_id).strip() or "unknown"

    # --- Rule 1: test_strategy.tests_to_pass ---
    test_strategy = plan.get("test_strategy") or {}
    if isinstance(test_strategy, Mapping):
        tests_to_pass = test_strategy.get("tests_to_pass") or []
        if isinstance(tests_to_pass, (list, tuple)):
            for test_name in tests_to_pass:
                if not isinstance(test_name, str) or not test_name.strip():
                    continue
                idx = len(claims)
                claims.append(PropertyClaim(
                    op_id=op_id_str,
                    claimed_at_phase="PLAN",
                    property=Property.make(
                        kind="test_passes",
                        name=f"test_passes:{test_name}",
                        evidence_required=("exit_code",),
                        metadata={"test_name": test_name},
                    ),
                    rationale=(
                        f"Plan declared test {test_name!r} must "
                        f"pass post-APPLY"
                    ),
                    severity=SEVERITY_MUST_HOLD,
                    claim_id=_derive_claim_id(
                        op_id_str, idx, session_id=session_id,
                    ),
                    ts_unix=ts,
                ))

    # --- Rule 2: risk_factors[].type == "regression" ---
    risk_factors = plan.get("risk_factors") or []
    if isinstance(risk_factors, (list, tuple)):
        for rf in risk_factors:
            if not isinstance(rf, Mapping):
                continue
            rf_type = str(rf.get("type", "")).strip().lower()
            if rf_type != "regression":
                continue
            mitigation = str(rf.get("mitigation", "")).strip()
            description = str(rf.get("description", "")).strip()
            if not mitigation:
                continue
            idx = len(claims)
            short_desc = description[:40] or "unspecified"
            claims.append(PropertyClaim(
                op_id=op_id_str,
                claimed_at_phase="PLAN",
                property=Property.make(
                    kind="key_present",
                    name=f"no_regression:{short_desc}",
                    evidence_required=("present",),
                    metadata={
                        "risk_type": "regression",
                        "mitigation": mitigation,
                        "description": description,
                    },
                ),
                rationale=(
                    f"Plan flagged regression risk; mitigation: "
                    f"{mitigation[:80]}"
                ),
                severity=SEVERITY_SHOULD_HOLD,
                claim_id=_derive_claim_id(
                    op_id_str, idx, session_id=session_id,
                ),
                ts_unix=ts,
            ))

    # --- Rule 3: test_strategy.tests_to_skip ---
    if isinstance(test_strategy, Mapping):
        tests_to_skip = test_strategy.get("tests_to_skip") or []
        if isinstance(tests_to_skip, (list, tuple)):
            for test_name in tests_to_skip:
                if not isinstance(test_name, str) or not test_name.strip():
                    continue
                idx = len(claims)
                claims.append(PropertyClaim(
                    op_id=op_id_str,
                    claimed_at_phase="PLAN",
                    property=Property.make(
                        kind="key_present",
                        name=f"test_skip_documented:{test_name}",
                        evidence_required=("present",),
                        metadata={
                            "test_name": test_name,
                            "skip_kind": "documented",
                        },
                    ),
                    rationale=(
                        f"Plan declared test {test_name!r} skipped "
                        f"with rationale (audit-only)"
                    ),
                    severity=SEVERITY_IDEAL,
                    claim_id=_derive_claim_id(
                        op_id_str, idx, session_id=session_id,
                    ),
                    ts_unix=ts,
                ))

    # --- Rule 4: approach.signature_invariants ---
    approach = plan.get("approach") or {}
    if isinstance(approach, Mapping):
        invariants = approach.get("signature_invariants") or []
        if isinstance(invariants, (list, tuple)):
            for inv in invariants:
                if not isinstance(inv, Mapping):
                    continue
                func_name = str(inv.get("function", "")).strip()
                expected = str(inv.get("signature", "")).strip()
                if not func_name or not expected:
                    continue
                idx = len(claims)
                claims.append(PropertyClaim(
                    op_id=op_id_str,
                    claimed_at_phase="PLAN",
                    property=Property.make(
                        kind="string_matches",
                        name=f"signature_invariant:{func_name}",
                        evidence_required=("actual", "expected"),
                        metadata={
                            "function": func_name,
                            "expected_signature": expected,
                        },
                    ),
                    rationale=(
                        f"Plan claimed signature of {func_name!r} "
                        f"will not change"
                    ),
                    severity=SEVERITY_MUST_HOLD,
                    claim_id=_derive_claim_id(
                        op_id_str, idx, session_id=session_id,
                    ),
                    ts_unix=ts,
                ))

    return tuple(claims)


# ---------------------------------------------------------------------------
# Capture API — records via Slice 1.3 capture_phase_decision
# ---------------------------------------------------------------------------


async def capture_claims(
    *,
    op_id: str,
    claims: Sequence[PropertyClaim],
    ctx: Any = None,
) -> int:
    """Persist claims via Slice 1.3's ``capture_phase_decision``.

    Each claim becomes one decision-runtime record under
    (op_id, "PLAN", "property_claim", ordinal). Records survive
    process restart + are replay-safe.

    When the master flag is OFF, this is a pure no-op (returns 0
    without recording). Operators can register/unregister claim
    capture independently of the rest of Phase 2 — the rest of the
    pipeline still works either way.

    Returns the number of claims successfully captured. NEVER
    raises — capture failures (disk fault, missing dependency)
    surface as a partial count + a debug log."""
    if not property_capture_enabled() or not claims:
        return 0

    captured = 0
    try:
        from backend.core.ouroboros.governance.determinism.phase_capture import (
            capture_phase_decision,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[verification.capture] phase_capture unavailable: %s — "
            "claims not persisted", exc,
        )
        return 0

    for claim in claims:
        if not isinstance(claim, PropertyClaim):
            continue
        try:
            claim_dict = claim.to_dict()

            async def _emit(
                _claim_dict: dict = claim_dict,
            ) -> Any:
                return _claim_dict

            await capture_phase_decision(
                op_id=op_id,
                phase="PLAN",
                kind="property_claim",
                ctx=ctx,
                compute=_emit,
                extra_inputs={
                    "claim_id": claim.claim_id,
                    "kind": claim.property.kind,
                    "severity": claim.severity,
                },
            )
            captured += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[verification.capture] failed to persist claim "
                "%s for op_id=%s: %s",
                claim.claim_id, op_id, exc,
            )
            continue

    return captured


# ---------------------------------------------------------------------------
# Reader API — fetches recorded claims from the per-session ledger
# ---------------------------------------------------------------------------


def _ledger_path_for_session(session_id: Optional[str] = None) -> Path:
    """Resolve the per-session decisions.jsonl path. Mirrors Slice
    1.2's storage layout. NEVER raises."""
    sid = (str(session_id).strip() if session_id else "")
    if not sid:
        sid = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"
    base = os.environ.get(
        "JARVIS_DETERMINISM_LEDGER_DIR",
        ".jarvis/determinism",
    ).strip()
    return Path(base) / sid / "decisions.jsonl"


def get_recorded_claims(
    *,
    op_id: str,
    session_id: Optional[str] = None,
) -> Tuple[PropertyClaim, ...]:
    """Read all recorded claims for ``op_id`` from the per-session
    decision ledger.

    Walks the JSONL directly (rather than going through
    DecisionRuntime.lookup) because we want all records under
    (op_id, "PLAN", "property_claim") regardless of ordinal.

    Returns claims in insertion order (matches synthesis order).
    NEVER raises — corrupt rows / missing files yield empty tuple
    + a debug log."""
    path = _ledger_path_for_session(session_id)
    if not path.exists():
        return tuple()

    safe_op = (str(op_id).strip() if op_id else "")
    if not safe_op:
        return tuple()

    claims: List[PropertyClaim] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                # Filter by op_id, phase=PLAN, kind=property_claim
                if record.get("op_id") != safe_op:
                    continue
                if record.get("phase") != "PLAN":
                    continue
                if record.get("kind") != "property_claim":
                    continue
                # The claim is in output_repr (canonical-serialized JSON)
                output_repr = record.get("output_repr", "")
                if not isinstance(output_repr, str):
                    continue
                try:
                    claim_dict = json.loads(output_repr)
                except json.JSONDecodeError:
                    continue
                claim = PropertyClaim.from_dict(claim_dict)
                if claim is not None:
                    claims.append(claim)
    except OSError as exc:
        logger.debug(
            "[verification.capture] could not read ledger %s: %s",
            path, exc,
        )
        return tuple()

    return tuple(claims)


def filter_load_bearing(
    claims: Sequence[PropertyClaim],
) -> Tuple[PropertyClaim, ...]:
    """Convenience: keep only must_hold claims. Used by Slice 2.4
    POSTMORTEM integration to decide which failures escalate.
    NEVER raises."""
    try:
        return tuple(c for c in claims if c.is_load_bearing)
    except Exception:  # noqa: BLE001 — defensive
        return tuple()


# ---------------------------------------------------------------------------
# Adapter for the decision-runtime registry (Slice 1.3 integration)
# ---------------------------------------------------------------------------


def _register_property_claim_adapter() -> None:
    """Register the (PLAN, property_claim) adapter at module load.

    The adapter serializes a claim dict (already JSON-friendly from
    PropertyClaim.to_dict) → identity passthrough. Deserialize
    converts the stored dict back to a PropertyClaim.

    Idempotent — safe to import multiple times. Defensive (NEVER
    raises) so a missing determinism module doesn't break this
    module's import chain."""
    try:
        from backend.core.ouroboros.governance.determinism.phase_capture import (
            OutputAdapter,
            register_adapter,
        )

        def _serialize(claim_dict: Any) -> Any:
            # Already JSON-friendly from PropertyClaim.to_dict
            try:
                if isinstance(claim_dict, dict):
                    return claim_dict
                # Defensive: if it's a PropertyClaim somehow,
                # convert. Shouldn't happen normally — capture_claims
                # converts before passing.
                if isinstance(claim_dict, PropertyClaim):
                    return claim_dict.to_dict()
                return {"_unparseable": str(claim_dict)[:200]}
            except Exception:  # noqa: BLE001 — defensive
                return {"_unparseable": "exception"}

        def _deserialize(stored: Any) -> Any:
            try:
                if isinstance(stored, dict):
                    claim = PropertyClaim.from_dict(stored)
                    if claim is not None:
                        return claim
                return stored
            except Exception:  # noqa: BLE001 — defensive
                return stored

        register_adapter(
            phase="PLAN",
            kind="property_claim",
            adapter=OutputAdapter(
                serialize=_serialize,
                deserialize=_deserialize,
                name="property_claim_adapter",
            ),
        )
    except Exception:  # noqa: BLE001 — defensive (import-time)
        # Determinism module unavailable — capture still works
        # (passthrough via identity adapter); operator just loses
        # the round-trip type fidelity.
        pass


_register_property_claim_adapter()


__all__ = [
    "CANONICAL_SEVERITIES",
    "PROPERTY_CLAIM_SCHEMA_VERSION",
    "PropertyClaim",
    "SEVERITY_IDEAL",
    "SEVERITY_MUST_HOLD",
    "SEVERITY_SHOULD_HOLD",
    "capture_claims",
    "filter_load_bearing",
    "get_recorded_claims",
    "property_capture_enabled",
    "synthesize_claims_from_plan",
]
