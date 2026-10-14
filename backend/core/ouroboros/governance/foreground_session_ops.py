"""A foreground session's accepted change becomes ONE governed op.

The session never writes (FOREGROUND_SESSION §3.2). ``/session apply`` takes
the last proposal and:

1. sanctions it exactly as cockpit ``/goal`` does -- a signed roadmap goal
   (``author_and_sign_goal``) scoped to the proposal's files -- so the cage
   re-derives the operator's authority from the signature, not a label;
2. injects it through the harness's ``_inject_sanctioned_goal`` (registered
   at boot via :func:`set_injector`), the same router path every signal
   takes, carrying ``foreground_session.seed_ref`` in its evidence;
3. holds the proposal under that one-shot ref -- in this process, AND
   sealed on disk (HMAC-SHA256 under the roadmap secret, the same key that
   signs the goal) so the approval survives a daemon restart.

GENERATE (``candidate_generator.generate``) asks :func:`take_seed` first:
when the op's evidence names a live ref, the operator-approved proposal is
parsed by the SAME ``_parse_generation_response`` model output goes through,
and VALIDATE / Iron Gate / GATE / APPLY / VERIFY run unchanged -- the op
lands the diff the operator saw, not a regeneration of it. The ref is
random and consumed once. After a restart the op (replayed from the intake
WAL or a checkpoint) finds its seed only through the seal: a file whose MAC
does not verify -- altered, or written under a secret since rotated -- is
dropped with a CRYPTO warning and the op generates normally. A seed cannot
be forged from disk without the roadmap secret; with no secret configured
nothing is written and a restart simply loses the seed, as before.

Risk tier is computed exactly as for any op (invariant 2); the operator's
presence lowers nothing.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ForegroundSessionOps")

Injector = Callable[..., Optional[str]]

_INJECTOR: Optional[Injector] = None
#: ref -> (raw proposal, session id, created, the session's tool records)
_SEEDS: Dict[str, Tuple[str, str, float, Tuple[Any, ...]]] = {}
_READ_TOOLS = ("read_file", "list_symbols", "search_code", "get_callers")
_LOCK = threading.Lock()
SEED_TTL_S = 3600.0
_REF_RE = re.compile(r"^[0-9a-f]{32}$")
SEAL_SCHEMA = "session_seed.v1"


def seed_ttl_s() -> float:
    """How long an approved seed stays takeable. Env
    ``JARVIS_SESSION_SEED_TTL_S`` (default :data:`SEED_TTL_S`)."""
    try:
        return max(0.0, float(os.environ.get("JARVIS_SESSION_SEED_TTL_S", "") or SEED_TTL_S))
    except ValueError:
        return SEED_TTL_S


def seed_dir() -> Path:
    """Sealed seeds: env ``JARVIS_SESSION_SEED_DIR``, else ``seeds/`` beside
    the session logs."""
    raw = os.environ.get("JARVIS_SESSION_SEED_DIR", "").strip()
    if raw:
        return Path(raw)
    from backend.core.ouroboros.governance.foreground_session import sessions_dir  # noqa: PLC0415
    return sessions_dir() / "seeds"


def set_injector(fn: Optional[Injector]) -> None:
    global _INJECTOR
    _INJECTOR = fn


def _put_seed(raw: str, session_id: str, records: Tuple[Any, ...] = ()) -> str:
    ref = secrets.token_hex(16)
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _SEEDS.items() if now - v[2] > seed_ttl_s()]:
            _SEEDS.pop(k, None)
        _SEEDS[ref] = (raw, session_id, now, tuple(records))
    _sweep_expired_seals(now)
    _seal(ref, raw, session_id, now, records)
    return ref


def _sweep_expired_seals(now: float) -> None:
    """Seals no op came back for (refused later, op abandoned) age out with
    the same TTL as the in-memory seed. NEVER raises."""
    try:
        d = seed_dir()
        if not d.is_dir():
            return
        for p in d.glob("*.json"):
            try:
                if now - p.stat().st_mtime > seed_ttl_s():
                    _unlink(p)
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        logger.debug("[Session] seal sweep skipped", exc_info=True)


def _seal(ref: str, raw: str, session_id: str, created: float,
          records: Tuple[Any, ...]) -> bool:
    """Persist one approved seed, MAC'd with the roadmap secret, atomically.
    No secret -> nothing written (an unsealed seed on disk would be one any
    writer could forge). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (  # noqa: PLC0415
            compute_signature, hmac_secret,
        )
        secret = hmac_secret()
        if not secret:
            logger.debug("[Session] no roadmap secret: seed %s is held in memory only", ref[:8])
            return False
        from backend.core.ouroboros.governance.foreground_session import (  # noqa: PLC0415
            record_to_dict,
        )
        body = {"schema": SEAL_SCHEMA, "ref": ref, "session_id": session_id,
                "created": created, "proposal": raw,
                "records": [x for x in (record_to_dict(r) for r in records) if x is not None]}
        sig = compute_signature(body, secret)
        if not sig:
            return False
        d = seed_dir()
        d.mkdir(parents=True, exist_ok=True)
        from backend.core.ouroboros.governance.durable_io import atomic_replace  # noqa: PLC0415
        tmp = d / f".{ref}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"body": body, "sig": sig}, fh)
        atomic_replace(tmp, d / f"{ref}.json")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("[Session] seed %s could not be sealed to disk -- it will not "
                       "survive a restart", ref[:8], exc_info=True)
        return False


def _unseal(ref: str, op_id: str) -> Optional[Tuple[str, str, float, Tuple[Any, ...]]]:
    """The sealed seed for *ref*, verified, or None. Consumes the file either
    way: a verified seed is used once; a bad one is never retried. A MAC that
    does not verify is a CRYPTO warning, never silent. NEVER raises."""
    try:
        if not _REF_RE.match(ref or ""):
            return None
        path = seed_dir() / f"{ref}.json"
        if not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        finally:
            _unlink(path)
        from backend.core.ouroboros.governance.roadmap_reader import (  # noqa: PLC0415
            hmac_secret, verify_signature,
        )
        body = doc.get("body") if isinstance(doc, dict) else None
        secret = hmac_secret()
        if (not isinstance(body, dict) or not secret
                or not verify_signature(body, str(doc.get("sig", "")), secret)
                or body.get("ref") != ref or body.get("schema") != SEAL_SCHEMA):
            logger.warning(
                "[Session] CRYPTO: sealed seed %s for op=%s failed verification (%s) -- "
                "dropped; the op generates normally", ref[:8], op_id,
                "no roadmap secret configured" if not secret
                else "the roadmap secret was rotated or the file was altered")
            return None
        created = float(body.get("created") or 0.0)
        if time.time() - created > seed_ttl_s():
            logger.info("[Session] sealed seed %s expired (> %.0fs) -- op=%s generates "
                        "normally", ref[:8], seed_ttl_s(), op_id)
            return None
        from backend.core.ouroboros.governance.foreground_session import (  # noqa: PLC0415
            record_from_dict,
        )
        records = tuple(r for r in (record_from_dict(x) for x in body.get("records") or []
                                    if isinstance(x, dict)) if r is not None)
        return str(body.get("proposal", "")), str(body.get("session_id", "")), created, records
    except Exception:  # noqa: BLE001
        logger.warning("[Session] sealed seed %s unreadable -- op=%s generates normally",
                       (ref or "")[:8], op_id, exc_info=True)
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("[Session] could not remove %s", path, exc_info=True)


def _drop_seed(ref: str) -> None:
    """Release a seed everywhere it is held (memory and disk)."""
    with _LOCK:
        _SEEDS.pop(ref, None)
    if _REF_RE.match(ref or ""):
        try:
            _unlink(seed_dir() / f"{ref}.json")
        except Exception:  # noqa: BLE001
            pass


#: The provider name a seeded generation carries (candidate_generator parses
#: the operator-approved proposal under it). One spelling, read by
#: :func:`is_operator_seed`.
SEED_PROVIDER = "operator_session"


def _seed_ref(ctx: Any) -> str:
    """The session seed ref an op's evidence names, or ``""``. Non-consuming.
    NEVER raises."""
    try:
        ev = json.loads(getattr(ctx, "intake_evidence_json", "") or "{}")
        return str(((ev.get("foreground_session") or {}).get("seed_ref")) or "")
    except Exception:  # noqa: BLE001
        return ""


def is_operator_seed(ctx: Any, generation: Any) -> bool:
    """Is *generation* the change the operator approved in a /session?

    Both halves must hold: the op was injected by ``/session apply`` (its
    evidence names a seed ref) AND this generation was parsed from that seed
    (``SEED_PROVIDER``) rather than generated after the seed was gone. Either
    alone proves nothing -- a regenerated candidate on a session op, or a
    provider name with no session behind it. NEVER raises."""
    try:
        return (str(getattr(generation, "provider_name", "") or "") == SEED_PROVIDER
                and bool(_seed_ref(ctx)))
    except Exception:  # noqa: BLE001
        return False


def take_seed(ctx: Any) -> Optional[str]:
    """The operator-approved proposal for this op, consumed once, or None.
    NEVER raises; anything unexpected means "generate normally"."""
    got = take_seed_with_records(ctx)
    return got[0] if got else None


def take_seed_with_records(ctx: Any) -> Optional[Tuple[str, Tuple[Any, ...]]]:
    """:func:`take_seed`, plus the session's real tool records -- the
    exploration that produced the proposal, which the Iron Gate judges."""
    try:
        ref = _seed_ref(ctx)
        if not ref:
            return None
        op_id = str(getattr(ctx, "op_id", "?"))
        with _LOCK:
            hit = _SEEDS.pop(ref, None)
        sealed = hit is None
        if not sealed:
            _drop_seed(ref)                      # its sealed copy is spent too
        else:
            # Not in this process: the daemon restarted since the approval.
            hit = _unseal(ref, op_id)
            if hit is None:
                return None
        raw, sid, created, records = hit
        if time.time() - created > seed_ttl_s():
            return None
        logger.info("[Session] op=%s takes the proposal approved in session %s%s",
                    op_id, sid, " (from its verified seal)" if sealed else "")
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
        _drop_seed(ref)
        return False, "the pipeline refused the change at intake (see the debug log)"
    if hasattr(manager, "record_owned_op"):
        manager.record_owned_op(s, str(op_id))
    else:
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
        _drop_seed(ref)
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


__all__ = ["apply_last", "reset_for_tests", "seed_dir", "seed_ttl_s", "set_injector",
           "take_seed", "take_seed_with_records"]
