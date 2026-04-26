"""P1.5 Slice 1 — `/hypothesis ledger` REPL surface.

The operator-visible surface for inspecting the HypothesisLedger.
Read-only — operators inspect, the engine + validator (Slice 2) write.

Commands::

  /hypothesis ledger                          # alias for `list`
  /hypothesis ledger list                     # all entries newest-first
  /hypothesis ledger pending                  # only open hypotheses
  /hypothesis ledger validated                # only confirmed-true
  /hypothesis ledger invalidated              # only confirmed-false
  /hypothesis ledger show <hypothesis_id>     # full detail
  /hypothesis ledger stats                    # count summary
  /hypothesis ledger help                     # usage

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * No writes — REPL is purely read-only on the ledger. The engine
    (Slice 2) writes via ``HypothesisLedger.append`` /
    ``record_outcome`` directly.
  * Mirrors ``BacklogAutoProposedResult`` shape so SerpentREPL
    fallthrough works without special-casing.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from backend.core.ouroboros.governance.hypothesis_ledger import (
    Hypothesis,
    HypothesisLedger,
)


_COMMANDS = {"/hypothesis"}

_HELP = (
    "  /hypothesis ledger [list]                  pending + validated count\n"
    "  /hypothesis ledger pending                 only open hypotheses\n"
    "  /hypothesis ledger validated               only confirmed-true\n"
    "  /hypothesis ledger invalidated             only confirmed-false\n"
    "  /hypothesis ledger show <hypothesis_id>    full detail of one\n"
    "  /hypothesis ledger stats                   count summary\n"
    "  /hypothesis ledger help                    this help\n"
)


@dataclass
class HypothesisDispatchResult:
    """Mirror of ``BacklogAutoProposedResult`` shape so the SerpentREPL
    fallthrough chain handles all REPL surfaces uniformly."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _status_glyph(h: Hypothesis) -> str:
    if h.is_validated():
        return "✓"
    if h.is_invalidated():
        return "✗"
    if h.is_open():
        return "?"
    return "·"


def _render_table(rows: List[Hypothesis], title: str) -> str:
    if not rows:
        return f"  /hypothesis ledger: no {title}.\n"
    lines = [
        f"  {title.capitalize()} ({len(rows)} total):",
        f"  {'st':<2s} {'id':<14s} {'op_id':<22s}  claim",
    ]
    for h in rows:
        glyph = _status_glyph(h)
        op_short = h.op_id[:20] if h.op_id else "?"
        claim_short = h.claim[:60] if h.claim else "(no claim)"
        lines.append(
            f"  {glyph:<2s} {h.hypothesis_id[:12]:<14s} {op_short:<22s}  {claim_short}"
        )
    lines.append("")
    lines.append(
        "  Use `/hypothesis ledger show <id>` for full detail."
    )
    return "\n".join(lines) + "\n"


def _render_detail(h: Hypothesis) -> str:
    actual = h.actual_outcome if h.actual_outcome is not None else "(pending)"
    if h.validated is True:
        validated_str = "VALIDATED ✓ (predicate held)"
    elif h.validated is False:
        validated_str = "INVALIDATED ✗ (predicate did NOT hold)"
    else:
        validated_str = "PENDING (validator has not yet decided)"
    sig_link = (
        f"\n  Linked proposal: {h.proposed_signature_hash}"
        if h.proposed_signature_hash else ""
    )
    return "\n".join([
        f"  Hypothesis: {h.hypothesis_id}",
        f"  Status: {validated_str}",
        f"  Op ID: {h.op_id}",
        f"  Claim: {h.claim}",
        f"  Expected outcome: {h.expected_outcome}",
        f"  Actual outcome: {actual}",
        f"  Created: {h.created_unix}",
        f"  Validated at: {h.validated_unix if h.validated_unix else '(open)'}",
        f"  Schema: {h.schema_version}{sig_link}",
        "",
    ]) + "\n"


def _render_stats(ledger: HypothesisLedger) -> str:
    s = ledger.stats()
    return (
        f"  Hypothesis ledger stats:\n"
        f"    total:        {s['total']}\n"
        f"    open:         {s['open']}\n"
        f"    validated ✓:  {s['validated']}\n"
        f"    invalidated ✗:{s['invalidated']}\n"
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    parts = line.split()
    if len(parts) < 2:
        return False
    if parts[0] not in _COMMANDS:
        return False
    return parts[1].lower() == "ledger"


def _handle_show(
    ledger: HypothesisLedger, args: List[str],
) -> HypothesisDispatchResult:
    if not args:
        return HypothesisDispatchResult(
            ok=False,
            text="  /hypothesis ledger show: missing <hypothesis_id>.\n",
        )
    found = ledger.find_by_id(args[0])
    if found is None:
        return HypothesisDispatchResult(
            ok=False,
            text=f"  /hypothesis ledger show: no hypothesis with id {args[0]!r}.\n",
        )
    return HypothesisDispatchResult(ok=True, text=_render_detail(found))


def dispatch_hypothesis_command(
    line: str,
    *,
    project_root: Optional[Path] = None,
    ledger: Optional[HypothesisLedger] = None,
) -> HypothesisDispatchResult:
    """Parse `/hypothesis ledger ...` and dispatch.

    Tests inject ``ledger`` directly; production resolves a singleton
    via ``hypothesis_ledger.get_default_ledger`` if ``ledger=None``."""
    if not _matches(line):
        return HypothesisDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return HypothesisDispatchResult(
            ok=False, text=f"  /hypothesis parse error: {exc}\n",
        )
    if len(tokens) < 2:
        return HypothesisDispatchResult(ok=False, text="", matched=False)

    args = tokens[2:]
    head = args[0].lower() if args else "list"
    rest = args[1:] if args else []

    resolved_ledger = ledger
    if resolved_ledger is None:
        from backend.core.ouroboros.governance.hypothesis_ledger import (
            get_default_ledger,
        )
        resolved_ledger = get_default_ledger(
            project_root=project_root or Path.cwd(),
        )

    if head in ("help", "?"):
        return HypothesisDispatchResult(ok=True, text=_HELP)
    if head == "list":
        rows = resolved_ledger.load_all()
        # Newest-first by created_unix
        rows.sort(key=lambda h: h.created_unix, reverse=True)
        return HypothesisDispatchResult(
            ok=True, text=_render_table(rows, "hypotheses"),
        )
    if head == "pending":
        rows = resolved_ledger.find_open()
        rows.sort(key=lambda h: h.created_unix, reverse=True)
        return HypothesisDispatchResult(
            ok=True, text=_render_table(rows, "open hypotheses"),
        )
    if head == "validated":
        rows = resolved_ledger.find_validated()
        rows.sort(key=lambda h: (h.validated_unix or 0.0), reverse=True)
        return HypothesisDispatchResult(
            ok=True, text=_render_table(rows, "validated hypotheses"),
        )
    if head == "invalidated":
        rows = resolved_ledger.find_invalidated()
        rows.sort(key=lambda h: (h.validated_unix or 0.0), reverse=True)
        return HypothesisDispatchResult(
            ok=True, text=_render_table(rows, "invalidated hypotheses"),
        )
    if head == "show":
        return _handle_show(resolved_ledger, rest)
    if head == "stats":
        return HypothesisDispatchResult(
            ok=True, text=_render_stats(resolved_ledger),
        )

    return HypothesisDispatchResult(
        ok=False,
        text=(
            f"  /hypothesis ledger: unknown subcommand {head!r}. "
            f"Run `help` for usage.\n"
        ),
    )


__all__ = [
    "HypothesisDispatchResult",
    "dispatch_hypothesis_command",
]
