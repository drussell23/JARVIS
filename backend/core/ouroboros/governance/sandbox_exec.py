"""Ephemeral Trinity-Docker execution for the two RCE vectors (bash, run_tests).

Thin wrapper over container_sandbox (already --network none / --read-only /
--cap-drop ALL / --rm ephemeral / auto-imploding, cross-platform arm64/amd64).

Fail-closed: if the sandbox is DISABLED or SPAWN_FAILED (Docker unavailable),
DENY — NEVER fall through to running bash/pytest unsandboxed on the host.
This is the core security property: cross-platform (M1 local + GCP); if Docker
is absent → denied, never run.

Master flag: JARVIS_RUNTIME_SANDBOX_ENABLED (default FALSE, shared with
container_sandbox / runtime_sandbox). When the flag is false the two callers
(bash, run_tests) both return SandboxResult(ok=False, denied=True) immediately.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of a sandboxed execution attempt.

    ``denied=True`` signals that the sandbox was unavailable/disabled and the
    command was NOT executed (fail-closed).  ``ok=True`` only when the command
    ran inside a container and exited 0.
    """
    ok: bool
    stdout: str
    stderr: str
    returncode: Optional[int]
    denied: bool
    reason: str


async def sandbox_run_bash(
    command: str,
    *,
    worktree: str,
    docker_run: Any = None,
) -> SandboxResult:
    """Run ``command`` in an ephemeral hardened container.

    Delegates to ``container_sandbox.run_in_container`` which supplies
    ``--network none --cap-drop ALL --read-only --tmpfs --rm``.

    Returns ``denied=True`` when the sandbox is DISABLED or SPAWN_FAILED —
    the command is NEVER executed unsandboxed.
    """
    from backend.core.ouroboros.governance.container_sandbox import (
        ContainmentBreach,
        run_in_container,
    )

    res = await run_in_container(command, worktree=worktree, docker_run=docker_run)

    breach = getattr(res, "breach", ContainmentBreach.SPAWN_FAILED)
    if breach in (ContainmentBreach.DISABLED, ContainmentBreach.SPAWN_FAILED):
        logger.warning(
            "[SandboxExec] bash DENIED — sandbox unavailable (%s); fail-closed",
            breach,
        )
        return SandboxResult(
            ok=False,
            stdout="",
            stderr=res.diagnostic,
            returncode=None,
            denied=True,
            reason=f"sandbox_unavailable:{breach}",
        )

    return SandboxResult(
        ok=bool(res.ok),
        stdout=res.stdout,
        stderr=res.stderr,
        returncode=res.returncode,
        denied=False,
        reason="",
    )


async def sandbox_run_tests(
    test_targets: List[str],
    *,
    worktree: str,
    docker_run: Any = None,
) -> SandboxResult:
    """Run pytest against ``test_targets`` inside an ephemeral hardened container.

    Delegates to ``container_sandbox.run_pytest_in_container`` which mounts the
    worktree read-only and runs ``python -m pytest`` with hardened Docker flags.

    Returns ``denied=True`` when the sandbox is DISABLED or SPAWN_FAILED —
    pytest is NEVER executed unsandboxed.
    """
    from backend.core.ouroboros.governance.container_sandbox import (
        ContainmentBreach,
        run_pytest_in_container,
    )

    res = await run_pytest_in_container(
        test_targets, worktree=worktree, docker_run=docker_run
    )

    breach = getattr(res, "breach", ContainmentBreach.SPAWN_FAILED)
    if breach in (ContainmentBreach.DISABLED, ContainmentBreach.SPAWN_FAILED):
        logger.warning(
            "[SandboxExec] run_tests DENIED — sandbox unavailable (%s); fail-closed",
            breach,
        )
        return SandboxResult(
            ok=False,
            stdout="",
            stderr=getattr(res, "diagnostic", ""),
            returncode=None,
            denied=True,
            reason=f"sandbox_unavailable:{breach}",
        )

    return SandboxResult(
        ok=bool(getattr(res, "ok", False)),
        stdout="",
        stderr=getattr(res, "diagnostic", ""),
        returncode=getattr(res, "returncode", None),
        denied=False,
        reason="",
    )
