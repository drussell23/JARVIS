"""Path D.3 — `/events` REPL operator surface for L1 EventEmitter.

Closes the §36.6 "6 unwired autonomy modules" entry for
``event_emitter`` (autonomy/) — composes the new
:meth:`EventEmitter.snapshot_all` classmethod (Class-level
Instance Registry pattern) so operators see emission metrics
across ALL live emitters (governed_loop_service +
safety_net + execution_graph_progress each construct their
own; no global singleton).

Auto-discovered via §32.11 Slice 4 ``repl_dispatch_registry``
naming-cage convention (file ends ``_repl.py`` → verb
``/events`` → dispatcher ``dispatch_events_command(line)``).
Zero edits to the registry.

Subcommands:

  * ``/events``       — aggregate snapshot across all live
                        emitters (instance count, totals,
                        by-event-type breakdown)
  * ``/events stats`` — same, dict-shaped projection
  * ``/events help``  — bypass-master help

Architectural locks:

  * **Read-only** — REPL composes ``snapshot_all`` only; no
    mutating calls (no ``subscribe`` / ``emit`` / etc.). AST-pinned.
  * **Authority asymmetry** — substrate purity (no orchestrator
    / iron_gate / policy / providers imports). AST-pinned.
  * **Composes substrate** — REPL imports ``EventEmitter``
    classmethod ONLY. AST-pinned.
  * **NEVER raises** — every dispatch path returns a structured
    result.

Identity preservation: cyan default, dim metadata, NO
``bright_green``.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_RED = "\033[31m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class EventsReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/events — L1 EventEmitter browser{_RESET}"
    f"\n"
    f"  {_DIM}Read-only operator surface aggregating across "
    f"all live EventEmitter instances (Class-level Instance "
    f"Registry pattern).{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/events{_RESET}        "
    f"{_DIM}aggregate snapshot{_RESET}\n"
    f"    {_CYAN}/events stats{_RESET}  "
    f"{_DIM}dict-shaped projection{_RESET}\n"
    f"    {_CYAN}/events help{_RESET}   "
    f"{_DIM}this message{_RESET}\n"
)


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/events"
        or s == "events"
        or s.startswith("/events ")
        or s.startswith("events ")
    )


def _get_aggregate():
    """Lazy-import the canonical classmethod. NEVER raises;
    returns None on substrate unavailability."""
    try:
        from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
            EventEmitter,
        )
        return EventEmitter.snapshot_all()
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


def _render_snapshot() -> str:
    agg = _get_aggregate()
    if agg is None:
        return (
            f"\n  {_RED}event_emitter substrate unavailable"
            f"{_RESET}\n"
        )
    instance_count = int(agg.get("instance_count", 0))
    total_emissions = int(agg.get("total_emissions", 0))
    total_subscribers = int(agg.get("total_subscribers", 0))
    by_type = agg.get("by_event_type", {}) or {}
    out = [
        f"\n  {_BOLD}{_CYAN}EventEmitter aggregate{_RESET}  "
        f"{_DIM}(across {instance_count} live emitter{'s' if instance_count != 1 else ''}){_RESET}",
        "",
        f"  {_DIM}total_emissions:{_RESET}    {total_emissions}",
        f"  {_DIM}total_subscribers:{_RESET}  {total_subscribers}",
    ]
    if by_type:
        out.append("")
        out.append(f"  {_BOLD}By event type:{_RESET}")
        # Sort by emission count descending for high-traffic-first.
        sorted_types = sorted(
            by_type.items(),
            key=lambda kv: (
                -int(kv[1].get("emission_count", 0)),
                kv[0],
            ),
        )
        for et, m in sorted_types[:30]:
            ec = int(m.get("emission_count", 0))
            sc = int(m.get("subscriber_count", 0))
            out.append(
                f"    {_CYAN}{et:<32}{_RESET}  "
                f"{_DIM}emissions={ec:<6} "
                f"subscribers={sc}{_RESET}"
            )
    out.append("")
    return "\n".join(out) + "\n"


def _render_stats() -> str:
    agg = _get_aggregate()
    if agg is None:
        return (
            f"\n  {_RED}event_emitter substrate unavailable"
            f"{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}EventEmitter.snapshot_all(){_RESET}",
        "",
    ]
    for k, v in agg.items():
        if isinstance(v, dict):
            out.append(f"  {_DIM}{k}:{_RESET}")
            for sk, sv in sorted(v.items()):
                out.append(f"    {_DIM}{sk:<32}{_RESET}{sv}")
        else:
            out.append(f"  {_DIM}{k:<22}{_RESET}{v}")
    out.append("")
    return "\n".join(out) + "\n"


def dispatch_events_command(
    line: str,
) -> EventsReplDispatchResult:
    if not _matches(line):
        return EventsReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return EventsReplDispatchResult(
            ok=False, text=f"  /events parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")
    if head in ("help", "?"):
        return EventsReplDispatchResult(ok=True, text=_HELP)
    try:
        if head == "":
            return EventsReplDispatchResult(
                ok=True, text=_render_snapshot(),
            )
        if head == "stats":
            return EventsReplDispatchResult(
                ok=True, text=_render_stats(),
            )
        return EventsReplDispatchResult(
            ok=False,
            text=(
                f"  /events: unknown subcommand "
                f"{head!r} — try /events help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return EventsReplDispatchResult(
            ok=False,
            text=f"  /events: error — {exc}. Try again.",
        )


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="events",
            description=(
                "L1 EventEmitter browser — aggregates across "
                "all live emitters (governed_loop_service + "
                "safety_net + execution_graph_progress). "
                "Read-only."
            ),
            posture_relevance="RELEVANT",
            since="Path D.3 (PRD §36.6, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``events_repl_authority_read_only`` — REPL never calls
         mutating methods (subscribe / emit / unsubscribe).
      2. ``events_repl_authority_asymmetry`` — substrate purity.
      3. ``events_repl_composes_classmethod`` — REPL composes
         ``EventEmitter.snapshot_all`` only; no parallel
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
        "backend/core/ouroboros/governance/events_repl.py"
    )

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_methods = (
            "subscribe", "emit", "unsubscribe",
            "_bridge_to_spine",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in forbidden_methods
                ):
                    # Allow .subscribe-like calls on registries
                    # (verb registration) — only flag receivers
                    # that look like an emitter.
                    if isinstance(fn.value, ast.Name):
                        rcv = fn.value.id.lower()
                        if (
                            "emitter" in rcv
                            or "event" in rcv
                        ):
                            violations.append(
                                f"events_repl.py is read-only;"
                                f" MUST NOT call .{fn.attr}() "
                                f"on emitter receiver"
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
                            f"events_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_composes_classmethod(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        # Direct construction of EventEmitter is forbidden.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "EventEmitter"
                ):
                    violations.append(
                        "events_repl.py MUST NOT construct "
                        "EventEmitter directly — compose "
                        "EventEmitter.snapshot_all classmethod"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="events_repl_authority_read_only",
            target_file=target,
            description=(
                "Path D.3 — REPL is read-only browser."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name="events_repl_authority_asymmetry",
            target_file=target,
            description=(
                "Path D.3 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "events_repl_composes_classmethod"
            ),
            target_file=target,
            description=(
                "Path D.3 — single pipeline; composes "
                "EventEmitter.snapshot_all classmethod only."
            ),
            validate=_validate_composes_classmethod,
        ),
    ]


__all__ = [
    "EventsReplDispatchResult",
    "dispatch_events_command",
    "register_shipped_invariants",
    "register_verbs",
]
