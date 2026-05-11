"""
Belief Revision Ledger
======================

Closes §40 Wave 4 #9 — the first Wave 4 (Tier 3 calibration learning)
arc. Per the operator binding:

  "Every PostmortemEngine assertion gets structured claim +
   falsifying-evidence hooks. When evidence contradicts, file
   revision op + SemanticGuardian weighs that domain lower.
   Bayesian calibration learning."

This substrate is a **pure-function calibration ledger** that
records explicit claims the system has stated (about a domain,
about a failure root cause, about the safety of a change) plus
the evidence accumulated about each claim. When falsifying
evidence crosses a threshold the substrate surfaces
``BeliefVerdict.FALSIFIED`` — operator-visible signal that a
prior belief needs revision.

The substrate is the load-bearing dependency for three
downstream Wave 4 items (§40.5):

* #11 Postmortem fusion — clusters falsified beliefs by
  domain into meta-postmortems.
* #10 Sleep consolidation pass — replays falsified beliefs
  against DreamEngine blueprints during idle windows.
* #13 Anti-fragility budget per-module — per-domain stress
  caps derived from the falsification rate.

Composition contract — thin pure-function ledger over canonical
substrates:

* :func:`cross_process_jsonl.flock_append_line` — §33.4 audit
  ledger at ``.jarvis/belief_revision_ledger.jsonl``. Best-
  effort writes, NEVER raise into the calibration path. Reads
  use plain stdlib line iteration (the file is append-only
  JSONL).
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave 2
  #5) — cage-touching belief domains route to FALSIFIED
  regardless of evidence count, so a wrong claim about the
  governance boundary cannot silently survive in STABLE.

NEVER raises. Ledger file missing / unreadable / corrupted /
claims with no evidence all degrade to ``STABLE`` or
``DISABLED`` verdict, not exception.

Closed 4-value :class:`BeliefVerdict` (top-level):

  STABLE       ✓ 0 falsifying evidence records
  DRIFTING     ⚠ 1+ falsifying records but below threshold
  FALSIFIED    🚨 falsifying ≥ ``falsify_threshold()`` OR
                  domain is cage-touching
  DISABLED     ◌ master flag off OR substrate unavailable

Closed 4-value :class:`EvidenceKind`:

  AFFIRMING    +1 toward STABLE
  FALSIFYING   +1 toward FALSIFIED
  NEUTRAL       no calibration effect (observed, not weighed)
  UNKNOWN       unrecognized kind (e.g., corrupted ledger row)

§33.1 cognitive substrate ``JARVIS_BELIEF_REVISION_ENABLED``
default-**FALSE** — operator-paced opt-in. With master off
every read-path returns ``DISABLED`` and every write-path is a
no-op (zero ledger growth, zero cost).

Authority asymmetry (AST-pinned): imports stdlib +
``cross_process_jsonl`` + ``governance_boundary_gate`` ONLY.
Does NOT import orchestrator / iron_gate / policy / providers
/ candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor. The
substrate is a read/write ledger; consumer-side wiring
(PostmortemEngine claim hook, SemanticGuardian domain-weight
adjustment) ships as separate slices.
"""
from __future__ import annotations

import ast
import enum
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


BELIEF_REVISION_SCHEMA_VERSION: str = "belief_revision.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_BELIEF_REVISION_ENABLED"
_ENV_FALSIFY_THRESHOLD = "JARVIS_BELIEF_REVISION_FALSIFY_THRESHOLD"
_ENV_MAX_RECORDS = "JARVIS_BELIEF_REVISION_MAX_RECORDS"
_ENV_PERSIST = "JARVIS_BELIEF_REVISION_PERSIST_ENABLED"
_ENV_LEDGER_PATH = "JARVIS_BELIEF_REVISION_LEDGER_PATH"

_DEFAULT_FALSIFY_THRESHOLD = 2
_DEFAULT_MAX_RECORDS = 200
_MIN_FALSIFY_THRESHOLD = 1
_MAX_FALSIFY_THRESHOLD = 1_000
_MIN_MAX_RECORDS = 1
_MAX_MAX_RECORDS = 100_000

_DEFAULT_LEDGER_REL = ".jarvis/belief_revision_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Substrate returns ``DISABLED`` verdict
    and refuses ledger writes when off. Flip
    ``JARVIS_BELIEF_REVISION_ENABLED=true`` to begin
    accumulating claims + evidence.
    """
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gates the §33.4 JSONL audit writes. Defaults
    to True (so flipping the master on enables both eval +
    persistence with a single env knob). Operator may keep
    persistence off if running an ephemeral eval-only mode."""
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def falsify_threshold() -> int:
    """Number of FALSIFYING evidence records required to
    transition from DRIFTING to FALSIFIED. Defaults to 2 (one
    falsifying record is DRIFTING; two are FALSIFIED). Clamped
    to [1, 1000]."""
    return _read_clamped_int(
        _ENV_FALSIFY_THRESHOLD,
        _DEFAULT_FALSIFY_THRESHOLD,
        _MIN_FALSIFY_THRESHOLD,
        _MAX_FALSIFY_THRESHOLD,
    )


def max_records() -> int:
    """Maximum claims / evidence rows read per evaluation pass.
    Bounds memory under pathologically-large ledger files.
    Clamped to [1, 100_000]."""
    return _read_clamped_int(
        _ENV_MAX_RECORDS,
        _DEFAULT_MAX_RECORDS,
        _MIN_MAX_RECORDS,
        _MAX_MAX_RECORDS,
    )


def ledger_path() -> Path:
    """Audit-ledger path. Defaults to
    ``.jarvis/belief_revision_ledger.jsonl`` relative to CWD;
    operator may override via ``JARVIS_BELIEF_REVISION_LEDGER_PATH``.
    """
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class BeliefVerdict(str, enum.Enum):
    """Closed 4-value top-level verdict — bytes-pinned via AST."""

    STABLE = "stable"
    DRIFTING = "drifting"
    FALSIFIED = "falsified"
    DISABLED = "disabled"


class EvidenceKind(str, enum.Enum):
    """Closed 4-value evidence taxonomy — bytes-pinned via AST."""

    AFFIRMING = "affirming"
    FALSIFYING = "falsifying"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


_VERDICT_GLYPH: Dict[str, str] = {
    BeliefVerdict.STABLE.value: "✓",
    BeliefVerdict.DRIFTING.value: "⚠",
    BeliefVerdict.FALSIFIED.value: "🚨",
    BeliefVerdict.DISABLED.value: "◌",
}


def verdict_glyph(verdict: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _coerce_evidence_kind(raw: Any) -> EvidenceKind:
    """Best-effort coercion. NEVER raises. Unknown → UNKNOWN."""
    if isinstance(raw, EvidenceKind):
        return raw
    try:
        s = str(getattr(raw, "value", raw) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return EvidenceKind.UNKNOWN
    for k in EvidenceKind:
        if k.value == s:
            return k
    return EvidenceKind.UNKNOWN


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class BeliefClaim:
    """A structured belief the system has asserted.

    Frozen §33.5 artifact. The ``claim_id`` is deterministic
    (sha256[:16] of normalized text + domain + claimed_at_unix)
    so a replayed claim with identical content lands the same
    id — enables idempotent re-recording across sessions.
    """

    claim_id: str
    text: str
    domain: str
    confidence: float
    target_files: Tuple[str, ...]
    claimed_at_iso: str
    claimed_at_unix: float
    schema_version: str = BELIEF_REVISION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "claim",
            "claim_id": self.claim_id,
            "text": self.text[:512],
            "domain": self.domain[:128],
            "confidence": float(self.confidence),
            "target_files": list(self.target_files),
            "claimed_at_iso": self.claimed_at_iso,
            "claimed_at_unix": float(self.claimed_at_unix),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class EvidenceRecord:
    """One evidence row attached to a claim."""

    claim_id: str
    kind: EvidenceKind
    source_op_id: str
    source_session_id: str
    observed_at_iso: str
    observed_at_unix: float
    note: str = ""
    schema_version: str = BELIEF_REVISION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "evidence",
            "claim_id": self.claim_id,
            "evidence_kind": self.kind.value,
            "source_op_id": self.source_op_id[:128],
            "source_session_id": self.source_session_id[:128],
            "observed_at_iso": self.observed_at_iso,
            "observed_at_unix": float(self.observed_at_unix),
            "note": self.note[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class BeliefRevisionReport:
    """Aggregate evaluation — frozen §33.5 artifact."""

    evaluated_at_unix: float
    master_enabled: bool
    claim: Optional[BeliefClaim]
    affirming_count: int
    falsifying_count: int
    neutral_count: int
    unknown_count: int
    verdict: BeliefVerdict
    boundary_crossed: bool
    diagnostic: str
    elapsed_s: float
    evidence_records: Tuple[EvidenceRecord, ...] = field(default_factory=tuple)
    schema_version: str = BELIEF_REVISION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "claim": self.claim.to_dict() if self.claim else None,
            "affirming_count": int(self.affirming_count),
            "falsifying_count": int(self.falsifying_count),
            "neutral_count": int(self.neutral_count),
            "unknown_count": int(self.unknown_count),
            "verdict": self.verdict.value,
            "boundary_crossed": bool(self.boundary_crossed),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "evidence_records": [
                e.to_dict() for e in self.evidence_records
            ],
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces
# ===========================================================================


def _is_boundary_crossed(target_files: Sequence[str]) -> bool:
    """Compose canonical Wave 2 #5 boundary gate. NEVER raises."""
    if not target_files:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(target_files))
    except Exception:  # noqa: BLE001
        return False


def _normalize_target_files(
    raw_target_files: Optional[Sequence[Any]],
) -> Tuple[str, ...]:
    """Coerce mixed-type / mixed-case path inputs into a
    canonical tuple of forward-slash repo-relative strings.
    Composes ``governance_boundary_gate._normalize_path`` when
    available. NEVER raises."""
    if not raw_target_files:
        return ()
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            _normalize_path,
        )
    except Exception:  # noqa: BLE001
        out: List[str] = []
        for raw in raw_target_files:
            try:
                s = str(raw or "").replace("\\", "/").strip()
                if s:
                    out.append(s)
            except Exception:  # noqa: BLE001
                continue
        return tuple(out)
    out2: List[str] = []
    for raw in raw_target_files:
        try:
            s = _normalize_path(raw)
            if s:
                out2.append(s)
        except Exception:  # noqa: BLE001
            continue
    return tuple(out2)


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 JSONL write via canonical
    ``cross_process_jsonl.flock_append_line``. Returns True on
    successful write, False on any failure. NEVER raises."""
    if not master_enabled():
        return False
    if not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_ledger_rows(
    *,
    max_total: Optional[int] = None,
    path_override: Optional[Path] = None,
) -> Tuple[Dict[str, Any], ...]:
    """Plain stdlib read-back of the append-only JSONL ledger.
    Returns parsed rows in append order (oldest first). Lines
    that fail to parse are silently skipped (corruption-
    tolerant). NEVER raises."""
    cap = max_records() if max_total is None else int(max_total)
    target = path_override or ledger_path()
    rows: List[Dict[str, Any]] = []
    try:
        if not target.exists():
            return ()
        with target.open("r", encoding="utf-8") as fp:
            for raw in fp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                rows.append(obj)
                if len(rows) >= cap:
                    break
    except Exception:  # noqa: BLE001
        return tuple(rows)
    return tuple(rows)


# ===========================================================================
# Producer-bridge — record_claim / record_evidence
# ===========================================================================


def _now_iso(now_unix: float) -> str:
    try:
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(now_unix).isoformat(
            timespec="seconds",
        )
    except Exception:  # noqa: BLE001
        return ""


def _claim_id_for(
    text: str, domain: str, claimed_at_unix: float,
) -> str:
    """Deterministic claim id — sha256[:16] over normalized
    text + domain + timestamp. Same inputs → same id."""
    payload = f"{text.strip()[:512]}|{domain.strip()[:128]}|{claimed_at_unix:.6f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def record_claim(
    text: str,
    domain: str,
    *,
    target_files: Optional[Sequence[Any]] = None,
    confidence: float = 0.5,
    now_unix: Optional[float] = None,
) -> Optional[BeliefClaim]:
    """Producer-bridge — record an explicit claim. NEVER raises.

    Returns the constructed :class:`BeliefClaim` even when
    master is off OR persistence fails (caller can route the
    claim downstream without depending on ledger durability).
    When master is off this is a no-op for persistence purposes
    but still returns a frozen artifact so the caller's audit
    trail is not silently lost.
    """
    try:
        text_safe = str(text or "")[:512]
        domain_safe = str(domain or "")[:128]
        confidence_clamped = max(0.0, min(1.0, float(confidence)))
    except Exception:  # noqa: BLE001
        return None
    if not text_safe or not domain_safe:
        return None
    now = time.time() if now_unix is None else float(now_unix)
    normalized = _normalize_target_files(target_files)
    cid = _claim_id_for(text_safe, domain_safe, now)
    claim = BeliefClaim(
        claim_id=cid,
        text=text_safe,
        domain=domain_safe,
        confidence=confidence_clamped,
        target_files=normalized,
        claimed_at_iso=_now_iso(now),
        claimed_at_unix=now,
    )
    _flock_append(claim.to_dict())
    return claim


def record_evidence(
    claim_id: str,
    kind: Any,
    *,
    source_op_id: str = "",
    source_session_id: str = "",
    note: str = "",
    now_unix: Optional[float] = None,
) -> Optional[EvidenceRecord]:
    """Producer-bridge — record one evidence row against an
    existing claim. NEVER raises. Returns the frozen artifact
    even with master off (caller may still route it elsewhere).
    """
    try:
        cid = str(claim_id or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not cid:
        return None
    coerced = _coerce_evidence_kind(kind)
    now = time.time() if now_unix is None else float(now_unix)
    rec = EvidenceRecord(
        claim_id=cid,
        kind=coerced,
        source_op_id=str(source_op_id or "")[:128],
        source_session_id=str(source_session_id or "")[:128],
        observed_at_iso=_now_iso(now),
        observed_at_unix=now,
        note=str(note or "")[:256],
    )
    _flock_append(rec.to_dict())
    _publish_revision_event(rec)
    return rec


# ===========================================================================
# Pure evaluator
# ===========================================================================


def _project_row_to_claim(row: Mapping[str, Any]) -> Optional[BeliefClaim]:
    if row.get("kind") != "claim":
        return None
    try:
        target_files_raw = row.get("target_files") or ()
        files = tuple(str(x) for x in target_files_raw if x)
        return BeliefClaim(
            claim_id=str(row.get("claim_id", "")),
            text=str(row.get("text", ""))[:512],
            domain=str(row.get("domain", ""))[:128],
            confidence=float(row.get("confidence", 0.0) or 0.0),
            target_files=files,
            claimed_at_iso=str(row.get("claimed_at_iso", "")),
            claimed_at_unix=float(
                row.get("claimed_at_unix", 0.0) or 0.0,
            ),
            schema_version=str(
                row.get("schema_version", BELIEF_REVISION_SCHEMA_VERSION),
            ),
        )
    except Exception:  # noqa: BLE001
        return None


def _project_row_to_evidence(row: Mapping[str, Any]) -> Optional[EvidenceRecord]:
    if row.get("kind") != "evidence":
        return None
    try:
        return EvidenceRecord(
            claim_id=str(row.get("claim_id", "")),
            kind=_coerce_evidence_kind(row.get("evidence_kind", "")),
            source_op_id=str(row.get("source_op_id", ""))[:128],
            source_session_id=str(
                row.get("source_session_id", ""),
            )[:128],
            observed_at_iso=str(row.get("observed_at_iso", "")),
            observed_at_unix=float(
                row.get("observed_at_unix", 0.0) or 0.0,
            ),
            note=str(row.get("note", ""))[:256],
            schema_version=str(
                row.get(
                    "schema_version",
                    BELIEF_REVISION_SCHEMA_VERSION,
                ),
            ),
        )
    except Exception:  # noqa: BLE001
        return None


def _index_claims(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, BeliefClaim]:
    out: Dict[str, BeliefClaim] = {}
    for r in rows:
        claim = _project_row_to_claim(r)
        if claim and claim.claim_id:
            out[claim.claim_id] = claim
    return out


def _group_evidence(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, List[EvidenceRecord]]:
    out: Dict[str, List[EvidenceRecord]] = {}
    for r in rows:
        ev = _project_row_to_evidence(r)
        if ev is None or not ev.claim_id:
            continue
        out.setdefault(ev.claim_id, []).append(ev)
    return out


def _build_report(
    claim: Optional[BeliefClaim],
    evidence: Sequence[EvidenceRecord],
    *,
    started_unix: float,
) -> BeliefRevisionReport:
    affirming = sum(1 for e in evidence if e.kind is EvidenceKind.AFFIRMING)
    falsifying = sum(
        1 for e in evidence if e.kind is EvidenceKind.FALSIFYING
    )
    neutral = sum(1 for e in evidence if e.kind is EvidenceKind.NEUTRAL)
    unknown = sum(1 for e in evidence if e.kind is EvidenceKind.UNKNOWN)
    boundary = False
    if claim is not None:
        boundary = _is_boundary_crossed(claim.target_files)
    threshold = falsify_threshold()
    if boundary:
        verdict = BeliefVerdict.FALSIFIED
        diagnostic = (
            f"cage-touching domain ({len(claim.target_files) if claim else 0} "
            f"file(s)) routes to FALSIFIED regardless of evidence "
            f"count; {falsifying} falsifying / {affirming} affirming"
        )
    elif falsifying >= threshold:
        verdict = BeliefVerdict.FALSIFIED
        diagnostic = (
            f"falsifying evidence ({falsifying}) ≥ threshold "
            f"({threshold}); revision warranted"
        )
    elif falsifying > 0:
        verdict = BeliefVerdict.DRIFTING
        diagnostic = (
            f"falsifying evidence ({falsifying}) below threshold "
            f"({threshold}); claim is drifting"
        )
    else:
        verdict = BeliefVerdict.STABLE
        diagnostic = (
            f"stable: {affirming} affirming, 0 falsifying"
        )
    return BeliefRevisionReport(
        evaluated_at_unix=started_unix,
        master_enabled=True,
        claim=claim,
        affirming_count=affirming,
        falsifying_count=falsifying,
        neutral_count=neutral,
        unknown_count=unknown,
        verdict=verdict,
        boundary_crossed=boundary,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started_unix),
        evidence_records=tuple(evidence),
    )


def _disabled_report(started_unix: float) -> BeliefRevisionReport:
    return BeliefRevisionReport(
        evaluated_at_unix=started_unix,
        master_enabled=False,
        claim=None,
        affirming_count=0,
        falsifying_count=0,
        neutral_count=0,
        unknown_count=0,
        verdict=BeliefVerdict.DISABLED,
        boundary_crossed=False,
        diagnostic=(
            f"gate disabled via {_ENV_MASTER}=false — "
            "operator opt-in workflow"
        ),
        elapsed_s=0.0,
        evidence_records=(),
    )


def evaluate_claim(
    claim_id: str,
    *,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
) -> BeliefRevisionReport:
    """Pure evaluator for a single claim. NEVER raises.

    Parameters
    ----------
    claim_id:
        The claim id to evaluate.
    rows:
        Caller-injectable ledger rows (testing seam). Defaults
        to :func:`_load_ledger_rows` over the canonical
        ``.jarvis/belief_revision_ledger.jsonl`` path.
    """
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return _disabled_report(started)
    cid = str(claim_id or "").strip()
    if not cid:
        return BeliefRevisionReport(
            evaluated_at_unix=started,
            master_enabled=True,
            claim=None,
            affirming_count=0,
            falsifying_count=0,
            neutral_count=0,
            unknown_count=0,
            verdict=BeliefVerdict.STABLE,
            boundary_crossed=False,
            diagnostic="empty claim_id — nothing to evaluate",
            elapsed_s=max(0.0, time.time() - started),
            evidence_records=(),
        )
    all_rows = rows if rows is not None else _load_ledger_rows()
    claims = _index_claims(all_rows)
    evidence_by = _group_evidence(all_rows)
    claim = claims.get(cid)
    evidence = tuple(evidence_by.get(cid, ()))
    return _build_report(claim, evidence, started_unix=started)


def evaluate_recent_beliefs(
    *,
    max_records_override: Optional[int] = None,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
) -> Tuple[BeliefRevisionReport, ...]:
    """Pure evaluator — emit one report per recently-recorded
    claim. NEVER raises. Returns an empty tuple when master is
    off or the ledger is empty."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return ()
    all_rows = rows if rows is not None else _load_ledger_rows(
        max_total=max_records_override,
    )
    claims = _index_claims(all_rows)
    evidence_by = _group_evidence(all_rows)
    reports: List[BeliefRevisionReport] = []
    for cid, claim in claims.items():
        ev = tuple(evidence_by.get(cid, ()))
        reports.append(
            _build_report(claim, ev, started_unix=started),
        )
    return tuple(reports)


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_revision_event(record: EvidenceRecord) -> None:
    """Best-effort SSE publish on evidence record. NEVER raises."""
    if not master_enabled():
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_BELIEF_REVISION_RECORDED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_BELIEF_REVISION_RECORDED,
            (
                f"system::belief_revision::"
                f"{record.claim_id[:16]}"
            ),
            record.to_dict(),
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_belief_panel(
    report: Optional[BeliefRevisionReport] = None,
    *,
    claim_id: Optional[str] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"belief revision: disabled "
                f"({_ENV_MASTER}=false)"
            )
        if not claim_id:
            return "belief revision: no claim_id specified"
        report = evaluate_claim(claim_id)
    if not report.master_enabled:
        return (
            f"belief revision: disabled "
            f"({_ENV_MASTER}=false)"
        )
    glyph = verdict_glyph(report.verdict)
    claim = report.claim
    lines = [
        f"🧮 Belief Revision  {glyph} {report.verdict.value}",
    ]
    if claim is not None:
        lines.extend([
            f"  claim_id            : {claim.claim_id}",
            f"  domain              : {claim.domain}",
            f"  text                : {claim.text[:80]}",
            f"  confidence          : {claim.confidence:.2f}",
            f"  target_files        : {len(claim.target_files)}",
        ])
    else:
        lines.append("  claim               : (not found in ledger)")
    lines.extend([
        f"  affirming_count     : {report.affirming_count}",
        f"  falsifying_count    : {report.falsifying_count}",
        f"  neutral_count       : {report.neutral_count}",
        f"  unknown_count       : {report.unknown_count}",
        f"  boundary_crossed    : {report.boundary_crossed}",
        f"  diagnostic          : {report.diagnostic}",
    ])
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "belief_revision_ledger.py"
    )

    _EXPECTED_VERDICTS = {
        "stable", "drifting", "falsified", "disabled",
    }
    _EXPECTED_EVIDENCE = {
        "affirming", "falsifying", "neutral", "unknown",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "BeliefVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"BeliefVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"BeliefVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("BeliefVerdict class not found",)

    def _validate_evidence_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "EvidenceKind"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_EVIDENCE - found
                extra = found - _EXPECTED_EVIDENCE
                if missing:
                    return (
                        f"EvidenceKind missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"EvidenceKind drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("EvidenceKind class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose canonical cross_process_jsonl "
                "(no parallel JSONL writer)",
            )
        if "flock_append_line" not in source:
            violations.append(
                "must use flock_append_line for §33.4 writes",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 governance_boundary_gate "
                "(no parallel cage detection)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "belief_revision_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "BeliefVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "belief_revision_evidence_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "EvidenceKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_evidence_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="belief_revision_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — pure ledger. MUST NOT "
                "import orchestrator / iron_gate / policy / "
                "providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "belief_revision_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="belief_revision_composes_canonical",
            target_file=target,
            description=(
                "Substrate composes cross_process_jsonl "
                "(flock_append_line for §33.4 writes) + "
                "Wave 2 #5 governance_boundary_gate (no "
                "parallel JSONL / cage detection)."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "belief_revision_ledger.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Belief revision ledger master switch. §33.1 "
                "cognitive substrate default-FALSE. When on, "
                "the substrate accepts record_claim + "
                "record_evidence producer-bridge calls and "
                "writes them through §33.4 flock'd JSONL at "
                ".jarvis/belief_revision_ledger.jsonl. "
                "Evaluation surfaces 4-value verdict "
                "(STABLE / DRIFTING / FALSIFIED / DISABLED). "
                "Closes §40 Wave 4 #9 (PRD v2.99+); "
                "load-bearing dependency for #11 / #10 / #13."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_FALSIFY_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_FALSIFY_THRESHOLD,
            description=(
                "Number of FALSIFYING evidence records "
                "required to transition from DRIFTING to "
                "FALSIFIED. Defaults to 2; clamped to "
                "[1, 1_000]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FALSIFY_THRESHOLD}=3",
        ),
        FlagSpec(
            name=_ENV_MAX_RECORDS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_RECORDS,
            description=(
                "Maximum ledger rows read per evaluation. "
                "Bounds memory under pathologically-large "
                "ledger files. Clamped to [1, 100_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_RECORDS}=500",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate the §33.4 JSONL audit "
                "writes. Default True when master on. "
                "Operator may set False for ephemeral "
                "eval-only mode (no ledger growth)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "BELIEF_REVISION_SCHEMA_VERSION",
    "BeliefVerdict",
    "EvidenceKind",
    "BeliefClaim",
    "EvidenceRecord",
    "BeliefRevisionReport",
    "master_enabled",
    "persistence_enabled",
    "falsify_threshold",
    "max_records",
    "ledger_path",
    "verdict_glyph",
    "record_claim",
    "record_evidence",
    "evaluate_claim",
    "evaluate_recent_beliefs",
    "format_belief_panel",
    "register_shipped_invariants",
    "register_flags",
]
