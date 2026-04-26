"""P1 Slice 4 — `/backlog auto-proposed` REPL operator-review surface.

The human-in-loop side of the Curiosity Engine v2 (PRD §9 Phase 2 P1).
Reads ``SelfGoalFormationEngine`` proposals from the JSONL audit ledger
and lets the operator approve / reject / inspect each one.

Commands::

  /backlog auto-proposed                          # alias for `list`
  /backlog auto-proposed list                     # pending proposals
  /backlog auto-proposed show <signature_hash>    # full detail of one
  /backlog auto-proposed approve <hash> [--reason TEXT]
  /backlog auto-proposed reject  <hash> [--reason TEXT]
  /backlog auto-proposed history [--limit N]      # recent decisions
  /backlog auto-proposed help                     # usage

State on disk (BOTH default to ``<repo>/.jarvis/``):
  * ``self_goal_formation_proposals.jsonl`` — engine's audit ledger
    (READ-ONLY here; written by SelfGoalFormationEngine in Slice 2)
  * ``self_goal_formation_decisions.jsonl``  — operator decisions
    (WRITTEN here, append-only audit trail)
  * ``backlog.json``                          — manual backlog entries
    (APPENDED to here on approve, with ``auto_proposed=true`` preserved)

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
    Pinned by ``test_backlog_auto_proposed_repl_no_authority_imports``.
  * Writes ONLY to its own decisions ledger + backlog.json (when
    operator explicitly approves). No FSM mutation, no provider calls.
  * The dispatcher is purely advisory: it cannot schedule any operation
    itself — approval just appends to backlog.json which the existing
    BacklogSensor will pick up on its next scan, going through every
    standard governance gate (Iron Gate, risk tier, etc.) downstream.
"""
from __future__ import annotations

import json
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


_COMMANDS = {"/backlog"}
_DEFAULT_HISTORY_LIMIT: int = 20


_HELP = (
    "  /backlog auto-proposed [list]                         pending proposals\n"
    "  /backlog auto-proposed show <signature_hash>          full detail of one\n"
    "  /backlog auto-proposed approve <hash> [--reason TEXT] accept → backlog.json\n"
    "  /backlog auto-proposed reject  <hash> [--reason TEXT] reject + blocklist\n"
    "  /backlog auto-proposed history [--limit N]            recent decisions\n"
    "  /backlog auto-proposed help                           this help\n"
)


# ---------------------------------------------------------------------------
# Result + decision dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BacklogAutoProposedResult:
    """Mirror of ``PostureDispatchResult`` shape so the SerpentREPL
    dispatcher contract stays consistent across all REPL surfaces."""

    ok: bool
    text: str
    matched: bool = True


@dataclass(frozen=True)
class DecisionRecord:
    """One operator approve/reject decision, persisted to the sidecar
    decisions ledger for audit."""

    signature_hash: str
    decision: str  # "approve" | "reject"
    reason: str
    timestamp_unix: float
    operator: str = "operator"

    def to_ledger_dict(self) -> Dict[str, Any]:
        return {
            "signature_hash": self.signature_hash,
            "decision": self.decision,
            "reason": self.reason,
            "timestamp_unix": self.timestamp_unix,
            "operator": self.operator,
        }


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _proposals_ledger_path(project_root: Path) -> Path:
    return project_root / ".jarvis" / "self_goal_formation_proposals.jsonl"


def _decisions_ledger_path(project_root: Path) -> Path:
    return project_root / ".jarvis" / "self_goal_formation_decisions.jsonl"


def _backlog_path(project_root: Path) -> Path:
    return project_root / ".jarvis" / "backlog.json"


# ---------------------------------------------------------------------------
# Ledger I/O — pure data, never raises
# ---------------------------------------------------------------------------


def _load_proposals(path: Path) -> List[Dict[str, Any]]:
    """Read the proposals JSONL ledger. Returns most-recent N rows in
    source order (newest last). Tolerates malformed lines."""
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict) and d.get("signature_hash"):
            out.append(d)
    return out


def _load_decisions(path: Path) -> List[DecisionRecord]:
    """Read the decisions ledger. Tolerates malformed lines + missing file."""
    if not path.exists():
        return []
    out: List[DecisionRecord] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        sig = str(d.get("signature_hash", "")).strip()
        decision = str(d.get("decision", "")).strip()
        if not sig or decision not in ("approve", "reject"):
            continue
        out.append(DecisionRecord(
            signature_hash=sig,
            decision=decision,
            reason=str(d.get("reason", "")),
            timestamp_unix=float(d.get("timestamp_unix", 0.0) or 0.0),
            operator=str(d.get("operator", "operator")),
        ))
    return out


def _append_decision(path: Path, record: DecisionRecord) -> bool:
    """Append one DecisionRecord to the JSONL ledger. Best-effort."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_ledger_dict()) + "\n")
        return True
    except OSError:
        return False


def _decided_signatures(decisions: List[DecisionRecord]) -> Set[str]:
    return {d.signature_hash for d in decisions}


def _pending_proposals(
    proposals: List[Dict[str, Any]],
    decisions: List[DecisionRecord],
) -> List[Dict[str, Any]]:
    """Return proposals that have NOT yet been decided. Newest-first."""
    decided = _decided_signatures(decisions)
    pending = [p for p in proposals if p.get("signature_hash") not in decided]
    pending.sort(
        key=lambda p: float(p.get("timestamp_unix", 0.0) or 0.0),
        reverse=True,
    )
    return pending


def _find_proposal(
    signature_hash: str, proposals: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    sh = signature_hash.strip().lower()
    for p in proposals:
        if str(p.get("signature_hash", "")).strip().lower() == sh:
            return p
    return None


def _append_to_backlog_json(path: Path, entry: Dict[str, Any]) -> bool:
    """Append a single entry to backlog.json. Creates the file if missing
    (with empty array). Best-effort, never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: List[Any] = []
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                parsed = json.loads(raw) if raw.strip() else []
                if isinstance(parsed, list):
                    existing = parsed
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append(entry)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------


def _extract_reason(tokens: List[str]) -> Tuple[List[str], str]:
    """Strip a ``--reason TEXT`` flag from tokens. Returns (remaining,
    reason). Reason may span multiple tokens after the flag."""
    if "--reason" not in tokens:
        return (tokens, "")
    idx = tokens.index("--reason")
    remaining = tokens[:idx]
    reason_parts = tokens[idx + 1 :]
    return (remaining, " ".join(reason_parts).strip())


def _matches(line: str) -> bool:
    if not line:
        return False
    parts = line.split()
    if len(parts) < 2:
        return False
    if parts[0] not in _COMMANDS:
        return False
    # Only "/backlog auto-proposed ..." routes here. Other "/backlog"
    # subcommands can coexist when added — they fall through with matched=False.
    return parts[1].lower() == "auto-proposed"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_age(ts_unix: float, now: Optional[float] = None) -> str:
    now = now or time.time()
    age_s = max(0.0, now - ts_unix)
    if age_s < 60:
        return f"{int(age_s)}s"
    if age_s < 3600:
        return f"{int(age_s // 60)}m"
    if age_s < 86400:
        return f"{int(age_s // 3600)}h"
    return f"{int(age_s // 86400)}d"


def _render_pending_table(pending: List[Dict[str, Any]]) -> str:
    if not pending:
        return "  /backlog auto-proposed: no pending proposals.\n"
    lines = [
        f"  Pending auto-proposed ({len(pending)} total):",
        f"  {'sig':<14s} {'count':>5s} {'posture':<13s} {'age':>6s}  description",
    ]
    for p in pending:
        sig = str(p.get("signature_hash", ""))[:12]
        count = int(p.get("cluster_member_count", 0) or 0)
        posture = str(p.get("posture_at_proposal", "?"))[:13]
        age = _format_age(float(p.get("timestamp_unix", 0.0) or 0.0))
        desc = str(p.get("description", ""))[:60]
        lines.append(
            f"  {sig:<14s} {count:>5d} {posture:<13s} {age:>6s}  {desc}"
        )
    lines.append("")
    lines.append(
        "  Use `/backlog auto-proposed show <sig>` for full detail, or "
        "`approve` / `reject` to act."
    )
    return "\n".join(lines) + "\n"


def _render_proposal_detail(p: Dict[str, Any]) -> str:
    files = p.get("target_files", []) or []
    files_str = (
        "\n    " + "\n    ".join(str(f) for f in files)
        if files else "(none)"
    )
    return "\n".join([
        f"  Auto-proposed entry: {p.get('signature_hash', '?')}",
        f"  Description: {p.get('description', '')}",
        f"  Posture at proposal: {p.get('posture_at_proposal', '?')}",
        f"  Cluster size: {int(p.get('cluster_member_count', 0) or 0)}",
        f"  Cost spent: ${float(p.get('cost_usd_spent', 0.0) or 0.0):.4f}",
        f"  Schema: {p.get('schema_version', '?')}",
        f"  Target files:{files_str}",
        "",
        f"  Rationale: {p.get('rationale', '')}",
        "",
    ]) + "\n"


def _render_history(decisions: List[DecisionRecord], limit: int) -> str:
    if not decisions:
        return "  /backlog auto-proposed: no decisions yet.\n"
    sorted_d = sorted(
        decisions, key=lambda d: d.timestamp_unix, reverse=True,
    )[:limit]
    lines = [f"  Last {len(sorted_d)} decision(s):"]
    for d in sorted_d:
        lines.append(
            f"    [{d.decision:<7s}] {d.signature_hash[:12]:<14s} "
            f"{_format_age(d.timestamp_unix):>6s} ago  reason={d.reason!r}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _handle_list(project_root: Path) -> BacklogAutoProposedResult:
    proposals = _load_proposals(_proposals_ledger_path(project_root))
    decisions = _load_decisions(_decisions_ledger_path(project_root))
    pending = _pending_proposals(proposals, decisions)
    return BacklogAutoProposedResult(ok=True, text=_render_pending_table(pending))


def _handle_show(
    project_root: Path, args: List[str],
) -> BacklogAutoProposedResult:
    if not args:
        return BacklogAutoProposedResult(
            ok=False,
            text="  /backlog auto-proposed show: missing <signature_hash>.\n",
        )
    sig = args[0]
    proposals = _load_proposals(_proposals_ledger_path(project_root))
    found = _find_proposal(sig, proposals)
    if found is None:
        return BacklogAutoProposedResult(
            ok=False,
            text=f"  /backlog auto-proposed show: no proposal with signature {sig!r}.\n",
        )
    return BacklogAutoProposedResult(ok=True, text=_render_proposal_detail(found))


def _handle_decision(
    project_root: Path,
    decision: str,
    args: List[str],
) -> BacklogAutoProposedResult:
    """Shared approve/reject handler. ``decision`` ∈ {"approve", "reject"}."""
    args, reason = _extract_reason(args)
    if not args:
        return BacklogAutoProposedResult(
            ok=False,
            text=f"  /backlog auto-proposed {decision}: missing <signature_hash>.\n",
        )
    sig = args[0]
    proposals = _load_proposals(_proposals_ledger_path(project_root))
    found = _find_proposal(sig, proposals)
    if found is None:
        return BacklogAutoProposedResult(
            ok=False,
            text=f"  /backlog auto-proposed {decision}: no proposal with signature {sig!r}.\n",
        )

    decisions = _load_decisions(_decisions_ledger_path(project_root))
    if found["signature_hash"] in _decided_signatures(decisions):
        prior = next(
            (d for d in decisions if d.signature_hash == found["signature_hash"]),
            None,
        )
        prior_str = (
            f"already {prior.decision}d at "
            f"{_format_age(prior.timestamp_unix)} ago"
            if prior else "already decided"
        )
        return BacklogAutoProposedResult(
            ok=False,
            text=f"  /backlog auto-proposed {decision}: {prior_str}.\n",
        )

    record = DecisionRecord(
        signature_hash=str(found["signature_hash"]),
        decision=decision,
        reason=reason,
        timestamp_unix=time.time(),
    )
    if not _append_decision(_decisions_ledger_path(project_root), record):
        return BacklogAutoProposedResult(
            ok=False,
            text="  /backlog auto-proposed: failed to write decisions ledger.\n",
        )

    if decision == "approve":
        # Persist as a real backlog.json entry (BacklogSensor scan
        # picks it up on next cycle). Preserve auto_proposed flag so
        # downstream surfaces can filter on it.
        entry = {
            "task_id": f"auto-proposed:{found['signature_hash']}",
            "description": found.get("description", ""),
            "target_files": list(found.get("target_files", []) or []),
            "priority": 3,
            "repo": "jarvis",
            "status": "pending",
            "auto_proposed": True,
            "approved_signature_hash": found["signature_hash"],
            "approval_reason": reason,
            "approval_timestamp_unix": record.timestamp_unix,
        }
        if not _append_to_backlog_json(_backlog_path(project_root), entry):
            return BacklogAutoProposedResult(
                ok=False,
                text="  /backlog auto-proposed: decision recorded but backlog.json append failed.\n",
            )
        logger.info(
            "[BacklogAutoProposed] APPROVED signature=%s reason=%r",
            found["signature_hash"], reason,
        )
        return BacklogAutoProposedResult(
            ok=True,
            text=(
                f"  /backlog auto-proposed: APPROVED {found['signature_hash']} "
                f"→ appended to backlog.json (BacklogSensor will pick up "
                f"on next scan).\n"
            ),
        )

    # reject
    logger.info(
        "[BacklogAutoProposed] REJECTED signature=%s reason=%r",
        found["signature_hash"], reason,
    )
    return BacklogAutoProposedResult(
        ok=True,
        text=(
            f"  /backlog auto-proposed: REJECTED {found['signature_hash']} "
            f"(signature added to operator-decisions blocklist).\n"
        ),
    )


def _handle_history(
    project_root: Path, args: List[str],
) -> BacklogAutoProposedResult:
    limit = _DEFAULT_HISTORY_LIMIT
    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            try:
                limit = max(1, int(args[idx + 1]))
            except ValueError:
                pass
    decisions = _load_decisions(_decisions_ledger_path(project_root))
    return BacklogAutoProposedResult(
        ok=True, text=_render_history(decisions, limit),
    )


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def dispatch_backlog_auto_proposed_command(
    line: str,
    *,
    project_root: Optional[Path] = None,
) -> BacklogAutoProposedResult:
    """Parse a ``/backlog auto-proposed ...`` line and dispatch.

    Returns a result with ``matched=False`` when the line doesn't begin
    ``/backlog auto-proposed`` so the SerpentREPL fallthrough can try
    other dispatchers (matches the posture_repl / cost_repl pattern)."""
    if not _matches(line):
        return BacklogAutoProposedResult(ok=False, text="", matched=False)

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return BacklogAutoProposedResult(
            ok=False, text=f"  /backlog parse error: {exc}\n",
        )

    # tokens = ["/backlog", "auto-proposed", subcommand?, ...args]
    if len(tokens) < 2:
        return BacklogAutoProposedResult(ok=False, text="", matched=False)

    args = tokens[2:]
    head = (args[0].lower() if args else "list")
    rest = args[1:] if args else []

    root = project_root or Path.cwd()

    if head in ("help", "?"):
        return BacklogAutoProposedResult(ok=True, text=_HELP)
    if head == "list":
        return _handle_list(root)
    if head == "show":
        return _handle_show(root, rest)
    if head == "approve":
        return _handle_decision(root, "approve", rest)
    if head == "reject":
        return _handle_decision(root, "reject", rest)
    if head == "history":
        return _handle_history(root, rest)

    return BacklogAutoProposedResult(
        ok=False,
        text=(
            f"  /backlog auto-proposed: unknown subcommand {head!r}. "
            f"Run `help` for usage.\n"
        ),
    )


__all__ = [
    "BacklogAutoProposedResult",
    "DecisionRecord",
    "dispatch_backlog_auto_proposed_command",
]
