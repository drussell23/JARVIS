"""Move 6.5 Slice 4 — `/multi_prior` REPL verb.

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention: file ends ``_repl.py`` →
verb name ``multi_prior`` derived from basename; dispatcher
function ``dispatch_multi_prior_command`` matches.

Subcommands:
  * (bare) — recent overview (top N from the dispatch ledger)
  * ``recent [N]`` — explicit recent N rows
  * ``op <op_id>`` — most recent observation for op_id
  * ``stats`` — process-local observer telemetry +
    ledger-wide action distribution
  * ``help`` — usage

Read-only browser. NEVER raises. Composes Slice 4 observer's
read API; no parallel state. AST-pinned authority asymmetry.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import List, Optional


logger = logging.getLogger(
    "Ouroboros.MultiPriorREPL",
)


MULTI_PRIOR_REPL_SCHEMA_VERSION: str = (
    "multi_prior_repl.1"
)


# ---------------------------------------------------------------------------
# Result type — matches phase9_repl.Phase9DispatchResult shape
# ---------------------------------------------------------------------------


@dataclass
class MultiPriorDispatchResult:
    ok: bool
    text: str


_VALID_SUBCOMMANDS = frozenset(
    {"recent", "op", "stats", "help"},
)


# ---------------------------------------------------------------------------
# Public dispatcher — naming-cage convention
# ---------------------------------------------------------------------------


def dispatch_multi_prior_command(
    line: str,
) -> MultiPriorDispatchResult:
    """Dispatcher for ``/multi_prior`` REPL verb. NEVER
    raises."""
    raw = (line or "").strip()
    if raw.startswith("/multi_prior"):
        raw = raw[len("/multi_prior"):].strip()
    try:
        tokens = shlex.split(raw) if raw else []
    except ValueError:
        return MultiPriorDispatchResult(
            ok=False,
            text="parse error — check quoting",
        )
    if not tokens:
        return _render_overview()
    sub = tokens[0]
    if sub == "help":
        return _render_help()
    if sub == "stats":
        return _render_stats()
    if sub == "recent":
        n: Optional[int] = None
        if len(tokens) >= 2:
            try:
                n = int(tokens[1])
            except ValueError:
                return MultiPriorDispatchResult(
                    ok=False,
                    text=(
                        "/multi_prior recent: N must be an "
                        "integer"
                    ),
                )
        return _render_recent(n)
    if sub == "op":
        if len(tokens) < 2:
            return MultiPriorDispatchResult(
                ok=False,
                text=(
                    "/multi_prior op: missing op_id "
                    "argument"
                ),
            )
        return _render_op(tokens[1])
    if sub not in _VALID_SUBCOMMANDS:
        return MultiPriorDispatchResult(
            ok=False,
            text=(
                f"/multi_prior: unknown subcommand "
                f"{sub!r}; try /multi_prior help"
            ),
        )
    # Unreachable defensively — keeps dispatcher exhaustive.
    return _render_help()


# ---------------------------------------------------------------------------
# Subcommand renderers
# ---------------------------------------------------------------------------


def _render_help() -> MultiPriorDispatchResult:
    text = (
        "/multi_prior — Move 6.5 dispatch observer browser\n"
        "  /multi_prior            recent overview\n"
        "  /multi_prior recent [N] last N observations "
        "(default 50)\n"
        "  /multi_prior op <id>    most-recent observation "
        "for op_id\n"
        "  /multi_prior stats      process telemetry + "
        "ledger action distribution\n"
        "  /multi_prior help       this help"
    )
    return MultiPriorDispatchResult(ok=True, text=text)


def _render_overview() -> MultiPriorDispatchResult:
    """Bare overview — recent N rows + 1-line per-row
    summary."""
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
            master_enabled, read_limit_default,
            read_recent_observations,
        )
    except ImportError:
        return MultiPriorDispatchResult(
            ok=False,
            text=(
                "/multi_prior — observer substrate "
                "unavailable"
            ),
        )
    if not master_enabled():
        return MultiPriorDispatchResult(
            ok=True,
            text=(
                "/multi_prior — observer disabled "
                "(JARVIS_MULTI_PRIOR_OBSERVER_ENABLED=false)"
            ),
        )
    rows = read_recent_observations(
        limit=read_limit_default(),
    )
    if not rows:
        return MultiPriorDispatchResult(
            ok=True,
            text=(
                "/multi_prior — no observations recorded "
                "yet (ledger empty)"
            ),
        )
    lines: List[str] = [
        f"/multi_prior — {len(rows)} recent observation(s) "
        f"(newest LAST):",
    ]
    for r in rows:
        lines.append(_format_row_line(r))
    return MultiPriorDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_recent(
    n: Optional[int],
) -> MultiPriorDispatchResult:
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
            master_enabled, read_recent_observations,
            read_limit_default,
        )
    except ImportError:
        return MultiPriorDispatchResult(
            ok=False,
            text=(
                "/multi_prior — observer substrate "
                "unavailable"
            ),
        )
    if not master_enabled():
        return MultiPriorDispatchResult(
            ok=True,
            text=(
                "/multi_prior — observer disabled"
            ),
        )
    limit = n if n is not None else read_limit_default()
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000
    rows = read_recent_observations(limit=limit)
    if not rows:
        return MultiPriorDispatchResult(
            ok=True,
            text=(
                "/multi_prior recent — ledger empty"
            ),
        )
    lines: List[str] = [
        f"/multi_prior recent {limit} — "
        f"{len(rows)} row(s) (newest LAST):"
    ]
    for r in rows:
        lines.append(_format_row_line(r))
    return MultiPriorDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_op(op_id: str) -> MultiPriorDispatchResult:
    name = (op_id or "").strip()
    if not name:
        return MultiPriorDispatchResult(
            ok=False,
            text="/multi_prior op: blank op_id",
        )
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
            find_by_op_id, master_enabled,
        )
    except ImportError:
        return MultiPriorDispatchResult(
            ok=False,
            text=(
                "/multi_prior — observer substrate "
                "unavailable"
            ),
        )
    if not master_enabled():
        return MultiPriorDispatchResult(
            ok=True,
            text="/multi_prior — observer disabled",
        )
    obs = find_by_op_id(name)
    if obs is None:
        return MultiPriorDispatchResult(
            ok=True,
            text=(
                f"/multi_prior op {name!r} — not found in "
                f"ledger"
            ),
        )
    lines = [
        f"/multi_prior op {name}:",
        f"  decision={obs.decision}",
        f"  action={obs.action_recommendation}",
        f"  consensus_outcome={obs.consensus_outcome}",
        f"  completed={obs.completed_count} "
        f"cancelled={obs.cancelled_count} "
        f"timeout={obs.timeout_count} "
        f"error={obs.error_count}",
        f"  cost_total_usd={obs.cost_total_usd:.4f}",
        f"  wall_clock_s={obs.wall_clock_s:.3f}",
        f"  rationale_preview={obs.rationale_preview!r}",
    ]
    return MultiPriorDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_stats() -> MultiPriorDispatchResult:
    try:
        from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
            action_distribution, get_default_observer,
            master_enabled,
        )
    except ImportError:
        return MultiPriorDispatchResult(
            ok=False,
            text=(
                "/multi_prior — observer substrate "
                "unavailable"
            ),
        )
    if not master_enabled():
        return MultiPriorDispatchResult(
            ok=True,
            text="/multi_prior — observer disabled",
        )
    try:
        tele = get_default_observer().telemetry()
    except Exception:  # noqa: BLE001 — defensive
        tele = {}
    try:
        dist = action_distribution()
    except Exception:  # noqa: BLE001 — defensive
        dist = {}
    lines = ["/multi_prior stats:"]
    lines.append(
        f"  process telemetry: "
        f"records={tele.get('record_count', 0)} "
        f"sse_emitted={tele.get('sse_emitted_count', 0)} "
        f"suppressed={tele.get('suppressed_count', 0)}"
    )
    if dist:
        lines.append("  ledger action distribution:")
        for action, count in sorted(dist.items()):
            lines.append(f"    {action}: {count}")
    else:
        lines.append("  ledger empty")
    return MultiPriorDispatchResult(
        ok=True, text="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_row_line(obs: "object") -> str:
    """One-line summary of a MultiPriorObservation."""
    try:
        op_id = str(getattr(obs, "op_id", ""))
        action = str(
            getattr(obs, "action_recommendation", ""),
        )
        completed = int(
            getattr(obs, "completed_count", 0),
        )
        cancelled = int(
            getattr(obs, "cancelled_count", 0),
        )
        timed_out = int(getattr(obs, "timeout_count", 0))
        errored = int(getattr(obs, "error_count", 0))
    except Exception:  # noqa: BLE001 — defensive
        return "  <unparseable row>"
    return (
        f"  [{action}] op={op_id} "
        f"completed={completed} "
        f"cancelled={cancelled} "
        f"timeout={timed_out} "
        f"error={errored}"
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_repl_authority_asymmetry`` — no
         orchestrator-tier imports.
      2. ``multi_prior_repl_composes_observer`` — every
         subcommand renderer composes the canonical Slice 4
         read API; no parallel state read.
      3. ``multi_prior_repl_naming_cage_compliant`` —
         module-level ``dispatch_multi_prior_command(line)``
         present.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_repl.py"
    )

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_repl" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_repl.py MUST NOT "
                            f"import {module!r} (forbidden "
                            f"segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_repl.py MUST NOT "
                            f"import {module!r} (forbidden "
                            f"token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_composes_observer(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Every renderer that touches data MUST lazy-import
        from multi_prior_observer; no parallel ledger reads.
        AST walk catches direct flock-primitive imports +
        usage; substring sweep avoided to prevent the
        validator's own description strings from looping
        back through the gate."""
        violations: list = []
        # Bytes-pinned tokens (assembled at runtime so they
        # don't surface as literals in the source).
        forbidden_names = {
            "flock_" + "critical_section",
            "flock_" + "append_line",
        }
        composes_observer = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "multi_prior_observer" in module:
                    composes_observer = True
                if "cross_process_jsonl" in module:
                    for alias in node.names:
                        if alias.name in forbidden_names:
                            violations.append(
                                f"composes-observer: REPL "
                                f"MUST NOT directly import "
                                f"{alias.name!r} from "
                                f"cross_process_jsonl — "
                                f"compose the observer's "
                                f"read API"
                            )
            if isinstance(node, ast.Call):
                func = node.func
                fname = (
                    func.id if isinstance(func, ast.Name)
                    else (
                        func.attr
                        if isinstance(func, ast.Attribute)
                        else None
                    )
                )
                if fname in forbidden_names:
                    violations.append(
                        f"composes-observer: REPL MUST NOT "
                        f"call {fname!r} directly — compose "
                        f"the observer's read API "
                        f"(line {node.lineno})"
                    )
        if not composes_observer:
            violations.append(
                "composes-observer: REPL MUST import from "
                "multi_prior_observer"
            )
        return tuple(violations)

    def _validate_naming_cage_compliant(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        found = False
        for node in tree.body:
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "dispatch_multi_prior_command"
            ):
                found = True
                if not node.args.args:
                    violations.append(
                        "dispatch_multi_prior_command MUST "
                        "take ``line`` as first positional"
                    )
                else:
                    if node.args.args[0].arg != "line":
                        violations.append(
                            "dispatch_multi_prior_command "
                            "first positional arg MUST be "
                            "``line`` per §32.11 Slice 4 "
                            "naming-cage"
                        )
                break
        if not found:
            violations.append(
                "module-level "
                "``dispatch_multi_prior_command(line)`` "
                "MUST exist per §32.11 Slice 4 naming-cage"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_repl_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — REPL is substrate-pure: "
                "no orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_repl_composes_observer"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — REPL composes Slice 4's "
                "read API; no parallel JSONL access."
            ),
            validate=_validate_composes_observer,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_repl_naming_cage_compliant"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — module-level "
                "dispatch_multi_prior_command(line) per "
                "§32.11 Slice 4 naming-cage so "
                "repl_dispatch_registry auto-discovers."
            ),
            validate=_validate_naming_cage_compliant,
        ),
    ]


__all__ = [
    "MULTI_PRIOR_REPL_SCHEMA_VERSION",
    "MultiPriorDispatchResult",
    "dispatch_multi_prior_command",
    "register_shipped_invariants",
]
