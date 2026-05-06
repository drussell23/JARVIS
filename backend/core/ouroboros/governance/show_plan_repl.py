"""§37 Slice 6 — `/show_plan` REPL surface composing canonical
SSE broker history (event_type=plan_generated).

Closes Tier 1 #3 from the §37 UX roadmap: surfaces the model-
reasoned PLAN-phase output (schema plan.1) that until now lived
only in `OperationContext.implementation_plan` (transient) +
GENERATE-prompt injection (one-shot consumed). Per the operator
binding "fully leverage existing files... no duplication":

  * **Single pipeline** — composes the canonical
    ``StreamEventBroker`` (Slice 2 territory). PlanGenerator
    publishes ``plan_generated`` events at PLAN-phase
    completion; this REPL reads via the same broker history
    that Slice 2's ``/listen`` reads from. NO new registry,
    NO parallel surface.
  * **Authority asymmetry / read-only** — REPL NEVER calls
    publish methods. Dashboard observes; PlanGenerator writes.
  * **Auto-discovered** — file ends `_repl.py` per §32.11
    Slice 4 naming-cage; verb name ``show_plan``; dispatcher
    ``dispatch_show_plan_command(line)``.
  * **Honest empty-state** — no plan events yet → transparent
    "no plans recorded" message rather than fabricated state.
  * **NEVER raises** — pure-function dispatch.

Subcommands:

  * ``/show_plan`` (bare)        — most-recent plan (full
                                   structured render)
  * ``/show_plan recent [N]``    — list last N plan events
                                   (one line each)
  * ``/show_plan op <op_id>``    — plan for a specific op_id
                                   (full structured render)
  * ``/show_plan complexity``    — distribution of plan
                                   complexities in history
  * ``/show_plan help``          — bypass-master help

Identity preservation: complexity coloring respects palette
(trivial=dim / moderate=cyan / complex=yellow / architectural=
red). NO ``bright_green`` in chrome (pinned by Slice 4).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, List, Optional


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class ShowPlanReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/show_plan — model-reasoned PLAN-phase output"
    f"{_RESET}\n"
    f"  {_DIM}Read-only operator view of the schema plan.1 "
    f"output published by PlanGenerator at PLAN-phase "
    f"completion. Composes canonical SSE broker history."
    f"{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/show_plan{_RESET}                 "
    f"{_DIM}most-recent plan (full structured render){_RESET}\n"
    f"    {_CYAN}/show_plan recent [N]{_RESET}      "
    f"{_DIM}list last N plan events (one line each){_RESET}\n"
    f"    {_CYAN}/show_plan op <op_id>{_RESET}      "
    f"{_DIM}plan for specific op_id (full render){_RESET}\n"
    f"    {_CYAN}/show_plan complexity{_RESET}      "
    f"{_DIM}distribution of plan complexities{_RESET}\n"
    f"    {_CYAN}/show_plan help{_RESET}            "
    f"{_DIM}this message{_RESET}\n"
)

_DEFAULT_RECENT_LIMIT = 10
_MAX_RECENT_LIMIT = 100


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/show_plan"
        or s == "show_plan"
        or s.startswith("/show_plan ")
        or s.startswith("show_plan ")
    )


def _color_for_complexity(complexity: str) -> str:
    """Identity-consistent complexity coloring."""
    c = (complexity or "").lower()
    if c == "trivial":
        return _DIM
    if c == "moderate":
        return _CYAN
    if c == "complex":
        return _YELLOW
    if c == "architectural":
        return _RED
    return _DIM


def _parse_limit(args: List[str]) -> int:
    if not args:
        return _DEFAULT_RECENT_LIMIT
    try:
        n = int(args[0])
    except (ValueError, TypeError):
        return _DEFAULT_RECENT_LIMIT
    if n < 1:
        return 1
    if n > _MAX_RECENT_LIMIT:
        return _MAX_RECENT_LIMIT
    return n


def _read_plan_events(limit: int = 100) -> List[Any]:
    """Read recent plan_generated events from canonical broker.
    Lazy-imported; defensive — returns empty list on broker
    unavailability."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PLAN_GENERATED,
            get_default_broker,
        )
    except ImportError:
        return []
    try:
        broker = get_default_broker()
        if broker is None:
            return []
        return broker.recent_history(
            limit=limit,
            event_type=EVENT_TYPE_PLAN_GENERATED,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []


def _format_plan_one_liner(event: Any) -> str:
    """Compact one-line summary of a plan event."""
    payload = event.payload or {}
    complexity = str(payload.get("complexity", "?"))
    skipped = bool(payload.get("skipped", False))
    n_changes = len(payload.get("ordered_changes", []) or [])
    n_risks = len(payload.get("risk_factors", []) or [])
    op_id = (event.op_id[:12] if event.op_id else "-")
    timestamp = (
        event.timestamp[11:19] if event.timestamp else ""
    )
    if skipped:
        skip_reason = str(payload.get("skip_reason", ""))[:40]
        return (
            f"  {_DIM}{timestamp}{_RESET}  "
            f"{_DIM}op={op_id}{_RESET}  "
            f"{_DIM}[skipped: {skip_reason}]{_RESET}"
        )
    color = _color_for_complexity(complexity)
    return (
        f"  {_DIM}{timestamp}{_RESET}  "
        f"{_DIM}op={op_id}{_RESET}  "
        f"{color}{complexity}{_RESET}  "
        f"{_DIM}changes={n_changes} risks={n_risks}{_RESET}"
    )


def _format_plan_full(event: Any) -> str:
    """Full structured render of a single plan event."""
    payload = event.payload or {}
    complexity = str(payload.get("complexity", "?"))
    skipped = bool(payload.get("skipped", False))
    op_id = event.op_id or "-"
    timestamp = event.timestamp or ""
    color = _color_for_complexity(complexity)
    out = [
        f"\n  {_BOLD}{_CYAN}Plan{_RESET}  "
        f"{_DIM}op={op_id}  ts={timestamp}{_RESET}",
        "",
    ]
    if skipped:
        out.append(
            f"  {_DIM}[Plan SKIPPED]{_RESET}  reason: "
            f"{payload.get('skip_reason', '(unknown)')}"
        )
        out.append(
            f"  {_DIM}complexity:{_RESET} "
            f"{color}{complexity}{_RESET}"
        )
        return "\n".join(out) + "\n"
    out.append(
        f"  {_DIM}complexity:{_RESET}  "
        f"{color}{complexity}{_RESET}"
    )
    duration = float(payload.get("planning_duration_s", 0.0))
    out.append(
        f"  {_DIM}duration:{_RESET}    "
        f"{duration:.2f}s"
    )
    ui_affected = bool(payload.get("ui_affected", False))
    if ui_affected:
        out.append(
            f"  {_DIM}ui_affected:{_RESET} "
            f"{_YELLOW}yes (Visual VERIFY trigger){_RESET}"
        )
    approach = str(payload.get("approach", "")).strip()
    if approach:
        out.extend([
            "",
            f"  {_BOLD}Approach:{_RESET}",
            f"    {approach}",
        ])
    ordered_changes = payload.get("ordered_changes", []) or []
    if ordered_changes:
        out.extend([
            "",
            f"  {_BOLD}Ordered changes ({len(ordered_changes)}):"
            f"{_RESET}",
        ])
        for i, change in enumerate(ordered_changes[:20], 1):
            if isinstance(change, dict):
                file_path = str(
                    change.get("file_path", "(unknown)"),
                )
                desc = str(
                    change.get("description", ""),
                ).strip()
                out.append(
                    f"  {_DIM}{i:>2}.{_RESET} "
                    f"{_CYAN}{file_path}{_RESET}"
                )
                if desc:
                    out.append(f"      {_DIM}{desc[:200]}{_RESET}")
            else:
                out.append(
                    f"  {_DIM}{i:>2}.{_RESET} "
                    f"{str(change)[:200]}"
                )
        if len(ordered_changes) > 20:
            out.append(
                f"      {_DIM}... +{len(ordered_changes) - 20} "
                f"more changes truncated{_RESET}"
            )
    risks = payload.get("risk_factors", []) or []
    if risks:
        out.extend([
            "",
            f"  {_BOLD}{_YELLOW}Risk factors ({len(risks)}):"
            f"{_RESET}",
        ])
        for r in risks[:10]:
            out.append(f"  {_YELLOW}⚠{_RESET}  {str(r)[:200]}")
        if len(risks) > 10:
            out.append(
                f"      {_DIM}... +{len(risks) - 10} more "
                f"risks truncated{_RESET}"
            )
    test_strategy = str(payload.get("test_strategy", "")).strip()
    if test_strategy:
        out.extend([
            "",
            f"  {_BOLD}Test strategy:{_RESET}",
            f"    {test_strategy[:500]}",
        ])
    arch_notes = str(payload.get("architectural_notes", "")).strip()
    if arch_notes:
        out.extend([
            "",
            f"  {_BOLD}Architectural notes:{_RESET}",
            f"    {arch_notes[:500]}",
        ])
    return "\n".join(out) + "\n"


def _empty_state_message() -> str:
    return (
        f"\n  {_BOLD}{_CYAN}Plan Stream{_RESET}\n"
        f"  {_DIM}No plan_generated events in broker history "
        f"yet — the PLAN phase hasn't fired in this session "
        f"(or master flag is off).{_RESET}\n"
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_most_recent() -> str:
    events = _read_plan_events(limit=1)
    if not events:
        return _empty_state_message()
    return _format_plan_full(events[-1])


def _render_recent(limit: int) -> str:
    events = _read_plan_events(limit=limit)
    if not events:
        return _empty_state_message()
    out = [
        f"\n  {_BOLD}{_CYAN}Recent Plans{_RESET}  "
        f"{_DIM}(showing {len(events)} most recent){_RESET}",
        "",
    ]
    for ev in events:
        out.append(_format_plan_one_liner(ev))
    out.append("")
    out.append(
        f"  {_DIM}Use /show_plan op <op_id> for full plan "
        f"detail.{_RESET}",
    )
    return "\n".join(out) + "\n"


def _render_by_op(op_id: str) -> str:
    events = _read_plan_events(limit=200)
    matches = [e for e in events if e.op_id == op_id]
    if not matches:
        # Try prefix match (operators paste short ids)
        matches = [
            e for e in events if e.op_id.startswith(op_id)
        ]
    if not matches:
        return (
            f"\n  {_RED}No plan event matches op_id "
            f"{op_id!r}.{_RESET}\n"
            f"  {_DIM}Use /show_plan recent to see available "
            f"op_ids.{_RESET}\n"
        )
    if len(matches) > 1:
        out = [
            f"\n  {_YELLOW}Multiple plans match prefix "
            f"{op_id!r}; showing the most recent.{_RESET}",
        ]
        out.append(_format_plan_full(matches[-1]))
        return "\n".join(out)
    return _format_plan_full(matches[0])


def _render_complexity_distribution() -> str:
    events = _read_plan_events(limit=_MAX_RECENT_LIMIT)
    if not events:
        return _empty_state_message()
    buckets = {
        "trivial": 0, "moderate": 0,
        "complex": 0, "architectural": 0, "skipped": 0,
        "(other)": 0,
    }
    for ev in events:
        payload = ev.payload or {}
        if payload.get("skipped"):
            buckets["skipped"] += 1
            continue
        c = str(payload.get("complexity", "")).lower()
        if c in buckets:
            buckets[c] += 1
        else:
            buckets["(other)"] += 1
    out = [
        f"\n  {_BOLD}{_CYAN}Plan Complexity Distribution"
        f"{_RESET}  {_DIM}({len(events)} plans){_RESET}",
        "",
    ]
    for complexity, count in buckets.items():
        if count == 0:
            continue
        color = _color_for_complexity(complexity)
        out.append(
            f"  {color}{complexity:>15}{_RESET}  "
            f"{_DIM}{count}{_RESET}"
        )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_show_plan_command(
    line: str,
) -> ShowPlanReplDispatchResult:
    """Parse a ``/show_plan`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return ShowPlanReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ShowPlanReplDispatchResult(
            ok=False,
            text=f"  /show_plan parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return ShowPlanReplDispatchResult(ok=True, text=_HELP)

    try:
        if head == "":
            return ShowPlanReplDispatchResult(
                ok=True, text=_render_most_recent(),
            )
        if head == "recent":
            limit = _parse_limit(args[1:])
            return ShowPlanReplDispatchResult(
                ok=True, text=_render_recent(limit),
            )
        if head == "op":
            if len(args) < 2:
                return ShowPlanReplDispatchResult(
                    ok=False,
                    text=(
                        "  /show_plan op <op_id> — op_id "
                        "(or prefix) required"
                    ),
                )
            return ShowPlanReplDispatchResult(
                ok=True, text=_render_by_op(args[1]),
            )
        if head == "complexity":
            return ShowPlanReplDispatchResult(
                ok=True,
                text=_render_complexity_distribution(),
            )
        return ShowPlanReplDispatchResult(
            ok=False,
            text=(
                f"  /show_plan: unknown subcommand "
                f"{head!r} — try /show_plan help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ShowPlanReplDispatchResult(
            ok=False,
            text=(
                f"  /show_plan: error reading broker — "
                f"{exc}. Try again after subsystems boot."
            ),
        )


# ---------------------------------------------------------------------------
# /help auto-discovery hook
# ---------------------------------------------------------------------------


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="show_plan",
            description=(
                "Plan inspection — read-only view of the "
                "model-reasoned PLAN-phase output (schema "
                "plan.1) for recent ops. Composes canonical "
                "SSE broker history (event_type=plan_generated)."
            ),
            posture_relevance="RELEVANT",
            since="§37 Slice 6 (PRD §36.5, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``show_plan_repl_composes_canonical_broker`` — module
         reads via `get_default_broker()` ONLY; never
         constructs broker.
      2. ``show_plan_repl_authority_read_only`` — module NEVER
         calls broker.publish*() methods.
      3. ``show_plan_repl_authority_asymmetry`` — substrate
         purity (no orchestrator / iron_gate / providers
         imports).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/show_plan_repl.py"
    )

    def _validate_composes_canonical_broker(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "StreamEventBroker"
                ):
                    violations.append(
                        "show_plan_repl.py MUST NOT construct "
                        "StreamEventBroker — compose "
                        "get_default_broker()"
                    )
        return tuple(violations)

    def _validate_authority_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if not func.attr.startswith("publish"):
                    continue
                receiver = func.value
                if (
                    isinstance(receiver, ast.Name)
                    and (
                        receiver.id == "broker"
                        or receiver.id.endswith("_broker")
                    )
                ):
                    violations.append(
                        f"show_plan_repl.py MUST NOT call "
                        f"broker.{func.attr}() — read-only"
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
                            f"show_plan_repl.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "show_plan_repl_composes_canonical_broker"
            ),
            target_file=target,
            description=(
                "§37 Slice 6 — single-pipeline guardrail."
            ),
            validate=_validate_composes_canonical_broker,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "show_plan_repl_authority_read_only"
            ),
            target_file=target,
            description=(
                "§37 Slice 6 — read-only operator surface."
            ),
            validate=_validate_authority_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "show_plan_repl_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Slice 6 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "ShowPlanReplDispatchResult",
    "dispatch_show_plan_command",
    "register_shipped_invariants",
    "register_verbs",
]
