"""RR Pass C Slice 1 — AdaptationLedger substrate.

The universal append-only audit log every other Pass C slice writes
to. Per `memory/project_reverse_russian_doll_pass_c.md` §5:

  > `backend/core/ouroboros/governance/adaptation/ledger.py` — the
  > universal substrate every other Pass C slice writes to.

Slice 1 ships the substrate only:

  * :class:`AdaptationSurface` — 5-value enum keying which adaptive
    surface a proposal targets (one per Pass C §3 thesis bullet).
  * :class:`OperatorDecisionStatus` — 3-value lifecycle enum
    (pending / approved / rejected).
  * :class:`MonotonicTighteningVerdict` — 2-value enum (passed /
    rejected:would_loosen) — the universal cage rule's outcome per
    proposal.
  * :class:`AdaptationEvidence` — bounded JSON shape capturing the
    window-summary that justified the proposal (event count, source
    IDs, summary string).
  * :class:`AdaptationProposal` — frozen dataclass; one ledger row.
  * :class:`AdaptationLedger` — thread-safe append-only writer +
    bounded reader + state-replay accessor.

The ledger is **substrate**: Slice 1 adds zero adaptive behavior.
Slices 2-5 author proposals via :meth:`AdaptationLedger.propose`;
Slice 6 (MetaAdaptationGovernor) coordinates them + provides the
operator REPL.

## Monotonic-tightening invariant (load-bearing)

Per §4.1: **Adaptive gates may only become more strict, never less.**
:meth:`AdaptationLedger.propose` validates this BEFORE the proposal
is persisted — a violating proposal is rejected with verdict
``rejected:would_loosen`` AND not written to disk. Slices 2-5
construct their proposed-state hashes such that the invariant
validator can compare against the current-state hash and decline
any direction-of-change that loosens.

The validator's per-surface logic is intentionally pluggable
(:class:`MonotonicTighteningValidator` Protocol) so each Slice 2-5
contributes its own surface-specific rule. Slice 1 ships a default
validator that requires structurally-distinct hash pairs and a
proposal_kind in the strict-direction allowlist; Slices 2-5 register
surface-specific validators that examine the actual state semantics.

## Append-only audit (load-bearing)

The ledger file (`.jarvis/adaptation_ledger.jsonl`) is **never
rewritten**. State transitions write NEW lines. Latest record per
proposal_id wins for current state. Per-record sha256 integrity hash
captures tamper attempts on read.

## Authority invariants (Pass C §4 + §5.2)

  * Pure data + read-only file I/O (the queue file is append-only).
    No subprocess, no env mutation, no network.
  * No imports of orchestrator / policy / iron_gate /
    risk_tier_floor / change_engine / candidate_generator / gate /
    semantic_guardian / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib only. Even the 5 adaptive surfaces (Slices 2-5)
    do not import this module circularly — they import the
    substrate; the substrate imports nothing of theirs.
  * Best-effort throughout — every operation returns a structured
    status; never raises into the caller.

## Default-off

Behind `JARVIS_ADAPTATION_LEDGER_ENABLED` (default `false`). When
off, every method returns the appropriate DISABLED status.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any, Callable, Dict, List, Mapping, Optional, Tuple,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Schema stamped into every AdaptationProposal; bump on field changes.
ADAPTATION_SCHEMA_VERSION: str = "2.0"
# Older rows from pre-Item-#2 (without `proposed_state_payload`) are
# still readable — from_dict() defaults the missing field to None.
ADAPTATION_SCHEMA_VERSIONS_READABLE: Tuple[str, ...] = ("1.0", "2.0")

# Soft caps (substrate is bounded).
MAX_PENDING_PROPOSALS: int = 256
MAX_HISTORY_LINES: int = 8_192
MAX_EVIDENCE_SUMMARY_CHARS: int = 1_024
MAX_OPERATOR_NAME_CHARS: int = 128
MAX_SOURCE_EVENT_IDS_PER_PROPOSAL: int = 64

# Hard cap on the rendered proposal_id length.
MAX_PROPOSAL_ID_CHARS: int = 128


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_epoch() -> float:
    return time.time()


def _hash_record(payload: Dict[str, Any]) -> str:
    """Stable sha256 of the proposal payload (sans the hash field
    itself). Sort keys so the hash is deterministic across Python
    versions."""
    sanitized = {k: v for k, v in payload.items() if k != "record_sha256"}
    blob = json.dumps(sanitized, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ADAPTATION_LEDGER_ENABLED`` (default
    ``true`` — graduated in Move 1 Pass C cadence 2026-04-29 after
    soak ``bt-2026-04-29-212606`` proved zero crash / regression /
    cost-contract violation under all 7 Pass C flags simultaneously).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_ADAPTATION_LEDGER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 1 Pass C cadence)
    return raw in _TRUTHY


def ledger_path() -> Path:
    """Return the ledger file path. Env-overridable via
    ``JARVIS_ADAPTATION_LEDGER_PATH``; defaults to
    ``.jarvis/adaptation_ledger.jsonl`` under the cwd."""
    raw = os.environ.get("JARVIS_ADAPTATION_LEDGER_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adaptation_ledger.jsonl"


# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------


class AdaptationSurface(str, enum.Enum):
    """The 5 adaptive surfaces, one per Pass C §3 thesis bullet."""

    SEMANTIC_GUARDIAN_PATTERNS = "semantic_guardian.patterns"
    """Slice 2 — POSTMORTEM-mined detector patterns."""

    IRON_GATE_EXPLORATION_FLOORS = "iron_gate.exploration_floors"
    """Slice 3 — auto-tightening exploration-category floors."""

    SCOPED_TOOL_BACKEND_MUTATION_BUDGET = "scoped_tool_backend.mutation_budget"
    """Slice 4 — per-Order mutation budget calibration."""

    RISK_TIER_FLOOR_TIERS = "risk_tier_floor.tiers"
    """Slice 4 — risk-tier ladder extension on novel attack surfaces."""

    EXPLORATION_LEDGER_CATEGORY_WEIGHTS = "exploration_ledger.category_weights"
    """Slice 5 — category-weight rebalance under mass conservation."""


class OperatorDecisionStatus(str, enum.Enum):
    """Lifecycle state of one proposal."""

    PENDING = "pending"
    """Initial state — written by Slices 2-5 mining surfaces;
    waiting for operator amend/reject via Slice 6 REPL."""

    APPROVED = "approved"
    """Operator approved + the adaptation is now live (the
    `applied_at` field is set non-null at this transition)."""

    REJECTED = "rejected"
    """Operator rejected. Terminal — does NOT delete or hide; the
    rejection itself is part of the audit trail."""


class MonotonicTighteningVerdict(str, enum.Enum):
    """Outcome of the universal cage rule check (Pass C §4.1)."""

    PASSED = "passed"
    """Proposal strictly tightens the gate. Eligible for operator
    review."""

    REJECTED_WOULD_LOOSEN = "rejected:would_loosen"
    """Proposal would loosen the gate. Pass C cannot loosen via any
    path — the proposal is not persisted; loosening goes through
    Pass B's `/order2 amend` REPL."""


class ProposeStatus(str, enum.Enum):
    """Outcome of one `AdaptationLedger.propose()` call."""
    OK = "OK"
    DISABLED = "DISABLED"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    DUPLICATE_PROPOSAL_ID = "DUPLICATE_PROPOSAL_ID"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    WOULD_LOOSEN = "WOULD_LOOSEN"
    PERSIST_ERROR = "PERSIST_ERROR"


class DecisionStatus(str, enum.Enum):
    """Outcome of one `AdaptationLedger.approve` / `reject` call."""
    OK = "OK"
    DISABLED = "DISABLED"
    NOT_FOUND = "NOT_FOUND"
    NOT_PENDING = "NOT_PENDING"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    PERSIST_ERROR = "PERSIST_ERROR"


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptationEvidence:
    """Bounded window-summary justifying a proposal.

    Captures the analytical evidence the mining surface (Slice 2-5)
    used to construct the proposal. Operator inspects this via the
    Slice 6 REPL `show <proposal_id>` subcommand."""

    window_days: int
    observation_count: int
    source_event_ids: Tuple[str, ...] = field(default_factory=tuple)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_days": int(self.window_days),
            "observation_count": int(self.observation_count),
            "source_event_ids": list(self.source_event_ids),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdaptationEvidence":
        ids_raw = data.get("source_event_ids") or []
        return cls(
            window_days=int(data.get("window_days") or 0),
            observation_count=int(data.get("observation_count") or 0),
            source_event_ids=tuple(
                str(x) for x in (ids_raw if isinstance(ids_raw, list) else [])
            ),
            summary=str(data.get("summary") or ""),
        )


@dataclass(frozen=True)
class AdaptationProposal:
    """One ledger row. Frozen so audit-trail integrity holds even if
    the same instance flows through multiple consumers."""

    schema_version: str
    proposal_id: str
    surface: AdaptationSurface
    proposal_kind: str
    evidence: AdaptationEvidence
    current_state_hash: str
    proposed_state_hash: str
    monotonic_tightening_verdict: MonotonicTighteningVerdict
    proposed_at: str
    proposed_at_epoch: float
    operator_decision: OperatorDecisionStatus = OperatorDecisionStatus.PENDING
    operator_decision_at: Optional[str] = None
    operator_decision_by: Optional[str] = None
    applied_at: Optional[str] = None
    record_sha256: str = ""
    # Item #2 schema extension (2026-04-26): per-surface JSON-
    # serializable payload that carries the FULL proposed state
    # (not just the hash). The hash is for tamper-detection;
    # the payload is for materialization. Backward-compat: existing
    # rows without this field load with `None` (older miners just
    # didn't populate it). YAML writer (`adaptation/yaml_writer.py`)
    # consumes this on `/adapt approve` to materialize the state
    # into `.jarvis/adapted_<surface>.yaml`. Bumped
    # ADAPTATION_SCHEMA_VERSION 1.0 → 2.0; older "1.0" rows still
    # readable.
    proposed_state_payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "surface": self.surface.value,
            "proposal_kind": self.proposal_kind,
            "evidence": self.evidence.to_dict(),
            "current_state_hash": self.current_state_hash,
            "proposed_state_hash": self.proposed_state_hash,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict.value
            ),
            "proposed_at": self.proposed_at,
            "proposed_at_epoch": self.proposed_at_epoch,
            "operator_decision": self.operator_decision.value,
            "operator_decision_at": self.operator_decision_at,
            "operator_decision_by": self.operator_decision_by,
            "applied_at": self.applied_at,
            "rollback_via": "pass_b_manifest_amendment",
        }
        # Item #2: serialize the payload only when populated. Keeps
        # pre-extension rows byte-identical for back-compat tests.
        if self.proposed_state_payload is not None:
            out["proposed_state_payload"] = self.proposed_state_payload
        out["record_sha256"] = self.record_sha256 or _hash_record(out)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdaptationProposal":
        try:
            surface = AdaptationSurface(str(data.get("surface") or ""))
        except ValueError:
            # Future Slice may add new surface values; load as a
            # placeholder so the row stays in history but we don't
            # crash the reader.
            surface = AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS
        try:
            verdict = MonotonicTighteningVerdict(
                str(data.get("monotonic_tightening_verdict") or "")
            )
        except ValueError:
            verdict = MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
        try:
            decision = OperatorDecisionStatus(
                str(data.get("operator_decision") or "pending")
            )
        except ValueError:
            decision = OperatorDecisionStatus.PENDING
        evidence_raw = data.get("evidence") or {}
        if not isinstance(evidence_raw, Mapping):
            evidence_raw = {}
        return cls(
            schema_version=str(data.get("schema_version") or ""),
            proposal_id=str(data.get("proposal_id") or ""),
            surface=surface,
            proposal_kind=str(data.get("proposal_kind") or ""),
            evidence=AdaptationEvidence.from_dict(evidence_raw),
            current_state_hash=str(data.get("current_state_hash") or ""),
            proposed_state_hash=str(data.get("proposed_state_hash") or ""),
            monotonic_tightening_verdict=verdict,
            proposed_at=str(data.get("proposed_at") or ""),
            proposed_at_epoch=float(data.get("proposed_at_epoch") or 0.0),
            operator_decision=decision,
            operator_decision_at=(
                str(data["operator_decision_at"])
                if data.get("operator_decision_at") else None
            ),
            operator_decision_by=(
                str(data["operator_decision_by"])
                if data.get("operator_decision_by") else None
            ),
            applied_at=(
                str(data["applied_at"])
                if data.get("applied_at") else None
            ),
            record_sha256=str(data.get("record_sha256") or ""),
            proposed_state_payload=(
                dict(data["proposed_state_payload"])
                if isinstance(
                    data.get("proposed_state_payload"), Mapping,
                )
                else None
            ),
        )

    def verify_integrity(self) -> bool:
        if not self.record_sha256:
            return False
        d = self.to_dict()
        return _hash_record(d) == self.record_sha256

    @property
    def is_terminal(self) -> bool:
        return self.operator_decision is not OperatorDecisionStatus.PENDING


@dataclass(frozen=True)
class ProposeResult:
    status: ProposeStatus
    proposal_id: str = ""
    detail: str = ""
    proposal: Optional[AdaptationProposal] = None


@dataclass(frozen=True)
class DecisionResult:
    status: DecisionStatus
    proposal_id: str = ""
    detail: str = ""
    proposal: Optional[AdaptationProposal] = None


# ---------------------------------------------------------------------------
# Monotonic-tightening invariant validator
# ---------------------------------------------------------------------------


# Pluggable per-surface validator. Each Slice 2-5 will register one
# at import time. The default validator (below) handles the universal
# checks; surface-specific semantics layer on top.
SurfaceValidator = Callable[[AdaptationProposal], Tuple[bool, str]]


# Surface-validator registry. Slice 1 ships an empty registry +
# default validator. Slices 2-5 call `register_surface_validator()`
# at module-import time.
_SURFACE_VALIDATORS: Dict[AdaptationSurface, SurfaceValidator] = {}
_VALIDATOR_LOCK = threading.Lock()


def register_surface_validator(
    surface: AdaptationSurface,
    validator: SurfaceValidator,
) -> None:
    """Register a per-surface validator. Last-write-wins.

    Slice 2-5 call this at module import. Validator returns
    ``(is_tightening, detail)`` where False means "would loosen,
    refuse to persist." Detail is a short rationale included in the
    proposal's monotonic_tightening_verdict if rejected.
    """
    with _VALIDATOR_LOCK:
        _SURFACE_VALIDATORS[surface] = validator


def get_surface_validator(
    surface: AdaptationSurface,
) -> Optional[SurfaceValidator]:
    """Return the registered validator for ``surface`` or None.
    Used by tests + the substrate's internal default-validator
    fallback."""
    with _VALIDATOR_LOCK:
        return _SURFACE_VALIDATORS.get(surface)


def reset_surface_validators() -> None:
    """Test-only: clear the surface-validator registry."""
    with _VALIDATOR_LOCK:
        _SURFACE_VALIDATORS.clear()


# Allowlist of proposal_kinds that strictly tighten by construction.
# Default validator falls back to this when no surface-specific
# validator is registered.
_TIGHTEN_KINDS: Tuple[str, ...] = (
    "add_pattern",      # SemanticGuardian — additive detector
    "raise_floor",      # IronGate exploration floors — only raise
    "lower_budget",     # Mutation budget — only lower
    "add_tier",         # Risk-tier ladder — only insert
    "rebalance_weight",  # Category weights — surface validator MUST verify mass-conservation
    "sunset_candidate",  # Phase 7.9 — advisory signal that an adapted pattern hasn't matched in N days; structurally conservative (operator must still file Pass B amendment to actually remove)
)


def _default_validate(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Universal default: structurally-distinct hashes + kind in the
    tighten-allowlist. Surface-specific validators (registered by
    Slices 2-5) layer on top."""
    if proposal.current_state_hash == proposal.proposed_state_hash:
        return (False, "no_state_change")
    if proposal.proposal_kind not in _TIGHTEN_KINDS:
        return (False, f"kind_not_in_tighten_allowlist:{proposal.proposal_kind}")
    return (True, "passed_default_check")


def validate_monotonic_tightening(
    proposal: AdaptationProposal,
) -> Tuple[MonotonicTighteningVerdict, str]:
    """Run the universal + surface-specific validation. Returns
    ``(verdict, detail)``. Public so tests can call it directly.

    Order:
      1. Default check (hash distinct + kind in allowlist).
      2. Surface-specific check (if a validator is registered).
    """
    ok, detail = _default_validate(proposal)
    if not ok:
        return MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN, detail
    surface_validator = get_surface_validator(proposal.surface)
    if surface_validator is not None:
        try:
            ok2, detail2 = surface_validator(proposal)
        except Exception as exc:  # noqa: BLE001 — defensive
            return (
                MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN,
                f"surface_validator_raised:{type(exc).__name__}:{exc}",
            )
        if not ok2:
            return MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN, detail2
        return MonotonicTighteningVerdict.PASSED, detail2
    return MonotonicTighteningVerdict.PASSED, detail


# ---------------------------------------------------------------------------
# Append-only ledger
# ---------------------------------------------------------------------------


class AdaptationLedger:
    """Append-only JSONL-backed ledger with thread-safe accessors.

    Mirror of Pass B's Order2ReviewQueue contract — every public
    method is best-effort + returns a structured status.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path if path is not None else ledger_path()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Read-side
    # ------------------------------------------------------------------

    def _read_all(self) -> List[AdaptationProposal]:
        if not self._path.exists():
            return []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[AdaptationLedger] read failed: %s", exc,
            )
            return []
        out: List[AdaptationProposal] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[AdaptationLedger] %s:%d malformed json: %s",
                    self._path, line_no, exc,
                )
                continue
            if not isinstance(obj, dict):
                continue
            try:
                proposal = AdaptationProposal.from_dict(obj)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "[AdaptationLedger] %s:%d parse failed: %s",
                    self._path, line_no, exc,
                )
                continue
            if proposal.record_sha256 and not proposal.verify_integrity():
                logger.warning(
                    "[AdaptationLedger] %s:%d sha256 mismatch "
                    "(proposal_id=%s) — tampered record skipped",
                    self._path, line_no, proposal.proposal_id,
                )
                continue
            out.append(proposal)
        return out

    def _latest_per_proposal(self) -> Dict[str, AdaptationProposal]:
        """Reduce append-only log to ``{proposal_id: latest_record}``.
        Latest = highest proposed_at_epoch (ties broken by file
        order)."""
        latest: Dict[str, AdaptationProposal] = {}
        for p in self._read_all():
            existing = latest.get(p.proposal_id)
            if existing is None or p.proposed_at_epoch >= existing.proposed_at_epoch:
                latest[p.proposal_id] = p
        return latest

    def get(self, proposal_id: str) -> Optional[AdaptationProposal]:
        if not is_enabled():
            return None
        with self._lock:
            return self._latest_per_proposal().get(proposal_id)

    def list_pending(self) -> Tuple[AdaptationProposal, ...]:
        if not is_enabled():
            return ()
        with self._lock:
            return tuple(
                p for p in self._latest_per_proposal().values()
                if p.operator_decision is OperatorDecisionStatus.PENDING
            )

    def history(
        self,
        surface: Optional[AdaptationSurface] = None,
        limit: int = 100,
    ) -> Tuple[AdaptationProposal, ...]:
        """Return up to `limit` most-recent proposals (any state),
        newest-first. Optionally filter by surface."""
        if not is_enabled():
            return ()
        if limit <= 0:
            return ()
        with self._lock:
            entries = self._read_all()
        if surface is not None:
            entries = [p for p in entries if p.surface is surface]
        entries.sort(key=lambda p: p.proposed_at_epoch, reverse=True)
        return tuple(entries[:limit])

    # ------------------------------------------------------------------
    # Write-side
    # ------------------------------------------------------------------

    def _append(self, proposal: AdaptationProposal) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[AdaptationLedger] mkdir failed for %s: %s",
                self._path.parent, exc,
            )
            return False
        try:
            line = json.dumps(proposal.to_dict(), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[AdaptationLedger] serialization failed "
                "(proposal_id=%s): %s", proposal.proposal_id, exc,
            )
            return False
        try:
            with self._path.open("a", encoding="utf-8") as f:
                # Phase 7.8 — cross-process advisory lock. Best-effort:
                # no-op fallback when fcntl unavailable (Windows) or
                # JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED=false. The
                # exclusive lock serializes append paths across
                # processes (within-process serialization remains
                # threading.RLock at the call site).
                from backend.core.ouroboros.governance.adaptation._file_lock import (  # noqa: E501
                    flock_exclusive,
                )
                with flock_exclusive(f.fileno()):
                    f.write(line)
                    f.write("\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
        except OSError as exc:
            logger.warning(
                "[AdaptationLedger] append failed (proposal_id=%s): %s",
                proposal.proposal_id, exc,
            )
            return False
        return True

    def propose(
        self,
        *,
        proposal_id: str,
        surface: AdaptationSurface,
        proposal_kind: str,
        evidence: AdaptationEvidence,
        current_state_hash: str,
        proposed_state_hash: str,
        proposed_state_payload: Optional[Dict[str, Any]] = None,
    ) -> ProposeResult:
        """Author a new proposal. Validates monotonic-tightening
        BEFORE persistence. Loosening proposals are rejected with
        WOULD_LOOSEN and NOT written to disk (the universal cage
        rule per Pass C §4.1).

        ``proposed_state_payload`` (Item #2 — 2026-04-26) is an
        OPTIONAL JSON-serializable dict carrying the FULL proposed
        state — used by the YAML writer at /adapt approve time to
        materialize the state into the live gate's adapted YAML.
        Backward-compat: pre-Item-#2 callers omit this kwarg and
        get the same behavior as before (payload=None → writer
        skips with SKIPPED_NO_PAYLOAD).
        """
        if not is_enabled():
            return ProposeResult(
                status=ProposeStatus.DISABLED, proposal_id=proposal_id,
                detail="master_flag_off",
            )

        # Argument hygiene
        pid = (proposal_id or "").strip()[:MAX_PROPOSAL_ID_CHARS]
        if not pid:
            return ProposeResult(
                status=ProposeStatus.INVALID_PROPOSAL,
                detail="proposal_id_empty",
            )
        kind = (proposal_kind or "").strip()
        if not kind:
            return ProposeResult(
                status=ProposeStatus.INVALID_PROPOSAL, proposal_id=pid,
                detail="proposal_kind_empty",
            )
        if not isinstance(evidence, AdaptationEvidence):
            return ProposeResult(
                status=ProposeStatus.INVALID_PROPOSAL, proposal_id=pid,
                detail="evidence_not_AdaptationEvidence",
            )
        if not isinstance(surface, AdaptationSurface):
            return ProposeResult(
                status=ProposeStatus.INVALID_PROPOSAL, proposal_id=pid,
                detail=f"surface_not_AdaptationSurface:{type(surface).__name__}",
            )

        # Bound the evidence (defensive; surfaces should already cap)
        clipped_evidence = AdaptationEvidence(
            window_days=int(evidence.window_days),
            observation_count=int(evidence.observation_count),
            source_event_ids=tuple(
                evidence.source_event_ids
            )[:MAX_SOURCE_EVENT_IDS_PER_PROPOSAL],
            summary=evidence.summary[:MAX_EVIDENCE_SUMMARY_CHARS],
        )

        with self._lock:
            latest = self._latest_per_proposal()
            existing = latest.get(pid)
            if existing is not None:
                return ProposeResult(
                    status=ProposeStatus.DUPLICATE_PROPOSAL_ID,
                    proposal_id=pid,
                    detail=f"already_exists:status={existing.operator_decision.value}",
                    proposal=existing,
                )
            pending_count = sum(
                1 for p in latest.values()
                if p.operator_decision is OperatorDecisionStatus.PENDING
            )
            if pending_count >= MAX_PENDING_PROPOSALS:
                return ProposeResult(
                    status=ProposeStatus.CAPACITY_EXCEEDED,
                    proposal_id=pid,
                    detail=(
                        f"pending_count={pending_count} >= "
                        f"MAX_PENDING_PROPOSALS={MAX_PENDING_PROPOSALS}"
                    ),
                )

            # Construct the proposal so the validator can examine it.
            now_iso = _utc_now_iso()
            now_epoch = _utc_now_epoch()
            # Item #2: validate payload shape (must be Mapping if
            # supplied — defends against caller-supplied garbage).
            normalized_payload: Optional[Dict[str, Any]] = None
            if proposed_state_payload is not None:
                if not isinstance(proposed_state_payload, Mapping):
                    return ProposeResult(
                        status=ProposeStatus.INVALID_PROPOSAL,
                        proposal_id=pid,
                        detail=(
                            "proposed_state_payload_must_be_mapping:"
                            f"{type(proposed_state_payload).__name__}"
                        ),
                    )
                normalized_payload = dict(proposed_state_payload)
            base = AdaptationProposal(
                schema_version=ADAPTATION_SCHEMA_VERSION,
                proposal_id=pid,
                surface=surface,
                proposal_kind=kind,
                evidence=clipped_evidence,
                current_state_hash=str(current_state_hash or ""),
                proposed_state_hash=str(proposed_state_hash or ""),
                # Provisional verdict; computed next.
                monotonic_tightening_verdict=(
                    MonotonicTighteningVerdict.PASSED
                ),
                proposed_at=now_iso,
                proposed_at_epoch=now_epoch,
                proposed_state_payload=normalized_payload,
            )
            verdict, detail = validate_monotonic_tightening(base)
            if verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN:
                logger.info(
                    "[AdaptationLedger] proposal_id=%s REJECTED_WOULD_LOOSEN "
                    "surface=%s kind=%s detail=%s",
                    pid, surface.value, kind, detail,
                )
                # NOT persisted. Cage rule: loosening goes through
                # Pass B manifest amendment, not through us.
                return ProposeResult(
                    status=ProposeStatus.WOULD_LOOSEN, proposal_id=pid,
                    detail=detail,
                )

            # Re-stamp with the validated verdict + final hash.
            stamped = AdaptationProposal(
                schema_version=base.schema_version,
                proposal_id=base.proposal_id,
                surface=base.surface,
                proposal_kind=base.proposal_kind,
                evidence=base.evidence,
                current_state_hash=base.current_state_hash,
                proposed_state_hash=base.proposed_state_hash,
                monotonic_tightening_verdict=verdict,
                proposed_at=base.proposed_at,
                proposed_at_epoch=base.proposed_at_epoch,
                proposed_state_payload=base.proposed_state_payload,
            )
            payload = stamped.to_dict()
            final = AdaptationProposal(
                schema_version=stamped.schema_version,
                proposal_id=stamped.proposal_id,
                surface=stamped.surface,
                proposal_kind=stamped.proposal_kind,
                evidence=stamped.evidence,
                current_state_hash=stamped.current_state_hash,
                proposed_state_hash=stamped.proposed_state_hash,
                monotonic_tightening_verdict=(
                    stamped.monotonic_tightening_verdict
                ),
                proposed_at=stamped.proposed_at,
                proposed_at_epoch=stamped.proposed_at_epoch,
                operator_decision=stamped.operator_decision,
                operator_decision_at=stamped.operator_decision_at,
                operator_decision_by=stamped.operator_decision_by,
                applied_at=stamped.applied_at,
                record_sha256=payload["record_sha256"],
                proposed_state_payload=stamped.proposed_state_payload,
            )
            ok = self._append(final)
            if not ok:
                return ProposeResult(
                    status=ProposeStatus.PERSIST_ERROR, proposal_id=pid,
                    detail="append_failed",
                )
            logger.info(
                "[AdaptationLedger] proposal_id=%s PROPOSED surface=%s kind=%s "
                "evidence.observations=%d",
                pid, surface.value, kind,
                clipped_evidence.observation_count,
            )
            return ProposeResult(
                status=ProposeStatus.OK, proposal_id=pid, proposal=final,
            )

    def approve(
        self,
        proposal_id: str,
        *,
        operator: str,
    ) -> DecisionResult:
        """Operator approves — append APPROVED transition + set
        `applied_at` non-null. The structural marker that an
        adaptation is now live."""
        return self._record_decision(
            proposal_id, operator=operator,
            target_status=OperatorDecisionStatus.APPROVED,
        )

    def reject(
        self,
        proposal_id: str,
        *,
        operator: str,
    ) -> DecisionResult:
        """Operator rejects — terminal; not deleted, just superseded
        in latest-state."""
        return self._record_decision(
            proposal_id, operator=operator,
            target_status=OperatorDecisionStatus.REJECTED,
        )

    def _record_decision(
        self,
        proposal_id: str,
        *,
        operator: str,
        target_status: OperatorDecisionStatus,
    ) -> DecisionResult:
        if not is_enabled():
            return DecisionResult(
                status=DecisionStatus.DISABLED, proposal_id=proposal_id,
                detail="master_flag_off",
            )
        pid = (proposal_id or "").strip()
        op_clean = (operator or "").strip()[:MAX_OPERATOR_NAME_CHARS]
        if not op_clean:
            return DecisionResult(
                status=DecisionStatus.OPERATOR_REQUIRED,
                proposal_id=pid, detail="operator_name_empty",
            )
        with self._lock:
            existing = self._latest_per_proposal().get(pid)
            if existing is None:
                return DecisionResult(
                    status=DecisionStatus.NOT_FOUND, proposal_id=pid,
                    detail="no_proposal",
                )
            if existing.operator_decision is not OperatorDecisionStatus.PENDING:
                return DecisionResult(
                    status=DecisionStatus.NOT_PENDING, proposal_id=pid,
                    detail=f"current_status={existing.operator_decision.value}",
                    proposal=existing,
                )
            now_iso = _utc_now_iso()
            applied_at = (
                now_iso if target_status is OperatorDecisionStatus.APPROVED
                else None
            )
            base = AdaptationProposal(
                schema_version=existing.schema_version,
                proposal_id=existing.proposal_id,
                surface=existing.surface,
                proposal_kind=existing.proposal_kind,
                evidence=existing.evidence,
                current_state_hash=existing.current_state_hash,
                proposed_state_hash=existing.proposed_state_hash,
                monotonic_tightening_verdict=(
                    existing.monotonic_tightening_verdict
                ),
                proposed_at=existing.proposed_at,
                # Use NEW epoch so latest-wins reduces to this
                # transition record (read-side picks the highest
                # epoch per proposal_id).
                proposed_at_epoch=_utc_now_epoch(),
                operator_decision=target_status,
                operator_decision_at=now_iso,
                operator_decision_by=op_clean,
                applied_at=applied_at,
                # Item #2: preserve payload across state transitions
                # so /adapt approve sees it.
                proposed_state_payload=existing.proposed_state_payload,
            )
            payload = base.to_dict()
            final = AdaptationProposal(
                schema_version=base.schema_version,
                proposal_id=base.proposal_id,
                surface=base.surface,
                proposal_kind=base.proposal_kind,
                evidence=base.evidence,
                current_state_hash=base.current_state_hash,
                proposed_state_hash=base.proposed_state_hash,
                monotonic_tightening_verdict=(
                    base.monotonic_tightening_verdict
                ),
                proposed_at=base.proposed_at,
                proposed_at_epoch=base.proposed_at_epoch,
                operator_decision=base.operator_decision,
                operator_decision_at=base.operator_decision_at,
                operator_decision_by=base.operator_decision_by,
                applied_at=base.applied_at,
                record_sha256=payload["record_sha256"],
                proposed_state_payload=base.proposed_state_payload,
            )
            if not self._append(final):
                return DecisionResult(
                    status=DecisionStatus.PERSIST_ERROR, proposal_id=pid,
                    detail="append_failed",
                )
            logger.info(
                "[AdaptationLedger] proposal_id=%s %s by=%s",
                pid, target_status.value.upper(), op_clean,
            )
            return DecisionResult(
                status=DecisionStatus.OK, proposal_id=pid, proposal=final,
            )


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_ledger: Optional[AdaptationLedger] = None
_default_lock = threading.Lock()


def get_default_ledger() -> AdaptationLedger:
    global _default_ledger
    with _default_lock:
        if _default_ledger is None:
            _default_ledger = AdaptationLedger()
    return _default_ledger


def reset_default_ledger() -> None:
    """Test-only: reset the cached default ledger."""
    global _default_ledger
    with _default_lock:
        _default_ledger = None


__all__ = [
    "ADAPTATION_SCHEMA_VERSION",
    "AdaptationEvidence",
    "AdaptationLedger",
    "AdaptationProposal",
    "AdaptationSurface",
    "DecisionResult",
    "DecisionStatus",
    "MAX_EVIDENCE_SUMMARY_CHARS",
    "MAX_HISTORY_LINES",
    "MAX_OPERATOR_NAME_CHARS",
    "MAX_PENDING_PROPOSALS",
    "MAX_PROPOSAL_ID_CHARS",
    "MAX_SOURCE_EVENT_IDS_PER_PROPOSAL",
    "MonotonicTighteningVerdict",
    "OperatorDecisionStatus",
    "ProposeResult",
    "ProposeStatus",
    "SurfaceValidator",
    "get_default_ledger",
    "get_surface_validator",
    "is_enabled",
    "ledger_path",
    "register_surface_validator",
    "reset_default_ledger",
    "reset_surface_validators",
    "validate_monotonic_tightening",
]
