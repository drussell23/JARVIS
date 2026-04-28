"""Phase 2 Slice 2.4 — Verification Postmortem (the loop-closing slice).

This is the slice that turns Phase 2 from "infrastructure that records
claims" into a CLOSED LOOP. Without this, captured claims sit in the
ledger as data but don't affect op outcomes — operators have to
manually grep the ledger to know if a claim failed. Slice 2.4 closes
the loop:

  PLAN time     → claims captured (Slice 2.3)
  APPLY time    → change committed (existing)
  VERIFY time   → tests run (existing)
  COMPLETE time → walk claims, evaluate against post-APPLY evidence,
                  produce structured VerificationPostmortem,
                  persist it as a permanent ledger lesson (THIS SLICE)

This is the third RSI gear from PRD §24.10 — Closed-Loop Memory.
Quoting Gemini's framing of I.J. Good's intelligence-explosion thesis:

  > "Suppose O+V merges a change to its own TopologySentinel to
  > make it faster. 72 hours later, the Post-Merge Auditor wakes up
  > and checks if the codebase is still stable. If it is, O+V logs
  > a permanent 'lesson' that this new routing logic is successful."

Slice 2.4 IS that lesson-logging machinery for the immediate (op-
close) horizon. Antigravity's parallel ``post_merge_auditor.py``
covers the 72-hour horizon. Together they bracket the consequence-
tracking surface.

LAYERING (no duplication):

  * Slice 1.1 — entropy: deterministic record IDs
  * Slice 1.2 — DecisionRuntime: per-session ledger (postmortem
    persisted here, kind="verification_postmortem")
  * Slice 1.3 — capture_phase_decision: phase-shaped wrapper
  * Slice 2.1 — PropertyOracle: single-claim dispatcher
  * Slice 2.2 — RepeatRunner: NOT used here (postmortem is one-pass
    per claim; statistical re-verification is the next-phase
    enhancement)
  * Slice 2.3 — get_recorded_claims: source of claim list

Slice 2.4 ships:
  * VerificationPostmortem schema (frozen + hashable)
  * ClaimOutcome (per-claim result)
  * produce_verification_postmortem (pure async producer)
  * persist_postmortem (Slice 1.2 ledger writer)
  * get_recorded_postmortem (Slice 1.2 ledger reader)
  * Default ``ctx_evidence_collector`` — pulls common signals from
    OperationContext so the postmortem is useful out-of-the-box
    without requiring operators to write evidence collectors

EVIDENCE COLLECTION STRATEGY:

Slice 2.4's challenge: at COMPLETE time, where does evidence come
from? Some claims need fresh test runs (out of scope — that's
Slice 2.2 RepeatRunner with a real collector). Some can be answered
from already-captured signals on the ctx (validation_passed,
target_files, etc.). Some require deeper observation (file content,
function signatures).

The DEFAULT evidence collector inspects the OperationContext for
common signal fields:

  * test_passes claims: ctx.validation_passed → exit_code (0 or 1)
  * key_present claims: defaults to "present"=True if VERIFY passed
  * string_matches claims: signature_invariant claims fall through
    to INSUFFICIENT_EVIDENCE (need fresh signature inspection —
    operators wire richer collectors as needed)

INSUFFICIENT_EVIDENCE verdicts are honest — they say "we recorded
this claim but couldn't verify it from available signals." That's
better than silent passes. Operators see the audit trail and know
which claims need richer collection.

OPERATOR'S DESIGN CONSTRAINTS APPLIED:

  * Asynchronous — producer is async; uses Slice 2.1 Oracle (sync)
    + Slice 2.3 reader (sync) under the hood
  * Dynamic — evidence_collector is a free-form callable; default
    is provided but operators can inject custom logic
  * Adaptive — INSUFFICIENT_EVIDENCE preserves audit value when
    evidence is missing; doesn't fake-pass to avoid noise
  * Intelligent — uses canonical evidence-hash via Antigravity's
    canonical_serialize (transitively via Slice 2.1 Oracle)
  * Robust — every public method NEVER raises. Defensive at every
    layer (Slice 1.2 reader, Slice 2.1 Oracle, Slice 2.3 claim
    reader, the producer itself).
  * No hardcoding — schema fields named explicitly, no enum
    constants for verdict outcome categories beyond Slice 2.1's
    VerdictKind.
  * Leverages existing — Slices 1.1/1.2/1.3/2.1/2.3 + Antigravity's
    canonical hashing. ZERO duplication.

AUTHORITY INVARIANTS (pinned by tests):
  * NEVER imports orchestrator / phase_runner (base) /
    candidate_generator
  * NEVER imports providers
  * Every public method NEVER raises
  * Postmortem persistence is best-effort — failure surfaces in
    the return value, not as exception
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, List, Mapping, Optional, Tuple,
)

from backend.core.ouroboros.governance.verification.property_capture import (
    PropertyClaim,
    SEVERITY_IDEAL,
    SEVERITY_MUST_HOLD,
    SEVERITY_SHOULD_HOLD,
    get_recorded_claims,
)
from backend.core.ouroboros.governance.verification.property_oracle import (
    PropertyOracle,
    PropertyVerdict,
    VerdictKind,
    get_default_oracle,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def postmortem_enabled() -> bool:
    """``JARVIS_VERIFICATION_POSTMORTEM_ENABLED`` (default ``false``).

    Phase 2 Slice 2.4 master flag. Re-read at call time so monkeypatch
    works in tests + operators can flip live without re-init. Default
    flips to ``true`` at Phase 2 Slice 2.5 graduation.

    When ``false``: producer returns an empty postmortem; persistence
    is a no-op. When ``true``: full closed-loop verification fires."""
    raw = os.environ.get(
        "JARVIS_VERIFICATION_POSTMORTEM_ENABLED", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


VERIFICATION_POSTMORTEM_SCHEMA_VERSION = "verification_postmortem.1"
CLAIM_OUTCOME_SCHEMA_VERSION = "claim_outcome.1"


@dataclass(frozen=True)
class ClaimOutcome:
    """One claim's verification outcome.

    Frozen + hashable for safe ledger persistence + cross-thread
    sharing. ``evidence_used_repr`` is a JSON string for replay-
    safety (the Mapping itself isn't hashable; the canonical repr
    is)."""
    claim: PropertyClaim
    verdict: PropertyVerdict
    evidence_used_repr: str = ""
    schema_version: str = CLAIM_OUTCOME_SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        return self.verdict.passed

    @property
    def is_terminal(self) -> bool:
        """True iff the verdict is PASSED or FAILED (not insufficient
        / error). Non-terminal outcomes don't count as evidence
        either way."""
        return self.verdict.is_terminal

    @property
    def is_blocking(self) -> bool:
        """True iff this outcome should block the op:
        must_hold severity AND verdict is FAILED. INSUFFICIENT /
        ERROR don't block — operator gets the diagnostic but the
        op still completes."""
        return (
            self.claim.severity == SEVERITY_MUST_HOLD
            and self.verdict.verdict is VerdictKind.FAILED
        )


@dataclass(frozen=True)
class VerificationPostmortem:
    """Structured postmortem record of all claim verifications for
    one op.

    Frozen + hashable. Persisted via Slice 1.2 decision-runtime
    ledger as kind="verification_postmortem", one record per op.

    Field semantics:
      * ``op_id`` — operation identifier
      * ``session_id`` — session identifier
      * ``total_claims`` — count of claims read from the ledger
      * ``must_hold/should_hold/ideal_count`` — by-severity counts
      * ``must_hold/should_hold/ideal_failed`` — failure counts
        (FAILED verdict only — INSUFFICIENT/ERROR not counted)
      * ``insufficient_count`` — claims that couldn't be verified
        from available evidence (operator should investigate)
      * ``error_count`` — claims where the evaluator raised
      * ``outcomes`` — per-claim outcomes for forensics
      * ``has_blocking_failures`` — True iff any must_hold FAILED
        (this is THE summary signal)
      * ``started_unix/completed_unix`` — wall-clock bounds for
        latency telemetry
    """
    op_id: str
    session_id: str
    total_claims: int = 0
    must_hold_count: int = 0
    should_hold_count: int = 0
    ideal_count: int = 0
    must_hold_failed: int = 0
    should_hold_failed: int = 0
    ideal_failed: int = 0
    insufficient_count: int = 0
    error_count: int = 0
    outcomes: Tuple[ClaimOutcome, ...] = field(default_factory=tuple)
    has_blocking_failures: bool = False
    started_unix: float = 0.0
    completed_unix: float = 0.0
    schema_version: str = VERIFICATION_POSTMORTEM_SCHEMA_VERSION

    @property
    def total_failed(self) -> int:
        """All FAILED verdicts (any severity)."""
        return (
            self.must_hold_failed
            + self.should_hold_failed
            + self.ideal_failed
        )

    @property
    def total_passed(self) -> int:
        """All PASSED verdicts (any severity)."""
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def is_clean(self) -> bool:
        """True iff zero failures across all severities + zero
        insufficient/error. The ideal outcome — every claim verified
        and passed."""
        return (
            self.total_failed == 0
            and self.insufficient_count == 0
            and self.error_count == 0
        )

    def to_dict(self) -> dict:
        """JSON-friendly serialization for the ledger."""
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "session_id": self.session_id,
            "total_claims": self.total_claims,
            "must_hold_count": self.must_hold_count,
            "should_hold_count": self.should_hold_count,
            "ideal_count": self.ideal_count,
            "must_hold_failed": self.must_hold_failed,
            "should_hold_failed": self.should_hold_failed,
            "ideal_failed": self.ideal_failed,
            "insufficient_count": self.insufficient_count,
            "error_count": self.error_count,
            "has_blocking_failures": self.has_blocking_failures,
            "started_unix": self.started_unix,
            "completed_unix": self.completed_unix,
            "outcomes": [
                _claim_outcome_to_dict(o) for o in self.outcomes
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Optional["VerificationPostmortem"]:
        """Parse from a ledger record. NEVER raises — returns None
        on unparseable input."""
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != VERIFICATION_POSTMORTEM_SCHEMA_VERSION:
                return None
            outcomes_raw = raw.get("outcomes") or []
            outcomes: List[ClaimOutcome] = []
            for o in outcomes_raw:
                co = _claim_outcome_from_dict(o)
                if co is not None:
                    outcomes.append(co)
            return cls(
                op_id=str(raw.get("op_id", "")),
                session_id=str(raw.get("session_id", "")),
                total_claims=int(raw.get("total_claims", 0) or 0),
                must_hold_count=int(
                    raw.get("must_hold_count", 0) or 0,
                ),
                should_hold_count=int(
                    raw.get("should_hold_count", 0) or 0,
                ),
                ideal_count=int(raw.get("ideal_count", 0) or 0),
                must_hold_failed=int(
                    raw.get("must_hold_failed", 0) or 0,
                ),
                should_hold_failed=int(
                    raw.get("should_hold_failed", 0) or 0,
                ),
                ideal_failed=int(raw.get("ideal_failed", 0) or 0),
                insufficient_count=int(
                    raw.get("insufficient_count", 0) or 0,
                ),
                error_count=int(raw.get("error_count", 0) or 0),
                outcomes=tuple(outcomes),
                has_blocking_failures=bool(
                    raw.get("has_blocking_failures", False),
                ),
                started_unix=float(
                    raw.get("started_unix", 0.0) or 0.0,
                ),
                completed_unix=float(
                    raw.get("completed_unix", 0.0) or 0.0,
                ),
            )
        except (TypeError, ValueError, KeyError):
            return None


# ---------------------------------------------------------------------------
# Internal serialization helpers
# ---------------------------------------------------------------------------


def _claim_outcome_to_dict(co: ClaimOutcome) -> dict:
    """Serialize a ClaimOutcome (NEVER raises)."""
    try:
        return {
            "schema_version": co.schema_version,
            "claim": co.claim.to_dict(),
            "verdict": {
                "property_name": co.verdict.property_name,
                "kind": co.verdict.kind,
                "verdict": (
                    co.verdict.verdict.value
                    if hasattr(co.verdict.verdict, "value")
                    else str(co.verdict.verdict)
                ),
                "confidence": co.verdict.confidence,
                "reason": co.verdict.reason,
                "evidence_hash": co.verdict.evidence_hash,
                "evaluation_ts_unix": co.verdict.evaluation_ts_unix,
            },
            "evidence_used_repr": co.evidence_used_repr,
        }
    except Exception:  # noqa: BLE001 — defensive
        return {"_unparseable": True}


def _claim_outcome_from_dict(raw: Any) -> Optional[ClaimOutcome]:
    """Parse a ClaimOutcome (NEVER raises)."""
    try:
        if not isinstance(raw, Mapping):
            return None
        if raw.get("schema_version") != CLAIM_OUTCOME_SCHEMA_VERSION:
            return None
        claim = PropertyClaim.from_dict(raw.get("claim") or {})
        if claim is None:
            return None
        v_raw = raw.get("verdict") or {}
        if not isinstance(v_raw, Mapping):
            return None
        verdict_str = str(v_raw.get("verdict", "evaluator_error"))
        try:
            verdict_kind = VerdictKind(verdict_str)
        except ValueError:
            verdict_kind = VerdictKind.EVALUATOR_ERROR
        verdict = PropertyVerdict(
            property_name=str(v_raw.get("property_name", "")),
            kind=str(v_raw.get("kind", "")),
            verdict=verdict_kind,
            confidence=float(v_raw.get("confidence", 0.0) or 0.0),
            reason=str(v_raw.get("reason", "")),
            evidence_hash=str(v_raw.get("evidence_hash", "")),
            evaluation_ts_unix=float(
                v_raw.get("evaluation_ts_unix", 0.0) or 0.0,
            ),
        )
        return ClaimOutcome(
            claim=claim,
            verdict=verdict,
            evidence_used_repr=str(raw.get("evidence_used_repr", "")),
        )
    except (TypeError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Default evidence collector
# ---------------------------------------------------------------------------


# An evidence collector takes a PropertyClaim + an optional
# OperationContext and returns the evidence Mapping for that
# claim. Returning an empty mapping is fine — Oracle will return
# INSUFFICIENT_EVIDENCE which is the honest answer.
EvidenceCollector = Callable[
    [PropertyClaim, Any], Awaitable[Mapping[str, Any]],
]


async def ctx_evidence_collector(
    claim: PropertyClaim, ctx: Any,
) -> Mapping[str, Any]:
    """Default evidence collector. Pulls common signals from
    OperationContext for the four canonical claim kinds.

    Returns empty mapping for unknown kinds — Oracle will return
    INSUFFICIENT_EVIDENCE. NEVER raises.

    The mapping per claim kind:

      * ``test_passes`` — ctx.validation_passed → exit_code (0 or 1)
        (assumes a passing op had its test_strategy.tests_to_pass
        actually exercised)
      * ``key_present`` — ctx.validation_passed AND op didn't fail
        → present=True (best-effort signal that no regression
        materialized)
      * ``string_matches`` — empty (signature inspection requires
        live AST parsing — operators wire richer collectors)
      * ``set_subset`` — empty (custom claim — operator-specific)
      * Unknown kind — empty
    """
    kind = claim.property.kind if claim and claim.property else ""

    try:
        if kind == "test_passes":
            # If validation_passed is True on ctx, we treat that as
            # exit_code=0 for any test_passes claim. This is a
            # best-effort signal — for true per-test verification,
            # operators wire a fresh pytest-spawn collector via
            # Slice 2.2 RepeatRunner.
            validated = bool(getattr(ctx, "validation_passed", False))
            return {"exit_code": 0 if validated else 1}

        if kind == "key_present":
            validated = bool(getattr(ctx, "validation_passed", False))
            return {"present": validated}

        # Other kinds: return empty → INSUFFICIENT_EVIDENCE
        return {}
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Producer — walk recorded claims, evaluate each, aggregate
# ---------------------------------------------------------------------------


async def produce_verification_postmortem(
    *,
    op_id: str,
    ctx: Any = None,
    evidence_collector: Optional[EvidenceCollector] = None,
    oracle: Optional[PropertyOracle] = None,
    session_id: Optional[str] = None,
) -> VerificationPostmortem:
    """Produce the verification postmortem for ``op_id``.

    Walks the recorded claims (Slice 2.3 reader), evaluates each
    via the Oracle (Slice 2.1) using evidence from the supplied
    collector (default: ``ctx_evidence_collector``), aggregates
    outcomes into a frozen ``VerificationPostmortem``.

    NEVER raises. When ``postmortem_enabled()`` returns False, the
    function returns an EMPTY postmortem — operators can call this
    safely from any production path; flag-off → no work, no signal.

    Empty claim list → empty postmortem (``has_blocking_failures
    == False``, ``is_clean == True``)."""
    started = _time.time()

    # Resolve session_id (mirrors Slice 2.3 reader's resolution)
    sid = session_id
    if sid is None or not str(sid).strip():
        sid = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"

    if not postmortem_enabled():
        return VerificationPostmortem(
            op_id=str(op_id), session_id=sid,
            started_unix=started, completed_unix=_time.time(),
        )

    if not op_id or not str(op_id).strip():
        return VerificationPostmortem(
            op_id="", session_id=sid,
            started_unix=started, completed_unix=_time.time(),
        )

    oracle = oracle or get_default_oracle()
    collector = evidence_collector or ctx_evidence_collector

    # Read recorded claims
    try:
        claims = get_recorded_claims(op_id=op_id, session_id=sid)
    except Exception:  # noqa: BLE001 — defensive
        claims = ()

    outcomes: List[ClaimOutcome] = []
    must_hold_count = should_hold_count = ideal_count = 0
    must_hold_failed = should_hold_failed = ideal_failed = 0
    insufficient_count = error_count = 0
    has_blocking = False

    for claim in claims:
        try:
            evidence = await collector(claim, ctx)
        except Exception:  # noqa: BLE001 — defensive
            evidence = {}
        if not isinstance(evidence, Mapping):
            evidence = {}

        try:
            verdict = oracle.evaluate(
                prop=claim.property, evidence=evidence,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            verdict = PropertyVerdict(
                property_name=claim.property.name,
                kind=claim.property.kind,
                verdict=VerdictKind.EVALUATOR_ERROR,
                confidence=0.0,
                reason=f"oracle.evaluate raised: {type(exc).__name__}: {exc}",
            )

        try:
            evidence_repr = json.dumps(
                dict(evidence), sort_keys=True, ensure_ascii=True,
            )
        except (TypeError, ValueError):
            evidence_repr = "{}"

        outcome = ClaimOutcome(
            claim=claim,
            verdict=verdict,
            evidence_used_repr=evidence_repr,
        )
        outcomes.append(outcome)

        # Tally by severity
        sev = claim.severity
        if sev == SEVERITY_MUST_HOLD:
            must_hold_count += 1
        elif sev == SEVERITY_SHOULD_HOLD:
            should_hold_count += 1
        elif sev == SEVERITY_IDEAL:
            ideal_count += 1

        # Tally by verdict
        if verdict.verdict is VerdictKind.FAILED:
            if sev == SEVERITY_MUST_HOLD:
                must_hold_failed += 1
                has_blocking = True
            elif sev == SEVERITY_SHOULD_HOLD:
                should_hold_failed += 1
            elif sev == SEVERITY_IDEAL:
                ideal_failed += 1
        elif verdict.verdict is VerdictKind.INSUFFICIENT_EVIDENCE:
            insufficient_count += 1
        elif verdict.verdict is VerdictKind.EVALUATOR_ERROR:
            error_count += 1

    return VerificationPostmortem(
        op_id=str(op_id),
        session_id=sid,
        total_claims=len(outcomes),
        must_hold_count=must_hold_count,
        should_hold_count=should_hold_count,
        ideal_count=ideal_count,
        must_hold_failed=must_hold_failed,
        should_hold_failed=should_hold_failed,
        ideal_failed=ideal_failed,
        insufficient_count=insufficient_count,
        error_count=error_count,
        outcomes=tuple(outcomes),
        has_blocking_failures=has_blocking,
        started_unix=started,
        completed_unix=_time.time(),
    )


# ---------------------------------------------------------------------------
# Persister — record postmortem via Slice 1.2 decision runtime
# ---------------------------------------------------------------------------


async def persist_postmortem(
    *,
    pm: VerificationPostmortem,
    op_id: Optional[str] = None,
    ctx: Any = None,
) -> bool:
    """Persist a verification postmortem to the per-session decision
    ledger as kind="verification_postmortem".

    Returns True on success, False on failure. NEVER raises.

    Uses Slice 1.3's ``capture_phase_decision`` so the record gets
    canonical hashing + atomic flock-protected append for free."""
    if not postmortem_enabled() or pm is None:
        return False

    try:
        from backend.core.ouroboros.governance.determinism.phase_capture import (
            capture_phase_decision,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[verification.postmortem] phase_capture unavailable: %s",
            exc,
        )
        return False

    target_op = op_id or pm.op_id
    if not target_op:
        return False

    try:
        pm_dict = pm.to_dict()

        async def _emit() -> Any:
            return pm_dict

        await capture_phase_decision(
            op_id=target_op,
            phase="COMPLETE",
            kind="verification_postmortem",
            ctx=ctx,
            compute=_emit,
            extra_inputs={
                "total_claims": pm.total_claims,
                "must_hold_failed": pm.must_hold_failed,
                "has_blocking_failures": pm.has_blocking_failures,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[verification.postmortem] persist failed for op_id=%s: %s",
            target_op, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Reader — fetch the recorded postmortem
# ---------------------------------------------------------------------------


def _ledger_path_for_session(session_id: Optional[str] = None) -> Path:
    """Resolve the per-session decisions.jsonl path. Mirrors Slice
    1.2 + 2.3 conventions. NEVER raises."""
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


def get_recorded_postmortem(
    *,
    op_id: str,
    session_id: Optional[str] = None,
) -> Optional[VerificationPostmortem]:
    """Read the most recent recorded verification postmortem for
    ``op_id``. Returns None if not found or unparseable.

    NEVER raises. Walks the per-session JSONL directly (mirrors
    Slice 2.3 reader pattern). When multiple postmortems exist for
    the same op (operator re-ran COMPLETE in some non-standard
    flow), returns the LAST one — most recent state wins."""
    if not op_id or not str(op_id).strip():
        return None
    path = _ledger_path_for_session(session_id)
    if not path.exists():
        return None
    safe_op = str(op_id).strip()
    last_pm: Optional[VerificationPostmortem] = None
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
                if record.get("op_id") != safe_op:
                    continue
                if record.get("phase") != "COMPLETE":
                    continue
                if record.get("kind") != "verification_postmortem":
                    continue
                output_repr = record.get("output_repr", "")
                if not isinstance(output_repr, str):
                    continue
                try:
                    pm_dict = json.loads(output_repr)
                except json.JSONDecodeError:
                    continue
                pm = VerificationPostmortem.from_dict(pm_dict)
                if pm is not None:
                    last_pm = pm
    except OSError as exc:
        logger.debug(
            "[verification.postmortem] read failed at %s: %s",
            path, exc,
        )
        return None
    return last_pm


# ---------------------------------------------------------------------------
# Adapter for the decision-runtime registry (Slice 1.3 integration)
# ---------------------------------------------------------------------------


def _register_postmortem_adapter() -> None:
    """Register the (COMPLETE, verification_postmortem) adapter at
    module load. Idempotent. Defensive (NEVER raises)."""
    try:
        from backend.core.ouroboros.governance.determinism.phase_capture import (
            OutputAdapter,
            register_adapter,
        )

        def _serialize(pm_dict: Any) -> Any:
            try:
                if isinstance(pm_dict, dict):
                    return pm_dict
                if isinstance(pm_dict, VerificationPostmortem):
                    return pm_dict.to_dict()
                return {"_unparseable": str(pm_dict)[:200]}
            except Exception:  # noqa: BLE001 — defensive
                return {"_unparseable": "exception"}

        def _deserialize(stored: Any) -> Any:
            try:
                if isinstance(stored, dict):
                    pm = VerificationPostmortem.from_dict(stored)
                    if pm is not None:
                        return pm
                return stored
            except Exception:  # noqa: BLE001 — defensive
                return stored

        register_adapter(
            phase="COMPLETE",
            kind="verification_postmortem",
            adapter=OutputAdapter(
                serialize=_serialize,
                deserialize=_deserialize,
                name="verification_postmortem_adapter",
            ),
        )
    except Exception:  # noqa: BLE001 — defensive (import-time)
        pass


_register_postmortem_adapter()


# ---------------------------------------------------------------------------
# Convenience: log a structured summary for operator visibility
# ---------------------------------------------------------------------------


def log_postmortem_summary(pm: VerificationPostmortem) -> None:
    """Emit a single structured INFO log line for operator visibility.

    Format: ``[VerifyPostmortem] op=X claims=N pass=P fail=F insuff=I
    err=E blocking=B``

    Only emits if claims > 0 (no noise on opes that produced no
    claims). NEVER raises."""
    try:
        if pm is None or pm.total_claims == 0:
            return
        logger.info(
            "[VerifyPostmortem] op=%s claims=%d pass=%d fail=%d "
            "insuff=%d err=%d must_hold_failed=%d blocking=%s",
            pm.op_id, pm.total_claims, pm.total_passed,
            pm.total_failed, pm.insufficient_count, pm.error_count,
            pm.must_hold_failed,
            "true" if pm.has_blocking_failures else "false",
        )
    except Exception:  # noqa: BLE001 — defensive
        pass


__all__ = [
    "CLAIM_OUTCOME_SCHEMA_VERSION",
    "ClaimOutcome",
    "EvidenceCollector",
    "VERIFICATION_POSTMORTEM_SCHEMA_VERSION",
    "VerificationPostmortem",
    "ctx_evidence_collector",
    "get_recorded_postmortem",
    "log_postmortem_summary",
    "persist_postmortem",
    "postmortem_enabled",
    "produce_verification_postmortem",
]
