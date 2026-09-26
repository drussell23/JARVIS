"""Continuous local promotion: every verified landing fast-forwards the
operator's branch, or says exactly why it could not.

## Where it listens

Both commit paths (the inline orchestrator and its Slice4b twin) already
announce a landing the same way: a CommProtocol ``HEARTBEAT`` with
``phase="commit"`` and the ``commit_hash``. Observing that one announcement
promotes every landing, whichever path produced it, and adds nothing to
either path — the same shape :mod:`landed_metrics` uses for the scoreboard.

## Why a single background worker

Promotion verifies before it moves anything, and verifying runs the covering
tests (``accumulation_promotion_gate._check_coverage``, minutes at worst).
``send`` is awaited by the CommProtocol for every message, so it only
enqueues. And the target is ONE ref: two concurrent fast-forwards would race
for it, so promotions run one at a time, in landing order.

## What it will not do

Push. The GitHub decision stays with the operator (``remote_push_guard`` is
air-gapped by default and the AutoCommitter refuses protected branches); a
promotion only reports how far the local branch is ahead of its remote so
the operator can make that call.

Master: ``JARVIS_ACCUMULATION_PROMOTION_ENABLED`` (the gate's own flag).
Target: ``JARVIS_ACCUMULATION_PROMOTION_TARGET``, else the remote's default
branch (``refs/remotes/origin/HEAD``) — derived, never assumed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Optional, Set, Tuple

logger = logging.getLogger("Ouroboros.MainPromoter")

#: The announcement both commit paths make after a successful AutoCommit.
COMMIT_PHASE = "commit"
#: The events this module emits back onto the CommProtocol.
PROMOTION_SUCCESS = "promotion_success"
PROMOTION_REFUSED = "promotion_refused"

_ENV_TARGET = "JARVIS_ACCUMULATION_PROMOTION_TARGET"
#: How many landing shas to remember for de-duplication. A re-emitted
#: heartbeat must not promote twice; the window only has to outlive a retry.
_SEEN_WINDOW = 256


# ---------------------------------------------------------------------------
# The record the cockpit reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionSnapshot:
    total: int = 0
    target: str = ""
    sha: str = ""
    #: Commits the local target is ahead of its remote; None = no remote
    #: tracking ref to compare against.
    unpushed: Optional[int] = None
    #: "promoted", or the refusal state ("diverged", "refused", …).
    state: str = ""
    detail: str = ""
    at: float = 0.0


class PromotionRecord:
    """Thread-safe last-promotion record. Readers (the status line, ~2 Hz)
    take a lock for a few field reads and never touch git."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._snap = PromotionSnapshot()

    def record(self, *, promoted: bool, target: str, sha: str, state: str,
               detail: str = "", unpushed: Optional[int] = None) -> None:
        with self._lock:
            total = self._snap.total + (1 if promoted else 0)
            self._snap = PromotionSnapshot(
                total=total, target=target, sha=sha,
                unpushed=unpushed if promoted else self._snap.unpushed,
                state="promoted" if promoted else state,
                detail=detail, at=self._clock(),
            )

    def snapshot(self) -> PromotionSnapshot:
        with self._lock:
            return self._snap


_record: Optional[PromotionRecord] = None
_record_lock = threading.Lock()


def get_promotion_record() -> PromotionRecord:
    global _record
    if _record is None:
        with _record_lock:
            if _record is None:
                _record = PromotionRecord()
    return _record


def reset_for_tests() -> None:
    global _record
    with _record_lock:
        _record = None


def render_promotion(snap: Any) -> str:
    """The ONE formatter for the cockpit token. ``""`` until something
    happened. ``main ← 06b35c990f · 3 unpushed`` after a promotion;
    ``main ✗ diverged — rebase`` when the last one was refused."""
    try:
        state = str(getattr(snap, "state", "") or "")
        if not state:
            return ""
        target = str(getattr(snap, "target", "") or "?")
        sha = str(getattr(snap, "sha", "") or "")[:10]
        if state == "promoted":
            unpushed = getattr(snap, "unpushed", None)
            tail = "" if unpushed is None else (
                " · pushed" if unpushed == 0 else f" · {int(unpushed)} unpushed"
            )
            return f"{target} ← {sha}{tail}"
        if state == "diverged":
            return f"{target} ✗ diverged — rebase"
        return f"{target} ✗ {state}"
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Resolving one landing
# ---------------------------------------------------------------------------


def _manager(repo_root: Optional[Path] = None) -> Any:
    from backend.core.ouroboros.governance.worktree_manager import (  # noqa: PLC0415
        WorktreeManager,
    )
    root = repo_root
    if root is None:
        from backend.core.ouroboros.governance.execution_context import (  # noqa: PLC0415
            authoritative_repo_root,
        )
        root = authoritative_repo_root(
            os.environ.get("JARVIS_PROJECT_ROOT") or os.getcwd(),
        )
    return WorktreeManager(repo_root=Path(root))


async def resolve_target_branch(mgr: Any) -> str:
    """The operator's branch to promote onto. The env override first, else
    the remote's declared default branch. ``""`` when neither answers — no
    promotion is safer than a guessed target."""
    explicit = (os.environ.get(_ENV_TARGET, "") or "").strip()
    if explicit:
        return explicit
    rc, out, _ = await mgr._run_git_rc(
        mgr._repo_root,
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
    )
    name = out.strip() if rc == 0 else ""
    return name.split("/", 1)[1] if "/" in name else ""


async def resolve_source_branch(mgr: Any, sha: str) -> Tuple[str, str]:
    """``(branch, reason)``. The session workspace branch that holds
    ``sha``, found from git: the commit's own ``Session:`` trailer names the
    session, and exactly one branch under that session's prefix must
    contain it. The branch name's nonce is never re-derived — another boot
    may have minted it."""
    from backend.core.ouroboros.governance.accumulation_promotion_gate import (  # noqa: PLC0415
        commit_session,
    )
    from backend.core.ouroboros.governance.autonomous_workspace import (  # noqa: PLC0415
        workspace_branch_prefix,
    )
    rc, msg, _ = await mgr._run_git_rc(mgr._repo_root, ["log", "-1", "--format=%B", sha])
    session = commit_session(msg) if rc == 0 else ""
    if not session:
        return "", "no Session trailer on the commit"
    prefix = workspace_branch_prefix(session)
    rc, out, _ = await mgr._run_git_rc(
        mgr._repo_root,
        ["for-each-ref", "--format=%(refname:short)", "--contains", sha,
         "refs/heads/%s*" % prefix],
    )
    branches = [b.strip() for b in out.splitlines() if b.strip()] if rc == 0 else []
    if len(branches) != 1:
        return "", "%d branch(es) under %s contain it" % (len(branches), prefix)
    return branches[0], ""


async def _unpushed(mgr: Any, target: str) -> Optional[int]:
    rc, out, _ = await mgr._run_git_rc(
        mgr._repo_root,
        ["rev-list", "--count", "refs/remotes/origin/%s..refs/heads/%s" % (target, target)],
    )
    try:
        return int(out.strip()) if rc == 0 else None
    except ValueError:
        return None


@dataclass(frozen=True)
class LandingPromotion:
    """What happened to one landing, with everything the event carries."""
    verdict: Any
    sha: str
    target: str = ""
    source_branch: str = ""
    unpushed: Optional[int] = None


async def promote_landing(sha: str, *, manager: Any = None) -> LandingPromotion:
    """Verify ``sha`` and fast-forward the target onto it. NEVER raises."""
    from backend.core.ouroboros.governance.accumulation_promotion_gate import (  # noqa: PLC0415
        PromotionVerdict, promote_accumulation_commit,
    )
    try:
        mgr = manager if manager is not None else _manager()
        target = await resolve_target_branch(mgr)
        if not target:
            return LandingPromotion(PromotionVerdict(
                False, "no_target", (sha,),
                detail=f"set {_ENV_TARGET} or give origin a HEAD",
            ), sha)
        branch, why = await resolve_source_branch(mgr, sha)
        if not branch:
            return LandingPromotion(PromotionVerdict(
                False, "no_source_branch", (sha,), detail=why,
            ), sha, target)
        # Verify in a checkout of EXACTLY the landing. A fast-forward makes
        # the target's tree identical to it, so this is the post-promotion
        # state; the target's live checkout is the pre-promotion state and
        # lacks the tests a test-synthesis landing adds.
        async with mgr.detached_checkout(sha) as root:
            verdict = await promote_accumulation_commit(
                sha=sha, branch=branch, repo_root=root, manager=mgr,
                target_branch=target,
                # THIS interpreter: the gate's default `python3` resolves to
                # the host's system Python, which has no pytest here — every
                # coverage check would refuse with "could not run tests".
                python_bin=sys.executable,
            )
        unpushed = await _unpushed(mgr, target) if verdict.promoted else None
        return LandingPromotion(verdict, sha, target, branch, unpushed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MainPromoter] %s promotion aborted: %r", sha[:12], exc)
        return LandingPromotion(PromotionVerdict(
            False, "aborted", (sha,), detail=f"{exc!r}"[:200],
        ), sha)


# ---------------------------------------------------------------------------
# The CommProtocol seam
# ---------------------------------------------------------------------------


class MainPromotionTransport:
    """Observes landings; promotes each one on a single background worker;
    announces the outcome. ``send`` only enqueues. NEVER raises."""

    def __init__(
        self,
        *,
        promote: Optional[Callable[[str], Awaitable[LandingPromotion]]] = None,
        record: Optional[PromotionRecord] = None,
        manager: Any = None,
    ) -> None:
        #: ONE repository handle for promotion and pruning, resolved lazily
        #: (the transport is built before the loop knows its project root).
        self._manager_obj = manager
        self._promote = promote or (
            lambda sha: promote_landing(sha, manager=self._mgr())
        )
        self._record = record
        self._comm: Any = None
        self._seen: Deque[str] = deque(maxlen=_SEEN_WINDOW)
        self._seen_set: Set[str] = set()
        self._queue: Optional["asyncio.Queue[Tuple[str, str]]"] = None
        self._worker: Optional["asyncio.Task[None]"] = None
        self._loop: Any = None

    def bind(self, comm: Any) -> None:
        """The protocol to announce outcomes on (built after its transports)."""
        self._comm = comm

    def _mgr(self) -> Any:
        if self._manager_obj is None:
            self._manager_obj = _manager()
        return self._manager_obj

    def _rec(self) -> PromotionRecord:
        return self._record if self._record is not None else get_promotion_record()

    async def send(self, msg: Any) -> None:
        try:
            if getattr(getattr(msg, "msg_type", None), "value", "") != "HEARTBEAT":
                return
            payload = getattr(msg, "payload", None) or {}
            if payload.get("phase") != COMMIT_PHASE:
                return
            sha = str(payload.get("commit_hash") or "").strip()
            if not sha:
                return
            from backend.core.ouroboros.governance.accumulation_promotion_gate import (  # noqa: PLC0415
                gate_enabled,
            )
            if not gate_enabled() or sha in self._seen_set:
                return
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(sha)
            self._seen_set.add(sha)
            self._ensure_worker().put_nowait((str(getattr(msg, "op_id", "") or ""), sha))
        except Exception:  # noqa: BLE001 — observing a landing never stops it
            logger.debug("[MainPromoter] enqueue degraded", exc_info=True)

    def _ensure_worker(self) -> "asyncio.Queue[Tuple[str, str]]":
        """One queue + worker per running loop. An asyncio.Queue binds to the
        loop that first waits on it; reusing one from a dead loop is how a
        consumer spins (the GapSignalBus defect)."""
        loop = asyncio.get_running_loop()
        if self._queue is None or self._loop is not loop or (
            self._worker is not None and self._worker.done()
        ):
            pending = []
            if self._queue is not None:
                while not self._queue.empty():
                    pending.append(self._queue.get_nowait())
            self._loop = loop
            self._queue = asyncio.Queue()
            for item in pending:
                self._queue.put_nowait(item)
            self._worker = loop.create_task(self._drain(), name="main-promoter")
        return self._queue

    async def _drain(self) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            op_id, sha = await queue.get()
            try:
                await self._promote_one(op_id, sha)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("[MainPromoter] %s promotion faulted", sha[:12],
                               exc_info=True)
            finally:
                queue.task_done()

    async def _promote_one(self, op_id: str, sha: str) -> None:
        outcome = await self._promote(sha)
        verdict = outcome.verdict
        promoted = bool(getattr(verdict, "promoted", False))
        state = str(getattr(verdict, "state", "") or "")
        detail = str(getattr(verdict, "detail", "") or "")
        self._rec().record(
            promoted=promoted, target=outcome.target, sha=sha, state=state,
            detail=detail, unpushed=outcome.unpushed,
        )
        if promoted:
            logger.warning(
                "[MainPromoter] %s ← %s (%s). Local only — `git push` is "
                "your call; %s.",
                outcome.target, sha[:12], getattr(verdict, "branch_disposition", ""),
                "no remote to compare" if outcome.unpushed is None
                else f"{outcome.unpushed} commit(s) not on origin",
            )
            await self._sweep_merged(outcome.target)
        await self._announce(op_id, outcome, promoted, state, detail)

    async def _sweep_merged(self, target: str) -> None:
        """Prune every session branch the target now contains — including
        earlier sessions' branches that were still checked out when their own
        promotion ran. The one safe deletion rule decides each."""
        try:
            from backend.core.ouroboros.governance.autonomous_workspace import (  # noqa: PLC0415
                WORKSPACE_BRANCH_ROOT,
            )
            got = await self._mgr().prune_merged_branches(
                WORKSPACE_BRANCH_ROOT, into=target,
            )
            deleted = sorted(b for b, d in got.items() if d == "deleted")
            if deleted:
                logger.info("[MainPromoter] pruned %d merged session branch(es): %s",
                            len(deleted), ", ".join(deleted[:4]))
        except Exception:  # noqa: BLE001
            logger.debug("[MainPromoter] merged-branch sweep degraded", exc_info=True)

    async def _announce(self, op_id: str, outcome: LandingPromotion,
                        promoted: bool, state: str, detail: str) -> None:
        comm = self._comm
        if comm is None:
            return
        try:
            await comm.emit_heartbeat(
                op_id=op_id,
                phase=PROMOTION_SUCCESS if promoted else PROMOTION_REFUSED,
                progress_pct=100.0,
                commit=outcome.sha,
                target_branch=outcome.target,
                source_branch=outcome.source_branch,
                landed_shas=list(getattr(outcome.verdict, "landed_shas", ()) or ()),
                unpushed=outcome.unpushed,
                branch_disposition=getattr(outcome.verdict, "branch_disposition", ""),
                state=state,
                detail=detail[:300],
            )
        except Exception:  # noqa: BLE001
            logger.debug("[MainPromoter] announce degraded", exc_info=True)

    async def aclose(self) -> None:
        """Cancel the worker. A queued landing is not lost: its branch is
        never deleted unmerged, so it remains promotable by hand."""
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = [
    "COMMIT_PHASE",
    "LandingPromotion",
    "MainPromotionTransport",
    "PROMOTION_REFUSED",
    "PROMOTION_SUCCESS",
    "PromotionRecord",
    "PromotionSnapshot",
    "get_promotion_record",
    "promote_landing",
    "render_promotion",
    "reset_for_tests",
    "resolve_source_branch",
    "resolve_target_branch",
]
