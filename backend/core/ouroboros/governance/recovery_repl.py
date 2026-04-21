"""
/recover REPL dispatcher — Slice 4 of the Recovery Guidance arc.
==================================================================

Operator verbs for drilling into "3 things to try" guidance:

    /recover                        list recent failed ops with plans
    /recover <op-id>                render the plan for a live op
    /recover <op-id> speak          render + announce via Karen voice
    /recover session <session-id>   historical plan from a past session
    /recover help                   verb surface

Authority posture
-----------------

* §1 read-only — the REPL never re-runs ops or mutates governor /
  orchestrator / session state. It only *renders* guidance that the
  advisor computed deterministically.
* §8 observable — every response carries the ``matched_rule`` so
  operators (and the IDE) know which branch of the rule table fired.
* No imports from orchestrator / policy / iron_gate / risk_tier_floor
  / semantic_guardian / tool_executor / candidate_generator /
  change_engine. Grep-pinned at graduation.
"""
from __future__ import annotations

import logging
import shlex
import textwrap
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger("Ouroboros.RecoveryREPL")


_COMMANDS = frozenset({"/recover"})

_HELP = textwrap.dedent(
    """
    Recovery guidance — "3 things to try next"
    -------------------------------------------
      /recover                        list recent ops with recovery plans
      /recover <op-id>                render the plan for that op
      /recover <op-id> speak          also announce via Karen voice
      /recover session <session-id>   historical plan from a past session
      /recover help                   this text

    Plans come from a deterministic rule table — no model call.
    Voice output requires OUROBOROS_NARRATOR_ENABLED=true AND
    JARVIS_RECOVERY_VOICE_ENABLED=true.
    """
).strip()


@dataclass
class RecoveryDispatchResult:
    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Plan provider hook — tests inject an in-memory source; production wires
# the SessionRecorder observer that stashes plans from POSTMORTEM.
# ---------------------------------------------------------------------------


_default_plan_provider: Optional[Any] = None


def set_default_plan_provider(provider: Any) -> None:
    """Install the process-wide plan provider.

    The provider must expose ``get_plan(op_id) -> RecoveryPlan | None``
    and ``recent_plans(limit) -> list[RecoveryPlan]``.
    """
    global _default_plan_provider
    _default_plan_provider = provider


def reset_default_plan_provider() -> None:
    global _default_plan_provider
    _default_plan_provider = None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_recovery_command(
    line: str,
    *,
    plan_provider: Optional[Any] = None,
    session_browser: Optional[Any] = None,
    announcer: Optional[Any] = None,
) -> RecoveryDispatchResult:
    """Parse a ``/recover`` line and dispatch to the right handler.

    Tests inject all three collaborators explicitly. Production wires
    the module-level defaults at boot.
    """
    if not _matches(line):
        return RecoveryDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return RecoveryDispatchResult(
            ok=False, text=f"  /recover parse error: {exc}",
        )
    if not tokens:
        return RecoveryDispatchResult(ok=False, text="", matched=False)
    args = tokens[1:]
    if not args:
        return _recent_plans(plan_provider)
    head = args[0].lower()
    if head in ("help", "?"):
        return RecoveryDispatchResult(ok=True, text=_HELP)
    if head == "session":
        if len(args) < 2:
            return RecoveryDispatchResult(
                ok=False, text="  /recover session <session-id>",
            )
        return _recover_historical(args[1], browser=session_browser)
    # Otherwise: args[0] is the op_id; args[1] may be "speak"
    op_id = args[0]
    speak = len(args) >= 2 and args[1].lower() == "speak"
    return _recover_live(
        op_id, plan_provider=plan_provider, speak=speak,
        announcer=announcer,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _resolve_provider(explicit: Optional[Any]) -> Optional[Any]:
    return explicit if explicit is not None else _default_plan_provider


def _recover_live(
    op_id: str,
    *,
    plan_provider: Optional[Any],
    speak: bool,
    announcer: Optional[Any],
) -> RecoveryDispatchResult:
    provider = _resolve_provider(plan_provider)
    if provider is None:
        return RecoveryDispatchResult(
            ok=False,
            text=(
                "  /recover: no plan provider attached — call "
                "set_default_plan_provider() at boot or pass explicitly"
            ),
        )
    try:
        plan = provider.get_plan(op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[RecoveryREPL] provider.get_plan raised: %s", exc)
        return RecoveryDispatchResult(
            ok=False, text=f"  /recover: provider error: {exc!r}",
        )
    if plan is None:
        return RecoveryDispatchResult(
            ok=False,
            text=f"  /recover: no plan for {op_id} (try /recover session <sid>)",
        )
    from backend.core.ouroboros.governance.recovery_formatter import (
        render_text, render_voice,
    )
    text_out = render_text(plan)
    if speak:
        # Resolve announcer — explicit > default > attempt singleton
        announcer_resolved = announcer
        if announcer_resolved is None:
            try:
                from backend.core.ouroboros.governance.recovery_announcer import (
                    get_default_announcer,
                )
                announcer_resolved = get_default_announcer()
            except Exception:  # noqa: BLE001
                announcer_resolved = None
        if announcer_resolved is not None:
            try:
                queued = announcer_resolved.announce_text(
                    f"repl:{op_id}", render_voice(plan),
                )
                if queued:
                    text_out += (
                        "\n    (queued for Karen voice announcement)"
                    )
                else:
                    text_out += (
                        "\n    (voice disabled — set "
                        "OUROBOROS_NARRATOR_ENABLED=true + "
                        "JARVIS_RECOVERY_VOICE_ENABLED=true)"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[RecoveryREPL] announce_text raised: %s", exc,
                )
                text_out += "\n    (voice announcement failed)"
    return RecoveryDispatchResult(ok=True, text=text_out)


def _recover_historical(
    session_id: str,
    *,
    browser: Optional[Any],
) -> RecoveryDispatchResult:
    if browser is None:
        try:
            from backend.core.ouroboros.governance.session_browser import (
                get_default_session_browser,
            )
            browser = get_default_session_browser()
        except Exception:  # noqa: BLE001
            return RecoveryDispatchResult(
                ok=False,
                text="  /recover session: session browser unavailable",
            )
    try:
        rec = browser.show(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[RecoveryREPL] browser.show raised: %s", exc)
        return RecoveryDispatchResult(
            ok=False, text=f"  /recover session: browser error: {exc!r}",
        )
    if rec is None:
        return RecoveryDispatchResult(
            ok=False,
            text=f"  /recover session: unknown session id {session_id}",
        )
    # Historical failure context from the session record
    from backend.core.ouroboros.governance.recovery_advisor import (
        FailureContext, advise,
    )
    from backend.core.ouroboros.governance.recovery_formatter import (
        render_text,
    )
    ctx = FailureContext(
        op_id="session-" + session_id,
        session_id=session_id,
        stop_reason=rec.stop_reason or "",
        final_phase=rec.stop_reason or "",
        cost_spent_usd=rec.cost_spent_usd or 0.0,
    )
    plan = advise(ctx)
    header = f"  Historical session {session_id}"
    if rec.stop_reason:
        header += f" ({rec.stop_reason})"
    return RecoveryDispatchResult(
        ok=True, text=header + "\n" + render_text(plan),
    )


def _recent_plans(
    plan_provider: Optional[Any], *, limit: int = 5,
) -> RecoveryDispatchResult:
    provider = _resolve_provider(plan_provider)
    if provider is None:
        return RecoveryDispatchResult(
            ok=True,
            text="  (no plan provider attached — no recent plans)",
        )
    try:
        plans = provider.recent_plans(limit)
    except Exception as exc:  # noqa: BLE001
        return RecoveryDispatchResult(
            ok=False, text=f"  /recover: provider error: {exc!r}",
        )
    if not plans:
        return RecoveryDispatchResult(
            ok=True, text="  (no recent recovery plans)",
        )
    lines: List[str] = [f"  {len(plans)} recent plan(s):"]
    for p in plans:
        top = p.top_suggestion()
        top_str = f"  top: {top.title}" if top is not None else ""
        lines.append(
            f"    {p.op_id}  [{p.matched_rule}]"
            f"  {p.failure_summary}{top_str}"
        )
    lines.append(
        "  Run /recover <op-id> for detail, "
        "/recover <op-id> speak for voice."
    )
    return RecoveryDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "RecoveryDispatchResult",
    "dispatch_recovery_command",
    "reset_default_plan_provider",
    "set_default_plan_provider",
]
