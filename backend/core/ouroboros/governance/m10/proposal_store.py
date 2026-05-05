"""M10 Slice 5 — ProposalStore: JSONL-backed lifecycle ledger.

Single-source-of-truth for M10 proposal records persisted at
``.jarvis/m10/proposals.jsonl`` via
:func:`cross_process_jsonl.flock_append_line` (zero new locking
code — same primitive used by M11 / M9 / Upgrade 3 / Upgrade 2's
decisions ledger).

Architectural locks (operator mandate, AST-pinned at Slice 5):

  * **Single-file ledger** — one `proposals.jsonl` per project
    root (override via ``JARVIS_M10_PROPOSALS_PATH``). Per-day
    rotation deferred to a future arc; the file size is
    bounded by the §32.4.3 5/day cap (≈ 5 × 1.5 KB = 7.5 KB
    per day, ~2.7 MB per year).
  * **Cross-process tear-safe** — every read uses
    :func:`flock_critical_section`; every append uses
    :func:`flock_append_line`. NEVER raw file I/O.
  * **NEVER raises** — all faults map to empty results
    (read) / False return (write). Defensive everywhere.
  * **Read-only consumers** — observability + REPL READ from
    this store; only the lifecycle orchestrator (Slice 4)
    + Slice 5 graduation seeding may WRITE.
  * **Authority asymmetry** — module imports stdlib +
    `cross_process_jsonl` ONLY. NEVER imports orchestrator /
    iron_gate / providers / candidate_generator / etc.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


M10_PROPOSAL_STORE_SCHEMA_VERSION: str = "m10_proposal_store.1"


_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 1_000
_MAX_FILE_BYTES: int = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def proposals_jsonl_path() -> Path:
    """``JARVIS_M10_PROPOSALS_PATH`` — JSONL ledger path.
    Default ``.jarvis/m10/proposals.jsonl``. Resolved at call
    time so tests can override per-fixture."""
    raw = os.environ.get(
        "JARVIS_M10_PROPOSALS_PATH", "",
    ).strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / "m10" / "proposals.jsonl"


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredProposal:
    """One proposal-ledger row. Frozen + JSON-projectable.
    Composes :class:`M10ProposalRecord` (Slice 1) +
    :class:`ProposalLifecycleResult` (Slice 4) snapshots."""

    proposal_id: str
    """Stable identifier from Slice 1's M10ProposalRecord."""

    kind: str
    """ProposalKind value (string for storage; Slice 5 surfaces
    convert back to enum on read)."""

    phase: str
    """M10ProposalPhase value at last update."""

    pattern_signature: str = ""
    detection_evidence: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    proposed_module_path: str = ""
    proposed_class_name: str = ""
    proposed_ast_pin_name: str = ""
    pr_url: str = ""
    pr_branch: str = ""
    failure_reason: str = ""
    cost_usd: float = 0.0
    consensus_signature: str = ""
    last_updated_at_unix: float = field(default_factory=time.time)
    schema_version: str = field(
        default=M10_PROPOSAL_STORE_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "phase": self.phase,
            "pattern_signature": self.pattern_signature,
            "detection_evidence": list(
                self.detection_evidence,
            ),
            "proposed_module_path": (
                self.proposed_module_path
            ),
            "proposed_class_name": self.proposed_class_name,
            "proposed_ast_pin_name": (
                self.proposed_ast_pin_name
            ),
            "pr_url": self.pr_url,
            "pr_branch": self.pr_branch,
            "failure_reason": self.failure_reason,
            "cost_usd": float(self.cost_usd),
            "consensus_signature": self.consensus_signature,
            "last_updated_at_unix": float(
                self.last_updated_at_unix,
            ),
        }

    @classmethod
    def from_dict(
        cls, raw: Dict[str, Any],
    ) -> Optional["StoredProposal"]:
        """Defensive parse — returns None on missing required
        fields. NEVER raises."""
        try:
            pid = str(raw.get("proposal_id", "")).strip()
            kind = str(raw.get("kind", "")).strip()
            phase = str(raw.get("phase", "")).strip()
            if not pid or not kind or not phase:
                return None
            evidence_raw = raw.get(
                "detection_evidence", (),
            ) or ()
            evidence = tuple(
                str(e) for e in evidence_raw
                if isinstance(e, (str, int, float))
            )
            return cls(
                proposal_id=pid,
                kind=kind,
                phase=phase,
                pattern_signature=str(
                    raw.get("pattern_signature", ""),
                ),
                detection_evidence=evidence,
                proposed_module_path=str(
                    raw.get("proposed_module_path", ""),
                ),
                proposed_class_name=str(
                    raw.get("proposed_class_name", ""),
                ),
                proposed_ast_pin_name=str(
                    raw.get("proposed_ast_pin_name", ""),
                ),
                pr_url=str(raw.get("pr_url", "")),
                pr_branch=str(raw.get("pr_branch", "")),
                failure_reason=str(
                    raw.get("failure_reason", ""),
                ),
                cost_usd=float(raw.get("cost_usd", 0.0)),
                consensus_signature=str(
                    raw.get("consensus_signature", ""),
                ),
                last_updated_at_unix=float(
                    raw.get(
                        "last_updated_at_unix", 0.0,
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_proposal(
    proposal: StoredProposal,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Append one proposal row. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
        target = path if path is not None else proposals_jsonl_path()
        # Ensure parent dir exists
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        line = json.dumps(
            proposal.to_dict(),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return flock_append_line(target, line)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[m10_proposal_store] append raised: %s", exc,
        )
        return False


def read_all_proposals(
    *,
    limit: int = _DEFAULT_LIMIT,
    path: Optional[Path] = None,
) -> Tuple[StoredProposal, ...]:
    """Read up to ``limit`` most-recent proposal rows from the
    JSONL ledger. NEVER raises."""
    try:
        bound = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        bound = _DEFAULT_LIMIT
    target = (
        path if path is not None else proposals_jsonl_path()
    )
    if not target.exists():
        return tuple()
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
        with flock_critical_section(target) as acquired:
            if not acquired:
                return tuple()
            try:
                stat = target.stat()
                if stat.st_size > _MAX_FILE_BYTES:
                    return tuple()
                text = target.read_text(encoding="utf-8")
            except OSError:
                return tuple()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[m10_proposal_store] read raised: %s", exc,
        )
        return tuple()

    out: List[StoredProposal] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
            if not isinstance(row, dict):
                continue
            parsed = StoredProposal.from_dict(row)
            if parsed is not None:
                out.append(parsed)
        except json.JSONDecodeError:
            continue
        except Exception:  # noqa: BLE001 — defensive
            continue
    if len(out) > bound:
        out = out[-bound:]
    return tuple(out)


def find_proposal_by_id(
    proposal_id: str,
    *,
    path: Optional[Path] = None,
) -> Optional[StoredProposal]:
    """Most-recent ledger row for ``proposal_id`` (since rows
    are append-only, the LAST occurrence wins). Returns None
    if not found. NEVER raises."""
    try:
        rows = read_all_proposals(
            limit=_MAX_LIMIT, path=path,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    found: Optional[StoredProposal] = None
    for r in rows:
        if r.proposal_id == proposal_id:
            found = r
    return found


def aggregate_phase_histogram(
    *,
    path: Optional[Path] = None,
) -> Dict[str, int]:
    """Phase histogram across all stored proposals (most-
    recent state per proposal_id). NEVER raises."""
    try:
        rows = read_all_proposals(
            limit=_MAX_LIMIT, path=path,
        )
    except Exception:  # noqa: BLE001 — defensive
        return {}
    # Most-recent state per proposal_id
    latest: Dict[str, str] = {}
    for r in rows:
        latest[r.proposal_id] = r.phase
    counts: Dict[str, int] = {}
    for phase in latest.values():
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def list_pending_proposals(
    *,
    limit: int = _DEFAULT_LIMIT,
    path: Optional[Path] = None,
) -> Tuple[StoredProposal, ...]:
    """Proposals in `awaiting_approval` or `awaiting_merge`
    phases. Most-recent state per proposal_id wins. NEVER
    raises."""
    try:
        rows = read_all_proposals(
            limit=_MAX_LIMIT, path=path,
        )
    except Exception:  # noqa: BLE001 — defensive
        return tuple()
    # Reduce to most-recent per proposal_id
    latest: Dict[str, StoredProposal] = {}
    for r in rows:
        latest[r.proposal_id] = r
    pending = [
        r for r in latest.values()
        if r.phase in (
            "awaiting_approval", "awaiting_merge",
        )
    ]
    pending.sort(
        key=lambda x: -x.last_updated_at_unix,
    )
    try:
        bound = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        bound = _DEFAULT_LIMIT
    return tuple(pending[:bound])


__all__ = [
    "M10_PROPOSAL_STORE_SCHEMA_VERSION",
    "StoredProposal",
    "aggregate_phase_histogram",
    "append_proposal",
    "find_proposal_by_id",
    "list_pending_proposals",
    "proposals_jsonl_path",
    "read_all_proposals",
]
