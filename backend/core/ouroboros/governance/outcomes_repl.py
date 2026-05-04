"""M11 Slice 5 — ``/outcomes`` REPL dispatcher (PRD §30.5.3).

Operator-facing CLI surface — symmetric pair to
:mod:`failures_repl` (Upgrade 3 Slice 5). Same patterns:
``register_verbs`` for /help auto-discovery, lazy
``action_outcome_memory`` import, frozen ``*DispatchResult``,
operator-explainability via per-component score surfacing.

Subcommands:

  * ``/outcomes``                  — alias for ``/outcomes top``
  * ``/outcomes top [N]``          — last N records, recency-
    sorted (default 20)
  * ``/outcomes for-cluster <id>`` — records keyed to one
    SemanticIndex cluster (or ``_global`` for the fallback)
  * ``/outcomes for-region <files...>`` — recall via
    :func:`recall_for_region` (top-K with full scoring)
  * ``/outcomes config``           — env-knob snapshot
  * ``/outcomes clear``            — truncate all cluster JSONLs
  * ``/outcomes help``             — usage listing (always
    available; bypasses master-flag gate)

Master gate: :func:`action_outcome_memory_enabled`. Auto-
discovered by :func:`help_dispatcher._discover_module_provided_-
verbs`. NEVER raises.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + ``action_outcome_memory`` ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor.
  * Read-only EXCEPT for ``clear`` subcommand (operator-explicit
    maintenance — same boundary discipline as ``/failures clear``).
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

from backend.core.ouroboros.governance.action_outcome_memory import (
    ACTION_OUTCOME_MEMORY_SCHEMA_VERSION,
    ActionOutcomeRecord,
    action_outcome_memory_enabled,
    action_outcome_min_weight,
    action_outcome_polarity_mode,
    action_outcome_recency_halflife_days,
    action_outcome_top_k,
    clear_action_outcomes,
    cluster_jsonl_path,
    dedup_window_days,
    history_dir,
    max_records_per_cluster,
    read_action_outcomes_for_cluster,
    read_all_action_outcomes,
    recall_for_region,
)

logger = logging.getLogger(__name__)


_HELP = (
    "/outcomes — Action-Outcome Memory (M11 / PRD §30.5.3)\n"
    "\n"
    "Subcommands:\n"
    "  /outcomes                     alias for /outcomes top\n"
    "  /outcomes top [N]             last N records (default 20)\n"
    "  /outcomes for-cluster <id>    records in one cluster\n"
    "  /outcomes for-region <files>  recall via region scoring\n"
    "  /outcomes config              env-knob snapshot\n"
    "  /outcomes clear               truncate the JSONL store\n"
    "  /outcomes help                this text\n"
    "\n"
    "Master flag: JARVIS_ACTION_OUTCOME_MEMORY_ENABLED (graduated\n"
    "default-TRUE post Slice 5; flip to false for instant revert)\n"
    "Live HTTP surface: GET /observability/action-outcomes[/...]\n"
    "Live SSE event:    action_outcome_recalled_at_generate\n"
)

_DEFAULT_TOP_LIMIT: int = 20
_MAX_TOP_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomesDispatchResult:
    """Result of an ``/outcomes`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't an
    ``/outcomes`` invocation at all (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return s == "/outcomes" or s == "outcomes" or (
        s.startswith("/outcomes ") or s.startswith("outcomes ")
    )


def _parse_limit(args: List[str]) -> int:
    if len(args) < 2:
        return _DEFAULT_TOP_LIMIT
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > _MAX_TOP_LIMIT:
            return _MAX_TOP_LIMIT
        return n
    except (TypeError, ValueError):
        return _DEFAULT_TOP_LIMIT


def dispatch_outcomes_command(line: str) -> OutcomesDispatchResult:
    """Parse an ``/outcomes`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return OutcomesDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return OutcomesDispatchResult(
            ok=False, text=f"  /outcomes parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "top")

    if head in ("help", "?"):
        return OutcomesDispatchResult(ok=True, text=_HELP)

    if not action_outcome_memory_enabled():
        return OutcomesDispatchResult(
            ok=False,
            text=(
                "  /outcomes: ActionOutcomeMemory disabled — set "
                "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED=true"
            ),
        )

    if head == "top":
        return _render_top(_parse_limit(args))
    if head == "for-cluster":
        if len(args) < 2:
            return OutcomesDispatchResult(
                ok=False,
                text=(
                    "  /outcomes for-cluster <id>: missing "
                    "cluster_id argument."
                ),
            )
        return _render_for_cluster(args[1])
    if head == "for-region":
        if len(args) < 2:
            return OutcomesDispatchResult(
                ok=False,
                text=(
                    "  /outcomes for-region <file...>: missing "
                    "target_files arguments."
                ),
            )
        return _render_for_region(tuple(args[1:]))
    if head == "clear":
        return _render_clear()
    if head == "config":
        return _render_config()
    return OutcomesDispatchResult(
        ok=False,
        text=(
            f"  /outcomes: unknown subcommand {head!r}. "
            f"Try /outcomes help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_record_summary(rec: ActionOutcomeRecord) -> str:
    sig_short = (rec.signature_hash or "")[:12]
    outcome = rec.outcome_kind.value
    attempt = rec.attempted_action_kind or "unspecified"
    cluster = rec.cluster_id or "_global"
    commit_seg = (
        f" commit=`{rec.commit_hash[:12]}`"
        if rec.commit_hash else ""
    )
    return (
        f"  • {outcome:<22} `{attempt:<22}` "
        f"weight={rec.weight} cluster=`{cluster}` "
        f"sig=`{sig_short}`{commit_seg}"
    )


def _render_top(limit: int) -> OutcomesDispatchResult:
    try:
        history = read_all_action_outcomes(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[outcomes_repl] _render_top raised: %s", exc,
        )
        history = ()
    lines: List[str] = [
        f"/outcomes top — last {limit} records "
        f"({len(history)} found)",
        "",
    ]
    if not history:
        lines.append("  (no records yet)")
        lines.append("")
        return OutcomesDispatchResult(
            ok=True, text="\n".join(lines),
        )
    # Reverse-chronological display
    for rec in reversed(history):
        try:
            lines.append(_format_record_summary(rec))
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  • (corrupt record — skipped)")
    lines.append("")
    return OutcomesDispatchResult(ok=True, text="\n".join(lines))


def _render_for_cluster(cluster_id: str) -> OutcomesDispatchResult:
    try:
        records = read_action_outcomes_for_cluster(cluster_id)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[outcomes_repl] _render_for_cluster raised: %s",
            exc,
        )
        records = ()
    cluster_display = cluster_id or "_global"
    lines: List[str] = [
        f"/outcomes for-cluster `{cluster_display}` — "
        f"{len(records)} record(s)",
        f"  path: {cluster_jsonl_path(cluster_id)}",
        "",
    ]
    if not records:
        lines.append("  (no records in this cluster)")
        lines.append("")
        return OutcomesDispatchResult(
            ok=True, text="\n".join(lines),
        )
    for rec in reversed(records):
        try:
            lines.append(_format_record_summary(rec))
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  • (corrupt record — skipped)")
    lines.append("")
    return OutcomesDispatchResult(ok=True, text="\n".join(lines))


def _render_for_region(
    target_files: tuple,
) -> OutcomesDispatchResult:
    try:
        matches = recall_for_region(
            target_files=target_files,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[outcomes_repl] _render_for_region raised: %s", exc,
        )
        matches = ()
    files_display = ", ".join(target_files)
    lines: List[str] = [
        f"/outcomes for-region `{files_display}` — "
        f"{len(matches)} match(es) at top_k={action_outcome_top_k()}",
        "",
    ]
    if not matches:
        lines.append("  (no matches — empty history or "
                     "below min_weight)")
        lines.append("")
        return OutcomesDispatchResult(
            ok=True, text="\n".join(lines),
        )
    # Per-component score surface — operator-explainability
    for m in matches:
        rec = m.record
        sig_short = (rec.signature_hash or "")[:12]
        commit_seg = (
            f" commit=`{rec.commit_hash[:12]}`"
            if rec.commit_hash else ""
        )
        lines.append(
            f"  • {rec.outcome_kind.value:<22} "
            f"`{rec.attempted_action_kind:<22}` "
            f"recency={m.recency_score:.2f} "
            f"jaccard={m.jaccard_score:.2f} "
            f"weight={m.weight_score:.2f} "
            f"polarity={m.polarity_score:.2f} "
            f"combined={m.combined_score:.3f} "
            f"sig=`{sig_short}`{commit_seg}"
        )
        if rec.summary:
            lines.append(f"    {rec.summary[:160]}")
    lines.append("")
    return OutcomesDispatchResult(ok=True, text="\n".join(lines))


def _render_clear() -> OutcomesDispatchResult:
    try:
        ok = clear_action_outcomes()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[outcomes_repl] _render_clear raised: %s", exc,
        )
        ok = False
    if ok:
        return OutcomesDispatchResult(
            ok=True,
            text=(
                f"/outcomes clear — store truncated under "
                f"{history_dir()}\n"
            ),
        )
    return OutcomesDispatchResult(
        ok=False,
        text=(
            "/outcomes clear — failed (master off, "
            "flock contention, or disk fault).\n"
        ),
    )


def _render_config() -> OutcomesDispatchResult:
    lines: List[str] = [
        "/outcomes config — env-knob snapshot",
        "",
        f"  schema_version              "
        f"{ACTION_OUTCOME_MEMORY_SCHEMA_VERSION}",
        f"  master_enabled              "
        f"{action_outcome_memory_enabled()}",
        f"  history_dir                 {history_dir()}",
        f"  max_records_per_cluster     "
        f"{max_records_per_cluster()}",
        f"  dedup_window_days           {dedup_window_days()}",
        f"  retrieval_top_k             {action_outcome_top_k()}",
        f"  retrieval_min_weight        "
        f"{action_outcome_min_weight()}",
        f"  retrieval_halflife_days     "
        f"{action_outcome_recency_halflife_days()}",
        f"  polarity_mode               "
        f"{action_outcome_polarity_mode()}",
        "",
    ]
    return OutcomesDispatchResult(ok=True, text="\n".join(lines))


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/outcomes`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/outcomes",
            one_line=(
                "Action-outcome memory: top records, cluster + "
                "region recall, store maintenance "
                "(M11 / PRD §30.5.3)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[outcomes_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "OutcomesDispatchResult",
    "dispatch_outcomes_command",
    "register_verbs",
]
