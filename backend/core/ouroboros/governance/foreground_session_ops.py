"""A foreground session's accepted change becomes ONE governed op.

The session never writes (FOREGROUND_SESSION §3.2). ``/session apply`` takes
the last proposal and:

1. sanctions it exactly as cockpit ``/goal`` does -- a signed roadmap goal
   (``author_and_sign_goal``) scoped to the proposal's files -- so the cage
   re-derives the operator's authority from the signature, not a label;
2. injects it through the harness's ``_inject_sanctioned_goal`` (registered
   at boot via :func:`set_injector`), the same router path every signal
   takes, carrying ``foreground_session.seed_ref`` in its evidence;
3. holds the proposal under that one-shot ref IN THIS PROCESS.

GENERATE (``candidate_generator.generate``) asks :func:`take_seed` first:
when the op's evidence names a live ref, the operator-approved proposal is
parsed by the SAME ``_parse_generation_response`` model output goes through,
and VALIDATE / Iron Gate / GATE / APPLY / VERIFY run unchanged -- the op
lands the diff the operator saw, not a regeneration of it. The ref is
random, consumed once and never written down, so an envelope replayed from
the intake WAL after a restart finds nothing and simply generates normally:
a seed cannot be forged from disk.

Risk tier is computed exactly as for any op (invariant 2); the operator's
presence lowers nothing.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ForegroundSessionOps")

Injector = Callable[..., Optional[str]]

_INJECTOR: Optional[Injector] = None
#: ref -> (raw proposal, session id, created, the session's tool records)
_SEEDS: Dict[str, Tuple[str, str, float, Tuple[Any, ...]]] = {}
_READ_TOOLS = ("read_file", "list_symbols", "search_code", "get_callers")
_LOCK = threading.Lock()
SEED_TTL_S = 3600.0


def set_injector(fn: Optional[Injector]) -> None:
    global _INJECTOR
    _INJECTOR = fn


def _put_seed(raw: str, session_id: str, records: Tuple[Any, ...] = ()) -> str:
    ref = secrets.token_hex(16)
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _SEEDS.items() if now - v[2] > SEED_TTL_S]:
            _SEEDS.pop(k, None)
        _SEEDS[ref] = (raw, session_id, now, tuple(records))
    return ref


def take_seed(ctx: Any) -> Optional[str]:
    """The operator-approved proposal for this op, consumed once, or None.
    NEVER raises; anything unexpected means "generate normally"."""
    got = take_seed_with_records(ctx)
    return got[0] if got else None


def take_seed_with_records(ctx: Any) -> Optional[Tuple[str, Tuple[Any, ...]]]:
    """:func:`take_seed`, plus the session's real tool records -- the
    exploration that produced the proposal, which the Iron Gate judges."""
    try:
        ev = json.loads(getattr(ctx, "intake_evidence_json", "") or "{}")
        ref = str(((ev.get("foreground_session") or {}).get("seed_ref")) or "")
        if not ref:
            return None
        with _LOCK:
            hit = _SEEDS.pop(ref, None)
        if hit is None:
            return None
        raw, sid, created, records = hit
        if time.time() - created > SEED_TTL_S:
            return None
        logger.info("[Session] op=%s takes the proposal approved in session %s",
                    getattr(ctx, "op_id", "?"), sid)
        return raw, records
    except Exception:  # noqa: BLE001
        return None


def apply_last(manager: Any, cockpit: str) -> Tuple[bool, str]:
    """``/session apply``: sanction + inject the last proposal. NEVER raises."""
    try:
        s = manager.active(cockpit)
        if s is None:
            return False, "not in a session"
        proposal = s.last_proposal()
        if proposal is None:
            return False, "nothing proposed yet in this session"
        if _INJECTOR is None:
            return False, "the governed pipeline is not wired in this process"
        files = tuple(dict.fromkeys(str(c.get("file_path", ""))
                                    for c in proposal.get("candidates", []) if c.get("file_path")))
        # The Iron Gate will judge the session's reads. A change to an existing
        # file the conversation never looked at would fail it, and its retry
        # would REGENERATE -- landing something the operator did not see.
        # Refuse here instead, while the operator can still ask for a read.
        import os  # noqa: PLC0415
        digests = [d for t in s.turns for d in t.tools if d.tool in _READ_TOOLS]
        unread = [f for f in files if os.path.exists(f)
                  and not any(f in d.target or d.target in f for d in digests if d.target)]
        if unread:
            return False, ("the session never read " + ", ".join(unread)
                           + " — ask it to look first, so the change is grounded")
        # Re-checked here, not only when proposed: the file may have moved on
        # since. A seed GENERATE cannot parse is dropped and the op generates
        # normally -- the operator would approve one change and land another.
        from backend.core.ouroboros.governance.foreground_session import (  # noqa: PLC0415
            proposal_problem,
        )
        problem = proposal_problem(proposal)
        if problem:
            return False, f"not applied — {problem}"
        why = "; ".join(str(c.get("rationale", "")) for c in proposal.get("candidates", [])
                        if c.get("rationale")) or "operator-approved change from a session"
        asked = next((t.user for t in reversed(s.turns) if t.proposal is proposal), "")
        from backend.core.ouroboros.governance.operator_goal_sanction import (  # noqa: PLC0415
            GoalSpec, author_and_sign_goal,
        )
        goal_id = f"session-{s.id}-{len(s.owned_ops) + 1}-{secrets.token_hex(2)}"
        spec = GoalSpec(goal_id=goal_id, title=why[:120],
                        description=f"{why}\n\nAsked in session {s.id}: {asked[:500]}",
                        target_files=files, note=f"foreground session {s.id}")
        res = author_and_sign_goal(spec)
        if not getattr(res, "ok", False):
            return False, f"could not sanction the change: {getattr(res, 'reason', '?')} " \
                          f"{getattr(res, 'detail', '')}".strip()
        records = tuple(r for t in s.turns for r in getattr(t, "records", ()) or ())
        ref = _put_seed(json.dumps(proposal), s.id, records)
        inject = functools.partial(
            _INJECTOR, goal_id=goal_id, description=spec.description, target_files=files,
            extra_evidence={"foreground_session": {"session_id": s.id, "seed_ref": ref}})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # Already off the loop (a worker thread): intake can be awaited here.
            return _settle(manager, cockpit, s, ref, files, inject())
        # ON the loop -- where /session runs (the operator input consumer).
        # The injector awaits intake's verdict, which it must NOT do from the
        # loop that runs intake: the dispatch seam refuses that ("submitter
        # called ON the event loop") and every apply was FILED, never run.
        # It goes to a worker, as the chat executor's submissions do; the
        # receipt is reported when intake answers.
        loop.create_task(_inject_off_loop(manager, cockpit, s, ref, files, inject))
        return True, f"sending {', '.join(files)} through the pipeline (validate → gate → apply → verify)…"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Session] apply failed", exc_info=True)
        return False, f"apply failed: {type(exc).__name__}: {exc}"


def _settle(manager: Any, cockpit: str, s: Any, ref: str, files: Tuple[str, ...],
            op_id: Optional[str]) -> Tuple[bool, str]:
    """Intake answered. A refusal releases the seed; an accepted op becomes
    the session's own, so the cockpit routes its blocks to the transcript."""
    if not op_id:
        with _LOCK:
            _SEEDS.pop(ref, None)
        return False, "the pipeline refused the change at intake (see the debug log)"
    s.owned_ops.append(str(op_id))
    if hasattr(manager, "notify_mode"):
        manager.notify_mode(cockpit, s)
    return True, (f"{op_id} · sent through the pipeline (validate → gate → apply → verify) · "
                  f"{', '.join(files)}")


async def _inject_off_loop(manager: Any, cockpit: str, s: Any, ref: str,
                           files: Tuple[str, ...], inject: Callable[[], Optional[str]]) -> None:
    """The on-loop apply's second half. NEVER raises."""
    try:
        op_id = await asyncio.to_thread(inject)
        ok, text = _settle(manager, cockpit, s, ref, files, op_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Session] apply injection failed", exc_info=True)
        with _LOCK:
            _SEEDS.pop(ref, None)
        ok, text = False, f"apply failed: {type(exc).__name__}: {exc}"
    try:
        from backend.core.ouroboros.ui.theme import mark  # noqa: PLC0415
        from rich.markup import escape  # noqa: PLC0415
        emit = getattr(manager, "_emit", None)
        if emit is not None:
            glyph = mark("check" if ok else "cross")
            emit(f"{glyph} {escape(text)}", cockpit)
    except Exception:  # noqa: BLE001
        logger.debug("[Session] apply receipt not shown", exc_info=True)


def reset_for_tests() -> None:
    set_injector(None)
    with _LOCK:
        _SEEDS.clear()


__all__ = ["apply_last", "reset_for_tests", "set_injector", "take_seed",
           "take_seed_with_records"]
