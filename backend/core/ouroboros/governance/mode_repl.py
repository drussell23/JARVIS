"""§37 Tier 2 #14 — `/mode` REPL verb (PRD §32.7 Pattern B).

Operator-facing surface for the OperationMode substrate.
Auto-discovered via the §32.11 Slice 4 naming-cage convention:
file ``mode_repl.py`` → verb ``/mode`` → dispatcher
``dispatch_mode_command(line)``. Zero edits to
``repl_dispatch_registry.py`` required.

**Subcommands**:

  * ``/mode`` — print current mode + master-flag status.
  * ``/mode status`` — alias for bare form.
  * ``/mode set <name>`` — stamp the active mode for the
    current session (``plan`` / ``analyze`` / ``apply`` /
    ``auto``). Persists via ContextVar (session-scoped — does
    NOT survive REPL re-launch; operator sets via env knob
    ``JARVIS_OPERATION_MODE`` for boot-default).
  * ``/mode help`` — usage.

**Composition** (operator binding 2026-05-07):

  * Composes :mod:`operation_mode` substrate — single source
    of truth for the enum, master flag, and mutation
    classification. Zero parallel state.
  * Read-only browser pattern (mirrors ``replay_repl.py`` /
    ``posture_repl.py``): operator's mode write IS the
    mode (state is owned here via ContextVar), but the dispatch
    function NEVER touches risk-tier / iron-gate / orchestrator
    state directly.

**Authority asymmetry** (AST-pinned downstream): no
orchestrator / iron_gate / providers / change_engine /
semantic_guardian / candidate_generator / urgency_router
imports.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger("Ouroboros.ModeREPL")


_VERBS = ("/mode",)
_VALID_SUBCOMMANDS = {"status", "set", "help"}


@dataclass
class ModeDispatchResult:
    """Mirrors ``PostureDispatchResult`` shape — auto-discovery
    convention requires ``ok: bool``, ``text: str``,
    ``matched: bool``."""
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _VERBS


def dispatch_mode_command(line: str) -> ModeDispatchResult:
    """Parse a ``/mode`` line and dispatch.

    Returns a :class:`ModeDispatchResult` with ``matched=True``
    only when the line begins with ``/mode``. Other input
    short-circuits with ``matched=False`` so the registry can
    fall through to the next verb. NEVER raises.
    """
    if not _matches(line):
        return ModeDispatchResult(
            ok=False, text="", matched=False,
        )
    # Defensive parse — shlex handles quoted args; on parse
    # failure we degrade gracefully rather than crash.
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ModeDispatchResult(
            ok=False,
            text=f"/mode: parse error — {exc}",
        )
    # Drop the leading verb.
    args = tokens[1:] if len(tokens) > 1 else []
    if not args:
        return _render_status()
    sub = args[0].lower()
    if sub not in _VALID_SUBCOMMANDS:
        return ModeDispatchResult(
            ok=False,
            text=(
                f"/mode: unknown subcommand {sub!r}. "
                f"Try /mode help."
            ),
        )
    if sub == "help":
        return _render_help()
    if sub == "status":
        return _render_status()
    if sub == "set":
        if len(args) < 2:
            return ModeDispatchResult(
                ok=False,
                text=(
                    "/mode set: missing mode name. "
                    "Usage: /mode set <plan|analyze|apply|auto>"
                ),
            )
        return _handle_set(args[1])
    # Defensive — should be unreachable per validation above.
    return ModeDispatchResult(
        ok=False, text=f"/mode: unhandled subcommand {sub!r}",
    )


def _render_help() -> ModeDispatchResult:
    text = (
        "/mode — Operation Mode steering surface "
        "(§37 Tier 2 #14)\n"
        "\n"
        "  /mode               show current mode + master "
        "flag status\n"
        "  /mode status        alias for bare form\n"
        "  /mode set <name>    set active mode "
        "(session-scoped)\n"
        "  /mode help          this message\n"
        "\n"
        "Modes:\n"
        "  plan      read-only exploration; mutation tools "
        "denied at dispatch\n"
        "  analyze   read-only deep-dive; same enforcement "
        "as plan\n"
        "  apply     status quo; mutations subject to other "
        "gates only\n"
        "  auto      alias for apply (reserved for future "
        "fully-autonomous expansion)\n"
        "\n"
        "Master flag: JARVIS_OPERATION_MODE_ENABLED "
        "(default-FALSE per §33.1)\n"
        "Boot default: JARVIS_OPERATION_MODE "
        "(default 'auto')"
    )
    return ModeDispatchResult(ok=True, text=text)


def _render_status() -> ModeDispatchResult:
    try:
        from backend.core.ouroboros.governance.operation_mode import (  # noqa: E501
            current_mode,
            master_enabled,
        )
    except ImportError:
        return ModeDispatchResult(
            ok=False,
            text="/mode: operation_mode substrate unavailable",
        )
    try:
        mode = current_mode()
        master = master_enabled()
    except Exception:  # noqa: BLE001 — defensive
        return ModeDispatchResult(
            ok=False,
            text="/mode: status read failed (non-fatal)",
        )
    enforcement = (
        "ENFORCING" if master and mode.value in ("plan", "analyze")
        else "passive (master OFF)" if not master
        else "passive (mode allows mutations)"
    )
    text = (
        f"/mode status:\n"
        f"  current_mode    = {mode.value}\n"
        f"  master_enabled  = {master}\n"
        f"  enforcement     = {enforcement}"
    )
    return ModeDispatchResult(ok=True, text=text)


def _handle_set(name: str) -> ModeDispatchResult:
    try:
        from backend.core.ouroboros.governance.operation_mode import (  # noqa: E501
            OperationMode,
            current_mode,
            set_mode,
        )
    except ImportError:
        return ModeDispatchResult(
            ok=False,
            text=(
                "/mode set: operation_mode substrate "
                "unavailable"
            ),
        )
    raw = (name or "").strip().lower()
    target: Optional[OperationMode] = None
    for mode in OperationMode:
        if raw == mode.value:
            target = mode
            break
    if target is None:
        valid = ", ".join(m.value for m in OperationMode)
        return ModeDispatchResult(
            ok=False,
            text=(
                f"/mode set: unknown mode {raw!r}. "
                f"Valid: {valid}"
            ),
        )
    try:
        prior = current_mode()
        set_mode(target)
    except Exception:  # noqa: BLE001 — defensive
        return ModeDispatchResult(
            ok=False,
            text="/mode set: state write failed (non-fatal)",
        )
    return ModeDispatchResult(
        ok=True,
        text=(
            f"/mode: {prior.value} -> {target.value} "
            f"(session-scoped; not persisted across REPL "
            f"re-launch)"
        ),
    )


__all__ = [
    "ModeDispatchResult",
    "dispatch_mode_command",
]
