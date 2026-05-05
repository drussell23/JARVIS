"""Move 7 Slice 4 — `/semantic_budget` REPL dispatcher
(PRD §29.4, 2026-05-05).

Operator-facing CLI surface for the Move 7 Cross-op Semantic
Budget arc. Auto-discovered by §32.11 Slice 4 `repl_dispatch_-
registry` via the §33.3 naming-cage convention:

  * file ends ``_repl.py``  → verb derived from basename
  * exposes module-level ``dispatch_<basename>_command(line) ->
    SemanticBudgetReplDispatchResult``
  * SerpentREPL routes any line matching ``/semantic_budget``,
    ``semantic_budget``, ``/semantic_budget …``, or
    ``semantic_budget …`` here zero-edit.

## Subcommands

  * ``/semantic_budget``                  — alias for ``status``
  * ``/semantic_budget status``           — current verdict +
    integrated drift + threshold (most-recent window)
  * ``/semantic_budget recent [N]``       — last N centroid rows
    (default 10, max 200)
  * ``/semantic_budget window``           — env-knob projection
    (window_size + threshold + approaching_ratio)
  * ``/semantic_budget help``             — usage listing
    (always available; bypasses master-flag gate)

## Architectural locks (operator mandate, AST-pinned)

  1. **Composes Slices 1+2+3 substrate** — invokes
     :func:`compute_semantic_budget` (Slice 1) over
     :func:`read_recent_centroids` (Slice 2) and renders
     verdict text. NO parallel math / persistence / cadence.
  2. **Read-only** — no mutation of the ledger; no observer
     triggers; pure operator snapshot surface.
  3. **Master-flag-gated** — every subcommand except ``help``
     short-circuits on
     :func:`cross_op_semantic_budget_enabled`.
  4. **Authority asymmetry** — imports stdlib + Slice 1 + Slice 2
     ONLY. NEVER imports orchestrator / iron_gate / policy /
     providers / candidate_generator / change_engine /
     semantic_guardian.
  5. **NEVER raises** — all subcommands defensive; exceptions
     surface as a non-ok ``SemanticBudgetReplDispatchResult``
     with diagnostic text, never propagate to the SerpentREPL
     loop.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


SEMANTIC_BUDGET_REPL_SCHEMA_VERSION: str = (
    "semantic_budget_repl.1"
)


_DEFAULT_RECENT_LIMIT: int = 10
_MAX_RECENT_LIMIT: int = 200


_HELP = (
    "/semantic_budget — Move 7 Cross-op Semantic Budget "
    "(PRD §29.4)\n"
    "\n"
    "Subcommands:\n"
    "  /semantic_budget                       alias for "
    "/semantic_budget status\n"
    "  /semantic_budget status                current verdict + "
    "integrated drift\n"
    "  /semantic_budget recent [N]            last N centroid "
    "rows (default 10, max 200)\n"
    "  /semantic_budget window                env-knob "
    "projection (window_size / threshold / approaching_ratio)\n"
    "  /semantic_budget help                  this text\n"
    "\n"
    "Verdict ladder: within_budget / approaching / exceeded "
    "/ insufficient_data / disabled\n"
    "\n"
    "Master flag: JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED "
    "(default-FALSE per §33.1 — flips only after empirical "
    "Phase-9 baseline)\n"
    "Live HTTP surface: GET /observability/semantic-budget\n"
    "Live SSE event:    semantic_budget_changed\n"
)


# ---------------------------------------------------------------------------
# Frozen result container — matches the §32.11 Slice 4 registry
# DispatchOutcome shape (.matched / .ok / .text)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticBudgetReplDispatchResult:
    """Result of a ``/semantic_budget`` dispatch. Frozen for
    safe propagation. ``matched=False`` signals the line wasn't a
    ``/semantic_budget`` invocation (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True
    schema_version: str = SEMANTIC_BUDGET_REPL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Master-flag check — defers to Slice 1's flag
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
            cross_op_semantic_budget_enabled,
        )
        return bool(cross_op_semantic_budget_enabled())
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Dispatcher — auto-discovered by §32.11 Slice 4 registry
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/semantic_budget"
        or s == "semantic_budget"
        or s.startswith("/semantic_budget ")
        or s.startswith("semantic_budget ")
    )


def dispatch_semantic_budget_command(
    line: str,
) -> SemanticBudgetReplDispatchResult:
    """Parse a ``/semantic_budget`` line and dispatch. NEVER
    raises — exceptions surface as non-ok results.

    Auto-discovered by :mod:`repl_dispatch_registry` (§32.11
    Slice 4) — file ends ``_repl.py`` and the dispatcher
    function name matches the basename. Verb name is
    ``semantic_budget``."""
    if not _matches(line):
        return SemanticBudgetReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return SemanticBudgetReplDispatchResult(
            ok=False,
            text=f"  /semantic_budget parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return SemanticBudgetReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return SemanticBudgetReplDispatchResult(
            ok=False,
            text=(
                "  /semantic_budget: Move 7 Cross-op Semantic "
                "Budget disabled (default per §33.1 — flips "
                "only after empirical Phase-9 baseline). Set "
                "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED=true "
                "to query."
            ),
        )

    if head == "status":
        return _render_status()
    if head == "recent":
        return _render_recent(
            _parse_limit(
                args,
                default=_DEFAULT_RECENT_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "window":
        return _render_window()
    return SemanticBudgetReplDispatchResult(
        ok=False,
        text=(
            f"  /semantic_budget: unknown subcommand "
            f"{head!r}. Try /semantic_budget help."
        ),
    )


def _parse_limit(args, *, default, ceiling):
    """Parse limit from ``args[1]``. Falls through to default
    on parse failure / out-of-bounds. NEVER raises."""
    if len(args) < 2:
        return default
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Renderers — pure projection over Slices 1+2 substrate
# ---------------------------------------------------------------------------


def _render_status() -> SemanticBudgetReplDispatchResult:
    """Compute the current verdict over the most-recent window
    and render a concise one-liner + diagnostics. Read-only —
    composes Slice 1 + Slice 2; no observer trigger."""
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
            compute_semantic_budget,
            window_size,
        )
        from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
            read_recent_centroids,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetReplDispatchResult(
            ok=False,
            text=(
                f"  /semantic_budget status: substrate "
                f"unavailable ({type(exc).__name__})"
            ),
        )

    try:
        centroids = read_recent_centroids(limit=window_size())
        report = compute_semantic_budget(
            centroids, enabled_override=True,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetReplDispatchResult(
            ok=False,
            text=(
                f"  /semantic_budget status: compute failed "
                f"({type(exc).__name__})"
            ),
        )

    verdict = report.verdict.value
    drift = report.integrated_drift
    threshold = report.threshold
    band = report.approaching_band
    seen = report.centroids_seen

    pct = (
        (drift / threshold * 100.0) if threshold > 0 else 0.0
    )
    lines = [
        f"/semantic_budget status",
        "",
        f"  verdict:               {verdict}",
        f"  integrated_drift:      {drift:.4f}",
        f"  threshold:             {threshold:.4f}",
        f"  approaching_band:      {band:.4f}",
        f"  drift_pct_of_budget:   {pct:.1f}%",
        f"  centroids_seen:        {seen}",
        f"  per_op_deltas:         {len(report.per_op_deltas)}",
    ]
    if report.diagnostics:
        lines.append("")
        lines.append("  diagnostics:")
        for d in report.diagnostics[:5]:
            lines.append(f"    - {d}")
    return SemanticBudgetReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_recent(
    limit: int,
) -> SemanticBudgetReplDispatchResult:
    """Project the last N centroid rows for operator audit.
    Composes Slice 2's reader."""
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
            read_recent_centroids,
        )
        rows = read_recent_centroids(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetReplDispatchResult(
            ok=False,
            text=(
                f"  /semantic_budget recent: read failed "
                f"({type(exc).__name__})"
            ),
        )
    if not rows:
        return SemanticBudgetReplDispatchResult(
            ok=True,
            text=(
                "/semantic_budget recent — no centroids in "
                "ledger.\n"
                "  hint: ledger lives at "
                ".jarvis/cross_op_semantic_centroids.jsonl "
                "(producer wires at COMPLETE phase boundary)"
            ),
        )
    lines = [
        f"/semantic_budget recent — {len(rows)} most-recent "
        f"centroid(s)",
        "",
    ]
    for r in rows:
        op_id = (r.op_id or "")[:24]
        ts = float(r.ts_unix)
        dim = len(r.centroid)
        h = (r.centroid_hash or "")[:8]
        lines.append(
            f"  ts={ts:>14.1f}  op={op_id:<24}  dim={dim:<4}  "
            f"hash={h}"
        )
    return SemanticBudgetReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_window() -> SemanticBudgetReplDispatchResult:
    """Render the env-knob configuration the primitive will
    apply on next compute. Composes Slice 1's env helpers."""
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
            window_size,
            drift_threshold,
            approaching_ratio,
        )
        from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
            centroids_jsonl_path,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetReplDispatchResult(
            ok=False,
            text=(
                f"  /semantic_budget window: substrate "
                f"unavailable ({type(exc).__name__})"
            ),
        )
    try:
        ws = window_size()
        thr = drift_threshold()
        ratio = approaching_ratio()
        path = centroids_jsonl_path()
    except Exception as exc:  # noqa: BLE001 — defensive
        return SemanticBudgetReplDispatchResult(
            ok=False,
            text=(
                f"  /semantic_budget window: env read failed "
                f"({type(exc).__name__})"
            ),
        )
    lines = [
        "/semantic_budget window — env-knob projection",
        "",
        f"  window_size:           {ws}",
        f"  drift_threshold:       {thr:.4f}",
        f"  approaching_ratio:     {ratio:.4f}",
        f"  approaching_band:      "
        f"{thr * ratio:.4f} ({ratio * 100:.0f}% of threshold)",
        f"  ledger_path:           {path}",
    ]
    return SemanticBudgetReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# /help auto-discovery hook (mirrors decisions_repl pattern)
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/semantic_budget`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/semantic_budget",
            one_line=(
                "Cross-op Semantic Budget: status / recent / "
                "window queries (Move 7 / PRD §29.4)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[semantic_budget_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 2 pins:
      1. Authority asymmetry — substrate purity
      2. Composes-substrate — recursion ban (no parallel math /
         persistence; defers all computation to Slices 1+2).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"semantic_budget_repl.py MUST "
                            f"NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_substrate(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "compute_semantic_budget" not in source:
            violations.append(
                "REPL MUST compose Slice 1 "
                "compute_semantic_budget (no parallel math)"
            )
        if "read_recent_centroids" not in source:
            violations.append(
                "REPL MUST compose Slice 2 "
                "read_recent_centroids (no parallel ledger "
                "read)"
            )
        if "cross_op_semantic_budget_enabled" not in source:
            violations.append(
                "REPL MUST gate on Slice 1's master flag "
                "helper (no parallel flag)"
            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "semantic_budget_repl.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "semantic_budget_repl_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "semantic_budget_repl.py MUST stay pure "
                "substrate composing Slices 1+2 ONLY (no "
                "orchestrator / iron_gate / policy / "
                "providers / change_engine imports)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "semantic_budget_repl_composes_substrate"
            ),
            target_file=target,
            description=(
                "REPL composes Slice 1 compute_semantic_budget "
                "+ Slice 2 read_recent_centroids + master "
                "flag gate. No parallel math / ledger read / "
                "flag check."
            ),
            validate=_validate_composes_substrate,
        ),
    ]


__all__ = [
    "SEMANTIC_BUDGET_REPL_SCHEMA_VERSION",
    "SemanticBudgetReplDispatchResult",
    "dispatch_semantic_budget_command",
    "register_shipped_invariants",
    "register_verbs",
]
