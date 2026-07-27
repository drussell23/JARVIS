"""The Iron Gate asks, and an attached operator can answer.

An APPROVAL_REQUIRED op pauses at APPROVE and waits. That much was correct:
`CLIApprovalProvider.await_decision` already wraps the wait in
`asyncio.wait_for` and stamps EXPIRED on timeout, so nothing hangs — the
orphan prevention has been right all along, and the daemon has never blocked
on stdin.

What was missing is that **nobody was ever asked**. The gate emitted a comm
heartbeat with `phase="approve"` — which no cockpit surface renders — and then
sat silently until it expired. An operator watching the deck saw an op stop
moving and had no way to know a question was pending, let alone answer it.

Two finished components, never joined:

* `OperatorPromptBridge` (#70085) — a single-slot pending-prompt registry whose
  future resolves from attached-terminal input. `harness._on_input` already
  consults it BEFORE the REPL. It had **zero callers of `begin()`**: a receive
  path with no sender, so `waiting` was permanently False and every keystroke
  fell through to the REPL.
* `_mirror_markup` — the chokepoint every ⏺/⎿ line already reaches the cockpit
  through.

This joins them. It adds no transport, no polling, and no second timeout.

Why race rather than replace
-----------------------------
`await_decision` is still the authority on the outcome — it owns the timeout,
the EXPIRED stamp and the ledger semantics. The bridge is raced ALONGSIDE it as
a faster path to the same decision: an operator answering `y` calls the
provider's own `approve()`, which sets the event `await_decision` is already
waiting on. One source of truth for what was decided; two ways to reach it.

Replacing the wait would have meant reimplementing expiry, and a second timeout
that disagrees with the first is worse than no second path at all.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger("Ouroboros.ApprovalNarrator")

__all__ = [
    "approval_narration_enabled",
    "interpret_answer",
    "render_gate_prompt",
    "await_decision_with_operator",
]

#: Answers that mean yes. Everything unrecognised is NOT a yes — an approval
#: must be affirmative and deliberate, so ambiguity resolves to rejection
#: rather than to the more convenient reading.
_YES = frozenset({"y", "yes", "approve", "ok", "go", "ship", "accept"})
_NO = frozenset({"n", "no", "reject", "stop", "deny", "cancel", "abort"})


def approval_narration_enabled() -> bool:
    """Default ON: an unanswerable gate is the failure this closes."""
    return os.environ.get(
        "JARVIS_APPROVAL_NARRATION_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def interpret_answer(text: Any) -> Optional[bool]:
    """``True`` approve, ``False`` reject, ``None`` "that wasn't an answer".

    None matters: an operator typing an unrelated command while a gate is
    pending must not have it read as a verdict. The caller leaves the gate
    armed and lets the text flow on to the REPL, rather than consuming a
    keystroke that was never a decision.
    """
    try:
        token = str(text or "").strip().lower()
        if not token:
            return None
        first = token.split()[0]
        if first in _YES:
            return True
        if first in _NO:
            return False
        return None
    except Exception:  # noqa: BLE001
        return None


def render_gate_prompt(
    op_id: str, risk: str = "", reason: str = "", timeout_s: float = 0.0,
) -> str:
    """``⏺ Iron Gate(7759-86) ⎿ approve? [y/n] · expires in 300s``

    The op ref is the TAIL — same reason as everywhere else: UUIDv7 shares its
    prefix, so the leading bytes distinguish nothing. Stating the expiry is
    what makes the silence honest: the operator learns both that a question is
    open and that not answering IS an answer.
    """
    parts = [p for p in str(op_id or "").split("-") if p]
    ref = "-".join(parts[-2:]) if len(parts) >= 3 else (op_id or "op")
    head = f"⏺ Iron Gate({ref})"
    if risk:
        head += f" · {risk}"
    detail = f" · {reason}" if reason else ""
    expiry = f" · expires in {int(timeout_s)}s" if timeout_s > 0 else ""
    return f"{head}\n  ⎿ approve? [y/n]{detail}{expiry}"


async def await_decision_with_operator(
    provider: Any,
    request_id: str,
    timeout_s: float,
    *,
    emit: Optional[Callable[[str], None]] = None,
    risk: str = "",
    reason: str = "",
) -> Any:
    """`await_decision`, plus a cockpit that can answer it.

    Falls back to `provider.await_decision(...)` verbatim whenever narration is
    disabled, the bridge is unavailable, or anything at all goes wrong. The
    gate's behaviour without an attached operator must be byte-identical to
    what it was — this can only ADD a way to answer, never change what happens
    when nobody does.
    """
    plain = provider.await_decision(request_id, timeout_s)
    if not approval_narration_enabled():
        return await plain

    bridge = None
    fut = None
    try:
        from backend.core.ouroboros.battle_test.operator_prompt_bridge import (
            get_operator_prompt_bridge,
        )
        bridge = get_operator_prompt_bridge()
        fut = bridge.begin(str(request_id))
    except Exception:  # noqa: BLE001
        bridge, fut = None, None

    if emit is not None:
        try:
            emit(render_gate_prompt(request_id, risk, reason, timeout_s))
        except Exception:  # noqa: BLE001
            logger.debug("[ApprovalNarrator] prompt emit degraded",
                         exc_info=True)

    if fut is None:
        # No bridge — the gate behaves exactly as before.
        return await plain

    decision_task = asyncio.ensure_future(plain)
    try:
        while True:
            done, _pending = await asyncio.wait(
                {decision_task, fut},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if decision_task in done:
                # Decided elsewhere (a verb, another terminal, or expiry).
                return decision_task.result()

            # The operator typed something while the gate was open.
            answer = interpret_answer(fut.result() if not fut.cancelled() else "")
            if answer is None:
                # Not a verdict. Re-arm and let the text reach the REPL —
                # consuming a keystroke that was never an answer would make
                # the cockpit feel possessed.
                fut = bridge.begin(str(request_id)) if bridge else None
                if fut is None:
                    return await decision_task
                continue
            try:
                if answer:
                    await provider.approve(request_id, "operator")
                else:
                    await provider.reject(request_id, "operator", "declined")
            except Exception:  # noqa: BLE001
                logger.debug("[ApprovalNarrator] decision relay failed",
                             exc_info=True)
            # The provider's own event is now set; await_decision returns the
            # authoritative result rather than one synthesised here.
            return await decision_task
    except Exception:  # noqa: BLE001
        logger.debug("[ApprovalNarrator] operator path degraded", exc_info=True)
        return await decision_task
    finally:
        try:
            if bridge is not None:
                bridge.end(fut)
        except Exception:  # noqa: BLE001
            pass
