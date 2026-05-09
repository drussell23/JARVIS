"""``/forecast`` REPL — §39 Tier-3 operator surface
(PRD v2.72 to v2.73, 2026-05-08).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage.

Combined operator surface for both Tier-3 predictive
modules — `trajectory` and `command` are sister surfaces
under the unified "what's likely to happen" frame.

Subcommands:
  /forecast                                alias for ``help``
  /forecast trajectory <op-id> [kind]      predict in-flight op outcome
  /forecast command <urgency> <source> <complexity> [files...] [--cross-repo]
                                           pre-submission risk preview
  /forecast status                         master flags + history bounds
  /forecast help                           this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)


FORECAST_REPL_SCHEMA_VERSION: str = "forecast_repl.1"


_HELP = (
    "/forecast — §39 Tier-3 predictive surfaces (PRD)\n"
    "\n"
    "Two sister predictors:\n"
    "  - 🎯 trajectory : in-flight op outcome (#4)\n"
    "  - 🔮 command    : pre-submission risk preview (#19)\n"
    "\n"
    "Subcommands:\n"
    "  /forecast trajectory <op-id> [kind]\n"
    "       Predict outcome of an in-flight op.\n"
    "       Optional `kind` filters similar-op sample\n"
    "       (explore/review/plan/general).\n"
    "\n"
    "  /forecast command <urgency> <source> <complexity>\n"
    "                    [target_files...] [--cross-repo]\n"
    "       Preview hypothetical command's predicted route\n"
    "       + risk-tier + cost + duration before submission.\n"
    "       Examples:\n"
    "         /forecast command high test_failure moderate\n"
    "         /forecast command normal voice_human heavy_code\n"
    "                          orchestrator.py risk_tier_floor.py\n"
    "\n"
    "  /forecast status        master flags + history bounds\n"
    "  /forecast help          this text\n"
    "\n"
    "Master flags:\n"
    "  JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED  (default false)\n"
    "  JARVIS_RISK_COMMAND_PREVIEW_ENABLED     (default false)\n"
)


@dataclass(frozen=True)
class ForecastReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = FORECAST_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/forecast"
        or s == "forecast"
        or s.startswith("/forecast ")
        or s.startswith("forecast ")
    )


def dispatch_forecast_command(
    line: str,
) -> ForecastReplDispatchResult:
    if not _matches(line):
        return ForecastReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ForecastReplDispatchResult(
            ok=False,
            text=f"  /forecast parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "help")

    if head in ("help", "?", ""):
        return ForecastReplDispatchResult(
            ok=True, text=_HELP,
        )

    if head == "status":
        return _render_status()

    try:
        if head == "trajectory":
            return _render_trajectory(args[1:])
        if head == "command":
            return _render_command(args[1:])
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                f"  /forecast: unknown subcommand "
                f"{head!r}. Try /forecast help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                f"  /forecast: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _render_trajectory(
    args: List[str],
) -> ForecastReplDispatchResult:
    if not args:
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                "  /forecast trajectory: op-id required. "
                "Try: /forecast trajectory <op-id> [kind]"
            ),
        )
    try:
        from backend.core.ouroboros.governance.op_trajectory_predictor import (  # noqa: E501
            format_trajectory_prediction,
            master_enabled, predict_trajectory,
        )
    except Exception as exc:  # noqa: BLE001
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                "  /forecast trajectory: substrate "
                f"unavailable ({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                "  /forecast trajectory: predictor disabled "
                "(default per §33.1). Set "
                "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED=true."
            ),
        )
    op_id = args[0]
    kind = args[1] if len(args) >= 2 else None
    pred = predict_trajectory(op_id, op_kind=kind)
    if pred is None:
        return ForecastReplDispatchResult(
            ok=True,
            text=(
                f"# /forecast trajectory {op_id} — "
                "no prediction (op not in buffer or "
                "canonical sources unavailable)"
            ),
        )
    out = format_trajectory_prediction(pred)
    if not out:
        return ForecastReplDispatchResult(
            ok=True,
            text=(
                f"# /forecast trajectory {op_id} — "
                "(empty render)"
            ),
        )
    parts = [
        "# /forecast trajectory",
        out,
        f"  [dim]score={pred.confidence_score:.3f} "
        f"median={pred.median_duration_s:.1f}s "
        f"p90={pred.p90_duration_s:.1f}s[/]",
    ]
    return ForecastReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_command(
    args: List[str],
) -> ForecastReplDispatchResult:
    if len(args) < 3:
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                "  /forecast command: usage: "
                "<urgency> <source> <complexity> "
                "[target_files...] [--cross-repo]"
            ),
        )
    try:
        from backend.core.ouroboros.governance.risk_command_preview import (  # noqa: E501
            format_command_preview, master_enabled,
            preview_command,
        )
    except Exception as exc:  # noqa: BLE001
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                "  /forecast command: substrate "
                f"unavailable ({type(exc).__name__})"
            ),
        )
    if not master_enabled():
        return ForecastReplDispatchResult(
            ok=False,
            text=(
                "  /forecast command: preview disabled "
                "(default per §33.1). Set "
                "JARVIS_RISK_COMMAND_PREVIEW_ENABLED=true."
            ),
        )

    urgency, source, complexity = args[0], args[1], args[2]
    rest = args[3:]
    cross_repo = False
    target_files: List[str] = []
    for a in rest:
        if a == "--cross-repo":
            cross_repo = True
        else:
            target_files.append(a)

    summary = (
        f"{urgency}/{source}/{complexity}"
        + (f" {len(target_files)} files" if target_files else "")
        + (" [cross-repo]" if cross_repo else "")
    )
    preview = preview_command(
        command_summary=summary,
        signal_urgency=urgency,
        signal_source=source,
        task_complexity=complexity,
        target_files=tuple(target_files),
        cross_repo=cross_repo,
    )
    if preview is None:
        return ForecastReplDispatchResult(
            ok=True,
            text=(
                "# /forecast command — preview unavailable"
            ),
        )
    out = format_command_preview(preview)
    if not out:
        return ForecastReplDispatchResult(
            ok=True,
            text=(
                "# /forecast command — (empty render)"
            ),
        )
    return ForecastReplDispatchResult(ok=True, text=out)


def _render_status() -> ForecastReplDispatchResult:
    parts = ["# /forecast status"]
    try:
        from backend.core.ouroboros.governance.op_trajectory_predictor import (  # noqa: E501
            history_limit, master_enabled as traj_master,
            min_samples,
        )
        parts.append(
            f"  trajectory_master : {traj_master()}"
        )
        parts.append(
            f"  min_samples       : {min_samples()}"
        )
        parts.append(
            f"  history_limit     : {history_limit()}"
        )
    except Exception:  # noqa: BLE001
        parts.append(
            "  trajectory_master : (substrate unavailable)"
        )
    try:
        from backend.core.ouroboros.governance.risk_command_preview import (  # noqa: E501
            master_enabled as preview_master,
        )
        parts.append(
            f"  preview_master    : {preview_master()}"
        )
    except Exception:  # noqa: BLE001
        parts.append(
            "  preview_master    : (substrate unavailable)"
        )
    return ForecastReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="forecast",
            description=(
                "Predictive surfaces — op trajectory + "
                "pre-submission risk preview"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "forecast_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "FORECAST_REPL_SCHEMA_VERSION",
    "ForecastReplDispatchResult",
    "dispatch_forecast_command",
    "register_verbs",
]
