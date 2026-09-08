"""The ONE gate every push to a remote passes — a global zero-push air-gap.

A push to a remote is the organism's only OUTWARD-FACING git act: it leaves
the machine, it is visible to other people, and (unlike a local commit) it
cannot be quietly undone. On 2026-09-07 twenty-two ``ouroboros/review/*``
branches reached ``origin`` from soaks that were supposed to be isolated,
because the policy lived INSIDE one lane (``orange_pr_reviewer``) while three
other call sites pushed with no policy at all:

* ``cross_repo.CrossRepoWorkspace`` — an unconditional ``git push``;
* ``auto_committer._git_push`` — guarded only against protected branch NAMES;
* ``state_persistence_daemon`` — the ``git`` state-vault backend.

A lane-local flag cannot be an air-gap. This module is the composed policy and
the single choke point, so "may this process push?" has exactly one answer no
matter which lane is asking.

## The air-gap is a HARD override

:func:`airgap_engaged` is consulted FIRST and **nothing lifts it** — not
``JARVIS_ORANGE_PR_PUSH_ENABLED``, not ``JARVIS_AUTO_PUSH_BRANCH``, not a lane
that forgot to ask. It is **ON by default** (fail-closed): an operator who
wants the organism to push must say so out loud with
``JARVIS_REMOTE_PUSH_AIRGAP=false``. That default is the whole point — the 22
branches escaped precisely because the safe state was the one you had to opt
INTO.

This governs the ORGANISM's pushes only. A human running ``git push`` in their
own shell never enters this process and is not affected.

## Why a module and not a wrapper

These are four independent ``asyncio.create_subprocess_exec`` calls; there is
no shared git transport object to wrap. So the guarantee is made structural a
different way: ``tests/governance/test_remote_push_guard.py`` walks the AST of
``backend/`` and fails if ANY function builds a remote-push argv without
consulting this module. A new push site cannot be added silently — the
invariant test is the enforcement, and it runs on every suite.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("Ouroboros.RemotePushGuard")

__all__ = [
    "PushVerdict",
    "airgap_engaged",
    "push_verdict",
    "remote_push_allowed",
]

#: The hard override. ON unless explicitly disabled — see the module docstring.
ENV_AIRGAP = "JARVIS_REMOTE_PUSH_AIRGAP"
#: Per-lane opt-in, honoured only when the air-gap is DOWN.
ENV_LANE_PUSH = "JARVIS_ORANGE_PR_PUSH_ENABLED"
#: The operator's declared auto-push target — the same declaration
#: ``auto_committer`` honours, so the two can never disagree about intent.
ENV_AUTO_PUSH_BRANCH = "JARVIS_AUTO_PUSH_BRANCH"

_TRUTHY = ("1", "true", "yes", "on")
_FALSEY = ("0", "false", "no", "off")


@dataclass(frozen=True)
class PushVerdict:
    """Why this process may or may not push, in words a log line can carry."""

    allowed: bool
    reason: str
    lane: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


def airgap_engaged() -> bool:
    """True when NO remote push may leave this process, whatever else is set.

    Default TRUE. Only an explicit falsey ``JARVIS_REMOTE_PUSH_AIRGAP`` lowers
    it; an unset or unparseable value keeps the gap closed, because the failure
    we are preventing is exactly "nobody set the flag". NEVER raises.
    """
    try:
        raw = (os.environ.get(ENV_AIRGAP, "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return True
    return raw not in _FALSEY


def push_verdict(lane: str = "") -> PushVerdict:
    """Compose the push policy for *lane*. NEVER raises.

    Order matters and is the contract:

    1. The air-gap. A hard refusal no lane flag can override.
    2. An explicit per-lane opt-in (``JARVIS_ORANGE_PR_PUSH_ENABLED``).
    3. The operator's declared auto-push branch.
    4. Otherwise: local branches only.
    """
    if airgap_engaged():
        return PushVerdict(False, f"airgap:{ENV_AIRGAP} not disabled", lane)
    try:
        explicit = (os.environ.get(ENV_LANE_PUSH, "") or "").strip().lower()
        if explicit:
            if explicit in _TRUTHY:
                return PushVerdict(True, f"operator:{ENV_LANE_PUSH}", lane)
            return PushVerdict(False, f"operator:{ENV_LANE_PUSH} disabled", lane)
        target = (os.environ.get(ENV_AUTO_PUSH_BRANCH, "") or "").strip()
        if target:
            return PushVerdict(True, f"operator:{ENV_AUTO_PUSH_BRANCH}={target}", lane)
    except Exception:  # noqa: BLE001
        return PushVerdict(False, "policy_read_failed", lane)
    return PushVerdict(False, "no operator push declaration", lane)


def remote_push_allowed(lane: str = "") -> bool:
    """Boolean form of :func:`push_verdict`, and the refusal's own log line.

    Call this immediately before building a push argv. A refusal is a normal,
    expected outcome — the caller keeps its local branch and reports honestly;
    it is never an error condition.
    """
    verdict = push_verdict(lane)
    if not verdict.allowed:
        logger.info(
            "[RemotePushGuard] push refused lane=%s reason=%s "
            "(local branch kept; set %s=false + an operator push declaration "
            "to allow)",
            lane or "?", verdict.reason, ENV_AIRGAP,
        )
    return verdict.allowed


def describe() -> str:
    """One line naming the effective policy — for boot banners and /organism."""
    verdict = push_verdict("describe")
    state = "OPEN" if verdict.allowed else "AIR-GAPPED"
    return f"remote push: {state} ({verdict.reason})"
