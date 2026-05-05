"""M9 Slice 4 — ``/curiosity`` REPL dispatcher (PRD §30.5.1).

Operator-facing CLI surface — parallel to :mod:`budget_repl`
(Upgrade 1) / :mod:`outcomes_repl` (M11) / :mod:`failures_repl`
(Upgrade 3). Same patterns: ``register_verbs`` for /help auto-
discovery, lazy ``curiosity_collector`` import, frozen
``CuriosityReplDispatchResult``.

Subcommands:

  * ``/curiosity``                 — alias for
    ``/curiosity top``
  * ``/curiosity top [N]``         — top-K cluster scores by
    magnitude descending (default 20)
  * ``/curiosity region <id>``     — single per-cluster detail
    with score + recent observations summary
  * ``/curiosity config``          — env-knob snapshot
  * ``/curiosity reset <id>``      — operator-explicit decay
    (writes ``CuriosityDecayReason.OPERATOR_RESET``) — the
    SOLE mutation surface in M9 Slice 4 read-only contract
  * ``/curiosity help``            — usage listing (always
    available; bypasses master-flag gate)

Master gate: :func:`curiosity_gradient_enabled`. Auto-discovered
by :func:`help_dispatcher._discover_module_provided_verbs`.
NEVER raises.

Authority invariants (AST-pinned at Slice 5):

  * Imports stdlib + ``curiosity_gradient`` +
    ``curiosity_collector`` ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor /
    sensor_governor / strategic_direction.
  * Read-only EXCEPT for ``reset`` subcommand (operator-
    explicit decay only — same boundary discipline as
    ``/failures clear`` and ``/outcomes clear``).
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.ouroboros.governance.curiosity_collector import (
    CuriosityCollector,
    get_default_collector,
)
from backend.core.ouroboros.governance.curiosity_gradient import (
    CURIOSITY_GRADIENT_SCHEMA_VERSION,
    CuriosityScore,
    curiosity_gradient_enabled,
    curiosity_halflife_days,
    curiosity_min_samples,
    curiosity_multiplier_ceiling,
    curiosity_multiplier_floor,
    curiosity_source_weight_logprob,
    curiosity_source_weight_prophecy,
    curiosity_source_weight_recurrence,
    curiosity_stale_focus_hours,
)

logger = logging.getLogger(__name__)


_HELP = (
    "/curiosity — CuriosityGradient (M9 / PRD §30.5.1)\n"
    "\n"
    "Subcommands:\n"
    "  /curiosity                       alias for /curiosity top\n"
    "  /curiosity top [N]               top-K clusters by "
    "magnitude (default 20, max 200)\n"
    "  /curiosity region <id>           per-cluster detail + "
    "source breakdown\n"
    "  /curiosity config                env-knob snapshot\n"
    "  /curiosity reset <id>            operator-explicit decay "
    "(writes OPERATOR_RESET)\n"
    "  /curiosity help                  this text\n"
    "\n"
    "Master flag: JARVIS_CURIOSITY_GRADIENT_ENABLED (graduates\n"
    "Slice 5; flip to false for instant revert)\n"
    "Live HTTP surface: GET /observability/curiosity[/region/{id}]\n"
    "Live SSE event:    curiosity_changed\n"
)

_DEFAULT_TOP_LIMIT: int = 20
_MAX_TOP_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuriosityReplDispatchResult:
    """Result of a ``/curiosity`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/curiosity`` invocation (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Module-level collector provider — tests inject; production
# uses :func:`get_default_collector`.
# ---------------------------------------------------------------------------


_default_collector: Optional[CuriosityCollector] = None


def set_default_collector(
    collector: Optional[CuriosityCollector],
) -> None:
    global _default_collector  # noqa: PLW0603
    _default_collector = collector


def reset_default_collector_for_tests() -> None:
    global _default_collector  # noqa: PLW0603
    _default_collector = None


def _resolve_collector(
    explicit: Optional[CuriosityCollector],
) -> CuriosityCollector:
    if explicit is not None:
        return explicit
    if _default_collector is not None:
        return _default_collector
    return get_default_collector()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/curiosity"
        or s == "curiosity"
        or s.startswith("/curiosity ")
        or s.startswith("curiosity ")
    )


def _parse_limit(args, default=_DEFAULT_TOP_LIMIT):
    if len(args) < 2:
        return default
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > _MAX_TOP_LIMIT:
            return _MAX_TOP_LIMIT
        return n
    except (TypeError, ValueError):
        return default


def dispatch_curiosity_command(
    line: str,
    *,
    collector: Optional[CuriosityCollector] = None,
) -> CuriosityReplDispatchResult:
    """Parse a ``/curiosity`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return CuriosityReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CuriosityReplDispatchResult(
            ok=False,
            text=f"  /curiosity parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "top")

    if head in ("help", "?"):
        return CuriosityReplDispatchResult(ok=True, text=_HELP)

    if not curiosity_gradient_enabled():
        return CuriosityReplDispatchResult(
            ok=False,
            text=(
                "  /curiosity: CuriosityGradient disabled — set "
                "JARVIS_CURIOSITY_GRADIENT_ENABLED=true"
            ),
        )

    resolved = _resolve_collector(collector)

    if head == "top":
        return _render_top(resolved, _parse_limit(args))
    if head == "region":
        if len(args) < 2:
            return CuriosityReplDispatchResult(
                ok=False,
                text=(
                    "  /curiosity region <id>: missing "
                    "cluster_id argument."
                ),
            )
        return _render_region(resolved, args[1])
    if head == "config":
        return _render_config()
    if head == "reset":
        if len(args) < 2:
            return CuriosityReplDispatchResult(
                ok=False,
                text=(
                    "  /curiosity reset <id>: missing "
                    "cluster_id argument."
                ),
            )
        return _render_reset(resolved, args[1])
    return CuriosityReplDispatchResult(
        ok=False,
        text=(
            f"  /curiosity: unknown subcommand {head!r}. "
            f"Try /curiosity help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_score_one_line(s: CuriosityScore) -> str:
    cid_short = (s.cluster_id or "")[:24]
    src_short = s.dominant_source.value
    decay = (
        s.decay_reason.value
        if s.is_decayed()
        else "-"
    )
    return (
        f"  {cid_short:<24}  mag={s.magnitude:.3f}  "
        f"conf={s.confidence:.3f}  n={s.samples_count:<4}  "
        f"src={src_short:<22}  decay={decay}"
    )


def _render_top(
    collector: CuriosityCollector, limit: int,
) -> CuriosityReplDispatchResult:
    try:
        scores = collector.snapshot_all()
    except Exception:  # noqa: BLE001 — defensive
        scores = ()
    if not scores:
        return CuriosityReplDispatchResult(
            ok=True,
            text=(
                "/curiosity top — no clusters currently "
                "tracked.\n"
                f"  schema_version="
                f"{CURIOSITY_GRADIENT_SCHEMA_VERSION}\n"
                "  master_enabled=true"
            ),
        )
    sorted_scores = sorted(
        scores,
        key=lambda s: (-s.magnitude, -s.samples_count),
    )[:limit]
    lines = [
        f"/curiosity top — {len(scores)} cluster(s) tracked, "
        f"showing top {len(sorted_scores)}",
        f"  schema_version={CURIOSITY_GRADIENT_SCHEMA_VERSION}",
        "",
    ]
    for s in sorted_scores:
        try:
            lines.append(_format_score_one_line(s))
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  <projection_failed>")
    return CuriosityReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_region(
    collector: CuriosityCollector, cluster_id: str,
) -> CuriosityReplDispatchResult:
    try:
        score = collector.score_for_cluster(cluster_id)
    except Exception:  # noqa: BLE001 — defensive
        score = None
    if score is None:
        return CuriosityReplDispatchResult(
            ok=False,
            text=f"  /curiosity region: {cluster_id!r} unknown",
        )
    try:
        proj = score.to_dict()
    except Exception:  # noqa: BLE001 — defensive
        proj = {}
    lines = [
        f"/curiosity region {score.cluster_id}",
        f"  magnitude            {proj.get('magnitude'):.3f}"
        if isinstance(proj.get("magnitude"), (int, float))
        else f"  magnitude            {proj.get('magnitude')}",
        f"  confidence           {proj.get('confidence'):.3f}"
        if isinstance(proj.get("confidence"), (int, float))
        else f"  confidence           {proj.get('confidence')}",
        f"  samples_count        {proj.get('samples_count')}",
        f"  dominant_source      {proj.get('dominant_source')}",
        f"  decay_reason         {proj.get('decay_reason')}",
        f"  is_cold_start        {proj.get('is_cold_start')}",
        f"  is_decayed           {proj.get('is_decayed')}",
    ]
    breakdown = proj.get("source_breakdown") or []
    if breakdown:
        lines.append("  source_breakdown:")
        for entry in breakdown:
            try:
                lines.append(
                    f"    {entry['source']:<24}  "
                    f"contribution={entry['contribution']:.3f}",
                )
            except Exception:  # noqa: BLE001 — defensive
                continue
    return CuriosityReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_config() -> CuriosityReplDispatchResult:
    try:
        cfg = {
            "halflife_days": curiosity_halflife_days(),
            "min_samples": curiosity_min_samples(),
            "stale_focus_hours": curiosity_stale_focus_hours(),
            "weight_logprob": (
                curiosity_source_weight_logprob()
            ),
            "weight_prophecy": (
                curiosity_source_weight_prophecy()
            ),
            "weight_recurrence": (
                curiosity_source_weight_recurrence()
            ),
            "multiplier_floor": (
                curiosity_multiplier_floor()
            ),
            "multiplier_ceiling": (
                curiosity_multiplier_ceiling()
            ),
        }
    except Exception:  # noqa: BLE001 — defensive
        cfg = {}
    lines = [
        "/curiosity config",
        f"  schema_version           "
        f"{CURIOSITY_GRADIENT_SCHEMA_VERSION}",
        f"  master_enabled           "
        f"{curiosity_gradient_enabled()}",
    ]
    for k, v in sorted(cfg.items()):
        lines.append(f"  {k:<24} {v}")
    return CuriosityReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_reset(
    collector: CuriosityCollector, cluster_id: str,
) -> CuriosityReplDispatchResult:
    """Operator-explicit decay surface. The SOLE mutation
    surface in M9's Slice 4 read-only contract — calls
    :meth:`CuriosityCollector.reset_cluster` which writes
    OPERATOR_RESET decay reason on the next score query."""
    try:
        ok = collector.reset_cluster(cluster_id)
    except Exception:  # noqa: BLE001 — defensive
        ok = False
    if not ok:
        return CuriosityReplDispatchResult(
            ok=False,
            text=(
                f"  /curiosity reset: failed for "
                f"{cluster_id!r} (master off?)"
            ),
        )
    return CuriosityReplDispatchResult(
        ok=True,
        text=(
            f"  /curiosity reset: cluster {cluster_id!r} "
            f"marked OPERATOR_RESET — decay applied on next "
            f"score query."
        ),
    )


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/curiosity`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/curiosity",
            one_line=(
                "Curiosity gradient: per-cluster prediction-"
                "error magnitude + decay diagnostics + operator-"
                "explicit reset (M9 / PRD §30.5.1)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[curiosity_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "CuriosityReplDispatchResult",
    "dispatch_curiosity_command",
    "register_verbs",
    "reset_default_collector_for_tests",
    "set_default_collector",
]
