"""§37 Slice 1 — `/health` REPL surface composing
:mod:`component_health.ComponentHealthTracker`.

Closes Tier 1 #4 from the §37 UX roadmap: surfaces per-component
state + transition history that until now was scoped to L3
SafetyNet's private `_health_tracker` instance and never operator-
visible. Per the operator binding "fully leverage the existing
files and architecture within the codebase so we avoid
duplication and build cleanly on what already exists" — this
module composes the existing `ComponentHealthTracker` substrate
via the new `get_default_tracker()` accessor (PRD §37 Slice 1
extension); SafetyNet now defaults to the same singleton, so
state already populated by SafetyNet flows directly into this
operator surface.

Architectural locks (operator binding 2026-05-05):

  * **Single pipeline** — read state via the canonical
    `get_default_tracker()` singleton ONLY. Forbidden to
    construct a new ComponentHealthTracker here (would create
    a stale, empty parallel surface). AST-pinned.
  * **Authority asymmetry** — REPL is READ-ONLY. Never calls
    `update()` / `register()`. The dashboard observes; producers
    (SafetyNet etc.) write.
  * **Auto-discovered** — file ends `_repl.py` per §32.11 Slice 4
    naming-cage convention; verb name `health` derived from
    basename; `dispatch_health_command(line)` matches naming
    convention exactly. Auto-mounts via `repl_dispatch_registry`.
  * **Master-flag honest UX** — gracefully renders "no data"
    rather than fabricating progress. Empty tracker prints a
    transparent guidance line.
  * **NEVER raises** — pure-function dispatch.

Subcommands:

  * ``/health`` (bare)        — overview: aggregate health,
                                 unhealthy + needs_attention buckets
  * ``/health components``    — list every registered component
  * ``/health show <name>``   — detail for one component
  * ``/health history [N]``   — recent transitions (default 20)
  * ``/health unhealthy``     — only components below health bar
  * ``/health help``          — bypass-master help

Identity preservation (PRD §37.9): uses standard color discipline
(green=healthy outcome / red=error / yellow=needs_attention /
dim=metadata). No `bright_green` in chrome (pinned by Slice 4).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import List, Optional


# ---------------------------------------------------------------------------
# ANSI palette — identity-consistent (green=outcomes / red=errors / etc.)
# ---------------------------------------------------------------------------


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


# ---------------------------------------------------------------------------
# Frozen result envelope (mirrors decisions_repl shape — pattern parity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthReplDispatchResult:
    """Result of a ``/health`` dispatch. Frozen for safe propagation.
    ``matched=False`` signals the line wasn't a ``/health`` invocation
    (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/health — component health dashboard{_RESET}\n"
    f"  {_DIM}Read-only operator view of every registered "
    f"component's state + transition history.{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/health{_RESET}              "
    f"{_DIM}aggregate health + unhealthy/attention buckets{_RESET}\n"
    f"    {_CYAN}/health components{_RESET}   "
    f"{_DIM}list every registered component{_RESET}\n"
    f"    {_CYAN}/health show <name>{_RESET}  "
    f"{_DIM}detail for one component{_RESET}\n"
    f"    {_CYAN}/health history [N]{_RESET}  "
    f"{_DIM}recent transitions (default 20, max 200){_RESET}\n"
    f"    {_CYAN}/health unhealthy{_RESET}    "
    f"{_DIM}only components below health bar{_RESET}\n"
    f"    {_CYAN}/health help{_RESET}         "
    f"{_DIM}this message{_RESET}\n"
)

_DEFAULT_HISTORY_LIMIT = 20
_MAX_HISTORY_LIMIT = 200


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/health"
        or s == "health"
        or s.startswith("/health ")
        or s.startswith("health ")
    )


def _color_for_state(state_name: str) -> str:
    """Identity-consistent state coloring — green=outcomes only
    (healthy/active are positive operational outcomes; error=red;
    needs_attention=yellow; chrome=dim)."""
    if state_name in ("READY", "ACTIVE"):
        return _GREEN
    if state_name == "ERROR":
        return _RED
    if state_name == "BUSY":
        return _YELLOW
    return _DIM


def _format_status_line(comp) -> str:
    """One-line component summary."""
    state_color = _color_for_state(comp.state.name)
    healthy_marker = (
        f"{_GREEN}✓{_RESET}" if comp.is_healthy
        else f"{_RED}✗{_RESET}"
    )
    return (
        f"  {healthy_marker} {_BOLD}{comp.name}{_RESET}"
        f"  {state_color}[{comp.state.name}]{_RESET}"
        f"  {_DIM}health={comp.health_score:.2f}"
        f"  errors={comp.error_count}{_RESET}"
    )


def _format_transition(t) -> str:
    """One-line transition summary for history."""
    return (
        f"  {_DIM}{t.component_name:>20}{_RESET}  "
        f"{_color_for_state(t.from_state.name)}{t.from_state.name}{_RESET}"
        f" {_DIM}→{_RESET} "
        f"{_color_for_state(t.to_state.name)}{t.to_state.name}{_RESET}"
        f"  {_DIM}({t.reason.value}){_RESET}"
    )


def _parse_limit(args: List[str]) -> int:
    """Parse optional integer limit arg with sane clamping."""
    if not args:
        return _DEFAULT_HISTORY_LIMIT
    try:
        n = int(args[0])
    except (ValueError, TypeError):
        return _DEFAULT_HISTORY_LIMIT
    if n < 1:
        return 1
    if n > _MAX_HISTORY_LIMIT:
        return _MAX_HISTORY_LIMIT
    return n


# ---------------------------------------------------------------------------
# Renderers — read via canonical singleton ONLY (single-pipeline guardrail)
# ---------------------------------------------------------------------------


def _render_overview() -> str:
    """Aggregate health + unhealthy + needs_attention buckets."""
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        get_default_tracker,
    )
    tracker = get_default_tracker()
    components = tracker.all_components()
    if not components:
        return (
            f"\n  {_BOLD}{_CYAN}Component Health{_RESET}\n"
            f"  {_DIM}No components registered yet — SafetyNet "
            f"and other producers will populate this surface as "
            f"the session progresses.{_RESET}\n"
        )
    aggregate = tracker.get_aggregate_health()
    unhealthy = tracker.get_unhealthy()
    needs_attn = tracker.get_needs_attention()
    n_total = len(components)
    n_healthy = sum(1 for c in components if c.is_healthy)
    health_color = _GREEN if aggregate >= 0.8 else (
        _YELLOW if aggregate >= 0.5 else _RED
    )
    out = [
        f"\n  {_BOLD}{_CYAN}Component Health{_RESET}  "
        f"{_DIM}({n_total} components){_RESET}",
        f"  aggregate={health_color}{aggregate:.2f}{_RESET}  "
        f"healthy={_GREEN}{n_healthy}/{n_total}{_RESET}  "
        f"unhealthy={_RED if unhealthy else _DIM}"
        f"{len(unhealthy)}{_RESET}  "
        f"needs_attention={_YELLOW if needs_attn else _DIM}"
        f"{len(needs_attn)}{_RESET}",
        "",
    ]
    if unhealthy:
        out.append(
            f"  {_BOLD}{_RED}Unhealthy:{_RESET}",
        )
        for c in unhealthy:
            out.append(_format_status_line(c))
        out.append("")
    if needs_attn:
        out.append(
            f"  {_BOLD}{_YELLOW}Needs Attention:{_RESET}",
        )
        for c in needs_attn:
            out.append(_format_status_line(c))
        out.append("")
    out.append(
        f"  {_DIM}Use /health components for full list, "
        f"/health show <name> for detail.{_RESET}",
    )
    return "\n".join(out) + "\n"


def _render_components() -> str:
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        get_default_tracker,
    )
    components = get_default_tracker().all_components()
    if not components:
        return (
            f"\n  {_DIM}No components registered yet.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Components{_RESET}  "
        f"{_DIM}({len(components)} total){_RESET}",
        "",
    ]
    for c in sorted(components, key=lambda c: c.name):
        out.append(_format_status_line(c))
    return "\n".join(out) + "\n"


def _render_show(name: str) -> str:
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        get_default_tracker,
    )
    tracker = get_default_tracker()
    status = tracker.get_status(name)
    if status is None:
        registered = tracker.list_names()
        if not registered:
            hint = (
                "  No components registered yet — populate by "
                "running ops that exercise SafetyNet."
            )
        else:
            hint = (
                f"  Registered: "
                f"{', '.join(_BOLD + n + _RESET for n in registered)}"
            )
        return (
            f"\n  {_RED}Component {name!r} not registered.{_RESET}\n"
            f"{_DIM}{hint}{_RESET}\n"
        )
    history = tracker.get_history(name=name, limit=10)
    out = [
        f"\n  {_BOLD}{_CYAN}{status.name}{_RESET}",
        _format_status_line(status),
        "",
        f"  {_DIM}last_update_ns: {status.last_update_ns}{_RESET}",
    ]
    if status.metadata:
        out.append(
            f"  {_DIM}metadata: "
            f"{_format_metadata(status.metadata)}{_RESET}",
        )
    if history:
        out.append("")
        out.append(
            f"  {_BOLD}Recent transitions{_RESET}  "
            f"{_DIM}({len(history)}){_RESET}",
        )
        for t in history:
            out.append(_format_transition(t))
    return "\n".join(out) + "\n"


def _format_metadata(meta) -> str:
    """Best-effort one-line metadata dict render. NEVER raises."""
    try:
        if not isinstance(meta, dict) or not meta:
            return "(empty)"
        # Bound to first 5 keys to avoid wall-of-text.
        items = list(meta.items())[:5]
        rendered = ", ".join(
            f"{k}={_format_meta_value(v)}" for k, v in items
        )
        if len(meta) > 5:
            rendered += f", ... +{len(meta) - 5} more"
        return rendered
    except Exception:  # noqa: BLE001 — defensive
        return "(unrenderable)"


def _format_meta_value(v) -> str:
    """Truncate long values for one-line render."""
    s = str(v)
    if len(s) > 40:
        return s[:37] + "..."
    return s


def _render_history(limit: int) -> str:
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        get_default_tracker,
    )
    history = get_default_tracker().get_history(limit=limit)
    if not history:
        return (
            f"\n  {_DIM}No transitions recorded yet.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_CYAN}Recent Transitions{_RESET}  "
        f"{_DIM}({len(history)}, oldest first){_RESET}",
        "",
    ]
    for t in history:
        out.append(_format_transition(t))
    return "\n".join(out) + "\n"


def _render_unhealthy() -> str:
    from backend.core.ouroboros.governance.autonomy.component_health import (  # noqa: E501
        get_default_tracker,
    )
    unhealthy = get_default_tracker().get_unhealthy()
    if not unhealthy:
        return (
            f"\n  {_GREEN}All components healthy.{_RESET}\n"
        )
    out = [
        f"\n  {_BOLD}{_RED}Unhealthy components{_RESET}  "
        f"{_DIM}({len(unhealthy)}){_RESET}",
        "",
    ]
    for c in sorted(unhealthy, key=lambda c: c.name):
        out.append(_format_status_line(c))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher (auto-mounted via repl_dispatch_registry)
# ---------------------------------------------------------------------------


def dispatch_health_command(
    line: str,
) -> HealthReplDispatchResult:
    """Parse a ``/health`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return HealthReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return HealthReplDispatchResult(
            ok=False,
            text=f"  /health parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return HealthReplDispatchResult(ok=True, text=_HELP)

    try:
        if head == "":
            return HealthReplDispatchResult(
                ok=True, text=_render_overview(),
            )
        if head == "components":
            return HealthReplDispatchResult(
                ok=True, text=_render_components(),
            )
        if head == "show":
            if len(args) < 2:
                return HealthReplDispatchResult(
                    ok=False,
                    text=(
                        "  /health show <name> — component name "
                        "required"
                    ),
                )
            return HealthReplDispatchResult(
                ok=True, text=_render_show(args[1]),
            )
        if head == "history":
            limit = _parse_limit(args[1:])
            return HealthReplDispatchResult(
                ok=True, text=_render_history(limit),
            )
        if head == "unhealthy":
            return HealthReplDispatchResult(
                ok=True, text=_render_unhealthy(),
            )
        return HealthReplDispatchResult(
            ok=False,
            text=(
                f"  /health: unknown subcommand "
                f"{head!r} — try /health help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        # Tracker access may fail on cold-start (race with
        # SafetyNet boot). Defer rather than raise into REPL.
        return HealthReplDispatchResult(
            ok=False,
            text=(
                f"  /health: error reading tracker — {exc}. "
                f"Try again after subsystems boot."
            ),
        )


# ---------------------------------------------------------------------------
# /help auto-discovery hook
# ---------------------------------------------------------------------------


def register_verbs(registry) -> int:
    """Auto-discovered by `help_dispatcher`. Registers the
    `/health` verb in the operator-facing /help index."""
    try:
        registry.register(
            verb="health",
            description=(
                "Component health dashboard — aggregate state, "
                "unhealthy components, transition history. "
                "Read-only operator view of the SafetyNet + "
                "subsystem-managed component tracker."
            ),
            posture_relevance="RELEVANT",
            since="§37 Slice 1 (PRD §36.5, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins (auto-discovered via shipped_code_invariants)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``health_repl_composes_canonical_tracker`` — module
         reads via `get_default_tracker()` ONLY; never
         constructs `ComponentHealthTracker()` directly (would
         create stale parallel surface).
      2. ``health_repl_authority_read_only`` — module NEVER
         calls `update()` / `register()` on the tracker.
         Read-only operator surface; producers write elsewhere.
      3. ``health_repl_authority_asymmetry`` — substrate purity
         (no orchestrator / iron_gate / providers imports).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/health_repl.py"
    )

    def _validate_composes_canonical_tracker(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "ComponentHealthTracker"
                ):
                    violations.append(
                        "health_repl.py MUST NOT construct "
                        "ComponentHealthTracker() directly — "
                        "compose get_default_tracker() (single-"
                        "pipeline guardrail)"
                    )
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "ComponentHealthTracker"
                ):
                    violations.append(
                        "health_repl.py MUST NOT construct "
                        "ComponentHealthTracker() via attribute "
                        "access either"
                    )
        return tuple(violations)

    def _validate_authority_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT call mutating tracker methods."""
        violations: list = []
        forbidden_methods = ("update", "register")
        # Walk Call nodes; if the call is `<expr>.update(...)` or
        # `<expr>.register(...)` AND <expr> chain involves a
        # tracker reference, flag it. To keep AST scoping
        # tight, look for direct calls on names that contain
        # "tracker" — proxy for "this is the tracker handle."
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in forbidden_methods:
                    continue
                # Heuristic: if the call's receiver is a Name
                # whose id ends with "tracker" or is "tracker",
                # flag it.
                receiver = func.value
                if (
                    isinstance(receiver, ast.Name)
                    and (
                        receiver.id == "tracker"
                        or receiver.id.endswith("_tracker")
                    )
                ):
                    violations.append(
                        f"health_repl.py MUST NOT call "
                        f"tracker.{func.attr}(...) — read-only "
                        f"operator surface; producers write "
                        f"elsewhere"
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
                            f"health_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "health_repl_composes_canonical_tracker"
            ),
            target_file=target,
            description=(
                "§37 Slice 1 — single-pipeline guardrail: "
                "module composes get_default_tracker() "
                "singleton; never constructs ComponentHealth"
                "Tracker directly."
            ),
            validate=_validate_composes_canonical_tracker,
        ),
        ShippedCodeInvariant(
            invariant_name="health_repl_authority_read_only",
            target_file=target,
            description=(
                "§37 Slice 1 — read-only operator surface: "
                "module MUST NOT call tracker.update / "
                "tracker.register. Producers write; dashboard "
                "observes."
            ),
            validate=_validate_authority_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name="health_repl_authority_asymmetry",
            target_file=target,
            description=(
                "§37 Slice 1 — substrate purity: no "
                "orchestrator / iron_gate / policy / providers "
                "/ candidate_generator imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "HealthReplDispatchResult",
    "dispatch_health_command",
    "register_shipped_invariants",
    "register_verbs",
]
