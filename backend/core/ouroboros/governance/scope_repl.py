"""§37 Tier 2 #16 Slice 2 — `/scope` REPL verb (per-component
tool scope inspector + register).

Operator-facing surface for the ``component_tool_scope``
substrate (Slice 1). Auto-discovered via §32.11 Slice 4
naming-cage: file ``scope_repl.py`` → verb ``/scope`` →
dispatcher ``dispatch_scope_command(line)``.

Lets the operator inspect which components have registered
scopes, see what tools each component is allowed to use, and
verify in-session whether a specific (component, tool) pair
would be allowed/denied without firing a real op.

**Subcommands**:

  * ``/scope`` (bare) — list all registered components +
    their scope summaries.
  * ``/scope show <component-id>`` — full scope for one
    component (allowed_tools / denied_tools / inherits_from).
  * ``/scope check <component-id> <tool-name>`` — dry-run
    decision (ALLOW / DENY / NO_SCOPE / DISABLED).
  * ``/scope active`` — show currently-active component_id
    via the ContextVar (or empty when none).
  * ``/scope help`` — usage.

**Read-only browser** (mirrors ``replay_repl`` /
``history_repl`` / ``mode_repl`` / ``canvas_repl`` authority
asymmetry): operator queries the registry but never registers
new scopes via the REPL. Scope registration is done by sensor
/ subagent dispatch sites at boot or per-op. (Future Slice 3
could add operator-facing register surface — explicitly
deferred to keep Slice 2 minimal.)

**Composition**: single source of truth — composes
:func:`component_tool_scope.list_components` /
``get_scope`` / ``evaluate_component_scope`` /
``get_active_component``; no parallel state.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional


logger = logging.getLogger("Ouroboros.ScopeREPL")


_VERBS = ("/scope",)
_VALID_SUBCOMMANDS = {"show", "check", "active", "help"}


@dataclass
class ScopeDispatchResult:
    """Mirrors sibling REPL dispatch shape."""
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _VERBS


def dispatch_scope_command(line: str) -> ScopeDispatchResult:
    """Parse a ``/scope`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return ScopeDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ScopeDispatchResult(
            ok=False, text=f"/scope: parse error — {exc}",
        )
    args = tokens[1:] if len(tokens) > 1 else []
    if not args:
        return _render_overview()
    sub = args[0].lower()
    if sub not in _VALID_SUBCOMMANDS:
        return ScopeDispatchResult(
            ok=False,
            text=(
                f"/scope: unknown subcommand {sub!r}. "
                f"Try /scope help."
            ),
        )
    if sub == "help":
        return _render_help()
    if sub == "active":
        return _render_active()
    if sub == "show":
        if len(args) < 2:
            return ScopeDispatchResult(
                ok=False,
                text=(
                    "/scope show: missing component-id. "
                    "Usage: /scope show <component-id>"
                ),
            )
        return _render_show(args[1])
    if sub == "check":
        if len(args) < 3:
            return ScopeDispatchResult(
                ok=False,
                text=(
                    "/scope check: missing args. Usage: "
                    "/scope check <component-id> <tool-name>"
                ),
            )
        return _render_check(args[1], args[2])
    return ScopeDispatchResult(
        ok=False,
        text=f"/scope: unhandled subcommand {sub!r}",
    )


def _render_help() -> ScopeDispatchResult:
    text = (
        "/scope — Per-component tool scope inspector "
        "(§37 Tier 2 #16, Pattern C)\n"
        "\n"
        "  /scope                        list all registered "
        "components\n"
        "  /scope show <component-id>    full scope for one "
        "component\n"
        "  /scope check <cid> <tool>     dry-run decision "
        "(ALLOW/DENY/NO_SCOPE/DISABLED)\n"
        "  /scope active                 show currently-active "
        "component (ContextVar)\n"
        "  /scope help                   this message\n"
        "\n"
        "Master flag: JARVIS_COMPONENT_TOOL_SCOPE_ENABLED "
        "(default-FALSE per §33.1)\n"
        "Composition: tool patterns use V4 tool_name_pattern "
        "regex semantics. Component scope is the structural "
        "gate — fires AFTER /mode (session-wide) + BEFORE V2 "
        "permission callbacks (operator-defined)."
    )
    return ScopeDispatchResult(ok=True, text=text)


def _resolve_substrate() -> Optional[Any]:
    """Lazy import of Slice 1 substrate. Returns ``None`` on
    ImportError (caller renders disabled message)."""
    try:
        from backend.core.ouroboros.governance import (
            component_tool_scope as cts,
        )
        return cts
    except ImportError:
        return None


def _disabled_result() -> ScopeDispatchResult:
    return ScopeDispatchResult(
        ok=True,
        text=(
            "/scope: component-scope substrate disabled. Set "
            "JARVIS_COMPONENT_TOOL_SCOPE_ENABLED=true to "
            "enable. Note: registry data structure stays "
            "alive master-off (descriptive); only the dispatch "
            "WRITE surface gates."
        ),
    )


def _render_overview() -> ScopeDispatchResult:
    cts = _resolve_substrate()
    if cts is None:
        return _disabled_result()
    try:
        if not cts.master_enabled():
            return _disabled_result()
    except Exception:  # noqa: BLE001 — defensive
        return _disabled_result()
    try:
        components = cts.list_components()
    except Exception:  # noqa: BLE001 — defensive
        return ScopeDispatchResult(
            ok=False,
            text="/scope: registry read failed (non-fatal)",
        )
    if not components:
        return ScopeDispatchResult(
            ok=True,
            text=(
                "/scope: no components registered. Components "
                "register their scopes at sensor/subagent boot "
                "or per-op. Run /scope help for usage."
            ),
        )
    lines = [
        f"/scope overview ({len(components)} component(s)):",
    ]
    for cid, scope in sorted(components.items()):
        try:
            allowed_n = len(scope.allowed_tools)
            denied_n = len(scope.denied_tools)
        except Exception:  # noqa: BLE001 — defensive
            allowed_n = 0
            denied_n = 0
        mode = (
            "allowlist" if allowed_n > 0 else "denylist-only"
        )
        lines.append(
            f"  {cid:<32} {mode:<14} "
            f"allowed={allowed_n:<3} denied={denied_n}"
        )
    return ScopeDispatchResult(ok=True, text="\n".join(lines))


def _render_show(component_id: str) -> ScopeDispatchResult:
    cts = _resolve_substrate()
    if cts is None:
        return _disabled_result()
    cid = component_id.strip()
    try:
        scope = cts.get_scope(cid)
    except Exception:  # noqa: BLE001 — defensive
        return ScopeDispatchResult(
            ok=False,
            text=(
                "/scope show: registry read failed "
                "(non-fatal)"
            ),
        )
    if scope is None:
        return ScopeDispatchResult(
            ok=False,
            text=f"/scope show: no scope for {cid!r}",
        )
    lines = [
        f"/scope show {cid}:",
        f"  schema_version  = {scope.schema_version}",
        f"  inherits_from   = "
        f"{scope.inherits_from or '(none)'}",
        f"  allowed_tools   ({len(scope.allowed_tools)}):",
    ]
    if scope.allowed_tools:
        for pattern in sorted(scope.allowed_tools):
            lines.append(f"      {pattern}")
    else:
        lines.append(
            "      (empty — denylist-only mode; all tools "
            "allowed except denied_tools)"
        )
    lines.append(f"  denied_tools    ({len(scope.denied_tools)}):")
    if scope.denied_tools:
        for pattern in sorted(scope.denied_tools):
            lines.append(f"      {pattern}")
    else:
        lines.append("      (none)")
    return ScopeDispatchResult(ok=True, text="\n".join(lines))


def _render_check(
    component_id: str, tool_name: str,
) -> ScopeDispatchResult:
    cts = _resolve_substrate()
    if cts is None:
        return _disabled_result()
    cid = component_id.strip()
    tool = tool_name.strip()
    try:
        decision = cts.evaluate_component_scope(
            component_id=cid, tool_name=tool,
        )
    except Exception:  # noqa: BLE001 — defensive
        return ScopeDispatchResult(
            ok=False,
            text=(
                "/scope check: evaluation failed (non-fatal)"
            ),
        )
    decision_value = (
        decision.value if hasattr(decision, "value")
        else str(decision)
    )
    advisory = ""
    if decision_value == "deny":
        advisory = (
            "  → tool dispatch would short-circuit with "
            "POLICY_DENIED"
        )
    elif decision_value == "allow":
        advisory = (
            "  → tool dispatch proceeds to V2 PermissionRegistry"
        )
    elif decision_value == "no_scope":
        advisory = (
            "  → no scope registered; falls through to global "
            "gates (V2 + risk tier + Iron Gate)"
        )
    elif decision_value == "disabled":
        advisory = (
            "  → master flag JARVIS_COMPONENT_TOOL_SCOPE_"
            "ENABLED is off; component scope is bypassed"
        )
    text = (
        f"/scope check {cid!r} {tool!r}:\n"
        f"  decision = {decision_value}\n"
        f"{advisory}"
    )
    return ScopeDispatchResult(ok=True, text=text)


def _render_active() -> ScopeDispatchResult:
    cts = _resolve_substrate()
    if cts is None:
        return _disabled_result()
    try:
        active = cts.get_active_component()
    except Exception:  # noqa: BLE001 — defensive
        return ScopeDispatchResult(
            ok=False,
            text="/scope active: read failed (non-fatal)",
        )
    if not active:
        text = (
            "/scope active: no component currently active "
            "(ContextVar empty). Sensors / subagent dispatch "
            "sites stamp the var via "
            "set_active_component()."
        )
    else:
        text = f"/scope active: component_id = {active!r}"
    return ScopeDispatchResult(ok=True, text=text)


__all__ = [
    "ScopeDispatchResult",
    "dispatch_scope_command",
]
