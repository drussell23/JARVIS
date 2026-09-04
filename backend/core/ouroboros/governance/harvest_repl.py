"""``/harvest`` — is the DPO/GRPO flywheel actually collecting anything?

## The gap this closes

O+V's whole local-lane arc exists to farm a trainable corpus: siblings are
drawn per op, verdicts are joined per candidate, and Reactor-Core trains on
the result. The cockpit had **no surface for any of it**. Thirty-seven
operator verbs, and not one answered "is the harvest working?" — the only
report was a single log line at teardown, by which time a 40-minute soak
has already produced whatever it was going to produce.

That is the wrong direction of blindness, in the sense ``cockpit_mount``
already names: the daemon PRODUCES this state and could not show it. Soak
`bt-2026-09-01-213353` ran 36 minutes, wrote **zero** rows, and looked
identical from the cockpit to a soak that was harvesting perfectly.

## Why it reports GROUPS, not rows

``record_generation`` fires per PROVIDER CALL, and ``_extend_with_siblings``
sits above it, so each sibling draw writes its own row carrying
``n_candidates=1``. A sibling group is therefore rows sharing an ``op_id``
with different ``attempt_index`` — and a row counter says nothing about
whether any of them can train.

Measured 2026-09-01: 8 sibling rows across 3 groups carried 3 structurally
distinct answers; every group collapsed to one, so zero preference pairs
were constructible while the row count looked healthy. ``groups_pairable``
is the number that decides trainability, so it is the number this prints
first. Rows climbing while ``groups_pairable`` stays 0 is the failure mode,
and it must be legible at a glance.

Read-only. Never mutates, never generates, never trains. Auto-discovered by
``repl_dispatch_registry`` (file ``harvest_repl.py`` → verb ``harvest``), so
the daemon cockpit and the attach cockpit get it from one definition and
cannot drift.

Authority invariants:
  * stdlib + ``observability.trajectory_recorder`` + ``sibling_entropy``.
  * NEVER imports orchestrator / phase_runners / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian / providers /
    urgency_router / tool_executor.
  * NEVER raises.
"""
from __future__ import annotations

import collections
import json
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

HARVEST_REPL_SCHEMA_VERSION = "harvest_repl.1"

_HELP = (
    "/harvest — DPO/GRPO corpus harvest state\n"
    "\n"
    "Subcommands:\n"
    "  /harvest              alias for /harvest status\n"
    "  /harvest status       recorder counters + corpus trainability\n"
    "  /harvest groups [N]   per-group structural breakdown (default 12)\n"
    "  /harvest help         this text\n"
    "\n"
    "A GROUP is the rows of one op_id (one row per sibling draw). Only a\n"
    "group holding 2+ structurally distinct answers can yield a preference\n"
    "pair — rows alone prove nothing.\n"
    "\n"
    "Master flag: JARVIS_TRAJECTORY_RECORDER_ENABLED\n"
    "Diversity:   JARVIS_SIBLING_ENTROPY_ENABLED / "
    "JARVIS_SIBLING_DIVERSITY_THRESHOLD\n"
)

_DEFAULT_LIMIT = 12
_MAX_LIMIT = 200

#: Counters worth showing first: each one names a distinct way the join
#: between a generation and its verdict can fail silently.
_KEY_COUNTERS = (
    "generations_queued", "events_written", "candidate_verdicts_queued",
    "candidate_verdicts_joined", "orphan_candidate_verdicts",
    "orphan_outcomes", "pending_open", "pending_expired",
    "dropped_queue_full", "dropped_no_loop", "write_failures",
)


@dataclass(frozen=True)
class HarvestDispatchResult:
    """``matched=False`` means the line was not a ``/harvest`` invocation."""

    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return s in ("/harvest", "harvest") or (
        s.startswith("/harvest ") or s.startswith("harvest ")
    )


def _parse_limit(args: List[str]) -> int:
    if len(args) < 2:
        return _DEFAULT_LIMIT
    try:
        n = int(args[1])
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, n))


def _render_status() -> str:
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501,PLC0415
        harvest_snapshot,
    )

    snap = harvest_snapshot()
    lines: List[str] = ["  /harvest — corpus harvest state", ""]
    state = "ON" if snap.get("enabled") else "OFF"
    lines.append(f"    recorder      {state}   {snap.get('path', '')}")
    if not snap.get("enabled"):
        lines.append(
            "                  (set JARVIS_TRAJECTORY_RECORDER_ENABLED=true "
            "— nothing is being written)"
        )

    groups = int(snap.get("groups", 0) or 0)
    pairable = int(snap.get("groups_pairable", 0) or 0)
    collapsed = int(snap.get("groups_collapsed", 0) or 0)
    lines.append("")
    lines.append(
        f"    rows          {snap.get('rows', 0)}"
        f"   ({snap.get('rows_trainable', 0)} trainable)"
        + ("   [truncated]" if snap.get("truncated") else "")
    )
    # The headline. A group that collapsed cannot become a preference pair
    # however many rows it wrote, so this is the only line that answers
    # "is the soak producing training signal".
    lines.append(
        f"    groups        {groups}"
        f"   PAIRABLE={pairable}   collapsed={collapsed}"
    )
    if groups and not pairable:
        lines.append(
            "                  ^ every group holds ONE answer — no "
            "preference pair is constructible"
        )

    counters: Dict[str, Any] = snap.get("counters") or {}
    if counters:
        lines.append("")
        lines.append("    recorder counters")
        for key in _KEY_COUNTERS:
            if key in counters:
                lines.append(f"      {key:<28} {counters[key]}")
    if snap.get("error"):
        lines.append("")
        lines.append(f"    read degraded: {snap['error']}")
    lines.append("")
    return "\n".join(lines)


def _render_groups(limit: int) -> str:
    """Per-group structural breakdown, largest first.

    Prints the MINIMUM pairwise structural similarity, not the maximum.
    A group is pairable when its two most DISTINCT answers differ, so the
    minimum is the statistic the verdict column actually follows; the
    maximum reads 1.0000 for any group containing one duplicate and would
    sit beside "PAIRABLE" looking like a contradiction.
    """
    from backend.core.ouroboros.governance import (  # noqa: PLC0415
        sibling_entropy as ent,
    )
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501,PLC0415
        events_dir,
    )

    path = events_dir()
    groups: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    if path.is_dir():
        for f in sorted(path.glob("*.jsonl")):
            try:
                fh = f.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if row.get("event_type") != "interaction":
                        continue
                    meta = row.get("metadata") or {}
                    key = str(
                        meta.get("op_id")
                        or meta.get("prompt_key")
                        or (row.get("user_input") or "")[:60]
                    )
                    groups[key].append(row)

    multi = sorted(
        ((k, v) for k, v in groups.items() if len(v) > 1),
        key=lambda kv: -len(kv[1]),
    )
    if not multi:
        return (
            "  /harvest groups: no group has 2+ rows yet.\n"
            "  A sibling group is rows sharing an op_id — if this stays "
            "empty while ops complete,\n  siblings are not being drawn "
            "(check JARVIS_LOCAL_SIBLING_CANDIDATES).\n"
        )

    out: List[str] = [
        f"  /harvest groups — {len(multi)} group(s) with 2+ rows "
        f"(showing {min(limit, len(multi))})",
        "",
        f"    {'op':<20} {'rows':>4} {'answers':>7} {'min_sim':>8}  verdict",
    ]
    for key, members in multi[:limit]:
        fps = []
        # A REFUSAL is its own answer class. Its body is a decline
        # envelope, not Python, so `structural_fingerprint` returns None
        # and it would be dropped from the set entirely -- making a
        # {refusal, patch} group report one answer and read "collapsed",
        # which is precisely the "rows healthy, pairs zero" blindness this
        # view exists to expose. The recorder already decided this and
        # stamped `metadata.structure_id`; trust that rather than
        # re-deriving a second opinion here.
        n_refusals = 0
        for row in members:
            meta = row.get("metadata") or {}
            if str(meta.get("candidate_status", "") or "") == "noop":
                n_refusals += 1
                continue
            fp = ent.structural_fingerprint(row.get("assistant_output") or "")
            if fp is not None:
                fps.append(fp)
        distinct = len({*fps}) + (1 if n_refusals else 0)
        sims = [
            ent.structural_similarity(fps[i], fps[j])
            for i in range(len(fps))
            for j in range(i + 1, len(fps))
        ]
        low = min(sims) if sims else 1.0
        verdict = "PAIRABLE" if distinct > 1 else "collapsed"
        out.append(
            f"    {key[:20]:<20} {len(members):>4} {distinct:>7} "
            f"{low:>8.4f}  {verdict}"
        )
    out.append("")
    out.append(
        f"    threshold {ent.diversity_threshold():.2f} — at or above it two "
        "answers count as one"
    )
    out.append("")
    return "\n".join(out)


def dispatch_harvest_command(line: str) -> HarvestDispatchResult:
    """Report DPO/GRPO harvest state. Read-only. NEVER raises."""
    if not _matches(line):
        return HarvestDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return HarvestDispatchResult(
            ok=False, text=f"  /harvest parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    try:
        if head in ("help", "?"):
            return HarvestDispatchResult(ok=True, text=_HELP)
        if head == "groups":
            return HarvestDispatchResult(
                ok=True, text=_render_groups(_parse_limit(args)),
            )
        if head in ("status", "stats"):
            return HarvestDispatchResult(ok=True, text=_render_status())
        return HarvestDispatchResult(
            ok=False,
            text=f"  /harvest: unknown subcommand {head!r}\n\n{_HELP}",
        )
    except Exception as exc:  # noqa: BLE001 — an observability verb never raises
        logger.debug("[HarvestRepl] dispatch failed", exc_info=True)
        return HarvestDispatchResult(
            ok=False, text=f"  /harvest degraded: {type(exc).__name__}: {exc}",
        )


__all__ = [
    "HARVEST_REPL_SCHEMA_VERSION",
    "HarvestDispatchResult",
    "dispatch_harvest_command",
]
