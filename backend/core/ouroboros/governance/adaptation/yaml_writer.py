"""Item #2 — MetaGovernor YAML writer (closes the producer-side gap).

When operator approves a proposal via ``/adapt approve <id>``, the
MetaGovernor previously only updated the AdaptationLedger
(operator_decision: PENDING → APPROVED). The 5 wired live gates read
from ``.jarvis/adapted_<surface>.yaml`` files — NOT from the ledger
directly. So approved proposals were stuck at the ledger and never
reached the live gates.

This module closes that gap. On approval, the proposal's
``proposed_state_payload`` (added in the same Item #2 schema
extension to ``AdaptationProposal``) is materialized into the
appropriate YAML file at the loader's expected path, with:

  * Per-surface schema knowledge (which file, which top-level key,
    which entry shape — matches each Phase 7.x loader's contract)
  * Cross-process flock (reuses Phase 7.8's ``flock_exclusive``)
  * Atomic-rename semantics (write to ``.tmp``, fsync, rename)
  * Latest-wins-per-key OR append semantics matching loader
    expectations (see per-surface notes)

## Design constraints (load-bearing)

  * **Default-off**: ``JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED``
    (default false until per-surface graduation cadences flip the
    individual loader flags). When the writer master flag is off,
    /adapt approve still works (ledger-only) — the YAML writer is
    a no-op + returns SKIPPED_MASTER_OFF.
  * **Best-effort**: every write failure (PyYAML missing, fs
    permission, oversize existing file, etc.) returns a structured
    WriteResult — NEVER raises into the caller.
  * **Stdlib + adaptation.ledger only**. Same cage discipline.
  * **No payload, no write**: proposal with
    ``proposed_state_payload=None`` returns SKIPPED_NO_PAYLOAD.
    Pre-Item-#2 mining surfaces that haven't been updated to
    populate the payload safely no-op.

## Per-surface schema mapping

| Surface | YAML path | Top-level key | Entry shape |
|---|---|---|---|
| SEMANTIC_GUARDIAN_PATTERNS | `.jarvis/adapted_guardian_patterns.yaml` | `patterns` | `{name, regex, severity, message, ...prov}` |
| IRON_GATE_FLOORS | `.jarvis/adapted_iron_gate_floors.yaml` | `floors` | `{category, floor, ...prov}` |
| SCOPED_TOOL_BUDGETS | `.jarvis/adapted_mutation_budgets.yaml` | `budgets` | `{order, budget, ...prov}` |
| RISK_TIER_LADDER | `.jarvis/adapted_risk_tiers.yaml` | `tiers` | `{tier_name, insert_after, failure_class, ...prov}` |
| EXPLORATION_CATEGORY_WEIGHTS | `.jarvis/adapted_category_weights.yaml` | `rebalances` | `{new_weights: {...}, ...prov}` |

Per-loader semantics (preserved on write):
  * 4 of 5 surfaces append + use latest-occurrence-wins per key
    (the loader collapses duplicates at read time)
  * Category-weights uses latest-occurrence-wins overall (Slice 5
    miner produces ONE rebalance per cycle)

The writer always APPENDS rather than rewriting — preserves the
ledger's append-only audit-trail philosophy at the YAML level too.

## Provenance fields

Every entry written by this writer carries:
  * ``proposal_id``: from `AdaptationProposal.proposal_id`
  * ``approved_at``: from `AdaptationProposal.operator_decision_at`
  * ``approved_by``: from `AdaptationProposal.operator_decision_by`

These match the loader's expected provenance fields. The payload's
own fields take precedence — writer adds these only when missing
(the miner can include richer provenance if it wants).

## Default-off

`JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED` (default false).
"""
from __future__ import annotations

import enum
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationProposal,
    AdaptationSurface,
    OperatorDecisionStatus,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Hard cap on the size of an existing YAML file before the writer
# will refuse to read+rewrite it. Defends against malicious or
# corrupted state files growing unbounded.
MAX_EXISTING_YAML_BYTES: int = 4 * 1024 * 1024


def is_writer_enabled() -> bool:
    """Master flag — ``JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED``
    (default false until per-surface graduation cadences ramp)."""
    return os.environ.get(
        "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Per-surface schema mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SurfaceSchema:
    """Per-surface YAML schema descriptor. Matches each Phase 7.x
    loader's contract (see module docstring table)."""

    yaml_path: Path
    top_level_key: str
    path_env_override: str


_SCHEMA_BY_SURFACE: Dict[AdaptationSurface, _SurfaceSchema] = {
    AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS: _SurfaceSchema(
        yaml_path=Path(".jarvis") / "adapted_guardian_patterns.yaml",
        top_level_key="patterns",
        path_env_override="JARVIS_ADAPTED_GUARDIAN_PATTERNS_PATH",
    ),
    AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS: _SurfaceSchema(
        yaml_path=Path(".jarvis") / "adapted_iron_gate_floors.yaml",
        top_level_key="floors",
        path_env_override="JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH",
    ),
    AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET: _SurfaceSchema(
        yaml_path=Path(".jarvis") / "adapted_mutation_budgets.yaml",
        top_level_key="budgets",
        path_env_override="JARVIS_ADAPTED_MUTATION_BUDGETS_PATH",
    ),
    AdaptationSurface.RISK_TIER_FLOOR_TIERS: _SurfaceSchema(
        yaml_path=Path(".jarvis") / "adapted_risk_tiers.yaml",
        top_level_key="tiers",
        path_env_override="JARVIS_ADAPTED_RISK_TIERS_PATH",
    ),
    AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS: _SurfaceSchema(
        yaml_path=Path(".jarvis") / "adapted_category_weights.yaml",
        top_level_key="rebalances",
        path_env_override="JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH",
    ),
}


def _resolve_yaml_path(schema: _SurfaceSchema) -> Path:
    """Resolve the YAML path with env override (matches each Phase
    7.x loader's path-resolution contract)."""
    raw = os.environ.get(schema.path_env_override)
    if raw:
        return Path(raw)
    return schema.yaml_path


# ---------------------------------------------------------------------------
# Result + status
# ---------------------------------------------------------------------------


class WriteStatus(str, enum.Enum):
    """Terminal status of a write attempt."""

    OK = "ok"
    SKIPPED_MASTER_OFF = "skipped_master_off"
    SKIPPED_NO_PAYLOAD = "skipped_no_payload"
    SKIPPED_NOT_APPROVED = "skipped_not_approved"
    UNKNOWN_SURFACE = "unknown_surface"
    NO_PYYAML = "no_pyyaml"
    EXISTING_OVERSIZE = "existing_oversize"
    EXISTING_UNREADABLE = "existing_unreadable"
    EXISTING_PARSE_ERROR = "existing_parse_error"
    EXISTING_NON_MAPPING = "existing_non_mapping"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class WriteResult:
    """Terminal result of a write attempt. Frozen so callers can
    persist this verbatim into observability ledgers."""

    status: WriteStatus
    surface: Optional[str] = None
    yaml_path: Optional[str] = None
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status is WriteStatus.OK

    @property
    def is_skipped(self) -> bool:
        return self.status in (
            WriteStatus.SKIPPED_MASTER_OFF,
            WriteStatus.SKIPPED_NO_PAYLOAD,
            WriteStatus.SKIPPED_NOT_APPROVED,
        )


# ---------------------------------------------------------------------------
# Atomic-rename writer with cross-process flock
# ---------------------------------------------------------------------------


def _read_existing_doc(
    path: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[WriteStatus], str]:
    """Read existing YAML doc (if present). Returns
    ``(doc, error_status, detail)``. ``doc=None`` with
    ``error_status=None`` means file simply absent (treat as fresh)."""
    if not path.exists():
        return ({"schema_version": 1}, None, "")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return (None, WriteStatus.EXISTING_UNREADABLE, str(exc))
    if size > MAX_EXISTING_YAML_BYTES:
        return (
            None, WriteStatus.EXISTING_OVERSIZE,
            f"size={size}>max={MAX_EXISTING_YAML_BYTES}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return (None, WriteStatus.EXISTING_UNREADABLE, str(exc))
    if not text.strip():
        return ({"schema_version": 1}, None, "")
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return (None, WriteStatus.NO_PYYAML, "PyYAML not installed")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return (
            None, WriteStatus.EXISTING_PARSE_ERROR,
            f"yaml_parse:{exc}",
        )
    if not isinstance(doc, dict):
        return (
            None, WriteStatus.EXISTING_NON_MAPPING,
            f"top-level type:{type(doc).__name__}",
        )
    return (doc, None, "")


def _enrich_with_provenance(
    payload: Dict[str, Any],
    proposal: AdaptationProposal,
) -> Dict[str, Any]:
    """Add provenance fields the loader expects, only when missing
    from the payload (miner-supplied values take precedence)."""
    out = dict(payload)
    out.setdefault("proposal_id", proposal.proposal_id)
    if proposal.operator_decision_at:
        out.setdefault("approved_at", proposal.operator_decision_at)
    if proposal.operator_decision_by:
        out.setdefault("approved_by", proposal.operator_decision_by)
    return out


def _atomic_write_yaml(
    path: Path, doc: Dict[str, Any],
) -> Tuple[bool, str]:
    """Atomic write: temp file + fsync + rename.

    Cross-process flock acquired on the temp file's parent dir
    (via the existing ``adaptation/_file_lock`` helper from Phase
    7.8) so concurrent writers from parallel /adapt approve
    sessions serialize correctly.

    Returns ``(success, detail)``. NEVER raises.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return (False, "PyYAML not installed")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (False, f"mkdir_failed:{exc}")
    try:
        text = yaml.safe_dump(doc, sort_keys=False)
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        return (False, f"yaml_dump:{exc}")
    # Write to a temp file in the SAME directory so rename is atomic
    # (same filesystem). Use a unique suffix so concurrent writers
    # don't clobber each other's temps.
    try:
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
    except OSError as exc:
        return (False, f"mkstemp_failed:{exc}")
    tmp_path = Path(tmp_path_str)
    try:
        # Use the file_lock helper for cross-process serialization
        # on the TARGET path (not the tmp). We open the target with
        # mode 'a' (won't truncate; just opens for the lock). If the
        # target doesn't exist yet, touch it first.
        if not path.exists():
            path.touch()
        with path.open("a", encoding="utf-8") as lock_handle:
            from backend.core.ouroboros.governance.adaptation._file_lock import (  # noqa: E501
                flock_exclusive,
            )
            with flock_exclusive(lock_handle.fileno()):
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                # Atomic rename within the same FS.
                os.replace(str(tmp_path), str(path))
    except OSError as exc:
        # Clean up temp on failure.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return (False, f"write_failed:{exc}")
    return (True, "")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def write_proposal_to_yaml(
    proposal: AdaptationProposal,
) -> WriteResult:
    """Materialize an APPROVED proposal's ``proposed_state_payload``
    into the live gate's adapted YAML.

    Pre-checks (in order):
      1. Master flag off → SKIPPED_MASTER_OFF
      2. proposal.operator_decision != APPROVED → SKIPPED_NOT_APPROVED
      3. proposal.proposed_state_payload is None → SKIPPED_NO_PAYLOAD
      4. surface unknown to the schema mapping → UNKNOWN_SURFACE

    On pass:
      * Read existing YAML (or initialize as fresh)
      * Append the enriched payload to the surface's top_level_key list
      * Atomic-rename write with cross-process flock

    NEVER raises.
    """
    if not is_writer_enabled():
        return WriteResult(
            status=WriteStatus.SKIPPED_MASTER_OFF,
            surface=proposal.surface.value,
            detail="master_off",
        )
    if proposal.operator_decision is not OperatorDecisionStatus.APPROVED:
        return WriteResult(
            status=WriteStatus.SKIPPED_NOT_APPROVED,
            surface=proposal.surface.value,
            detail=(
                f"operator_decision={proposal.operator_decision.value}"
            ),
        )
    if proposal.proposed_state_payload is None:
        return WriteResult(
            status=WriteStatus.SKIPPED_NO_PAYLOAD,
            surface=proposal.surface.value,
            detail="proposed_state_payload_is_None",
        )
    schema = _SCHEMA_BY_SURFACE.get(proposal.surface)
    if schema is None:
        return WriteResult(
            status=WriteStatus.UNKNOWN_SURFACE,
            surface=proposal.surface.value,
            detail=f"no_schema_for:{proposal.surface.value}",
        )

    yaml_path = _resolve_yaml_path(schema)
    doc, err_status, err_detail = _read_existing_doc(yaml_path)
    if err_status is not None:
        return WriteResult(
            status=err_status,
            surface=proposal.surface.value,
            yaml_path=str(yaml_path),
            detail=err_detail,
        )
    assert doc is not None  # for type-checker
    entries = doc.get(schema.top_level_key)
    if not isinstance(entries, list):
        # Initialize the list slot if missing or wrong shape (the
        # loader treats wrong-shape as empty, so re-initializing is
        # safe for fresh files; for existing files with garbage in
        # this slot we replace — operator can recover via Pass B
        # /order2 amend if needed).
        entries = []
    enriched = _enrich_with_provenance(
        proposal.proposed_state_payload, proposal,
    )
    entries.append(enriched)
    doc[schema.top_level_key] = entries
    ok, detail = _atomic_write_yaml(yaml_path, doc)
    if not ok:
        return WriteResult(
            status=WriteStatus.WRITE_FAILED,
            surface=proposal.surface.value,
            yaml_path=str(yaml_path),
            detail=detail,
        )
    logger.info(
        "[YAMLWriter] WROTE proposal_id=%s surface=%s path=%s "
        "entries_now=%d",
        proposal.proposal_id, proposal.surface.value,
        yaml_path, len(entries),
    )
    return WriteResult(
        status=WriteStatus.OK,
        surface=proposal.surface.value,
        yaml_path=str(yaml_path),
        detail=f"entries_now={len(entries)}",
    )


# ---------------------------------------------------------------------------
# Gap #2 Slice 5 — confidence-thresholds materialization
# ---------------------------------------------------------------------------
#
# The five Pass C surfaces all use an "append-to-list" YAML schema
# (latest-occurrence-wins per key). The Confidence-monitor surface
# uses a SINGLE-DOCUMENT MAPPING schema (see
# ``adapted_confidence_loader.AdaptedConfidenceThresholds``) — there
# is one current adapted policy at any time, not a stack of historical
# proposals. The Confidence loader reads ``thresholds.{floor,
# window_k, approaching_factor, enforce}`` (a flat mapping), not a
# list of entries.
#
# Rather than fork the per-surface schema map (which would force
# every existing surface to add a "shape" discriminator field), we
# ship a SIBLING entry point that REUSES ``_atomic_write_yaml`` +
# the master flag + the provenance enrichment — a mapping write
# alongside the list-append writes. Same atomicity, same flock,
# same defensive contract.
#
# A successful write triggers Slice 4's ``confidence_policy_applied``
# SSE event; the call is wired by ``ide_policy_router._handle_approve``
# (best-effort — ledger approval succeeds even if YAML writer fails).


def _confidence_yaml_path() -> Path:
    """Resolve the adapted-confidence-thresholds YAML path. Mirrors
    ``adapted_confidence_loader.adapted_thresholds_path`` so the
    writer + loader agree by construction. Env-overridable via
    ``JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH``."""
    raw = os.environ.get(
        "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
    )
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adapted_confidence_thresholds.yaml"


def write_confidence_proposal_to_yaml(
    proposal: AdaptationProposal,
) -> WriteResult:
    """Materialize an APPROVED Confidence-surface proposal's
    ``proposed_state_payload`` into the live loader's adapted YAML.

    Pre-checks (in order):
      1. Master flag off → SKIPPED_MASTER_OFF
      2. proposal.surface != CONFIDENCE_MONITOR_THRESHOLDS →
         UNKNOWN_SURFACE (defensive — caller should dispatch by
         surface).
      3. proposal.operator_decision != APPROVED →
         SKIPPED_NOT_APPROVED.
      4. proposal.proposed_state_payload missing the ``proposed``
         key → SKIPPED_NO_PAYLOAD.

    On pass:
      * Compose a single-document mapping
        ``{schema_version: 1, proposal_id, approved_at,
        approved_by, thresholds: {<knob>: <value> for each
        non-baseline knob in payload['proposed']}}``.
      * Atomic-rename write with cross-process flock (REUSES
        ``_atomic_write_yaml`` from the existing list-append path).

    Each call OVERWRITES the YAML file (single-document semantic).
    The loader's tighten-only filter is the second-line defense
    against any post-write hand-edit that loosens a value.

    NEVER raises into the caller — every failure path returns a
    structured WriteResult.
    """
    if not is_writer_enabled():
        return WriteResult(
            status=WriteStatus.SKIPPED_MASTER_OFF,
            surface=(
                proposal.surface.value
                if hasattr(proposal, "surface") else None
            ),
        )
    if proposal.surface is not (
        AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS
    ):
        return WriteResult(
            status=WriteStatus.UNKNOWN_SURFACE,
            surface=proposal.surface.value,
            detail=(
                "use write_proposal_to_yaml for non-confidence "
                "surfaces"
            ),
        )
    if proposal.operator_decision is not (
        OperatorDecisionStatus.APPROVED
    ):
        return WriteResult(
            status=WriteStatus.SKIPPED_NOT_APPROVED,
            surface=proposal.surface.value,
            detail=(
                f"operator_decision="
                f"{proposal.operator_decision.value}"
            ),
        )
    payload = proposal.proposed_state_payload or {}
    proposed = payload.get("proposed")
    if not isinstance(proposed, dict) or not proposed:
        return WriteResult(
            status=WriteStatus.SKIPPED_NO_PAYLOAD,
            surface=proposal.surface.value,
            detail="payload['proposed'] missing or empty",
        )

    yaml_path = _confidence_yaml_path()

    # Compose the mapping doc shape the loader expects. Only
    # non-default knobs go into the materialized thresholds map —
    # if the operator only moved `floor`, the YAML carries
    # `thresholds: {floor: 0.10}` and the loader leaves the other
    # three knobs at None (consumer falls through to baseline).
    # Determining "non-default" requires knowing the baseline; we
    # delegate that to the loader's per-knob filter at READ time
    # by including every knob in the payload — the loader is
    # authoritative on tighten-only.
    thresholds_block: Dict[str, Any] = {}
    for key in ("floor", "window_k", "approaching_factor", "enforce"):
        if key in proposed:
            thresholds_block[key] = proposed[key]

    if not thresholds_block:
        return WriteResult(
            status=WriteStatus.SKIPPED_NO_PAYLOAD,
            surface=proposal.surface.value,
            detail="no recognized threshold keys in payload",
        )

    doc: Dict[str, Any] = {
        "schema_version": 1,
        "proposal_id": proposal.proposal_id,
        "approved_at": proposal.operator_decision_at or "",
        "approved_by": proposal.operator_decision_by or "",
        "thresholds": thresholds_block,
    }

    ok, detail = _atomic_write_yaml(yaml_path, doc)
    if not ok:
        return WriteResult(
            status=WriteStatus.WRITE_FAILED,
            surface=proposal.surface.value,
            yaml_path=str(yaml_path),
            detail=detail,
        )
    return WriteResult(
        status=WriteStatus.OK,
        surface=proposal.surface.value,
        yaml_path=str(yaml_path),
        detail=f"thresholds_keys={sorted(thresholds_block.keys())}",
    )


__all__ = [
    "MAX_EXISTING_YAML_BYTES",
    "WriteResult",
    "WriteStatus",
    "is_writer_enabled",
    "write_confidence_proposal_to_yaml",
    "write_proposal_to_yaml",
]
