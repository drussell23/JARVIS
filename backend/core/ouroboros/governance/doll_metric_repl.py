"""``/doll`` REPL — §40 Tier 1 #15 operator surface (PRD v2.98+).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry`` via
§33.3 naming-cage convention (filename ``doll_metric_repl.py`` →
verb ``doll_metric``; we also accept the shorter ``doll`` alias
through ``_matches`` so operators can type ``/doll`` directly).

Subcommands::

    /doll                              alias for ``status``
    /doll status                       per-axis stage table
    /doll axes                         alias for ``status``
    /doll refresh                      force-recompute snapshot
    /doll show <category>              per-axis detail view
    /doll completion                   single-line completion ratio
    /doll help                         this text

The REPL is a thin browser over
:func:`second_order_doll_metric.aggregate_doll_completion` — zero
parallel state, zero authority. Composes the canonical substrate
via lazy-import; falls through cleanly when the master flag is
off or the substrate is unavailable. NEVER raises.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)


DOLL_METRIC_REPL_SCHEMA_VERSION: str = "doll_metric_repl.1"


_HELP = (
    "/doll — §40 Tier 1 #15 second-order doll completion (PRD)\n"
    "\n"
    "RSI-acceleration probe. Measures how close O+V is to safely\n"
    "completing **second-order self-modification** — modifying\n"
    "its own governance cage (Iron Gate / SemanticGuardian /\n"
    "risk-tier-floor / mutation budget) under operator authority.\n"
    "\n"
    "Stages (ladder, low→high):\n"
    "  ○ UNTOUCHED   no autonomous commits in this axis\n"
    "  · OBSERVED    minimal autonomous activity (approval-gated)\n"
    "  ◌ PROPOSED    autonomous proposals at APPROVAL_REQUIRED\n"
    "  ◐ APPLIED     autonomous landings at SAFE_AUTO|NOTIFY_APPLY\n"
    "  ● GRADUATED   sustained autonomous modifications, safe\n"
    "\n"
    "Subcommands:\n"
    "  /doll                       alias for /status\n"
    "  /doll status                per-axis stage table\n"
    "  /doll axes                  alias for /status\n"
    "  /doll refresh               recompute snapshot now\n"
    "  /doll show <category>       per-axis detail view\n"
    "  /doll completion            single-line completion ratio\n"
    "  /doll help                  this text\n"
    "\n"
    "Master flag: JARVIS_SECOND_ORDER_DOLL_METRIC_ENABLED "
    "(default false per §33.1 graduation contract).\n"
)


@dataclass(frozen=True)
class DollMetricReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = DOLL_METRIC_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        # canonical naming-cage verb
        s == "/doll_metric"
        or s == "doll_metric"
        or s.startswith("/doll_metric ")
        or s.startswith("doll_metric ")
        # short alias (operator ergonomics)
        or s == "/doll"
        or s == "doll"
        or s.startswith("/doll ")
        or s.startswith("doll ")
    )


def _master_enabled() -> bool:
    """Compose canonical master accessor. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_doll_metric_command(
    line: str,
) -> DollMetricReplDispatchResult:
    """Canonical §32.11 Slice 4 entry point — auto-discovered by
    naming convention (``dispatch_<basename>_command``).
    """
    if not _matches(line):
        return DollMetricReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return DollMetricReplDispatchResult(
            ok=False,
            text=f"  /doll parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return DollMetricReplDispatchResult(ok=True, text=_HELP)

    if not _master_enabled():
        return DollMetricReplDispatchResult(
            ok=False,
            text=(
                "  /doll: second-order doll metric disabled "
                "(default per §33.1). Set "
                "JARVIS_SECOND_ORDER_DOLL_METRIC_ENABLED=true."
            ),
        )

    try:
        if head in ("status", "axes"):
            return _render_status()
        if head == "refresh":
            return _refresh()
        if head == "show":
            return _render_show(
                args[1] if len(args) >= 2 else "",
            )
        if head == "completion":
            return _render_completion()
        return DollMetricReplDispatchResult(
            ok=False,
            text=(
                f"  /doll: unknown subcommand {head!r}. "
                "Try /doll help."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — REPL never crashes
        return DollMetricReplDispatchResult(
            ok=False,
            text=(
                f"  /doll: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _render_status() -> DollMetricReplDispatchResult:
    from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
        aggregate_doll_completion,
        format_doll_completion_panel,
    )
    snapshot = aggregate_doll_completion()
    out = format_doll_completion_panel(snapshot)
    if not out:
        return DollMetricReplDispatchResult(
            ok=True,
            text=(
                "# /doll — no axes available "
                "(FlagRegistry empty? canonical sources unreachable?)"
            ),
        )
    return DollMetricReplDispatchResult(ok=True, text=out)


def _refresh() -> DollMetricReplDispatchResult:
    from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
        aggregate_doll_completion,
    )
    snapshot = aggregate_doll_completion(force_refresh=True)
    parts: List[str] = [
        "# /doll refresh",
        f"  aggregated_at_unix : {snapshot.aggregated_at_unix:.0f}",
        f"  elapsed_s          : {snapshot.elapsed_s:.3f}",
        f"  master_enabled     : {snapshot.master_enabled}",
        f"  axis_count         : {len(snapshot.axes)}",
        f"  completion_ratio   : {snapshot.completion_ratio:.3f}",
    ]
    if snapshot.stage_counts:
        parts.append("  stage_counts:")
        for stage, cnt in sorted(snapshot.stage_counts.items()):
            parts.append(f"    {stage:<10} : {cnt}")
    return DollMetricReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_show(category: str) -> DollMetricReplDispatchResult:
    if not category:
        return DollMetricReplDispatchResult(
            ok=False,
            text=(
                "  /doll show: category required "
                "(safety|timing|capacity|routing|"
                "observability|integration|experimental|tuning)"
            ),
        )
    from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
        aggregate_doll_completion,
        format_axis_detail,
        get_cached_snapshot,
    )
    snapshot = get_cached_snapshot() or aggregate_doll_completion()
    out = format_axis_detail(snapshot, category)
    return DollMetricReplDispatchResult(ok=True, text=out)


def _render_completion() -> DollMetricReplDispatchResult:
    from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
        aggregate_doll_completion,
    )
    snapshot = aggregate_doll_completion()
    pct = snapshot.completion_ratio * 100.0
    graduated = snapshot.stage_counts.get("graduated", 0)
    applied = snapshot.stage_counts.get("applied", 0)
    untouched = snapshot.stage_counts.get("untouched", 0)
    text = (
        f"# Second-order doll: {pct:.1f}% complete "
        f"({len(snapshot.axes)} axes · "
        f"graduated={graduated} applied={applied} "
        f"untouched={untouched})"
    )
    return DollMetricReplDispatchResult(ok=True, text=text)


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    """Auto-discovered registration hook for help_dispatcher."""
    if registry is None:
        return 0
    try:
        registry.register(
            verb="doll",
            description=(
                "Second-order doll completion metric — "
                "RSI-acceleration probe measuring how close "
                "O+V is to safely modifying its own cage"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "doll_metric_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "DOLL_METRIC_REPL_SCHEMA_VERSION",
    "DollMetricReplDispatchResult",
    "dispatch_doll_metric_command",
    "register_verbs",
]
