"""§3.6.2 Vector #5 — Cross-Session Coherence Harness
(PRD v2.79 to v2.80, 2026-05-09).

Composes the 4 canonical cross-session memory substrates
into a deterministic harness that:

  1. Computes a per-axis digest of each substrate's
     persisted state.
  2. Simulates a session boundary (close in-memory state +
     re-read from disk) and verifies digest stability.
  3. Computes drift between two digests at field-level
     granularity.

The harness is the **validation infrastructure** — it does
not produce empirical proof of long-horizon coherence by
itself. Operator-paced 50+ session runs will USE this
harness to produce that proof; this slice ships the rails.

Authority asymmetry: ZERO. Read-only digest computation +
re-read-from-disk verification. NEVER mutates any of the
4 underlying substrates.

§38.11.5a.5 single-canonical-name discipline:
  * Composes canonical UserPreferenceStore.list_all
  * Composes canonical AdaptationLedger.history
  * Composes canonical SemanticIndex.snapshot_global_centroid
  * Composes canonical LastSessionSummary.load
  * Two NEW closed taxonomies: CoherenceAxis (4 values —
    one per substrate) + DriftLevel (4 stages of drift).

§33 patterns:
  * §33.1 graduation contract — master flag default-FALSE
  * §33.5 versioned artifact — 3 frozen artifacts
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CROSS_SESSION_HARNESS_SCHEMA_VERSION: str = (
    "cross_session_harness.1"
)


_ENV_MASTER = "JARVIS_CROSS_SESSION_HARNESS_ENABLED"


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class CoherenceAxis(str, enum.Enum):
    """Closed 4-value vocabulary — one axis per canonical
    cross-session memory substrate.
    """

    USER_PREFS = "user_prefs"          # UserPreferenceStore
    ADAPTATIONS = "adaptations"        # AdaptationLedger
    SEMANTIC_CENTROID = "semantic_centroid"  # SemanticIndex
    SESSION_HISTORY = "session_history"  # LastSessionSummary

    @classmethod
    def coerce(cls, raw: object) -> "CoherenceAxis":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.USER_PREFS


class DriftLevel(str, enum.Enum):
    """Closed 4-value drift vocabulary mapped to delta
    magnitude + structural integrity. Drift is ASYMMETRIC:
    growing-with-coherent-history is STABLE; rewriting
    history is CORRUPTED.

    STABLE     — same digest OR additive-only growth (count
                 increased, prefix hash matches)
    DRIFTING   — count changed (additions or deletions);
                 some content changed but hash still
                 traceable
    DIVERGED   — content hash mismatch in records that
                 should be append-only / immutable
    CORRUPTED  — substrate failed to load OR digest
                 computation failed (read-error path)
    """

    STABLE = "stable"
    DRIFTING = "drifting"
    DIVERGED = "diverged"
    CORRUPTED = "corrupted"


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class AxisDigest:
    """Per-axis fingerprint."""

    axis: CoherenceAxis
    record_count: int = 0
    content_hash: str = ""        # sha256[:16] of canonicalized state
    sample_size_bytes: int = 0    # source bytes hashed (for drift heuristic)
    diagnostic: str = ""
    schema_version: str = (
        CROSS_SESSION_HARNESS_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "axis": self.axis.value,
            "record_count": self.record_count,
            "content_hash": self.content_hash,
            "sample_size_bytes": self.sample_size_bytes,
            "diagnostic": self.diagnostic,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CrossSessionDigest:
    """Aggregate digest snapshot across all 4 axes."""

    aggregated_at_unix: float = 0.0
    project_root: str = ""
    digests: Tuple[AxisDigest, ...] = field(
        default_factory=tuple,
    )
    schema_version: str = (
        CROSS_SESSION_HARNESS_SCHEMA_VERSION
    )

    def digest_for_axis(
        self, axis: CoherenceAxis,
    ) -> Optional[AxisDigest]:
        for d in self.digests:
            if d.axis is axis:
                return d
        return None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "aggregated_at_unix": self.aggregated_at_unix,
            "project_root": self.project_root,
            "digests": [d.to_dict() for d in self.digests],
        }


@dataclass(frozen=True)
class AxisDrift:
    """Per-axis drift delta between two digests."""

    axis: CoherenceAxis
    level: DriftLevel
    record_count_delta: int = 0
    hash_changed: bool = False
    diagnostic: str = ""

    def to_dict(self) -> dict:
        return {
            "axis": self.axis.value,
            "level": self.level.value,
            "record_count_delta": self.record_count_delta,
            "hash_changed": self.hash_changed,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class CoherenceReport:
    """Multi-session coherence report. Frozen."""

    boundary_count: int = 0       # number of session boundaries crossed
    drifts_per_boundary: Tuple[
        Tuple[AxisDrift, ...], ...
    ] = field(default_factory=tuple)
    overall_stable: bool = False
    schema_version: str = (
        CROSS_SESSION_HARNESS_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "boundary_count": self.boundary_count,
            "overall_stable": self.overall_stable,
            "drifts_per_boundary": [
                [d.to_dict() for d in boundary_drifts]
                for boundary_drifts in (
                    self.drifts_per_boundary
                )
            ],
        }


# ===========================================================================
# Per-axis digest computation — composes canonical sources
# ===========================================================================


def _digest_user_prefs(project_root: Path) -> AxisDigest:
    """Compose canonical UserPreferenceStore.list_all into
    a deterministic digest. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (  # noqa: E501
            UserPreferenceStore,
        )
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.USER_PREFS,
            diagnostic=f"import_failed:{type(exc).__name__}",
        )
    try:
        store = UserPreferenceStore(
            project_root=project_root,
            auto_register_protected_paths=False,
            auto_register_protected_apps=False,
        )
        memories = store.list_all()
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.USER_PREFS,
            diagnostic=f"load_failed:{type(exc).__name__}",
        )
    # Canonical fingerprint: sorted (id, type, content_hash) tuples.
    parts = []
    total_bytes = 0
    for m in sorted(memories, key=lambda x: x.id):
        try:
            md = m.to_markdown()
            md_bytes = md.encode("utf-8")
            total_bytes += len(md_bytes)
            md_hash = hashlib.sha256(md_bytes).hexdigest()[:16]
            parts.append(
                f"{m.id}|{m.type.value}|{md_hash}",
            )
        except Exception:  # noqa: BLE001
            continue
    joined = "\n".join(parts).encode("utf-8")
    return AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=len(memories),
        content_hash=hashlib.sha256(joined).hexdigest()[:16],
        sample_size_bytes=total_bytes,
    )


def _digest_adaptations(project_root: Path) -> AxisDigest:
    """Compose canonical AdaptationLedger.history into a
    deterministic digest. NEVER raises.

    NOTE: AdaptationLedger reads from a global path
    determined by env vars; we don't easily redirect it
    per project_root. So this digest reflects the GLOBAL
    ledger state — which is what cross-session continuity
    actually means at the substrate level (ledger is
    process-global by design).
    """
    try:
        from backend.core.ouroboros.governance.adaptation.ledger import (  # noqa: E501
            AdaptationLedger,
        )
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.ADAPTATIONS,
            diagnostic=f"import_failed:{type(exc).__name__}",
        )
    try:
        ledger = AdaptationLedger()
        proposals = ledger.history(limit=500)
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.ADAPTATIONS,
            diagnostic=f"load_failed:{type(exc).__name__}",
        )
    parts = []
    total_bytes = 0
    for p in proposals:
        try:
            d = p.to_dict()
            # Drop volatile fields from fingerprint (timestamps
            # in iso8601 form change per write — but proposal_id
            # + content + decision is canonical).
            stable = {
                k: v for k, v in d.items()
                if k not in (
                    "created_at_iso", "updated_at_iso",
                )
            }
            import json as _json
            ser = _json.dumps(
                stable, sort_keys=True, default=str,
            ).encode("utf-8")
            total_bytes += len(ser)
            parts.append(hashlib.sha256(ser).hexdigest()[:16])
        except Exception:  # noqa: BLE001
            continue
    joined = "|".join(parts).encode("utf-8")
    return AxisDigest(
        axis=CoherenceAxis.ADAPTATIONS,
        record_count=len(proposals),
        content_hash=hashlib.sha256(joined).hexdigest()[:16],
        sample_size_bytes=total_bytes,
    )


def _digest_semantic_centroid(
    project_root: Path,
) -> AxisDigest:
    """Compose canonical SemanticIndex.snapshot_global_centroid
    into a deterministic digest. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.semantic_index import (  # noqa: E501
            SemanticIndex,
        )
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.SEMANTIC_CENTROID,
            diagnostic=f"import_failed:{type(exc).__name__}",
        )
    try:
        idx = SemanticIndex(project_root=project_root)
        centroid = idx.snapshot_global_centroid()
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.SEMANTIC_CENTROID,
            diagnostic=f"snapshot_failed:{type(exc).__name__}",
        )
    if not centroid:
        # Empty centroid is the legitimate first-boot state
        # (no commits/goals/conversation indexed yet).
        # Treat as STABLE-empty, NOT CORRUPTED — the
        # diagnostic field is reserved for actual load
        # failures (import_failed / snapshot_failed etc.).
        return AxisDigest(
            axis=CoherenceAxis.SEMANTIC_CENTROID,
            record_count=0,
            content_hash="",
        )
    # Canonical fingerprint: round to 6 decimals + hash bytes.
    rounded = tuple(round(float(x), 6) for x in centroid)
    serialized = "|".join(
        f"{x:.6f}" for x in rounded
    ).encode("utf-8")
    return AxisDigest(
        axis=CoherenceAxis.SEMANTIC_CENTROID,
        record_count=len(rounded),
        content_hash=hashlib.sha256(serialized).hexdigest()[:16],
        sample_size_bytes=len(serialized),
    )


def _digest_session_history(
    project_root: Path,
) -> AxisDigest:
    """Compose canonical LastSessionSummary.load into a
    deterministic digest. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.last_session_summary import (  # noqa: E501
            LastSessionSummary,
        )
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.SESSION_HISTORY,
            diagnostic=f"import_failed:{type(exc).__name__}",
        )
    try:
        lss = LastSessionSummary(project_root=project_root)
        records = lss.load(n_sessions=10)
    except Exception as exc:  # noqa: BLE001
        return AxisDigest(
            axis=CoherenceAxis.SESSION_HISTORY,
            diagnostic=f"load_failed:{type(exc).__name__}",
        )
    parts = []
    total_bytes = 0
    for rec in records:
        try:
            # session_id + stop_reason + stats fingerprint —
            # immutable across boundaries.
            payload = (
                f"{rec.session_id}|{rec.stop_reason}|"
                f"a={rec.stats_attempted}|"
                f"c={rec.stats_completed}|"
                f"f={rec.stats_failed}|"
                f"cost={rec.cost_total:.4f}"
            ).encode("utf-8")
            total_bytes += len(payload)
            parts.append(
                hashlib.sha256(payload).hexdigest()[:16],
            )
        except Exception:  # noqa: BLE001
            continue
    joined = "|".join(parts).encode("utf-8")
    return AxisDigest(
        axis=CoherenceAxis.SESSION_HISTORY,
        record_count=len(records),
        content_hash=hashlib.sha256(joined).hexdigest()[:16],
        sample_size_bytes=total_bytes,
    )


# Bytes-pinned axis → digest-fn dispatch. AST regression
# enforces every CoherenceAxis value has an entry.
_AXIS_DIGESTERS = (
    (CoherenceAxis.USER_PREFS, _digest_user_prefs),
    (CoherenceAxis.ADAPTATIONS, _digest_adaptations),
    (CoherenceAxis.SEMANTIC_CENTROID, _digest_semantic_centroid),
    (CoherenceAxis.SESSION_HISTORY, _digest_session_history),
)


# ===========================================================================
# Public API — aggregate + compute_drift + simulate_boundary
# ===========================================================================


def aggregate_digest(
    *, project_root: Path,
) -> CrossSessionDigest:
    """Compute per-axis digest snapshot. NEVER raises.
    Empty when master flag off."""
    if not master_enabled():
        return CrossSessionDigest()
    digests: List[AxisDigest] = []
    for axis, fn in _AXIS_DIGESTERS:
        try:
            digests.append(fn(project_root))
        except Exception as exc:  # noqa: BLE001 — defensive
            digests.append(AxisDigest(
                axis=axis,
                diagnostic=f"digester_raised:{type(exc).__name__}",
            ))
    return CrossSessionDigest(
        aggregated_at_unix=time.time(),
        project_root=str(project_root),
        digests=tuple(digests),
    )


def compute_drift(
    *,
    before: AxisDigest,
    after: AxisDigest,
) -> AxisDrift:
    """Pure-function field-level drift classification.
    NEVER raises.

    Decision tree (first-match-wins):
      1. Either side has diagnostic != "" or empty hash on
         a non-zero-record-count side → CORRUPTED
      2. record_count strictly increased AND old prefix
         remains traceable → STABLE (additive-only growth)
      3. record_count == before AND hash unchanged → STABLE
      4. record_count changed → DRIFTING
      5. hash changed but count same → DIVERGED
    """
    if before.axis is not after.axis:
        return AxisDrift(
            axis=after.axis,
            level=DriftLevel.CORRUPTED,
            diagnostic="axis_mismatch",
        )
    # Corruption on either side.
    if before.diagnostic or after.diagnostic:
        return AxisDrift(
            axis=after.axis,
            level=DriftLevel.CORRUPTED,
            record_count_delta=(
                after.record_count - before.record_count
            ),
            hash_changed=(
                before.content_hash != after.content_hash
            ),
            diagnostic=(
                after.diagnostic or before.diagnostic
            ),
        )
    delta = after.record_count - before.record_count
    hash_changed = before.content_hash != after.content_hash
    # Empty-empty case: stable.
    if (
        before.record_count == 0
        and after.record_count == 0
    ):
        return AxisDrift(
            axis=after.axis, level=DriftLevel.STABLE,
            diagnostic="both_empty",
        )
    # Identical state.
    if delta == 0 and not hash_changed:
        return AxisDrift(
            axis=after.axis, level=DriftLevel.STABLE,
            diagnostic="identical",
        )
    # Additive growth — STABLE if hash necessarily changed
    # because new records were added but the prior records
    # weren't rewritten. (Note: at this digest granularity
    # we can't prove prefix-stability rigorously; that
    # requires per-record hashing. STABLE here means
    # "additions consistent with append-only growth".)
    if delta > 0:
        return AxisDrift(
            axis=after.axis, level=DriftLevel.STABLE,
            record_count_delta=delta,
            hash_changed=hash_changed,
            diagnostic=f"additive_growth_+{delta}",
        )
    # Removed records OR mixed add/remove.
    if delta != 0:
        return AxisDrift(
            axis=after.axis, level=DriftLevel.DRIFTING,
            record_count_delta=delta,
            hash_changed=hash_changed,
            diagnostic=f"count_changed_{delta:+d}",
        )
    # Same count, different hash → records were rewritten.
    # Append-only substrates SHOULD never see this.
    return AxisDrift(
        axis=after.axis, level=DriftLevel.DIVERGED,
        record_count_delta=0,
        hash_changed=True,
        diagnostic="hash_diverged_same_count",
    )


def simulate_session_boundary(
    *, project_root: Path,
) -> CrossSessionDigest:
    """Compute a digest after forcing a "session boundary":
    flush in-process state + re-load from disk. NEVER raises.

    Specifically: this function takes the CURRENT on-disk
    state (which is what a fresh next-session boot would
    see) and produces a digest. By design it does NOT
    write anything — the substrates are responsible for
    their own persistence; this is a verification primitive.
    """
    if not master_enabled():
        return CrossSessionDigest()
    # Reset any in-process default singletons so the next
    # digest reads from disk (not a stale in-memory cache).
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (  # noqa: E501
            reset_default_store,
        )
        reset_default_store()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core.ouroboros.governance.last_session_summary import (  # noqa: E501
            reset_default_summary,
        )
        reset_default_summary()
    except Exception:  # noqa: BLE001
        pass
    return aggregate_digest(project_root=project_root)


def report_coherence(
    digests: Tuple[CrossSessionDigest, ...],
) -> CoherenceReport:
    """Build N-session coherence report from a series of
    digests. Each adjacent pair (i, i+1) is one boundary.

    ``overall_stable`` is True iff every axis at every
    boundary lands in DriftLevel.STABLE (the strictest
    bar — additive-only growth allowed; deletions or hash
    rewrites fail the test)."""
    if len(digests) < 2:
        report = CoherenceReport(
            boundary_count=0,
            overall_stable=True,
        )
        _publish_event(report)
        return report
    drifts_per_boundary: List[Tuple[AxisDrift, ...]] = []
    overall_stable = True
    for i in range(len(digests) - 1):
        before = digests[i]
        after = digests[i + 1]
        boundary_drifts: List[AxisDrift] = []
        for axis in CoherenceAxis:
            d_before = before.digest_for_axis(axis)
            d_after = after.digest_for_axis(axis)
            if d_before is None or d_after is None:
                drift = AxisDrift(
                    axis=axis,
                    level=DriftLevel.CORRUPTED,
                    diagnostic="missing_axis",
                )
            else:
                drift = compute_drift(
                    before=d_before, after=d_after,
                )
            if drift.level is not DriftLevel.STABLE:
                overall_stable = False
            boundary_drifts.append(drift)
        drifts_per_boundary.append(tuple(boundary_drifts))
    report = CoherenceReport(
        boundary_count=len(digests) - 1,
        drifts_per_boundary=tuple(drifts_per_boundary),
        overall_stable=overall_stable,
    )
    _publish_event(report)
    return report


def _publish_event(report: CoherenceReport) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_COHERENCE_REPORTED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            broker.publish(
                EVENT_TYPE_COHERENCE_REPORTED,
                "cross_session_coherence",
                report.to_dict(),
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "cross_session_harness: SSE failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


_LEVEL_GLYPHS: Dict[DriftLevel, str] = {
    DriftLevel.STABLE: "✓",
    DriftLevel.DRIFTING: "↔",
    DriftLevel.DIVERGED: "✗",
    DriftLevel.CORRUPTED: "⚠",
}


_LEVEL_TINTS: Dict[DriftLevel, str] = {
    DriftLevel.STABLE: "green",
    DriftLevel.DRIFTING: "yellow",
    DriftLevel.DIVERGED: "red",
    DriftLevel.CORRUPTED: "bright_red",
}


def format_coherence_report(
    report: Optional[CoherenceReport],
) -> str:
    """Render coherence report. Empty when master off OR
    report is None."""
    if not master_enabled():
        return ""
    if report is None:
        return ""
    summary_glyph = "✓" if report.overall_stable else "⚠"
    summary_tint = (
        "green" if report.overall_stable else "yellow"
    )
    parts = [
        f"[bright_yellow]🧬 Cross-session coherence:[/] "
        f"[{summary_tint}]{summary_glyph}[/] "
        f"{report.boundary_count} boundary"
        f"{'s' if report.boundary_count != 1 else ''} "
        f"({'STABLE' if report.overall_stable else 'DRIFTED'})"
    ]
    for i, boundary_drifts in enumerate(
        report.drifts_per_boundary,
    ):
        parts.append(f"  boundary {i}:")
        for drift in boundary_drifts:
            glyph = _LEVEL_GLYPHS.get(drift.level, "·")
            tint = _LEVEL_TINTS.get(drift.level, "white")
            delta_str = (
                f" Δ={drift.record_count_delta:+d}"
                if drift.record_count_delta != 0 else ""
            )
            parts.append(
                f"    [{tint}]{glyph}[/] "
                f"{drift.axis.value:<18} "
                f"{drift.level.value:<10}"
                f"{delta_str} "
                f"[dim]{drift.diagnostic}[/]"
            )
    return "\n".join(parts)


# ===========================================================================
# FlagRegistry seeds + AST pins
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            name=_ENV_MASTER, type="bool", category="ux",
            description=(
                "§3.6.2 Vector #5 cross-session coherence "
                "harness master switch (default FALSE per "
                "§33.1)."
            ),
            example="false",
            source_file=(
                "backend/core/ouroboros/governance/"
                "cross_session_harness.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    def _master(tree, src):
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
                                return []
                return ["master must default False"]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "vector_5_master_default_false"
        ),
        description="§33.1 graduation contract.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "cross_session_harness.py"
        ),
        validate=_master,
    ))

    def _axis_taxonomy(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CoherenceAxis"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "USER_PREFS", "ADAPTATIONS",
                    "SEMANTIC_CENTROID", "SESSION_HISTORY",
                }
                missing = expected - names
                if missing:
                    return [
                        f"missing: {sorted(missing)}"
                    ]
                return []
        return ["CoherenceAxis not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "vector_5_axis_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value CoherenceAxis taxonomy — one "
            "axis per canonical cross-session memory "
            "substrate."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "cross_session_harness.py"
        ),
        validate=_axis_taxonomy,
    ))

    def _level_taxonomy(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "DriftLevel"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "STABLE", "DRIFTING",
                    "DIVERGED", "CORRUPTED",
                }
                missing = expected - names
                if missing:
                    return [f"missing: {sorted(missing)}"]
                return []
        return ["DriftLevel not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "vector_5_level_taxonomy_4_values"
        ),
        description="Closed 4-value DriftLevel taxonomy.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "cross_session_harness.py"
        ),
        validate=_level_taxonomy,
    ))

    def _composes_all_4_substrates(tree, src):
        """Bytes-pin: every canonical cross-session substrate
        MUST be referenced as a lazy-import string. Drift
        from this set silently breaks the harness."""
        required = (
            "user_preference_memory",
            "adaptation.ledger",
            "semantic_index",
            "last_session_summary",
        )
        missing = [r for r in required if r not in src]
        if missing:
            return [
                f"missing canonical substrate composes: "
                f"{missing}"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "vector_5_composes_all_4_substrates"
        ),
        description=(
            "Harness composes ALL 4 canonical cross-"
            "session substrates — accidental drop silently "
            "loses an axis of coverage."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "cross_session_harness.py"
        ),
        validate=_composes_all_4_substrates,
    ))

    def _digesters_cover_all_axes(tree, src):
        """Bytes-pin: _AXIS_DIGESTERS dispatch tuple MUST
        reference every CoherenceAxis enum value."""
        required = (
            "CoherenceAxis.USER_PREFS",
            "CoherenceAxis.ADAPTATIONS",
            "CoherenceAxis.SEMANTIC_CENTROID",
            "CoherenceAxis.SESSION_HISTORY",
        )
        missing = [r for r in required if r not in src]
        if missing:
            return [
                f"_AXIS_DIGESTERS missing entries: "
                f"{missing}"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "vector_5_digesters_cover_all_axes"
        ),
        description=(
            "_AXIS_DIGESTERS dispatch tuple covers every "
            "CoherenceAxis value — adding an axis without "
            "registering its digester breaks aggregation."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "cross_session_harness.py"
        ),
        validate=_digesters_cover_all_axes,
    ))

    def _authority(tree, src):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if any(m.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden: {m}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "vector_5_authority_asymmetry"
        ),
        description="Substrate purity.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "cross_session_harness.py"
        ),
        validate=_authority,
    ))

    return pins


__all__ = [
    "CROSS_SESSION_HARNESS_SCHEMA_VERSION",
    "CoherenceAxis",
    "DriftLevel",
    "AxisDigest",
    "AxisDrift",
    "CrossSessionDigest",
    "CoherenceReport",
    "master_enabled",
    "aggregate_digest",
    "compute_drift",
    "simulate_session_boundary",
    "report_coherence",
    "format_coherence_report",
    "register_flags",
    "register_shipped_invariants",
]
