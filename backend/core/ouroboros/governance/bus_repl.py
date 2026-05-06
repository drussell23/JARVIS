"""Path D.4 — `/bus` REPL operator surface for L1 CommandBus.

Closes the §36.6 "6 unwired autonomy modules" entry for
``command_bus`` (autonomy/) — composes the new
:meth:`CommandBus.snapshot_all` classmethod (Class-level
Instance Registry pattern) so operators see dispatch metrics
+ back-pressure rejection counters across ALL live buses
(governed_loop_service + subagent_scheduler +
advanced_coordination + safety_net + feedback_engine each
construct their own; no global singleton).

Auto-discovered via §32.11 Slice 4 ``repl_dispatch_registry``
naming-cage convention (file ends ``_repl.py`` → verb
``/bus`` → dispatcher ``dispatch_bus_command(line)``). Zero
edits to the registry.

Subcommands:

  * ``/bus``       — aggregate snapshot across all live buses
                     (instance count, qsize totals, dispatch
                     totals, rejection counters, by-command-
                     type breakdown)
  * ``/bus stats`` — dict-shaped projection
  * ``/bus help``  — bypass-master help

Architectural locks (mirror :mod:`events_repl`):

  * **Read-only** — REPL composes ``snapshot_all`` only; no
    mutating calls (no ``put`` / ``get`` / etc.). AST-pinned.
  * **Authority asymmetry** — substrate purity (no orchestrator
    / iron_gate / policy / providers imports). AST-pinned.
  * **Composes substrate** — REPL imports ``CommandBus``
    classmethod ONLY. AST-pinned.
  * **NEVER raises** — every dispatch path returns a structured
    result.

Identity preservation: cyan default, yellow elevated rejection,
red high back-pressure, dim metadata. NO ``bright_green``.
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
class BusReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/bus — L1 CommandBus browser{_RESET}\n"
    f"  {_DIM}Read-only operator surface aggregating across "
    f"all live CommandBus instances (Class-level Instance "
    f"Registry pattern).{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/bus{_RESET}        "
    f"{_DIM}aggregate snapshot{_RESET}\n"
    f"    {_CYAN}/bus stats{_RESET}  "
    f"{_DIM}dict-shaped projection{_RESET}\n"
    f"    {_CYAN}/bus help{_RESET}   "
    f"{_DIM}this message{_RESET}\n"
)


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/bus"
        or s == "bus"
        or s.startswith("/bus ")
        or s.startswith("bus ")
    )


def _color_for_rejection_ratio(ratio: float) -> str:
    """Color heuristic — cost-band-style ladder for back-
    pressure rejection ratios."""
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return _DIM
    if r >= 0.10:
        return _RED
    if r >= 0.02:
        return _YELLOW
    if r > 0.0:
        return _CYAN
    return _DIM


def _get_aggregate():
    try:
        from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
            CommandBus,
        )
        return CommandBus.snapshot_all()
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


def _render_snapshot() -> str:
    agg = _get_aggregate()
    if agg is None:
        return (
            f"\n  {_RED}command_bus substrate unavailable"
            f"{_RESET}\n"
        )
    instance_count = int(agg.get("instance_count", 0))
    total_qsize = int(agg.get("total_qsize", 0))
    total_dispatched = int(agg.get("total_dispatched", 0))
    rd = int(agg.get("total_rejected_dedup", 0))
    rb = int(agg.get("total_rejected_backpressure", 0))
    by_type = agg.get("by_command_type", {}) or {}
    # Compute rejection ratio for color heuristic
    total_attempted = total_dispatched + rd + rb
    bp_ratio = (
        float(rb) / float(total_attempted)
        if total_attempted > 0 else 0.0
    )
    out = [
        f"\n  {_BOLD}{_CYAN}CommandBus aggregate{_RESET}  "
        f"{_DIM}(across {instance_count} live bus{'es' if instance_count != 1 else ''}){_RESET}",
        "",
        f"  {_DIM}qsize_total:{_RESET}              "
        f"{total_qsize}",
        f"  {_DIM}dispatched_total:{_RESET}         "
        f"{total_dispatched}",
        f"  {_DIM}rejected_dedup:{_RESET}           "
        f"{rd}",
        f"  {_DIM}rejected_backpressure:{_RESET}    "
        f"{_color_for_rejection_ratio(bp_ratio)}{rb}{_RESET}  "
        f"{_DIM}({bp_ratio:.1%}){_RESET}",
    ]
    if by_type:
        out.append("")
        out.append(f"  {_BOLD}By command type:{_RESET}")
        sorted_types = sorted(
            by_type.items(),
            key=lambda kv: (-int(kv[1]), kv[0]),
        )
        for ct, count in sorted_types[:30]:
            out.append(
                f"    {_CYAN}{ct:<32}{_RESET}  "
                f"{_DIM}{count}{_RESET}"
            )
    out.append("")
    return "\n".join(out) + "\n"


def _render_stats() -> str:
    agg = _get_aggregate()
    if agg is None:
        return (
            f"\n  {_RED}command_bus substrate unavailable"
            f"{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}CommandBus.snapshot_all(){_RESET}",
        "",
    ]
    for k, v in agg.items():
        if isinstance(v, dict):
            out.append(f"  {_DIM}{k}:{_RESET}")
            for sk, sv in sorted(v.items()):
                out.append(f"    {_DIM}{sk:<32}{_RESET}{sv}")
        else:
            out.append(f"  {_DIM}{k:<32}{_RESET}{v}")
    out.append("")
    return "\n".join(out) + "\n"


def dispatch_bus_command(
    line: str,
) -> BusReplDispatchResult:
    if not _matches(line):
        return BusReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return BusReplDispatchResult(
            ok=False, text=f"  /bus parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")
    if head in ("help", "?"):
        return BusReplDispatchResult(ok=True, text=_HELP)
    try:
        if head == "":
            return BusReplDispatchResult(
                ok=True, text=_render_snapshot(),
            )
        if head == "stats":
            return BusReplDispatchResult(
                ok=True, text=_render_stats(),
            )
        return BusReplDispatchResult(
            ok=False,
            text=(
                f"  /bus: unknown subcommand "
                f"{head!r} — try /bus help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return BusReplDispatchResult(
            ok=False,
            text=f"  /bus: error — {exc}. Try again.",
        )


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="bus",
            description=(
                "L1 CommandBus browser — aggregates across all "
                "live buses (5 internal consumers). Read-only."
            ),
            posture_relevance="RELEVANT",
            since="Path D.4 (PRD §36.6, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``bus_repl_authority_read_only`` — REPL never calls
         mutating methods (put / get / try_put).
      2. ``bus_repl_authority_asymmetry`` — substrate purity.
      3. ``bus_repl_composes_classmethod`` — REPL composes
         ``CommandBus.snapshot_all`` only.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/bus_repl.py"
    )

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_methods = ("put", "try_put", "get")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in forbidden_methods
                    and isinstance(fn.value, ast.Name)
                ):
                    rcv = fn.value.id.lower()
                    if "bus" in rcv:
                        violations.append(
                            f"bus_repl.py is read-only; MUST "
                            f"NOT call .{fn.attr}() on bus "
                            f"receiver"
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
                            f"bus_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_composes_classmethod(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "CommandBus"
                ):
                    violations.append(
                        "bus_repl.py MUST NOT construct "
                        "CommandBus directly — compose "
                        "CommandBus.snapshot_all classmethod"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="bus_repl_authority_read_only",
            target_file=target,
            description=(
                "Path D.4 — REPL is read-only browser."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name="bus_repl_authority_asymmetry",
            target_file=target,
            description=(
                "Path D.4 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "bus_repl_composes_classmethod"
            ),
            target_file=target,
            description=(
                "Path D.4 — single pipeline; composes "
                "CommandBus.snapshot_all classmethod only."
            ),
            validate=_validate_composes_classmethod,
        ),
    ]


__all__ = [
    "BusReplDispatchResult",
    "dispatch_bus_command",
    "register_shipped_invariants",
    "register_verbs",
]
