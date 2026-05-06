"""Path D.1 — `/graph` REPL operator surface for L3 execution-
graph progress.

Closes the §36.6 "6 unwired autonomy modules" entry for
``execution_graph_progress`` (autonomy/) — the module already
ships a singleton (:func:`get_default_tracker`) + rich read API
(``snapshot``, ``all_active``, ``all_tracked``, ``stats``); this
slice adds the operator-facing browser. Composes canonical
substrate without modifying it.

Auto-discovered via §32.11 Slice 4 ``repl_dispatch_registry``
naming-cage (file ends ``_repl.py`` → verb ``/graph`` →
dispatcher ``dispatch_graph_command(line)``). Zero edits to
the registry.

Subcommands:

  * ``/graph``                — list active execution graphs +
                                tracker stats
  * ``/graph all``            — list ALL retained graphs
                                (including terminal)
  * ``/graph show <graph_id>`` — full detail for one graph
  * ``/graph stats``          — tracker-level aggregate
  * ``/graph help``           — bypass-master help

Architectural locks (mirrors `/health`, `/why_changed`,
`/causal`):

  * **Read-only** — REPL NEVER writes to the tracker; composes
    snapshot APIs only (AST-pinned).
  * **Authority asymmetry** — substrate purity (no orchestrator
    / iron_gate / policy / providers / candidate_generator /
    urgency_router / change_engine / semantic_guardian imports;
    AST-pinned).
  * **Composes singleton** — REPL composes
    :func:`get_default_tracker` only; no parallel tracker
    construction (AST-pinned).
  * **NEVER raises** — every dispatch path returns a structured
    result, fail-silent on substrate unavailability.

Identity preservation: cyan default, yellow running phase,
red failed/cancelled, dim metadata. NO ``bright_green``
(§37.9 invariant #3 + Slice 4 lint pin).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class GraphReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/graph — L3 execution-graph browser"
    f"{_RESET}\n"
    f"  {_DIM}Read-only operator surface for "
    f"ExecutionGraphProgressTracker (parallel subagent fan-out)."
    f"{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/graph{_RESET}                  "
    f"{_DIM}active execution graphs + stats{_RESET}\n"
    f"    {_CYAN}/graph all{_RESET}              "
    f"{_DIM}all retained graphs incl. terminal{_RESET}\n"
    f"    {_CYAN}/graph show <graph_id>{_RESET}  "
    f"{_DIM}full detail for one graph{_RESET}\n"
    f"    {_CYAN}/graph stats{_RESET}            "
    f"{_DIM}tracker-level aggregate{_RESET}\n"
    f"    {_CYAN}/graph help{_RESET}             "
    f"{_DIM}this message{_RESET}\n"
    f"\n"
    f"  {_BOLD}Master flag:{_RESET}\n"
    f"    {_DIM}JARVIS_EXEC_GRAPH_PROGRESS_ENABLED{_RESET}\n"
)


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/graph"
        or s == "graph"
        or s.startswith("/graph ")
        or s.startswith("graph ")
    )


def _color_for_phase(phase: str) -> str:
    p = (phase or "").lower()
    if p in ("failed", "cancelled"):
        return _RED
    if p in ("running",):
        return _YELLOW
    if p in ("completed",):
        return _CYAN
    return _DIM


# ---------------------------------------------------------------------------
# Tracker access — single source of truth
# ---------------------------------------------------------------------------


def _get_tracker():
    """Lazy-import the canonical singleton. NEVER raises;
    returns None on substrate unavailability."""
    try:
        from backend.core.ouroboros.governance.autonomy.execution_graph_progress import (  # noqa: E501
            get_default_tracker,
        )
        return get_default_tracker()
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_active_graphs() -> str:
    tracker = _get_tracker()
    if tracker is None:
        return (
            f"\n  {_RED}execution_graph_progress substrate "
            f"unavailable{_RESET}\n"
            f"  {_DIM}master flag JARVIS_EXEC_GRAPH_PROGRESS_"
            f"ENABLED may be off; install_default_tracker has "
            f"not been called{_RESET}\n"
        )
    try:
        active = tracker.all_active()
        stats = tracker.stats()
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}snapshot raised: {exc}{_RESET}\n"
    out = [
        f"\n  {_BOLD}{_CYAN}Active L3 Execution Graphs{_RESET}  "
        f"{_DIM}(active={stats.get('active_graphs', 0)} "
        f"tracked={stats.get('tracked_graphs', 0)}){_RESET}",
        "",
    ]
    if not active:
        out.append(
            f"  {_DIM}No active execution graphs. The L3 "
            f"subagent scheduler dispatches a graph per "
            f"parallel-eligible op.{_RESET}"
        )
    else:
        for gp in active[:50]:
            out.append(_render_graph_line(gp))
    out.append("")
    out.append(
        f"  {_DIM}/graph show <graph_id> for unit-level "
        f"detail{_RESET}"
    )
    return "\n".join(out) + "\n"


def _render_all_graphs() -> str:
    tracker = _get_tracker()
    if tracker is None:
        return (
            f"\n  {_RED}execution_graph_progress substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        graphs = tracker.all_tracked()
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}snapshot raised: {exc}{_RESET}\n"
    out = [
        f"\n  {_BOLD}{_CYAN}All Retained Execution Graphs"
        f"{_RESET}  {_DIM}(showing up to 50){_RESET}",
        "",
    ]
    if not graphs:
        out.append(f"  {_DIM}No graphs retained.{_RESET}")
    else:
        for gp in graphs[:50]:
            out.append(_render_graph_line(gp))
    out.append("")
    return "\n".join(out) + "\n"


def _render_graph_line(gp) -> str:
    """One-line graph summary for list views."""
    try:
        gid = (
            getattr(gp, "graph_id", "?")[:24]
        )
        phase = (
            getattr(getattr(gp, "phase", None), "value", "?")
        )
        completion = gp.completion_pct() * 100
        runtime = gp.runtime_ms
        unit_count = len(gp.units)
        return (
            f"  {_CYAN}{gid:<24}{_RESET}  "
            f"{_color_for_phase(phase)}{phase:<10}{_RESET}  "
            f"{_DIM}units={unit_count:<3} "
            f"complete={completion:5.1f}% "
            f"runtime={runtime:.0f}ms{_RESET}"
        )
    except Exception:  # noqa: BLE001 — defensive
        return f"  {_DIM}(unrenderable graph row){_RESET}"


def _render_graph_detail(graph_id: str) -> str:
    tracker = _get_tracker()
    if tracker is None:
        return (
            f"\n  {_RED}execution_graph_progress substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        gp = tracker.snapshot(graph_id)
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}snapshot raised: {exc}{_RESET}\n"
    if gp is None:
        return (
            f"\n  {_YELLOW}No graph with id {graph_id!r}"
            f"{_RESET}\n"
            f"  {_DIM}/graph all to list retained graphs"
            f"{_RESET}\n"
        )
    try:
        phase_value = getattr(
            getattr(gp, "phase", None), "value", "?",
        )
        out = [
            f"\n  {_BOLD}{_CYAN}{gp.graph_id}{_RESET}  "
            f"{_color_for_phase(phase_value)}{phase_value}"
            f"{_RESET}",
            "",
            f"  {_DIM}op_id:{_RESET}            {gp.op_id}",
            f"  {_DIM}planner_id:{_RESET}       {gp.planner_id}",
            f"  {_DIM}schema_version:{_RESET}   {gp.schema_version}",
            f"  {_DIM}concurrency_limit:{_RESET} "
            f"{gp.concurrency_limit}",
            f"  {_DIM}plan_digest:{_RESET}      "
            f"{gp.plan_digest[:16]}",
            f"  {_DIM}completion:{_RESET}       "
            f"{gp.completion_pct() * 100:.1f}%",
            f"  {_DIM}runtime:{_RESET}          "
            f"{gp.runtime_ms:.0f}ms",
            f"  {_DIM}unit_count:{_RESET}       "
            f"{len(gp.units)}",
        ]
        if gp.last_error:
            out.append(
                f"  {_DIM}last_error:{_RESET}       "
                f"{_RED}{gp.last_error[:120]}{_RESET}"
            )
        # Units by state.
        try:
            buckets = gp.units_by_status()
            out.append("")
            out.append(f"  {_BOLD}Units by state:{_RESET}")
            for state, units in buckets.items():
                if not units:
                    continue
                state_value = getattr(state, "value", str(state))
                out.append(
                    f"    {_DIM}{state_value:<14}{_RESET}  "
                    f"{len(units)}"
                )
        except Exception:  # noqa: BLE001 — defensive
            pass
        # Critical path.
        try:
            cp = gp.critical_path()
            if cp:
                out.append("")
                out.append(f"  {_BOLD}Critical path:{_RESET}")
                for uid in cp[:8]:
                    out.append(f"    {_CYAN}{uid}{_RESET}")
        except Exception:  # noqa: BLE001 — defensive
            pass
        out.append("")
        return "\n".join(out) + "\n"
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}render raised: {exc}{_RESET}\n"


def _render_stats() -> str:
    tracker = _get_tracker()
    if tracker is None:
        return (
            f"\n  {_RED}execution_graph_progress substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        stats = tracker.stats()
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}stats raised: {exc}{_RESET}\n"
    out = [
        f"\n  {_BOLD}{_CYAN}ExecutionGraphProgressTracker stats"
        f"{_RESET}",
        "",
    ]
    for k, v in stats.items():
        out.append(f"  {_DIM}{k:<18}{_RESET}{v}")
    out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_graph_command(
    line: str,
) -> GraphReplDispatchResult:
    if not _matches(line):
        return GraphReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return GraphReplDispatchResult(
            ok=False, text=f"  /graph parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")
    if head in ("help", "?"):
        return GraphReplDispatchResult(ok=True, text=_HELP)
    try:
        if head == "":
            return GraphReplDispatchResult(
                ok=True, text=_render_active_graphs(),
            )
        if head == "all":
            return GraphReplDispatchResult(
                ok=True, text=_render_all_graphs(),
            )
        if head == "stats":
            return GraphReplDispatchResult(
                ok=True, text=_render_stats(),
            )
        if head == "show":
            if len(args) < 2:
                return GraphReplDispatchResult(
                    ok=False,
                    text=(
                        "  /graph show <graph_id> — "
                        "argument required"
                    ),
                )
            return GraphReplDispatchResult(
                ok=True,
                text=_render_graph_detail(args[1]),
            )
        return GraphReplDispatchResult(
            ok=False,
            text=(
                f"  /graph: unknown subcommand "
                f"{head!r} — try /graph help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return GraphReplDispatchResult(
            ok=False,
            text=f"  /graph: error — {exc}. Try again.",
        )


# ---------------------------------------------------------------------------
# Help dispatcher hook
# ---------------------------------------------------------------------------


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="graph",
            description=(
                "L3 execution-graph browser — composes "
                "ExecutionGraphProgressTracker. Read-only."
            ),
            posture_relevance="RELEVANT",
            since="Path D.1 (PRD §36.6, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``graph_repl_authority_read_only`` — REPL composes
         only read APIs; no mutating calls (no ``register_graph``,
         ``unsubscribe_all``, etc.).
      2. ``graph_repl_authority_asymmetry`` — substrate purity.
      3. ``graph_repl_composes_singleton`` — REPL composes
         ``get_default_tracker`` only; no parallel tracker
         construction.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/graph_repl.py"
    )

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_methods = (
            "register_graph",
            "unsubscribe_all",
            "bind",
            "_synthesize_stub",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in forbidden_methods
                ):
                    violations.append(
                        f"graph_repl.py is read-only; MUST "
                        f"NOT call .{fn.attr}() on the tracker"
                    )
        return tuple(violations)

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
                            f"graph_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_composes_singleton(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        # Direct construction of ExecutionGraphProgressTracker
        # is forbidden — must compose the singleton accessor.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "ExecutionGraphProgressTracker"
                ):
                    violations.append(
                        "graph_repl.py MUST NOT construct "
                        "ExecutionGraphProgressTracker directly "
                        "— compose get_default_tracker()"
                    )
        # Confirm get_default_tracker is imported.
        found_singleton_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "execution_graph_progress" in node.module
                ):
                    for alias in node.names:
                        if alias.name == "get_default_tracker":
                            found_singleton_import = True
        if not found_singleton_import:
            violations.append(
                "graph_repl.py MUST compose "
                "execution_graph_progress.get_default_tracker"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="graph_repl_authority_read_only",
            target_file=target,
            description=(
                "Path D.1 — REPL composes read APIs only."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name="graph_repl_authority_asymmetry",
            target_file=target,
            description=(
                "Path D.1 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="graph_repl_composes_singleton",
            target_file=target,
            description=(
                "Path D.1 — single pipeline; composes "
                "get_default_tracker only."
            ),
            validate=_validate_composes_singleton,
        ),
    ]


__all__ = [
    "GraphReplDispatchResult",
    "dispatch_graph_command",
    "register_shipped_invariants",
    "register_verbs",
]
