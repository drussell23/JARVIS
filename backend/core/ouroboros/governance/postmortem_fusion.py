"""
Postmortem Fusion
=================

Closes §40 Wave 4 #11 — the second Wave 4 (Tier 3 calibration
learning) arc. Depends on Wave 4 #9 (belief revision ledger).
Per the operator binding:

  "After K postmortems mention same root cause, synthesize
   meta-postmortem proposing structural fix. Learning across
   failures at architectural scale."

This substrate is a **pure-function meta-postmortem synthesizer**.
For each recurring failure pattern (≥ K postmortems sharing the
same ``(failed_phase, root_cause_class)`` cluster signature) it
emits a frozen :class:`MetaPostmortem` artifact, optionally
durably records it as a falsifying belief via Wave 4 #9, and
surfaces an operator-visible :class:`FusionReport`.

The fusion synthesis is **deterministic** — same postmortem
corpus + same threshold → same meta-postmortems. No LLM call.
The architectural fix is *proposed* (representative_root_cause +
target_files_union + suggested_next_action) — actual structural
repair stays operator-paced.

Composition contract — thin pure-function fuser over canonical
substrates:

* :func:`postmortem_recall.gather_recent_postmortems` — walker
  over ``.ouroboros/sessions/<id>/debug.log`` POSTMORTEM rows.
* :func:`postmortem_clusterer.cluster_postmortems` — pure
  deterministic clustering by ``(failed_phase, normalized
  root_cause)``. Already battle-tested in Curiosity Engine v2.
* :func:`belief_revision_ledger.record_claim` (Wave 4 #9) —
  optional persistence of the meta-postmortem as a domain-
  scoped claim, so downstream calibration (Wave 4 #13
  anti-fragility budget) reads cluster-recurrence as falsifying
  evidence for "this domain is reliable".
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave 2
  #5) — cluster severity escalates when target_files_union
  touches the governance cage.

NEVER raises. Empty corpus / postmortem walker unavailable /
belief ledger ledger write failure all degrade to
``NO_PATTERN`` or ``DISABLED`` verdict, not exception.

Closed 4-value :class:`FusionVerdict` (top-level):

  NO_PATTERN     ✓ no cluster ≥ fuse threshold
  EMERGING       ⚠ ≥1 cluster in [emerge_threshold, fuse_threshold)
  FUSED          🚨 ≥1 cluster ≥ fuse_threshold — meta-postmortem
                    synthesized
  DISABLED       ◌ master flag off

Closed 4-value :class:`FusionSeverity` (per-meta artifact):

  LOW            cluster = fuse_threshold, no cage touch
  MEDIUM         cluster > fuse_threshold, no cage touch
  HIGH           cluster = fuse_threshold, cage-touching
  CRITICAL       cluster > fuse_threshold, cage-touching

§33.1 cognitive substrate ``JARVIS_POSTMORTEM_FUSION_ENABLED``
default-**FALSE** — operator-paced opt-in. Sub-flag
``JARVIS_POSTMORTEM_FUSION_RECORD_CLAIM_ENABLED`` gates the
belief-ledger persistence side-effect (default TRUE when
master on).

Authority asymmetry (AST-pinned): imports stdlib +
``postmortem_recall`` + ``postmortem_clusterer`` +
``belief_revision_ledger`` + ``governance_boundary_gate`` ONLY.
Does NOT import orchestrator / iron_gate / policy / providers
/ candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor. The
substrate is a read-only synthesizer (with one optional
write-path through Wave 4 #9 — gated by sub-flag).
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import time
from dataclasses import dataclass, field
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


POSTMORTEM_FUSION_SCHEMA_VERSION: str = "postmortem_fusion.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_POSTMORTEM_FUSION_ENABLED"
_ENV_FUSE_THRESHOLD = "JARVIS_POSTMORTEM_FUSION_FUSE_THRESHOLD"
_ENV_EMERGE_THRESHOLD = "JARVIS_POSTMORTEM_FUSION_EMERGE_THRESHOLD"
_ENV_MAX_META = "JARVIS_POSTMORTEM_FUSION_MAX_META"
_ENV_MAX_POSTMORTEMS = "JARVIS_POSTMORTEM_FUSION_MAX_POSTMORTEMS"
_ENV_RECORD_CLAIM = (
    "JARVIS_POSTMORTEM_FUSION_RECORD_CLAIM_ENABLED"
)

_DEFAULT_FUSE_THRESHOLD = 3
_DEFAULT_EMERGE_THRESHOLD = 2
_DEFAULT_MAX_META = 10
_DEFAULT_MAX_POSTMORTEMS = 200
_MIN_THRESHOLD = 1
_MAX_THRESHOLD = 1_000
_MIN_MAX = 1
_MAX_MAX = 100_000


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Substrate returns DISABLED verdict
    when off. Flip ``JARVIS_POSTMORTEM_FUSION_ENABLED=true`` to
    fuse recurring failure patterns into meta-postmortems.
    """
    return _flag(_ENV_MASTER, default=False)


def record_claim_enabled() -> bool:
    """Sub-flag — gate the optional belief-ledger persistence
    side-effect. Default TRUE when master on (composes Wave 4
    #9 so downstream Wave 4 #13 can read cluster-recurrence as
    domain-falsifying evidence). Operator may set False for
    eval-only mode (no ledger growth)."""
    return _flag(_ENV_RECORD_CLAIM, default=True)


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


def fuse_threshold() -> int:
    """Minimum cluster size to FUSE (synthesize meta-postmortem).
    Defaults to 3 (matches Curiosity Engine v2's
    DEFAULT_MIN_CLUSTER_SIZE). Clamped to [1, 1_000]."""
    return _read_clamped_int(
        _ENV_FUSE_THRESHOLD,
        _DEFAULT_FUSE_THRESHOLD,
        _MIN_THRESHOLD,
        _MAX_THRESHOLD,
    )


def emerge_threshold() -> int:
    """Minimum cluster size to surface as EMERGING (below
    fuse_threshold). Defaults to 2. Clamped so caller can never
    accidentally make emerge >= fuse."""
    raw = _read_clamped_int(
        _ENV_EMERGE_THRESHOLD,
        _DEFAULT_EMERGE_THRESHOLD,
        _MIN_THRESHOLD,
        _MAX_THRESHOLD,
    )
    fuse = fuse_threshold()
    # Clamp emerge_threshold so it's always strictly < fuse_threshold.
    return max(_MIN_THRESHOLD, min(raw, max(_MIN_THRESHOLD, fuse - 1)))


def max_meta_postmortems() -> int:
    """Cap on per-evaluation meta-postmortem count. Bounds
    downstream consumer cost. Clamped to [1, 100_000]."""
    return _read_clamped_int(
        _ENV_MAX_META,
        _DEFAULT_MAX_META,
        _MIN_MAX,
        _MAX_MAX,
    )


def max_postmortems_to_scan() -> int:
    """Cap on postmortem corpus read per evaluation. Clamped to
    [1, 100_000]."""
    return _read_clamped_int(
        _ENV_MAX_POSTMORTEMS,
        _DEFAULT_MAX_POSTMORTEMS,
        _MIN_MAX,
        _MAX_MAX,
    )


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class FusionVerdict(str, enum.Enum):
    """Closed 4-value top-level verdict — bytes-pinned via AST."""

    NO_PATTERN = "no_pattern"
    EMERGING = "emerging"
    FUSED = "fused"
    DISABLED = "disabled"


class FusionSeverity(str, enum.Enum):
    """Closed 4-value severity — bytes-pinned via AST."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_VERDICT_GLYPH: Dict[str, str] = {
    FusionVerdict.NO_PATTERN.value: "✓",
    FusionVerdict.EMERGING.value: "⚠",
    FusionVerdict.FUSED.value: "🚨",
    FusionVerdict.DISABLED.value: "◌",
}


_SEVERITY_GLYPH: Dict[str, str] = {
    FusionSeverity.LOW.value: "·",
    FusionSeverity.MEDIUM.value: "▴",
    FusionSeverity.HIGH.value: "▲",
    FusionSeverity.CRITICAL.value: "🔥",
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


def severity_glyph(severity: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(severity, "value"):
            return _SEVERITY_GLYPH.get(str(severity.value), "?")
        return _SEVERITY_GLYPH.get(
            str(severity or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class MetaPostmortem:
    """One synthesized meta-postmortem — frozen audit record."""

    cluster_signature_hash: str
    failed_phase: str
    root_cause_class: str
    representative_root_cause: str
    member_op_ids: Tuple[str, ...]
    member_count: int
    target_files_union: Tuple[str, ...]
    suggested_next_action: str
    severity: FusionSeverity
    boundary_crossed: bool
    oldest_unix: float
    newest_unix: float
    claim_id_emitted: str = ""  # filled when belief-ledger persistence fires
    schema_version: str = POSTMORTEM_FUSION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cluster_signature_hash": self.cluster_signature_hash,
            "failed_phase": self.failed_phase[:128],
            "root_cause_class": self.root_cause_class[:256],
            "representative_root_cause": (
                self.representative_root_cause[:512]
            ),
            "member_op_ids": list(self.member_op_ids),
            "member_count": int(self.member_count),
            "target_files_union": list(self.target_files_union),
            "suggested_next_action": (
                self.suggested_next_action[:256]
            ),
            "severity": self.severity.value,
            "boundary_crossed": bool(self.boundary_crossed),
            "oldest_unix": float(self.oldest_unix),
            "newest_unix": float(self.newest_unix),
            "claim_id_emitted": self.claim_id_emitted[:32],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class FusionReport:
    """Aggregate fusion report — frozen §33.5 artifact."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: FusionVerdict
    postmortems_scanned: int
    clusters_examined: int
    emerging_count: int
    fused_count: int
    meta_postmortems: Tuple[MetaPostmortem, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = POSTMORTEM_FUSION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "postmortems_scanned": int(self.postmortems_scanned),
            "clusters_examined": int(self.clusters_examined),
            "emerging_count": int(self.emerging_count),
            "fused_count": int(self.fused_count),
            "meta_postmortems": [
                m.to_dict() for m in self.meta_postmortems
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces
# ===========================================================================


def _load_postmortems(max_total: int) -> Tuple[Any, ...]:
    """Compose canonical postmortem_recall walker. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.postmortem_recall import (  # noqa: E501
            gather_recent_postmortems,
        )
        return tuple(gather_recent_postmortems(max_total=max_total))
    except Exception:  # noqa: BLE001
        return ()


def _cluster(
    records: Sequence[Any],
    *,
    min_cluster_size: int,
    max_clusters: int,
) -> Tuple[Any, ...]:
    """Compose canonical postmortem_clusterer. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.postmortem_clusterer import (  # noqa: E501
            cluster_postmortems,
        )
        out = cluster_postmortems(
            records,
            min_cluster_size=min_cluster_size,
            max_clusters=max_clusters,
        )
        return tuple(out)
    except Exception:  # noqa: BLE001
        return ()


def _is_boundary_crossed(files: Sequence[str]) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not files:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(files))
    except Exception:  # noqa: BLE001
        return False


def _record_belief_claim(
    meta: MetaPostmortem,
    *,
    now_unix: float,
) -> str:
    """Compose Wave 4 #9 belief_revision_ledger.record_claim.
    Returns the emitted claim_id (or empty string on no-op /
    failure). NEVER raises."""
    if not record_claim_enabled():
        return ""
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (  # noqa: E501
            record_claim,
        )
    except ImportError:
        return ""
    try:
        text = (
            f"recurring failure pattern in phase={meta.failed_phase}: "
            f"{meta.representative_root_cause[:240]}"
        )
        domain = (
            f"phase={meta.failed_phase}|class="
            f"{meta.root_cause_class[:120]}"
        )
        claim = record_claim(
            text=text,
            domain=domain,
            target_files=list(meta.target_files_union),
            confidence=min(1.0, meta.member_count / 10.0),
            now_unix=now_unix,
        )
        if claim is None:
            return ""
        return claim.claim_id
    except Exception:  # noqa: BLE001
        return ""


# ===========================================================================
# Pure synthesizer
# ===========================================================================


def _severity_for(
    member_count: int,
    boundary_crossed: bool,
    fuse_t: int,
) -> FusionSeverity:
    if boundary_crossed:
        return (
            FusionSeverity.CRITICAL
            if member_count > fuse_t
            else FusionSeverity.HIGH
        )
    return (
        FusionSeverity.MEDIUM
        if member_count > fuse_t
        else FusionSeverity.LOW
    )


def synthesize_meta_postmortem(
    cluster: Any,
    *,
    fuse_t: Optional[int] = None,
) -> Optional[MetaPostmortem]:
    """Pure-function projection of a ``ProposalCandidate`` (from
    postmortem_clusterer) into a frozen :class:`MetaPostmortem`.
    NEVER raises. Returns None for malformed cluster shapes.
    """
    if cluster is None:
        return None
    try:
        sig = getattr(cluster, "signature", None)
        if sig is None:
            return None
        sig_hash = (
            sig.signature_hash() if hasattr(sig, "signature_hash") else ""
        )
        failed_phase = str(getattr(sig, "failed_phase", "") or "")
        root_cause_class = str(
            getattr(sig, "root_cause_class", "") or "",
        )
        member_ops = tuple(
            str(x) for x in getattr(cluster, "member_op_ids", ()) or ()
        )
        member_count = int(getattr(cluster, "member_count", 0) or 0)
        target_files = tuple(
            str(x) for x in getattr(cluster, "target_files_union", ())
            or ()
        )
        next_action = str(
            getattr(cluster, "dominant_next_safe_action", "") or "",
        )
        rep_root = str(
            getattr(cluster, "representative_root_cause", "") or "",
        )
        oldest = float(getattr(cluster, "oldest_unix", 0.0) or 0.0)
        newest = float(getattr(cluster, "newest_unix", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return None
    threshold = fuse_t if fuse_t is not None else fuse_threshold()
    boundary = _is_boundary_crossed(target_files)
    severity = _severity_for(member_count, boundary, threshold)
    return MetaPostmortem(
        cluster_signature_hash=sig_hash,
        failed_phase=failed_phase,
        root_cause_class=root_cause_class,
        representative_root_cause=rep_root,
        member_op_ids=member_ops,
        member_count=member_count,
        target_files_union=target_files,
        suggested_next_action=next_action,
        severity=severity,
        boundary_crossed=boundary,
        oldest_unix=oldest,
        newest_unix=newest,
    )


def fuse_recent_postmortems(
    *,
    postmortems: Optional[Sequence[Any]] = None,
    now_unix: Optional[float] = None,
) -> FusionReport:
    """Top-level fuser. NEVER raises.

    Parameters
    ----------
    postmortems:
        Caller-injectable corpus (testing seam). Defaults to
        canonical ``postmortem_recall.gather_recent_postmortems``.
    now_unix:
        Reference time for ``evaluated_at_unix`` + per-claim
        timestamps.
    """
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return FusionReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=FusionVerdict.DISABLED,
            postmortems_scanned=0,
            clusters_examined=0,
            emerging_count=0,
            fused_count=0,
            meta_postmortems=(),
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false — "
                "operator opt-in workflow"
            ),
            elapsed_s=0.0,
        )

    fuse_t = fuse_threshold()
    emerge_t = emerge_threshold()
    meta_cap = max_meta_postmortems()
    corpus_cap = max_postmortems_to_scan()

    if postmortems is None:
        corpus = _load_postmortems(corpus_cap)
    else:
        corpus = tuple(postmortems)[:corpus_cap]

    # Cluster at emerge threshold so EMERGING (size in
    # [emerge_t, fuse_t)) and FUSED (size >= fuse_t) both fall
    # out of one pass. Cap returned clusters at meta_cap * 2 to
    # leave headroom for the FUSED filter.
    clusters = _cluster(
        corpus,
        min_cluster_size=emerge_t,
        max_clusters=max(meta_cap * 2, meta_cap),
    )

    fused: List[MetaPostmortem] = []
    emerging = 0
    for c in clusters:
        mc = int(getattr(c, "member_count", 0) or 0)
        if mc >= fuse_t:
            meta = synthesize_meta_postmortem(c, fuse_t=fuse_t)
            if meta is None:
                continue
            claim_id = _record_belief_claim(meta, now_unix=started)
            if claim_id:
                meta = MetaPostmortem(
                    cluster_signature_hash=meta.cluster_signature_hash,
                    failed_phase=meta.failed_phase,
                    root_cause_class=meta.root_cause_class,
                    representative_root_cause=(
                        meta.representative_root_cause
                    ),
                    member_op_ids=meta.member_op_ids,
                    member_count=meta.member_count,
                    target_files_union=meta.target_files_union,
                    suggested_next_action=meta.suggested_next_action,
                    severity=meta.severity,
                    boundary_crossed=meta.boundary_crossed,
                    oldest_unix=meta.oldest_unix,
                    newest_unix=meta.newest_unix,
                    claim_id_emitted=claim_id,
                )
            fused.append(meta)
            if len(fused) >= meta_cap:
                break
        else:
            emerging += 1

    if fused:
        verdict = FusionVerdict.FUSED
        diagnostic = (
            f"{len(fused)} meta-postmortem(s) synthesized from "
            f"{len(corpus)} postmortem corpus; {emerging} "
            f"emerging cluster(s) below threshold {fuse_t}"
        )
    elif emerging > 0:
        verdict = FusionVerdict.EMERGING
        diagnostic = (
            f"{emerging} emerging cluster(s) below fuse "
            f"threshold {fuse_t} (emerge≥{emerge_t}); none "
            "synthesized yet"
        )
    else:
        verdict = FusionVerdict.NO_PATTERN
        diagnostic = (
            f"no clusters at emerge threshold {emerge_t} across "
            f"{len(corpus)} postmortem corpus"
        )

    report = FusionReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        postmortems_scanned=len(corpus),
        clusters_examined=len(clusters),
        emerging_count=emerging,
        fused_count=len(fused),
        meta_postmortems=tuple(fused),
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _publish_fusion_event(report)
    return report


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_fusion_event(report: FusionReport) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.verdict not in (FusionVerdict.FUSED, FusionVerdict.EMERGING):
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_POSTMORTEM_FUSED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_POSTMORTEM_FUSED,
            (
                f"system::postmortem_fusion::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "fused_count": report.fused_count,
                "emerging_count": report.emerging_count,
                "postmortems_scanned": report.postmortems_scanned,
                "clusters_examined": report.clusters_examined,
                "evaluated_at_unix": report.evaluated_at_unix,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_fusion_panel(
    report: Optional[FusionReport] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"postmortem fusion: disabled "
                f"({_ENV_MASTER}=false)"
            )
        report = fuse_recent_postmortems()
    if not report.master_enabled:
        return (
            f"postmortem fusion: disabled "
            f"({_ENV_MASTER}=false)"
        )
    glyph = verdict_glyph(report.verdict)
    lines = [
        f"🧬 Postmortem Fusion  {glyph} {report.verdict.value}",
        f"  postmortems_scanned : {report.postmortems_scanned}",
        f"  clusters_examined   : {report.clusters_examined}",
        f"  emerging_count      : {report.emerging_count}",
        f"  fused_count         : {report.fused_count}",
    ]
    if report.meta_postmortems:
        lines.append("  meta-postmortems:")
        for m in report.meta_postmortems[:5]:
            sev = severity_glyph(m.severity)
            phase = m.failed_phase or "?"
            ops = m.member_count
            files = len(m.target_files_union)
            lines.append(
                f"    {sev} sig={m.cluster_signature_hash} "
                f"phase={phase} ops={ops} files={files}"
            )
        if len(report.meta_postmortems) > 5:
            lines.append(
                f"    ... (+{len(report.meta_postmortems) - 5} more)"
            )
    lines.append(f"  diagnostic          : {report.diagnostic}")
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
        "backend/core/ouroboros/governance/postmortem_fusion.py"
    )

    _EXPECTED_VERDICTS = {
        "no_pattern", "emerging", "fused", "disabled",
    }
    _EXPECTED_SEVERITY = {
        "low", "medium", "high", "critical",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "FusionVerdict"
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
                        f"FusionVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"FusionVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("FusionVerdict class not found",)

    def _validate_severity_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "FusionSeverity"
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
                missing = _EXPECTED_SEVERITY - found
                extra = found - _EXPECTED_SEVERITY
                if missing:
                    return (
                        f"FusionSeverity missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"FusionSeverity drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("FusionSeverity class not found",)

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
        if "postmortem_clusterer" not in source:
            violations.append(
                "must compose canonical postmortem_clusterer "
                "(no parallel cluster engine)",
            )
        if "cluster_postmortems" not in source:
            violations.append(
                "must use cluster_postmortems",
            )
        if "postmortem_recall" not in source:
            violations.append(
                "must compose canonical postmortem_recall "
                "(no parallel postmortem walker)",
            )
        if "belief_revision_ledger" not in source:
            violations.append(
                "must compose Wave 4 #9 belief_revision_ledger "
                "(meta-postmortem persistence)",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 governance_boundary_gate "
                "(severity escalation for cage-touching clusters)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_fusion_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "FusionVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_fusion_severity_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "FusionSeverity 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_severity_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_fusion_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure synthesizer. MUST "
                "NOT import orchestrator / iron_gate / policy "
                "/ providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_fusion_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "postmortem_fusion_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes postmortem_clusterer "
                "(cluster_postmortems) + postmortem_recall + "
                "Wave 4 #9 belief_revision_ledger + Wave 2 #5 "
                "governance_boundary_gate — no parallel "
                "clusterer / walker / cage detector."
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
        "backend/core/ouroboros/governance/postmortem_fusion.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Postmortem fusion master switch. §33.1 "
                "cognitive substrate default-FALSE. When on, "
                "the substrate clusters recent postmortems "
                "via canonical postmortem_clusterer and "
                "synthesizes meta-postmortems for clusters "
                "with member_count ≥ fuse_threshold. "
                "Composes Wave 4 #9 belief_revision_ledger "
                "for optional persistence. Closes §40 Wave 4 "
                "#11 (PRD v2.99+)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_FUSE_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_FUSE_THRESHOLD,
            description=(
                "Minimum cluster size to FUSE (synthesize "
                "meta-postmortem). Defaults to 3. Clamped to "
                "[1, 1_000]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FUSE_THRESHOLD}=5",
        ),
        FlagSpec(
            name=_ENV_EMERGE_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_EMERGE_THRESHOLD,
            description=(
                "Minimum cluster size to surface as EMERGING "
                "(below fuse_threshold). Defaults to 2. "
                "Auto-clamped < fuse_threshold."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_EMERGE_THRESHOLD}=2",
        ),
        FlagSpec(
            name=_ENV_MAX_META,
            type=FlagType.INT,
            default=_DEFAULT_MAX_META,
            description=(
                "Cap on meta-postmortems per evaluation. "
                "Clamped to [1, 100_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_META}=20",
        ),
        FlagSpec(
            name=_ENV_MAX_POSTMORTEMS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_POSTMORTEMS,
            description=(
                "Cap on postmortem corpus read per evaluation. "
                "Clamped to [1, 100_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_POSTMORTEMS}=500",
        ),
        FlagSpec(
            name=_ENV_RECORD_CLAIM,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate the optional belief-ledger "
                "persistence side-effect. Defaults True when "
                "master on (composes Wave 4 #9 so downstream "
                "Wave 4 #13 can read cluster-recurrence as "
                "domain-falsifying evidence). Operator may set "
                "False for eval-only mode."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_RECORD_CLAIM}=false",
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
    "POSTMORTEM_FUSION_SCHEMA_VERSION",
    "FusionVerdict",
    "FusionSeverity",
    "MetaPostmortem",
    "FusionReport",
    "master_enabled",
    "record_claim_enabled",
    "fuse_threshold",
    "emerge_threshold",
    "max_meta_postmortems",
    "max_postmortems_to_scan",
    "verdict_glyph",
    "severity_glyph",
    "synthesize_meta_postmortem",
    "fuse_recent_postmortems",
    "format_fusion_panel",
    "register_shipped_invariants",
    "register_flags",
]
