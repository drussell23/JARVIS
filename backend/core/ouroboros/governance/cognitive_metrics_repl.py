"""Phase 4 P3 Slice 1 — `/cognitive` REPL surface.

Read-only operator surface for inspecting the CognitiveMetricsService
audit ledger. Mirrors the BacklogAutoProposedResult / HypothesisDispatchResult
shape so SerpentREPL fallthrough handles it uniformly.

Commands::

  /cognitive                                    # alias for `stats`
  /cognitive stats                              # aggregate counts + means
  /cognitive list [--limit N]                   # most-recent N rows
  /cognitive show <op_id>                       # all rows for one op_id
  /cognitive pre-scores [--limit N]             # only pre_score rows
  /cognitive vindications [--limit N]           # only vindication rows
  /cognitive help

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * REPL is purely read-only on the ledger. Slice 2 will wire the
    orchestrator to call ``score_pre_apply`` / ``reflect_post_apply``;
    that's the only write path.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from backend.core.ouroboros.governance.cognitive_metrics import (
    CognitiveMetricRecord,
    CognitiveMetricsService,
)


_COMMANDS = {"/cognitive"}
_DEFAULT_LIST_LIMIT: int = 20


_HELP = (
    "  /cognitive [stats]                          aggregate counts + means\n"
    "  /cognitive list [--limit N]                 most-recent N rows\n"
    "  /cognitive show <op_id>                     all rows for one op_id\n"
    "  /cognitive pre-scores [--limit N]           only pre_score rows\n"
    "  /cognitive vindications [--limit N]         only vindication rows\n"
    "  /cognitive help                             this help\n"
)


@dataclass
class CognitiveDispatchResult:
    """Mirror of ``HypothesisDispatchResult`` shape."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _gate_glyph(gate: Optional[str]) -> str:
    if gate == "FAST_TRACK":
        return "→"
    if gate == "WARN":
        return "!"
    if gate == "NORMAL":
        return "·"
    return " "


def _advisory_glyph(advisory: Optional[str]) -> str:
    if advisory == "vindicating":
        return "✓"
    if advisory == "warning":
        return "!"
    if advisory == "concerning":
        return "?"
    if advisory == "neutral":
        return "·"
    return " "


def _render_record_row(rec: CognitiveMetricRecord) -> str:
    op_short = (rec.op_id or "?")[:22]
    if rec.kind == "pre_score":
        score = rec.pre_score if rec.pre_score is not None else 0.0
        glyph = _gate_glyph(rec.pre_score_gate)
        return (
            f"  {glyph} pre_score    {op_short:<22s}  "
            f"score={score:+.3f}  gate={rec.pre_score_gate or '?':<10s}  "
            f"files={len(rec.target_files)}"
        )
    if rec.kind == "vindication":
        score = rec.vindication_score if rec.vindication_score is not None else 0.0
        glyph = _advisory_glyph(rec.vindication_advisory)
        return (
            f"  {glyph} vindication  {op_short:<22s}  "
            f"score={score:+.3f}  advisory={rec.vindication_advisory or '?':<11s}  "
            f"files={len(rec.target_files)}"
        )
    return f"  ? unknown      {op_short:<22s}"


def _render_list(records: List[CognitiveMetricRecord], title: str) -> str:
    if not records:
        return f"  /cognitive: no {title}.\n"
    sorted_recs = sorted(
        records, key=lambda r: r.timestamp_unix, reverse=True,
    )
    lines = [f"  {title.capitalize()} ({len(records)} total):"]
    for rec in sorted_recs:
        lines.append(_render_record_row(rec))
    return "\n".join(lines) + "\n"


def _render_stats(service: CognitiveMetricsService) -> str:
    s = service.stats()
    mps = (
        f"{s['mean_pre_score']:+.3f}"
        if s["mean_pre_score"] is not None else "n/a"
    )
    mvs = (
        f"{s['mean_vindication_score']:+.3f}"
        if s["mean_vindication_score"] is not None else "n/a"
    )
    gate_str = (
        ", ".join(f"{k}={v}" for k, v in sorted(s["gate_counts"].items()))
        or "(none)"
    )
    advisory_str = (
        ", ".join(
            f"{k}={v}" for k, v in sorted(s["advisory_counts"].items())
        )
        or "(none)"
    )
    return (
        f"  Cognitive metrics ledger stats:\n"
        f"    total rows:           {s['total']}\n"
        f"    pre_score count:      {s['pre_score_count']}\n"
        f"    vindication count:    {s['vindication_count']}\n"
        f"    mean pre_score:       {mps}\n"
        f"    mean vindication:     {mvs}\n"
        f"    pre_score gates:      {gate_str}\n"
        f"    vindication advisory: {advisory_str}\n"
    )


def _render_show(records: List[CognitiveMetricRecord], op_id: str) -> str:
    matching = [r for r in records if r.op_id == op_id]
    if not matching:
        return f"  /cognitive show: no rows for op_id {op_id!r}.\n"
    lines = [f"  Records for op_id={op_id} ({len(matching)} total):"]
    for r in matching:
        lines.append(_render_record_row(r))
        if r.subsignals:
            for k, v in r.subsignals.items():
                try:
                    lines.append(f"      {k:<20s} {float(v):+.3f}")
                except (TypeError, ValueError):
                    pass
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------


def _extract_limit(args: List[str]) -> int:
    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            try:
                return max(1, int(args[idx + 1]))
            except ValueError:
                pass
    return _DEFAULT_LIST_LIMIT


def _matches(line: str) -> bool:
    if not line:
        return False
    parts = line.split()
    return bool(parts) and parts[0] in _COMMANDS


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_cognitive_command(
    line: str,
    *,
    project_root: Optional[Path] = None,
    service: Optional[CognitiveMetricsService] = None,
) -> CognitiveDispatchResult:
    """Parse `/cognitive ...` and dispatch.

    Tests inject ``service`` directly; production resolves a singleton
    via ``cognitive_metrics.get_default_service``."""
    if not _matches(line):
        return CognitiveDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CognitiveDispatchResult(
            ok=False, text=f"  /cognitive parse error: {exc}\n",
        )
    if not tokens:
        return CognitiveDispatchResult(ok=False, text="", matched=False)

    args = tokens[1:]
    head = args[0].lower() if args else "stats"
    rest = args[1:] if args else []

    if head in ("help", "?"):
        return CognitiveDispatchResult(ok=True, text=_HELP)

    resolved = service
    if resolved is None:
        from backend.core.ouroboros.governance.cognitive_metrics import (
            get_default_service,
        )
        resolved = get_default_service(project_root=project_root or Path.cwd())
        if resolved is None:
            return CognitiveDispatchResult(
                ok=False,
                text=(
                    "  /cognitive: service not initialised. Set "
                    "JARVIS_COGNITIVE_METRICS_ENABLED=true and ensure "
                    "the orchestrator boot has wired the singleton.\n"
                ),
            )

    if head == "stats":
        return CognitiveDispatchResult(ok=True, text=_render_stats(resolved))
    if head == "list":
        records = resolved.load_records()
        limit = _extract_limit(rest)
        sliced = sorted(
            records, key=lambda r: r.timestamp_unix, reverse=True,
        )[:limit]
        return CognitiveDispatchResult(
            ok=True, text=_render_list(sliced, "metric records"),
        )
    if head == "show":
        if not rest:
            return CognitiveDispatchResult(
                ok=False,
                text="  /cognitive show: missing <op_id>.\n",
            )
        return CognitiveDispatchResult(
            ok=True,
            text=_render_show(resolved.load_records(), rest[0]),
        )
    if head == "pre-scores":
        records = [r for r in resolved.load_records() if r.kind == "pre_score"]
        limit = _extract_limit(rest)
        sliced = sorted(
            records, key=lambda r: r.timestamp_unix, reverse=True,
        )[:limit]
        return CognitiveDispatchResult(
            ok=True, text=_render_list(sliced, "pre-score rows"),
        )
    if head == "vindications":
        records = [
            r for r in resolved.load_records() if r.kind == "vindication"
        ]
        limit = _extract_limit(rest)
        sliced = sorted(
            records, key=lambda r: r.timestamp_unix, reverse=True,
        )[:limit]
        return CognitiveDispatchResult(
            ok=True, text=_render_list(sliced, "vindication rows"),
        )

    return CognitiveDispatchResult(
        ok=False,
        text=(
            f"  /cognitive: unknown subcommand {head!r}. "
            f"Run `help` for usage.\n"
        ),
    )


__all__ = [
    "CognitiveDispatchResult",
    "dispatch_cognitive_command",
]
